"""Play audio straight through a camera speaker.

The ``media_player`` platform of this integration needs one YAML block per
camera. This module offers the same thing as a service, so a camera can
play music without any configuration at all: the card just calls
``webrtc_mic.play_media`` with the go2rtc stream name.

The audio is pushed to the camera through the go2rtc backchannel, exactly
like the media_player platform does it, so both paths behave the same.
"""

import asyncio
import logging

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

PLAY_MEDIA_SCHEMA = vol.Schema(
    {
        vol.Required("stream"): cv.string,
        vol.Required("media_content_id"): cv.string,
        vol.Optional("title"): cv.string,
        vol.Optional("audio", default=DEFAULT_AUDIO): cv.string,
    }
)

STOP_MEDIA_SCHEMA = vol.Schema(
    {
        vol.Required("stream"): cv.string,
    }
)

# One lock per stream: go2rtc accepts only one pushed source per
# destination, so two concurrent pushes would leave an orphan behind.
_LOCKS: dict[str, asyncio.Lock] = {}


def _lock(stream: str) -> asyncio.Lock:
    """Return the push lock of a stream, creating it if needed."""
    if stream not in _LOCKS:
        _LOCKS[stream] = asyncio.Lock()
    return _LOCKS[stream]


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


class Speaker:
    """Push an audio source to a camera and make sure it really plays."""

    def __init__(self, hass: HomeAssistant, stream: str, audio: str = DEFAULT_AUDIO):
        self.hass = hass
        self.stream = stream
        self.audio = audio

    async def _pushed_producers(self) -> list[dict]:
        """All audio producers go2rtc currently pushes to this camera."""
        try:
            r = await async_get_clientsession(self.hass).get(
                utils.api_streams(self.hass), params={"src": self.stream}, timeout=6
            )
            if not r.ok:
                return []
            resp = await r.json(content_type=None)
        except Exception:  # noqa: BLE001
            return []
        return [
            p for p in resp.get("producers") or [] if p.get("format_name") == "wav"
        ]

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
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "go2rtc purge failed (%s): %s",
                    type(exc).__name__,
                    exc or "no details",
                )
            await asyncio.sleep(0.4)
            remaining = len(await self._pushed_producers())
            if not remaining:
                return 0
        return remaining

    async def async_play(self, url: str) -> None:
        """Push the audio at the given URL to the camera."""
        src = f"ffmpeg:{url}#audio={self.audio}#input=file"

        async with _lock(self.stream):
            await self._detach()
            leftover = await self._purge()
            if leftover:
                _LOGGER.warning(
                    "go2rtc kept %s audio sources for %s, forcing them off",
                    leftover,
                    self.stream,
                )

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
            for _ in range(8):
                await asyncio.sleep(0.4)
                if await self._pushed_producers():
                    _LOGGER.info("Audio push accepted by go2rtc: %s", src)
                    return

        raise HomeAssistantError("go2rtc 接受了音訊，但相機沒有聲音")

    async def async_stop(self) -> None:
        """Stop whatever is playing on the camera."""
        async with _lock(self.stream):
            await self._detach()
            await self._purge()


async def async_play_media(call: ServiceCall) -> None:
    """Handle the webrtc_mic.play_media service."""
    hass = call.hass
    stream = call.data["stream"]
    audio = call.data.get("audio") or DEFAULT_AUDIO

    url = await async_resolve_url(hass, call.data["media_content_id"])
    await Speaker(hass, stream, audio).async_play(url)


async def async_stop_media(call: ServiceCall) -> None:
    """Handle the webrtc_mic.stop_media service."""
    await Speaker(call.hass, call.data["stream"]).async_stop()
