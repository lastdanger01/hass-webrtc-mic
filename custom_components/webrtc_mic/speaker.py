"""Play audio straight through a camera speaker.

The ``media_player`` platform of this integration needs one YAML block per
camera. This module offers the same thing as a service, so a camera can
play music without any configuration at all: the card just calls
``webrtc_mic.play_media`` with the go2rtc stream name.

The audio is pushed to the camera through the go2rtc backchannel, exactly
like the media_player platform does it, so both paths behave the same.

Resume support (v1.0.31)
-----------------------
A track that is interrupted - the page was reloaded, Home Assistant was
restarted, the network hiccuped - has to continue where it stopped instead
of starting from the beginning again. The position is measured from the
bytes go2rtc actually pushed to the camera (8000 Hz mono PCMA is one byte
per millisecond) and the track is restarted with a "start" offset that the
media server turns into an ffmpeg seek.

A track that simply reached its end is *not* resumed: playing it again
starts it from the beginning.
"""

import asyncio
import logging
import time
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import voluptuous as vol
from homeassistant.components import media_source
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import get_url

from . import utils

_LOGGER = logging.getLogger(__name__)

# The backchannel format the Tapo cameras expect: 8000 Hz mono PCMA.
DEFAULT_AUDIO = "pcma"

# 8000 Hz mono PCMA: one byte per millisecond of audio.
PCMA_BYTES_PER_SECOND = 8000

# How often the position of a playing track is checked.
POLL_INTERVAL = 2.0

PLAY_MEDIA_SCHEMA = vol.Schema(
    {
        vol.Required("stream"): cv.string,
        vol.Required("media_content_id"): cv.string,
        vol.Optional("title"): cv.string,
        # The original media id of the track. The resolved URL carries a
        # signature that is regenerated on every resolve, so it cannot be
        # used to recognise "the same track" after a page reload.
        vol.Optional("source_id"): cv.string,
        vol.Optional("audio", default=DEFAULT_AUDIO): cv.string,
        # Resume a track that was interrupted at this position (seconds).
        # When it is not given, the position is taken from the state that
        # is kept in Home Assistant.
        vol.Optional("start", default=0): vol.Coerce(float),
    }
)

STOP_MEDIA_SCHEMA = vol.Schema(
    {
        vol.Required("stream"): cv.string,
    }
)

# What is currently playing on each camera, keyed by go2rtc stream name.
#
# The playback itself lives in go2rtc, not in the browser, so the state is
# kept here in Home Assistant. The card can then ask "what is playing?"
# after a page reload and restore the button, instead of pushing the same
# track a second time.
_STATE: dict[str, dict] = {}

# The task that watches the playback position of every playing track.
_WATCHERS: dict[str, asyncio.Task] = {}


def _lock(stream: str) -> asyncio.Lock:
    """Return the push lock of a stream.

    The lock is shared with the media_player platform, so the card and a
    script cannot push two sources to the same camera at the same time -
    go2rtc accepts only one pushed source per destination and the second
    one would be left behind as an orphan producer.
    """
    return utils.stream_lock(stream)


def media_server(hass: HomeAssistant) -> str:
    """The plain HTTP media server that serves the audio to go2rtc."""
    return (hass.data.get(utils.MEDIA_SERVER_KEY) or "").rstrip("/")


async def async_resolve_url(hass: HomeAssistant, media_content_id: str) -> str:
    """Turn a media id into an absolute URL that go2rtc can fetch.

    A ``media-source://`` id is resolved by Home Assistant, which returns a
    signed URL. The signature is valid for the URL itself, so go2rtc can
    fetch it even though it runs on another host.
    """
    if media_source.is_media_source_id(media_content_id):
        resolved = await media_source.async_resolve_media(
            hass, media_content_id, target_media_player=None
        )
        url = resolved.url
    else:
        url = media_content_id

    if url.startswith("/"):
        url = get_url(hass).rstrip("/") + url

    return url


def _filename(url: str) -> str:
    """The file name of a media URL, without any query string."""
    path = urlsplit(url).path
    name = unquote(path.rsplit("/", 1)[-1])
    return name or url


def cast_url(url: str, server: str) -> str:
    """Rewrite a Home Assistant media URL to the plain media server.

    Home Assistant signs the media URLs it hands out and the signature is
    bound to the client that requested it, so go2rtc (a different host)
    gets a 401 and the push fails. The very same files are also served by
    the plain media server, which go2rtc can always reach.

    On top of that the plain server understands a "start" parameter, which
    is what makes a track resume in the middle.
    """
    if not server:
        return url

    if "/media/local/" in url:
        name = _filename(url)
        return f"{server}/s/{quote(name)}"

    parsed = urlsplit(url)
    params = dict(parse_qsl(parsed.query))
    if "start" not in params and "volume" not in params:
        return url

    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(params), parsed.fragment)
    )


def with_start(url: str, start: float, volume: float = 1.0) -> str:
    """Add the start offset (and the volume) to a media URL."""
    parsed = urlsplit(url)
    params = dict(parse_qsl(parsed.query))
    if start > 0.001:
        params["start"] = "%.2f" % start
    if abs(volume - 1.0) > 0.001:
        params["volume"] = "%.2f" % volume
    query = urlencode(params)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment)
    )


class Speaker:
    """Push an audio source to a camera and make sure it really plays."""

    def __init__(self, hass: HomeAssistant, stream: str, audio: str = DEFAULT_AUDIO):
        self.hass = hass
        self.stream = stream
        self.audio = audio

    async def _producers(self) -> list[dict]:
        """All producers go2rtc holds for this camera."""
        try:
            r = await async_get_clientsession(self.hass).get(
                utils.api_streams(self.hass), params={"src": self.stream}, timeout=6
            )
            if not r.ok:
                return []
            resp = await r.json(content_type=None)
        except Exception:  # noqa: BLE001
            return []
        return resp.get("producers") or []

    async def _pushed_producers(self) -> list[dict]:
        """The audio producers go2rtc currently pushes to this camera."""
        return [
            p
            for p in await self._producers()
            if p.get("format_name") == "wav"
        ]

    async def _orphan_producers(self) -> list[dict]:
        """The leftover "internal" producers of earlier pushes.

        go2rtc keeps one "internal" producer for every push that was
        started. They are removed when the push ends, but an interrupted
        push can leave one behind - and a pile of them keeps an ffmpeg
        running on the camera and pushes bytes nobody asked for.

        An "internal" producer without a format name has no connection
        any more (go2rtc prints only the URL for those), so it is just a
        leftover entry and harmless.
        """
        return [
            p
            for p in await self._producers()
            if p.get("url") == "internal" and p.get("format_name")
        ]

    async def _has_source(self, url: str) -> bool:
        """Whether the given URL is the source that is pushed right now.

        go2rtc rewrites the pushed source into an "exec:ffmpeg ..." URL,
        so the original URL is only a part of it. The path of the URL is
        used for the comparison - the query string carries the start
        offset and the volume, which change on every restart.
        """
        path = urlsplit(url).path
        for producer in await self._pushed_producers():
            producer_url = producer.get("url") or ""
            if path and path in producer_url:
                return True
        return False

    async def _detach(self) -> None:
        """Detach whatever source is currently pushed to the camera.

        go2rtc allows only one pushed source per destination. Attaching a
        second one without detaching the first leaves the camera with no
        audio at all, so the old source has to go first.
        """
        try:
            r = await async_get_clientsession(self.hass).post(
                utils.api_streams(self.hass),
                params={"dst": self.stream, "src": ""},
                timeout=8,
            )
            if not r.ok:
                _LOGGER.debug("go2rtc detach returned %s", r.status)
        except Exception as exc:  # noqa: BLE001 - never block playback
            _LOGGER.debug(
                "go2rtc detach failed (%s): %s", type(exc).__name__, exc or "no details"
            )
            return

        # go2rtc releases the pushed sources asynchronously, so wait until
        # they are really gone. Attaching too early silently fails.
        for _ in range(60):
            await asyncio.sleep(0.25)
            if not await self._pushed_producers():
                return

    async def _purge(self, attempts: int = 6) -> int:
        """Force every pushed audio source off the camera.

        A plain detach does not always remove the producers: when go2rtc is
        busy the request times out and the old ffmpeg processes stay alive.
        Every orphan keeps an ffmpeg running and pushes bytes to the camera.
        """
        remaining = 0
        for _ in range(attempts):
            try:
                r = await async_get_clientsession(self.hass).post(
                    utils.api_streams(self.hass),
                    params={"dst": self.stream, "src": ""},
                    timeout=10,
                )
                if not r.ok:
                    _LOGGER.debug("go2rtc purge returned %s", r.status)
            except Exception as exc:  # noqa: BLE001 - never block playback
                _LOGGER.debug(
                    "go2rtc purge failed (%s): %s", type(exc).__name__, exc or "no details"
                )
            await asyncio.sleep(0.4)
            remaining = len(await self._pushed_producers())
            if not remaining:
                return 0
        return remaining

    async def async_play(self, url: str) -> None:
        """Push the audio at the given URL to the camera.

        The new source is pushed first and only the old one is stopped
        when the new one really plays. Stopping the old source first
        would leave the camera silent while the new source is starting,
        and a failed push would silence a track that was playing fine.
        """
        src = f"ffmpeg:{url}#audio={self.audio}#input=file"

        async with _lock(self.stream):
            try:
                r = await async_get_clientsession(self.hass).post(
                    utils.api_streams(self.hass),
                    params={"dst": self.stream, "src": src},
                    timeout=15,
                )
            except Exception as exc:  # noqa: BLE001
                raise HomeAssistantError(
                    f"無法把音訊送到相機（{type(exc).__name__}: {exc or 'no details'}）"
                ) from exc

            if not r.ok:
                body = ""
                try:
                    body = (await r.text())[:300]
                except Exception:  # noqa: BLE001
                    pass
                _LOGGER.error(
                    "go2rtc refused the audio push: status=%s url=%s body=%s",
                    r.status,
                    r.url,
                    body,
                )
                if "consumer" in body:
                    raise HomeAssistantError(
                        "這台相機沒有對講（backchannel）功能，無法播放音訊"
                    )
                raise HomeAssistantError(
                    f"go2rtc 拒絕了音訊推送（status {r.status}）"
                )

            # go2rtc can answer 200 and still drop the source a moment
            # later, so the push is confirmed before it is reported as
            # successful.
            #
            # The new source is looked for by its URL: the producer of
            # the previous track may still be there for a moment, and
            # that one must not be mistaken for the new track.
            for _ in range(8):
                await asyncio.sleep(0.4)
                if await self._has_source(url):
                    _LOGGER.info("Audio push accepted by go2rtc: %s", src)
                    orphans = await self._orphan_producers()
                    if orphans:
                        _LOGGER.warning(
                            "%s leftover audio sources are still attached to %s",
                            len(orphans),
                            self.stream,
                        )
                    return

        raise HomeAssistantError("go2rtc 接受了音訊，但相機沒有聲音")

    async def async_stop(self) -> None:
        """Stop whatever is playing on the camera."""
        async with _lock(self.stream):
            await self._detach()
            await self._purge()


def get_state(stream: str) -> dict | None:
    """Return what is playing on a camera, or None when it is idle."""
    state = _STATE.get(stream)
    if not state:
        return None
    return dict(state)


def get_all_states() -> dict:
    """Return the playback state of every camera that plays something."""
    return {name: dict(state) for name, state in _STATE.items()}


def _set_state(stream: str, url: str, title: str | None, content_id: str | None) -> None:
    """Remember that a camera started playing a track.

    The original media id is stored next to the resolved URL: the
    resolved URL carries a signature that is regenerated on every
    resolve, so it cannot be used to recognise "the same track".
    """
    _STATE[stream] = {
        "stream": stream,
        "url": url,
        "content_id": content_id or url,
        "title": title or url.rsplit("/", 1)[-1] or stream,
        "started_at": time.time(),
        "position": 0.0,
        "duration": None,
        "playing": False,
        "ended": False,
    }


def _clear_state(stream: str) -> None:
    """Forget the playback state of a camera."""
    _STATE.pop(stream, None)
    task = _WATCHERS.pop(stream, None)
    if task and not task.done():
        task.cancel()


async def async_is_playing(hass: HomeAssistant, stream: str) -> bool:
    """Whether go2rtc currently pushes an audio source to this camera."""
    try:
        r = await async_get_clientsession(hass).get(
            utils.api_streams(hass), params={"src": stream}, timeout=6
        )
        if not r.ok:
            return False
        resp = await r.json(content_type=None)
    except Exception:  # noqa: BLE001
        return False
    return any(p.get("format_name") == "wav" for p in resp.get("producers") or [])


async def _pushed_source(hass: HomeAssistant, stream: str) -> tuple[str, int]:
    """The URL of the pushed audio source and the bytes already sent.

    The pushed source shows up as its own producer (format_name "wav").
    Its byte counter starts at zero whenever the source is restarted,
    which makes it a reliable progress base.
    """
    try:
        r = await async_get_clientsession(hass).get(
            utils.api_streams(hass), params={"src": stream}, timeout=9
        )
        if not r.ok:
            return "", 0
        resp = await r.json(content_type=None)
    except Exception:  # noqa: BLE001
        return "", 0
    for producer in resp.get("producers") or []:
        if producer.get("format_name") == "wav":
            return producer.get("url") or "", producer.get("bytes_recv") or 0
    return "", 0


async def _sent_bytes(hass: HomeAssistant, stream: str) -> int:
    """Bytes of audio go2rtc already pushed to the camera."""
    return (await _pushed_source(hass, stream))[1]


async def _fetch_duration(hass: HomeAssistant, url: str) -> float | None:
    """Read the total duration advertised by the media server.

    The plain media server answers HEAD requests with an X-Cast-Duration
    header holding the length of the track in seconds.
    """
    try:
        r = await async_get_clientsession(hass).head(
            url, timeout=10, allow_redirects=True
        )
        if not r.ok:
            return None
        raw = r.headers.get("X-Cast-Duration")
        if raw:
            return max(0.0, float(raw))
    except Exception:  # noqa: BLE001 - never block playback
        return None
    return None


def _start_watcher(hass: HomeAssistant, stream: str) -> None:
    """Watch the playback position of a track until it ends."""
    old = _WATCHERS.pop(stream, None)
    if old and not old.done():
        old.cancel()

    task = asyncio.create_task(_watch(hass, stream))
    _WATCHERS[stream] = task


async def _watch(hass: HomeAssistant, stream: str) -> None:
    """Follow the playback position and notice when the track ends.

    The pushed producer disappears when the track ends, but it also
    disappears when the playback was interrupted. The two cases are told
    apart by the position: a track that reached its length ended
    normally, anything else was interrupted and can be resumed.
    """
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)

            state = _STATE.get(stream)
            if not state:
                return

            sent = await _sent_bytes(hass, stream)
            if sent <= 0:
                # No audio producer (any more). The track either ended
                # or the playback was interrupted.
                duration = state.get("duration")
                position = state.get("position") or 0.0
                ended = duration is not None and position >= duration - 0.75
                state["playing"] = False
                state["ended"] = ended
                state["position"] = position
                _LOGGER.info(
                    "Playback of %s on %s stopped (position %.1fs, ended=%s)",
                    state.get("title"),
                    stream,
                    position,
                    ended,
                )
                return

            position = (state.get("start") or 0.0) + sent / PCMA_BYTES_PER_SECOND
            duration = state.get("duration")
            if duration is not None and position >= duration:
                position = duration
            state["position"] = position
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a watcher must never crash
        _LOGGER.debug("Playback watcher for %s stopped: %s", stream, exc)


async def async_play_media(call: ServiceCall) -> None:
    """Handle the webrtc_mic.play_media service."""
    hass = call.hass
    stream = call.data["stream"]
    audio = call.data.get("audio") or DEFAULT_AUDIO

    url = await async_resolve_url(hass, call.data["media_content_id"])
    title = call.data.get("title")
    # The stable id of the track. The card sends the original media id
    # here, because the resolved URL is signed and changes every time.
    content_id = call.data.get("source_id") or call.data["media_content_id"]

    server = media_server(hass)
    url = cast_url(url, server)

    # Playing the same track again must not restart it. A page reload
    # makes the card ask for the same track again, and a second push
    # would start the music from the beginning.
    #
    # The media id is compared, not the resolved URL: the resolved URL
    # carries a signature that is regenerated on every resolve, so two
    # resolves of the same file never match.
    state = _STATE.get(stream)
    start = 0.0

    if state and state.get("content_id") == content_id:
        if await async_is_playing(hass, stream):
            _LOGGER.debug("Already playing %s on %s, keeping it", title or url, stream)
            state["playing"] = True
            state["ended"] = False
            _start_watcher(hass, stream)
            return

        # The track is not playing any more. A track that ended normally
        # starts from the beginning again, an interrupted one continues
        # where it stopped.
        if state.get("ended"):
            _LOGGER.info(
                "%s finished on %s, playing it again from the start", title or url, stream
            )
            start = 0.0
        else:
            start = float(state.get("position") or 0.0)
            _LOGGER.info(
                "%s was interrupted on %s, resuming at %.1fs", title or url, stream, start
            )
    else:
        start = 0.0

    push_url = with_start(url, start)
    await Speaker(hass, stream, audio).async_play(push_url)

    duration = await _fetch_duration(hass, url)
    _set_state(stream, url, title, content_id)
    state = _STATE[stream]
    state["start"] = start
    state["position"] = start
    state["duration"] = duration
    state["playing"] = True
    state["ended"] = False
    _start_watcher(hass, stream)


async def async_stop_media(call: ServiceCall) -> None:
    """Handle the webrtc_mic.stop_media service."""
    await Speaker(call.hass, call.data["stream"]).async_stop()
    _clear_state(call.data["stream"])
