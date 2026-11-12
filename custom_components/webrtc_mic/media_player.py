import asyncio
from datetime import datetime, timedelta, UTC
import logging

import voluptuous as vol
from homeassistant.components import media_source
from homeassistant.components.media_player import (
    async_process_play_media_url,
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    PLATFORM_SCHEMA,
)
from homeassistant.const import STATE_PLAYING, STATE_PAUSED, STATE_IDLE, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.typing import ConfigType
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from . import utils
from .utils import DOMAIN

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required("stream"): cv.string,
        vol.Required("audio"): cv.string,
        # Optional media server that serves the audio to go2rtc. It must be a
        # plain HTTP URL without a client bound auth signature, otherwise
        # go2rtc gets a 401 and the push fails with "exec/pipe: EOF".
        vol.Optional("media_server", default=""): cv.string,
        # Optional mapping of the dashboard song title -> file name in the
        # media library. Without it the dashboard selector only needs the
        # file name to match the media source file name.
        vol.Optional("music_library", default={}): vol.Schema(
            {cv.string: cv.string}
        ),
    },
    extra=vol.REMOVE_EXTRA,
)

SCAN_INTERVAL = timedelta(seconds=2)


_LOGGER = logging.getLogger(__name__)

# Backchannel audio is 8000 Hz mono PCMA: one byte per millisecond of audio.
_PCMA_BYTES_PER_SECOND = 8000


async def async_setup_platform(
    hass: HomeAssistant, config: ConfigType, async_add_entities, discovery_info=None
) -> None:
    await async_setup_reload_service(hass, DOMAIN, ["media_player"])

    player = WebRTCPlayer(**config)
    async_add_entities([player])


class WebRTCPlayer(MediaPlayerEntity):
    def __init__(
        self,
        name: str,
        stream: str,
        audio: str,
        media_server: str = "",
        music_library: dict | None = None,
        **kwargs,
    ):
        self._attr_supported_features = (
            MediaPlayerEntityFeature.PLAY_MEDIA
            | MediaPlayerEntityFeature.BROWSE_MEDIA
            | MediaPlayerEntityFeature.STOP
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.VOLUME_MUTE
            | MediaPlayerEntityFeature.VOLUME_STEP
            | MediaPlayerEntityFeature.SEEK
            | MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.PAUSE
        )
        self._attr_name = name
        self._attr_unique_id = stream
        self.audio = audio
        self.media_server = media_server
        self.music_library = music_library or {}

        # Playback state used to expose a progress bar.
        self._attr_volume_level = 1.0
        self._attr_is_volume_muted = False
        self._attr_media_duration = None
        self._attr_media_position = 0
        self._attr_media_title = None
        self._attr_media_content_id = None
        self._attr_media_content_type = None
        # The frontend only draws the progress bar when this attribute is
        # present, and it uses it to extrapolate the position while playing.
        self._attr_media_position_updated_at = datetime.now(UTC)

        # Current source parameters, kept so a volume change or a seek can
        # restart the very same audio with new parameters.
        self._source_url = None
        self._source_start = 0.0
        self._duration = None
        self._start_time = None
        self._bytes_at_start = 0
        self._paused_position = None
        self._is_paused = False
        self._last_stream = None
        # Serialises the push/detach cycles. Without it, two service calls
        # (for example the dashboard button and a script) can push a source
        # at the same time and leave orphan producers behind. It is created
        # lazily because the lock must belong to the running event loop.
        self._push_lock = None
        # File name of the track that is currently loaded. Used to tell a
        # real song change apart from pressing play again on the same song.
        self._current_file = None

    # -- helpers ---------------------------------------------------------
    def _build_url(self, media_id: str, volume: float, start: float) -> str:
        """Merge volume/start parameters into the source URL.

        The caller may already have put a volume or start in the URL, so the
        existing query is parsed and the parameters are replaced instead of
        appended (appending would produce duplicate keys and the server
        would honour only the first one).
        """
        parsed = urlsplit(media_id)
        params = dict(parse_qsl(parsed.query))
        params["volume"] = "%.2f" % volume
        params["start"] = "%.2f" % start
        query = urlencode(params)
        base = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment)
        )
        return base

    async def _async_source_url(self) -> str | None:
        """URL of the audio currently pushed to the camera, if any."""
        resp = self._last_stream
        if resp is None:
            try:
                r = await async_get_clientsession(self.hass).get(
                    utils.api_streams(self.hass), params={"src": self.unique_id}, timeout=9
                )
                if not r.ok:
                    return None
                resp = await r.json(content_type=None)
            except Exception:  # noqa: BLE001
                return None
        for producer in resp.get("producers") or []:
            if producer.get("format_name") == "wav":
                url = producer.get("url")
                if url:
                    # Recover the start offset that the media server was
                    # told to use, so the progress position stays correct.
                    try:
                        params = dict(parse_qsl(urlsplit(url).query))
                        self._source_start = float(params.get("start") or 0.0)
                    except (TypeError, ValueError):
                        self._source_start = 0.0
                return url
        return None

    def _cast_media_url(self, media_id: str) -> str:
        """Rewrite a Home Assistant media source URL to the cast server.

        The frontend media browser hands out URLs like
        https://<ha>/media/local/<file>?authSig=... The auth signature is
        bound to the client that requested it, so go2rtc (a different host)
        gets a 401 and the push fails with "exec/pipe: EOF". The very same
        files are also served by the cast-media server, which go2rtc can
        always reach, so rewrite the URL to point there.
        """
        if "/media/local/" not in media_id:
            return media_id
        name = unquote(media_id.split("/media/local/", 1)[1].split("?")[0])
        name = name.split("/")[-1]
        if not name:
            return media_id
        if not self.media_server:
            return media_id
        return f"{self.media_server}/s/{quote(name)}"

    def _stamp_position(self, position: float) -> None:
        """Store the position together with the time it was measured.

        The frontend extrapolates the playback position from
        media_position_updated_at, and it refuses to draw the progress bar
        at all when that attribute is missing.
        """
        self._attr_media_position = position
        self._attr_media_position_updated_at = datetime.now(UTC)

    async def _current_position(self) -> float:
        """Playback position in seconds, derived from the source counter."""
        sent = await self._async_sent_bytes()
        return self._source_start + sent / _PCMA_BYTES_PER_SECOND

    async def _restart(self, volume: float, start: float) -> None:
        """Restart the current source with new volume / start offset.

        A volume change or a seek has to replace the audio that is already
        pushed to the camera. go2rtc only accepts one pushed source per
        destination, so the old source must be detached first. Attaching
        without detaching leaves the camera with several wav producers and
        no working audio, which shows up as an idle player.
        """
        if not self._source_url:
            return
        url = self._build_url(self._source_url, volume, start)
        await self._async_push_source(
            f"ffmpeg:{url}#audio={self.audio}#input=file"
        )
        self._source_start = start
        self._bytes_at_start = await self._async_sent_bytes(fresh=True)
        self._attr_state = STATE_PLAYING
        self._stamp_position(start)

    def _lock(self) -> asyncio.Lock:
        """Return the push lock, creating it on the running event loop."""
        if self._push_lock is None:
            self._push_lock = asyncio.Lock()
        return self._push_lock

    async def _async_purge_sources(self, attempts: int = 6) -> int:
        """Force every pushed audio source off the camera.

        A plain detach does not always remove the producers: when go2rtc is
        busy, the detach request times out and the old ffmpeg processes stay
        alive. Every orphan keeps an ffmpeg running and pushes bytes to the
        camera, and once there are a dozen of them go2rtc stops answering
        its API at all - every push then fails with a timeout.
        """
        remaining = 0
        for _ in range(attempts):
            try:
                r = await async_get_clientsession(self.hass).post(
                    utils.api_streams(self.hass),
                    params={"dst": self.unique_id, "src": ""},
                    timeout=10,
                )
                if not r.ok:
                    _LOGGER.debug("go2rtc purge returned %s", r.status)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "go2rtc purge failed (%s): %s", type(exc).__name__, exc or "no details"
                )
            await asyncio.sleep(0.4)
            remaining = len(await self._async_pushed_producers())
            if not remaining:
                return 0
        return remaining

    async def _async_push_source(self, src: str) -> None:
        """Push an audio source to the camera and make sure it really plays.

        go2rtc answers with HTTP 200 even when it silently refuses to
        replace the source that is currently pushed to the camera. The
        symptom is a player that stays idle with no audio at all. So the
        push is verified against the stream list and retried a few times,
        detaching the stale source in between.

        The whole cycle runs under a lock: two concurrent service calls
        would otherwise push two sources and leave one of them orphaned.
        """
        async with self._lock():
            await self._async_push_source_locked(src)

    async def _async_push_source_locked(self, src: str) -> None:
        """Push the source, retrying until the camera really plays it."""
        last_status = None
        for attempt in range(3):
            # Make sure nothing is left over from a previous push. A plain
            # detach can time out and leave the old producer running, and
            # those orphans pile up until go2rtc stops answering at all.
            await self._async_detach_source()
            leftover = await self._async_purge_sources()
            if leftover:
                _LOGGER.warning(
                    "go2rtc kept %s audio sources for %s, forcing them off",
                    leftover,
                    self.unique_id,
                )
            try:
                r = await async_get_clientsession(self.hass).post(
                    utils.api_streams(self.hass),
                    params={"dst": self.unique_id, "src": src},
                    timeout=15,
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "go2rtc push failed (%s): %s",
                    type(exc).__name__,
                    exc or "no details",
                )
                await asyncio.sleep(1.0)
                continue

            last_status = r.status
            if not r.ok:
                body = ""
                try:
                    body = (await r.text())[:400]
                except Exception:  # noqa: BLE001
                    pass
                _LOGGER.error(
                    "go2rtc refused the audio push: status=%s url=%s body=%s",
                    r.status,
                    r.url,
                    body,
                )
                raise HomeAssistantError(
                    f"go2rtc refused the audio push (status {r.status})"
                )

            # go2rtc can answer 200 and still drop the source a moment
            # later, so the push is confirmed before it is reported as
            # successful.
            for _ in range(8):
                await asyncio.sleep(0.4)
                if await self._async_has_pushed_source():
                    if attempt:
                        _LOGGER.info(
                            "Audio push succeeded on attempt %s: %s", attempt + 1, src
                        )
                    return
            _LOGGER.warning(
                "go2rtc accepted the push but no audio source is active "
                "(attempt %s), retrying",
                attempt + 1,
            )

        raise HomeAssistantError(
            "go2rtc accepted the audio push but the camera stayed silent "
            f"(last status {last_status})"
        )

    async def _async_detach_source(self) -> None:
        """Detach whatever source is currently pushed to the camera.

        go2rtc allows only one pushed source per destination. Attaching a
        second source without detaching the first leaves the camera with no
        audio at all (the entity ends up idle and silent), so the old source
        has to be detached first.

        go2rtc releases the producer asynchronously, so the code waits until
        the "wav" producer is really gone before returning. Attaching too
        early silently fails.
        """
        try:
            r = await async_get_clientsession(self.hass).post(
                utils.api_streams(self.hass),
                params={"dst": self.unique_id, "src": ""},
                timeout=8,
            )
            if not r.ok:
                _LOGGER.debug("go2rtc detach returned %s", r.status)
        except Exception as exc:  # noqa: BLE001 - never block playback
            _LOGGER.debug(
                "go2rtc detach failed (%s): %s", type(exc).__name__, exc or "no details"
            )
            return

        # go2rtc releases the pushed sources asynchronously and it can hold
        # on to several of them at once. Wait until *all* of them are gone,
        # otherwise the next attach leaves an orphan producer behind. Those
        # orphans pile up until go2rtc stops answering its API at all.
        for _ in range(60):
            await asyncio.sleep(0.25)
            if not await self._async_pushed_producers():
                return
        _LOGGER.warning(
            "go2rtc still holds %s audio sources for %s after detaching",
            len(await self._async_pushed_producers()),
            self.unique_id,
        )

    async def _async_pushed_producers(self) -> list[dict]:
        """All audio producers go2rtc currently pushes to this camera."""
        try:
            r = await async_get_clientsession(self.hass).get(
                utils.api_streams(self.hass), params={"src": self.unique_id}, timeout=6
            )
            if not r.ok:
                return []
            resp = await r.json(content_type=None)
        except Exception:  # noqa: BLE001
            return []
        return [
            p
            for p in resp.get("producers") or []
            if p.get("format_name") == "wav"
        ]

    async def _async_has_pushed_source(self) -> bool:
        """Whether go2rtc still has an audio source pushed to the camera."""
        return bool(await self._async_pushed_producers())

    async def _async_sent_bytes(self, fresh: bool = False) -> int:
        """Bytes of audio already decoded from the pushed source.

        The pushed source shows up as its own producer (format_name "wav")
        whose bytes_recv counts the audio handed to the camera. That counter
        starts at zero whenever the source is restarted, which makes it a
        reliable progress base; the camera-side sender counter keeps
        counting across restarts and cannot be used to measure progress.
        """
        resp = None if fresh else self._last_stream
        if resp is None:
            try:
                r = await async_get_clientsession(self.hass).get(
                    utils.api_streams(self.hass), params={"src": self.unique_id}, timeout=9
                )
                if not r.ok:
                    return 0
                resp = await r.json(content_type=None)
                self._last_stream = resp
            except Exception:  # noqa: BLE001 - never break the update loop
                return 0

        for producer in resp.get("producers") or []:
            if producer.get("format_name") == "wav":
                return producer.get("bytes_recv") or 0
        return 0

    async def _async_fetch_duration(self, media_id: str) -> float | None:
        """Read the total duration advertised by the media server.

        The cast-media server answers HEAD requests with an X-Cast-Duration
        header holding the length of the track in seconds.
        """
        try:
            r = await async_get_clientsession(self.hass).head(
                media_id, timeout=10, allow_redirects=True
            )
            if not r.ok:
                return None
            raw = r.headers.get("X-Cast-Duration")
            if raw:
                return max(0.0, float(raw))
        except Exception:  # noqa: BLE001 - never block playback
            return None
        return None

    # -- media player ----------------------------------------------------
    async def async_play_media(self, media_type: str, media_id: str, **kwargs) -> None:
        if media_source.is_media_source_id(media_id):
            sourced_media = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id = sourced_media.url

        media_id = async_process_play_media_url(self.hass, media_id)
        media_id = self._cast_media_url(media_id)

        # Remember the raw source so volume/seek can restart it later.
        self._source_url = media_id
        self._source_start = 0.0
        self._current_file = (
            unquote(media_id.rsplit("/", 1)[-1].split("?")[0]) or None
        )
        self._attr_media_content_id = media_id
        self._attr_media_content_type = media_type
        self._attr_media_title = kwargs.get("title") or unquote(
            media_id.rsplit("/", 1)[-1].split("?")[0]
        )
        self._stamp_position(0.0)
        duration = kwargs.get("duration") or await self._async_fetch_duration(media_id)
        self._attr_media_duration = duration
        self._duration = duration

        if not media_type.startswith("#"):
            media_type = "#input=file"

        volume = 1.0 if self._attr_is_volume_muted else (self._attr_volume_level or 1.0)
        url = self._build_url(media_id, volume, self._source_start)

        # A previous track may still be pushed to the camera. go2rtc refuses
        # to attach a second source to the same destination, so the old one
        # has to be detached first or the new track silently fails.
        src = f"ffmpeg:{url}#audio={self.audio}{media_type}"
        await self._async_push_source(src)
        _LOGGER.info("Audio push accepted by go2rtc: %s", src)
        self._bytes_at_start = await self._async_sent_bytes(fresh=True)

    async def async_media_stop(self) -> None:
        async with self._lock():
            await self._async_detach_source()
            await self._async_purge_sources()
        self._attr_state = STATE_IDLE
        self._attr_media_position = 0
        self._source_url = None
        self._paused_position = None
        self._is_paused = False
        self._current_file = None

    async def async_set_volume_level(self, volume: float) -> None:
        volume = max(0.0, min(1.0, volume))
        self._attr_volume_level = volume
        self._attr_is_volume_muted = volume == 0.0
        if self._source_url:
            # Restart from the current position so a volume change does not
            # jump back to the beginning of the track.
            position = await self._current_position()
            await self._restart(volume, position)

    async def async_volume_up(self, **kwargs) -> None:
        await self.async_set_volume_level((self._attr_volume_level or 0.0) + 0.1)

    async def async_volume_down(self, **kwargs) -> None:
        await self.async_set_volume_level((self._attr_volume_level or 0.0) - 0.1)

    async def async_mute_volume(self, mute: bool = False, **kwargs) -> None:
        # Home Assistant renames the service field to "mute" before calling
        # this, so the parameter must be named mute (not muted).
        self._attr_is_volume_muted = bool(mute)
        if self._source_url:
            position = await self._current_position()
            await self._restart(
                0.0 if mute else (self._attr_volume_level or 1.0),
                position,
            )

    async def async_media_pause(self, **kwargs) -> None:
        """Pause playback, remembering where to resume."""
        if not self._source_url:
            return
        self._paused_position = await self._current_position()
        await self._async_detach_source()
        self._is_paused = True
        self._attr_state = STATE_PAUSED
        self._stamp_position(self._paused_position)

    async def async_media_play(self, **kwargs) -> None:
        """Start the song chosen in the dashboard, or resume a paused track.

        The play button of a media-control card calls media_player.media_play,
        so this is what the user actually presses on the dashboard.

        A song picked in the dashboard selector always wins: the user
        selected it, so that is what must play. Only when nothing is
        selected does the button fall back to resuming the paused track.
        """
        song = self.hass.states.get("input_select.music_selector")
        title = (song.state if song else "") or ""
        file = self.music_library.get(title)
        if file:
            # The selector still holds the song that is already loaded: the
            # user pressed play again, not a new song. Resume where the
            # track was paused instead of starting over.
            if self._current_file == file and self._paused_position is not None:
                volume = (
                    1.0
                    if self._attr_is_volume_muted
                    else (self._attr_volume_level or 1.0)
                )
                await self._restart(volume, self._paused_position)
                self._is_paused = False
                self._attr_state = STATE_PLAYING
                return

            # A different song was selected, so it starts from the
            # beginning and the remembered pause position must not leak
            # into it.
            self._paused_position = None
            self._is_paused = False
            url = (
                f"{self.media_server}/s/{quote(file)}"
                if self.media_server
                else f"media-source://media_source/local/{quote(file)}"
            )
            await self.async_play_media("music", url)
            return

        # Nothing selected in the dashboard (the selector resets to its
        # placeholder after a Home Assistant restart). Resume the track that
        # was paused instead of doing nothing at all.
        if self._source_url and self._paused_position is not None:
            volume = (
                1.0 if self._attr_is_volume_muted else (self._attr_volume_level or 1.0)
            )
            await self._restart(volume, self._paused_position)
            self._is_paused = False
            self._attr_state = STATE_PLAYING
            return

        if self._source_url:
            _LOGGER.info("No song selected; resuming the last played track")
            volume = (
                1.0 if self._attr_is_volume_muted else (self._attr_volume_level or 1.0)
            )
            await self._restart(volume, 0.0)
            self._is_paused = False
            self._attr_state = STATE_PLAYING
            return

        _LOGGER.warning(
            "Play pressed but no song is selected in input_select.music_selector"
        )

    async def async_media_seek(self, position: float, **kwargs) -> None:
        if not self._source_url:
            return
        await self._restart(
            0.0 if self._attr_is_volume_muted else (self._attr_volume_level or 1.0),
            max(0.0, float(position)),
        )
        self._stamp_position(max(0.0, float(position)))

    async def async_update(self):
        try:
            r = await async_get_clientsession(self.hass).get(
                utils.api_streams(self.hass), params={"src": self.unique_id}, timeout=9
            )
            self._attr_available = r.ok
            resp = await r.json(content_type=None)
            # Remember the pushed source (and its byte counter) from this
            # single request instead of asking go2rtc again for each value.
            self._last_stream = resp
            # A pushed source shows up as an extra producer next to the
            # camera's own one. The camera's own producer has no format
            # name (None) or "tapo"; the pushed audio producer is "wav".
            # Only the "wav" producer means audio is actually being sent.
            playing = any(
                p.get("format_name") == "wav" for p in resp.get("producers") or []
            )
            # A paused player must never be flipped back to playing by a
            # producer that is still lingering for a moment after the pause.
            if not self._is_paused:
                self._attr_state = STATE_PLAYING if playing else STATE_IDLE
        except Exception:  # noqa: BLE001
            pass

        if self._is_paused:
            self._attr_state = STATE_PAUSED
            self._stamp_position(self._paused_position or 0)
            return

        if self._attr_state != STATE_PLAYING:
            return

        # After a Home Assistant restart the entity has no title or duration
        # yet, but the audio is still playing. Recover both from the source
        # that go2rtc is pushing.
        if not self._source_url:
            source = await self._async_source_url()
            if source:
                self._source_url = source
                self._source_start = 0.0
        if self._source_url:
            if not self._attr_media_title:
                self._attr_media_title = self._source_url.rsplit("/", 1)[-1]
            if not self._attr_media_duration:
                self._attr_media_duration = await self._async_fetch_duration(
                    self._source_url.split("?")[0]
                )
                self._duration = self._attr_media_duration

        # Derive the playback position from the bytes actually pushed to the
        # camera: one byte is one millisecond of 8000 Hz mono PCMA audio.
        position = await self._current_position()
        if self._attr_media_duration:
            position = min(position, self._attr_media_duration)
        self._stamp_position(position)

    async def async_browse_media(
        self, media_content_type: str = None, media_content_id: str = None
    ) -> BrowseMedia:
        return await media_source.async_browse_media(self.hass, media_content_id)
