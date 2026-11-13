import asyncio
import logging
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urljoin

import voluptuous as vol
from aiohttp import web
from aiohttp.web_exceptions import HTTPUnauthorized, HTTPGone, HTTPNotFound
from homeassistant.components.binary_sensor import HomeAssistant  # fix tests
from homeassistant.components.camera import async_get_stream_source, async_get_image
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, CONF_URL, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import get_url
from homeassistant.helpers.template import Template

from . import utils
from .utils import DOMAIN, Server

_LOGGER = logging.getLogger(__name__)

CREATE_LINK_SCHEMA = vol.Schema(
    {
        vol.Required("link_id"): cv.string,
        vol.Exclusive("url", "url"): cv.string,
        vol.Exclusive("entity", "url"): cv.entity_id,
        vol.Optional("open_limit", default=1): cv.positive_int,
        vol.Optional("time_to_live", default=60): cv.positive_int,
    },
    required=True,
)

DASH_CAST_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Exclusive("url", "url"): cv.string,
        vol.Exclusive("entity", "url"): cv.entity_id,
        vol.Optional("extra"): dict,
        vol.Optional("force", default=False): bool,
        vol.Optional("hass_url"): str,
    },
    required=True,
)

LINKS = {}  # 2 3 4

# YAML setup support.
#
# The card JS is only served (and only registered as a Lovelace resource)
# from async_setup. For a config_flow-only integration async_setup runs
# only after a config entry exists - and the config flow refuses to create
# one while go2rtc is unreachable. That leaves a fresh installation with a
# "Custom element not found" card and no obvious way out.
#
# Declaring a CONFIG_SCHEMA lets the integration be set up from YAML, so
# the card is available even before (or without) go2rtc being reachable.
#
# The schema is keyed by DOMAIN: Home Assistant applies CONFIG_SCHEMA to
# the *whole* configuration, so the schema has to describe the
# "webrtc_mic:" block, not the top level. No default is given for the
# domain block, so nothing is injected into the global config when the
# user does not use YAML at all.
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                # URL of the go2rtc server. Omit to auto-detect it.
                vol.Optional("url"): cv.string,
            },
            extra=vol.ALLOW_EXTRA,
        ),
    },
    extra=vol.ALLOW_EXTRA,
)

# DDoS protection against requests to HLS proxy
# streams are additionally protected by a random playlist identifier
HLS_COOKIE = "webrtc-mic-hls-session"
HLS_SESSION = str(uuid.uuid4())


async def async_setup(hass: HomeAssistant, config: dict):
    # 0. go2rtc URL from YAML (if any). The config entry overwrites it later.
    #    Without YAML we auto-detect, so the card works out of the box.
    yaml_config = config.get(DOMAIN) or {}
    url = yaml_config.get("url")
    if not url:
        url = await utils.detect_go2rtc(hass) or utils.DEFAULT_URL
    hass.data[DOMAIN] = url

    # 1. Serve lovelace card
    path = Path(__file__).parent / "www"
    for name in ("video-rtc.js", "webrtc-mic-camera.js", "digital-ptz.js"):
        await utils.register_static_path(hass, "/webrtc_mic/" + name, str(path / name))

    # 2. Add card to resources
    version = getattr(hass.data["integrations"][DOMAIN], "version", 0)
    await utils.init_resource(hass, "/webrtc_mic/webrtc-mic-camera.js", str(version))

    # 3. Serve html page
    await utils.register_static_path(hass, "/webrtc_mic/embed", str(path / "embed.html"))

    # 4. Serve WebSocket API
    hass.http.register_view(WebSocketView)

    # 5. Serve HLS proxy
    hass.http.register_view(HLSView)

    # 6. Register webrtc.create_link and webrtc.dash_cast services:

    async def create_link(call: ServiceCall):
        link_id = call.data["link_id"]
        ttl = call.data["time_to_live"]
        LINKS[link_id] = {
            "url": call.data.get("url"),
            "entity": call.data.get("entity"),
            "limit": call.data["open_limit"],
            "ts": time.time() + ttl if ttl else 0,
        }

    async def dash_cast(call: ServiceCall):
        link_id = uuid.uuid4().hex
        LINKS[link_id] = {
            "url": call.data.get("url"),  # camera URL (rtsp...)
            "entity": call.data.get("entity"),  # camera entity id
            "limit": 1,  # 1 attempt
            "ts": time.time() + 30,  # for 30 seconds
        }

        hass_url = call.data.get("hass_url") or get_url(hass)
        query = call.data.get("extra", {})
        query["url"] = link_id
        cast_url = hass_url + "/webrtc_mic/embed?" + urlencode(query)

        _LOGGER.debug(f"dash_cast: {cast_url}")

        await hass.async_add_executor_job(
            utils.dash_cast,
            hass,
            call.data[ATTR_ENTITY_ID],
            cast_url,
            call.data.get("force", False),
        )

    hass.services.async_register(DOMAIN, "create_link", create_link, CREATE_LINK_SCHEMA)
    hass.services.async_register(DOMAIN, "dash_cast", dash_cast, DASH_CAST_SCHEMA)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    # 1. If user set custom url
    go_url = entry.data.get(CONF_URL)

    # 2. Check if go2rtc running on same server (auto-detect the location)
    if not go_url:
        go_url = await utils.detect_go2rtc(hass)

    if go_url:
        # netloc example: admin:admin@192.168.1.123:1984
        hass.data[DOMAIN] = go_url
        return True

    # 3. Serve go2rtc binary manually.
    #
    # This is a last resort: a bundled go2rtc has NO streams configured, so
    # every camera card would fail with "Can't get URL for ...". Only use
    # it when the user explicitly asked for it (the bundled binary has
    # streams of its own in some setups) - otherwise fail loudly with a
    # message that tells the user what to configure.
    if not entry.data.get("bundled_go2rtc"):
        _LOGGER.error(
            "Could not find a go2rtc server. Set the go2rtc URL in "
            "configuration.yaml (webrtc_mic: url: http://<host>:1984/) or "
            "add the integration again and enter the URL."
        )
        return False

    binary = await hass.async_add_executor_job(utils.validate_binary, hass)
    if not binary:
        return False

    hass.data[DOMAIN] = server = Server(binary)
    server.start()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, server.stop)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    server = hass.data[DOMAIN]
    if isinstance(server, Server):
        server.stop()
    return True


async def ws_connect(hass: HomeAssistant, params: dict) -> str:
    # 1. Server URL from card param
    server: str = params.get("server")
    # 2. Server URL from integration settings
    if not server:
        server: str | Server = hass.data[DOMAIN]
    # 3. Server is manual binary
    if isinstance(server, Server):
        assert server.available, "WebRTC server not available"
        server = "http://localhost:1984/"

    if entity_id := params.get("entity"):
        src = None
        try:
            src = await async_get_stream_source(hass, entity_id)
        except Exception:
            # Not a camera entity. Users often put a go2rtc *stream name*
            # here (e.g. "tapo44"). Accept that as well instead of
            # failing with "Camera not found".
            src = None

        if src is None:
            # build link to MJPEG stream
            if state := hass.states.get(entity_id):
                if token := state.attributes.get("access_token"):
                    src = f"{get_url(hass)}/api/camera_proxy_stream/{entity_id}?token={token}"

        if src is None:
            # go2rtc stream name fallback. Search every reachable go2rtc
            # instance, because the configured one may not hold the
            # cameras - that produced the bare "stream not found".
            stream, stream_server = await utils.find_stream_server(hass, entity_id)
            if stream:
                src = stream
                if stream_server:
                    server = stream_server

        if src is None:
            # Give the user something actionable instead of a bare assert.
            per_server = await utils.all_go2rtc_streams(hass)
            go_url = hass.data.get(DOMAIN)
            if isinstance(go_url, Server):
                # the integration started its own bundled go2rtc, which has
                # no streams configured at all
                raise Exception(
                    f"Can't get URL for {entity_id}. The integration is running "
                    f"its own bundled go2rtc (no streams configured). Point the "
                    f"integration at your real go2rtc server - set "
                    f"`webrtc_mic: url: http://<go2rtc>:1984/` in "
                    f"configuration.yaml, or use the URL of a camera entity."
                )
            if per_server:
                detail = "; ".join(
                    f"{url} -> {', '.join(names)}" for url, names in per_server.items()
                )
                raise Exception(
                    f"Can't get URL for {entity_id}. go2rtc instances reached: "
                    f"{detail}. Use one of these stream names as `entity`, or "
                    f"use `url:` instead."
                )
            raise Exception(
                f"Can't get URL for {entity_id}. The go2rtc server at {go_url} "
                f"returned no streams. Check that the go2rtc URL is correct and "
                f"reachable from Home Assistant."
            )

        query = {"src": src, "name": entity_id}
    elif src := params.get("url"):
        if "{{" in src or "{%" in src:
            src = Template(src, hass).async_render()
        # A bare name (no scheme) is a go2rtc *stream name*, not a URL.
        # Resolve it against every reachable instance and switch to the
        # one that really has it, instead of connecting to an empty
        # go2rtc and failing with "stream not found".
        if "://" not in src:
            stream, stream_server = await utils.find_stream_server(hass, src)
            if stream:
                src = stream
                if stream_server:
                    server = stream_server
            else:
                per_server = await utils.all_go2rtc_streams(hass)
                if per_server:
                    detail = "; ".join(
                        f"{url} -> {', '.join(names)}"
                        for url, names in per_server.items()
                    )
                    raise Exception(
                        f"go2rtc stream '{src}' not found. go2rtc instances "
                        f"reached: {detail}. Use one of these stream names, or "
                        f"a full go2rtc URL."
                    )
        query = {"src": src}
    else:
        raise Exception("Missing url or entity")

    return urljoin("ws" + server[4:], "api/ws") + "?" + urlencode(query)


def _get_image_from_entity_id(hass: HomeAssistant, entity_id: str):
    """Get camera component from entity_id."""
    if (component := hass.data.get("image")) is None:
        raise Exception("Image integration not set up")

    if (image := component.get_entity(entity_id)) is None:
        raise Exception("Image not found")

    return image


async def ws_poster(hass: HomeAssistant, params: dict) -> web.Response:
    poster: str = params["poster"]

    if "{{" in poster or "{%" in poster:
        # support Jinja2 tempaltes inside poster
        poster = Template(poster, hass).async_render()

    if poster.startswith("camera."):
        # support entity_id as poster
        image = await async_get_image(hass, poster)
        return web.Response(body=image.content, content_type=image.content_type)

    if poster.startswith("image."):
        # support entity_id as poster
        image_entity = _get_image_from_entity_id(hass, poster)
        image = await image_entity.async_image()
        _LOGGER.debug(f"webrtc image_entity: {image_entity} - {len(image) if image else 0}")
        return web.Response(body=image, content_type="image/jpeg")

    # support poster from go2rtc stream name
    entry = hass.data[DOMAIN]
    url = "http://localhost:1984/" if isinstance(entry, Server) else entry
    url = urljoin(url, "api/frame.jpeg") + "?" + urlencode({"src": poster})

    async with async_get_clientsession(hass).get(url) as r:
        body = await r.read()
        return web.Response(body=body, content_type=r.content_type)


class WebSocketView(HomeAssistantView):
    url = "/api/webrtc_mic/ws"
    name = "api:webrtc_mic:ws"
    requires_auth = False

    async def get(self, request: web.Request):
        params = request.query
        _LOGGER.debug(f"New client: {dict(params)}")

        if request.query.get("embed"):
            link_id = request.query.get("url")
            if link_id not in LINKS:
                raise HTTPNotFound()

            link = LINKS[link_id]
            if link["ts"] and time.time() > link["ts"]:
                LINKS.pop(link_id)
                raise HTTPGone()

            if link["limit"]:
                link["limit"] -= 1
                if link["limit"] == 0:
                    LINKS.pop(link_id)

            params = link

        # fix for https://github.com/AlexxIT/WebRTC/pull/320
        elif not utils.validate_signed_request(request):
            # you shall not pass
            raise HTTPUnauthorized()

        hass = request.app["hass"]

        if "poster" in params:
            return await ws_poster(hass, params)

        ws_server = web.WebSocketResponse(autoclose=False, autoping=False)
        ws_server.set_cookie(HLS_COOKIE, HLS_SESSION)
        await ws_server.prepare(request)

        try:
            url = await ws_connect(hass, params)

            remote = request.headers.get("X-Forwarded-For")
            remote = remote + ", " + request.remote if remote else request.remote

            # https://www.nginx.com/resources/wiki/start/topics/examples/forwarded/
            async with async_get_clientsession(hass).ws_connect(
                url,
                autoclose=False,
                autoping=False,
                headers={
                    "User-Agent": request.headers.get("User-Agent"),
                    "X-Forwarded-For": remote,
                    "X-Forwarded-Host": request.host,
                    "X-Forwarded-Proto": request.scheme,
                },
            ) as ws_client:
                # Proxy requests
                await asyncio.wait(
                    [
                        asyncio.create_task(utils.websocket_forward(ws_server, ws_client)),
                        asyncio.create_task(utils.websocket_forward(ws_client, ws_server)),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )

        except Exception as e:
            await ws_server.send_json({"type": "error", "value": str(e)})

        return ws_server


class HLSView(HomeAssistantView):
    url = "/api/webrtc_mic/hls/{filename}"
    name = "api:webrtc_mic:hls"
    requires_auth = False

    async def get(self, request: web.Request, filename: str):
        if request.cookies.get(HLS_COOKIE) != HLS_SESSION:
            raise HTTPUnauthorized()

        if filename not in ("playlist.m3u8", "init.mp4", "segment.m4s", "segment.ts"):
            raise HTTPNotFound()

        hass: HomeAssistant = request.app["hass"]
        entry = hass.data[DOMAIN]
        url = "http://localhost:1984/" if isinstance(entry, Server) else entry
        url = urljoin(url, "api/hls/" + filename) + "?" + request.query_string

        async with async_get_clientsession(hass).get(url) as r:
            if not r.ok:
                raise HTTPNotFound()

            body = await r.read()
            return web.Response(body=body, content_type=r.content_type)
