import io
import logging
import os
import platform
import re
import stat
import subprocess
import zipfile
from threading import Thread
from typing import Optional
from urllib.parse import urljoin, urlparse

import asyncio
import aiohttp
import jwt
import requests
from aiohttp import web
from homeassistant.components.frontend import add_extra_js_url

try:
    # Only available since Home Assistant 2024.12. Older versions cannot
    # remove an extra module url, but they also never add one twice,
    # because the url changes with every version.
    from homeassistant.components.frontend import remove_extra_js_url
except ImportError:  # pragma: no cover - depends on the HA version
    remove_extra_js_url = None
from homeassistant.components.http.auth import DATA_SIGN_SECRET, SIGN_QUERY_PARAM
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.const import CONF_URL, MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_component import DATA_INSTANCES

_LOGGER = logging.getLogger(__name__)

DOMAIN = "webrtc_mic"

# Key in hass.data that holds the extra module urls we registered with
# the frontend, so an older version of our own module can be replaced
# instead of loading it twice.
DATA_EXTRA_MODULES = "webrtc_mic_extra_modules"

# Key in hass.data that holds the URL of the plain HTTP media server that
# serves the audio to go2rtc. It is used to rewrite the signed Home
# Assistant media URLs (which go2rtc cannot fetch) and to add the "start"
# parameter that lets a track resume in the middle.
MEDIA_SERVER_KEY = "webrtc_mic_media_server"

# One lock per go2rtc stream, shared by the media_player platform and by
# the speaker services. go2rtc accepts only one pushed source per
# destination, so both paths have to take the same lock - otherwise the
# card and a script can push two sources at the same time and leave an
# orphan producer behind.
_STREAM_LOCKS: dict = {}


def stream_lock(name: str) -> asyncio.Lock:
    """Return the push lock of a go2rtc stream, creating it if needed."""
    if name not in _STREAM_LOCKS:
        _STREAM_LOCKS[name] = asyncio.Lock()
    return _STREAM_LOCKS[name]

BINARY_VERSION = "1.9.12"

SYSTEM = {
    "Windows": {"AMD64": "go2rtc_win64.zip", "ARM64": "go2rtc_win_arm64.zip"},
    "Darwin": {"x86_64": "go2rtc_mac_amd64.zip", "arm64": "go2rtc_mac_arm64.zip"},
    "Linux": {
        "armv7l": "go2rtc_linux_arm",
        "armv8l": "go2rtc_linux_arm",  # https://github.com/AlexxIT/WebRTC/issues/18
        "aarch64": "go2rtc_linux_arm64",
        "x86_64": "go2rtc_linux_amd64",
        "i386": "go2rtc_linux_386",
        "i486": "go2rtc_linux_386",
        "i586": "go2rtc_linux_386",
        "i686": "go2rtc_linux_386",
    },
}

DEFAULT_URL = "http://localhost:1984/"

BINARY_NAME = re.compile(
    r"^(go2rtc-\d\.\d\.\d+|go2rtc_v0\.1-rc\.[5-9]|rtsp2webrtc_v[1-5])(\.exe)?$"
)


def get_arch() -> Optional[str]:
    system = SYSTEM.get(platform.system())
    if not system:
        return None
    return system.get(platform.machine())


def unzip(content: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for filename in zf.namelist():
            with zf.open(filename) as f:
                return f.read()


def validate_binary(hass: HomeAssistant) -> Optional[str]:
    filename = f"go2rtc-{BINARY_VERSION}"
    if platform.system() == "Windows":
        filename += ".exe"

    filename = hass.config.path(filename)
    try:
        if os.path.isfile(filename) and subprocess.check_output(
            [filename, "-v"]
        ).startswith(b"go2rtc"):
            return filename
    except:
        pass

    # remove all old binaries
    for file in os.listdir(hass.config.config_dir):
        if BINARY_NAME.match(file):
            _LOGGER.debug(f"Remove old binary: {file}")
            os.remove(hass.config.path(file))

    # download new binary
    url = (
        f"https://github.com/AlexxIT/go2rtc/releases/download/"
        f"v{BINARY_VERSION}/{get_arch()}"
    )
    _LOGGER.debug(f"Download new binary: {url}")
    r = requests.get(url)
    if not r.ok:
        return None

    raw = r.content

    # unzip binary for windows
    if url.endswith(".zip"):
        raw = unzip(raw)

    # save binary to config folder
    with open(filename, "wb") as f:
        f.write(raw)

    # change binary access rights
    os.chmod(filename, os.stat(filename).st_mode | stat.S_IEXEC)

    return filename


async def register_static_path(hass: HomeAssistant, url_path: str, path: str):
    if (MAJOR_VERSION, MINOR_VERSION) >= (2024, 7):
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(url_path, path, True)]
        )
    else:
        hass.http.register_static_path(url_path, path)


def _lovelace_resources(hass: HomeAssistant):
    """Return the Lovelace resource collection, whatever shape it has.

    Home Assistant changed the shape of ``hass.data["lovelace"]`` over
    time: it used to be a dict, newer versions use a dataclass. Both are
    supported here, and a missing Lovelace returns None instead of
    raising, so a not-yet-ready Lovelace cannot break the setup.
    """
    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        return None
    if hasattr(lovelace, "resources"):
        return lovelace.resources
    if isinstance(lovelace, dict):
        return lovelace.get("resources")
    return None


def _add_extra_module(hass: HomeAssistant, url: str) -> None:
    """Register the card module with the frontend, replacing older versions.

    The frontend loads ``extra_module_url`` modules straight into the
    index page. That works in every Lovelace mode, so it is used as a
    safety net next to the Lovelace resource itself.

    Only the newest version of our own module is kept: two different
    urls for the same file would load the module twice.
    """
    known = hass.data.setdefault(DATA_EXTRA_MODULES, set())
    base = url.split("?")[0]

    for old in list(known):
        if old == url or old.split("?")[0] != base:
            continue
        if remove_extra_js_url is not None:
            try:
                remove_extra_js_url(hass, old)
            except Exception:  # noqa: BLE001 - never break the setup
                _LOGGER.debug("Could not remove old module url %s", old)
        known.discard(old)

    try:
        add_extra_js_url(hass, url)
    except KeyError:
        # The frontend integration is not set up (yet).
        _LOGGER.debug("Frontend not ready, skipping extra module url %s", url)
        return
    except Exception:  # noqa: BLE001 - never break the setup
        _LOGGER.exception("Could not register extra module url %s", url)
        return

    known.add(url)


async def init_resource(hass: HomeAssistant, url: str, ver: str) -> bool:
    """Make the Lovelace frontend load the card module.

    The module has to reach the browser through two different channels,
    because which one is used depends on how Lovelace is set up:

    - a Lovelace resource of type ``module`` (storage mode)
    - ``extra_module_url`` in the frontend (YAML mode, and as a safety
      net in storage mode)

    Registering only one of them is what made the card missing on some
    installs: the JS file was served fine (HTTP 200) but the frontend
    never loaded it, so the browser only said
    "Custom element doesn't exist: webrtc-mic-camera".

    Both channels are always used now, and a failure is logged instead
    of being swallowed.
    """
    url2 = f"{url}?v={ver}"
    changed = False

    resources = _lovelace_resources(hass)

    if resources is None:
        _LOGGER.warning(
            "Lovelace is not set up yet, the card module is only registered "
            "as an extra module url for now"
        )
    else:
        try:
            # force load storage
            await resources.async_get_info()

            existing = None
            for item in resources.async_items():
                if item.get("url", "").startswith(url):
                    existing = item
                    break

            # Duck typing instead of isinstance: the class of the
            # collection changed between Home Assistant versions, but a
            # writable collection always has these two methods.
            writable = hasattr(resources, "async_create_item") and hasattr(
                resources, "async_update_item"
            )

            if existing is not None:
                if existing.get("url", "").endswith(ver) and existing.get(
                    "res_type"
                ) == "module":
                    pass  # already up to date
                else:
                    _LOGGER.debug(f"Update lovelace resource to: {url2}")
                    if writable:
                        await resources.async_update_item(
                            existing["id"], {"res_type": "module", "url": url2}
                        )
                    else:
                        # not the best solution, but what else can we do
                        existing["url"] = url2
                    changed = True
            elif writable:
                _LOGGER.debug(f"Add new lovelace resource: {url2}")
                await resources.async_create_item({"res_type": "module", "url": url2})
                changed = True
            else:
                # YAML mode: the frontend gets the module from the
                # configuration, so nothing to store here.
                _LOGGER.debug("Lovelace runs in YAML mode, using extra module url")
        except Exception:  # noqa: BLE001 - never break the setup
            _LOGGER.exception("Could not register the lovelace resource %s", url2)

    _add_extra_module(hass, url2)

    return changed


# noinspection PyProtectedMember
def dash_cast(hass: HomeAssistant, entities: list, url: str, force: bool):
    """Cast webpage to chromecast device via DashCast application."""
    try:
        for entity in hass.data[DATA_INSTANCES]["media_player"].entities:
            if entity.entity_id not in entities or not hasattr(entity, "_chromecast"):
                continue

            if not hasattr(entity, "dashcast"):
                from pychromecast.controllers.dashcast import DashCastController

                entity.dashcast = DashCastController()
                entity._chromecast.register_handler(entity.dashcast)

            _LOGGER.debug(f"DashCast to {entity.entity_id}")
            entity.dashcast.load_url(url, force=force)

    except Exception as e:
        _LOGGER.error(f"Can't DashCast to {entities}", exc_info=e)


def validate_signed_request(request: web.Request) -> bool:
    try:
        hass = request.app["hass"]
        secret = hass.data.get(DATA_SIGN_SECRET)
        signature = request.query.get(SIGN_QUERY_PARAM)
        claims = jwt.decode(signature, secret, algorithms=["HS256"])
        return claims["path"] == request.path
    except Exception:
        return False


def candidate_urls(hass: HomeAssistant) -> list:
    """Build the list of go2rtc URLs worth probing, most specific first.

    Zero-config goal: the user should never have to type a go2rtc URL.
    We look at (in order): the URL stored by the official go2rtc
    integration, the HA-managed go2rtc API port, localhost, the docker
    host gateway, the Frigate add-on hostnames and finally the LAN
    address of this HA host.
    """
    out: list = []

    def add(url):
        if not url:
            return
        # normalise: always end with "/" so urljoin() behaves predictably
        if not url.endswith("/"):
            url += "/"
        if url not in out:
            out.append(url)

    # 1. URLs that other integrations already know about.
    #
    #    The official *go2rtc* integration stores it, and so does the
    #    upstream *webrtc* integration (AlexxIT/WebRTC) - its config entry
    #    holds the very same go2rtc URL. Reading the upstream entry
    #    matters: on a machine that has the upstream integration
    #    installed, that entry is often the only place the real go2rtc URL
    #    is recorded. Missing it made us fall back to a candidate that
    #    answers HTTP but has no streams, and every camera card then failed
    #    with "stream not found".
    #
    #    Only trust a URL if it points at the user's own server - the
    #    HA-managed instance stores HA_MANAGED_URL there and never has any
    #    cameras of its own.
    for domain in ("go2rtc", "webrtc", DOMAIN):
        try:
            for entry in hass.config_entries.async_entries(domain):
                url = entry.data.get(CONF_URL)
                if url and "11984" not in url:
                    add(url)
        except Exception:
            pass

    # 2. go2rtc on the same host / in the same container
    add(DEFAULT_URL)
    add("http://127.0.0.1:1984/")

    # 3. docker host gateway (go2rtc in another container)
    add("http://host.docker.internal:1984/")

    # 5. go2rtc bundled in the Frigate add-on (closed public port)
    for host in (
        "ccab4aaf-frigate",
        "ccab4aaf-frigate-fa",
        "ccab4aaf-frigate-beta",
    ):
        add(f"http://{host}:1984/")

    # 5b. Home Assistant OS add-on hostnames. The add-on slug is prefixed
    #     with a per-install id, so the name is not predictable; try the
    #     well-known aliases plus the add-on id that the official go2rtc
    #     add-on uses when it is the only candidate.
    for host in (
        "a0d7b954-go2rtc",
        "core-go2rtc",
        "hassio-go2rtc",
        "homeassistant-go2rtc",
        "go2rtc",
    ):
        add(f"http://{host}:1984/")

    # 6. LAN address of this HA host (go2rtc on the host, not containerised)
    try:
        internal = hass.config.internal_url
    except Exception:
        internal = None
    if internal:
        host = urlparse(internal).hostname
        if host:
            add(f"http://{host}:1984/")

    # 7. go2rtc managed by HA itself (unix socket + internal API port).
    #    Absolute last resort - it answers HTTP but never has any
    #    streams of its own, so picking it breaks every camera card.
    add("http://localhost:11984/")

    return out


async def detect_go2rtc(hass: HomeAssistant) -> Optional[str]:
    """Probe all known go2rtc locations and return the best one.

    A candidate only counts if it answers AND actually exposes a stream
    list. This matters because several plausible URLs answer while being
    useless:

    - the HA-managed go2rtc (``localhost:11984``) answers but has no
      streams at all - it is HA's own empty instance
    - the official *go2rtc* integration may point at an instance without
      the user's cameras

    Preferring such a URL is what makes every camera card fail with
    "Can't get URL for ...". So we check the stream list, not just the
    HTTP status, and we prefer candidates that actually have streams.
    """
    candidates = candidate_urls(hass)
    if not candidates:
        return None

    session = async_get_clientsession(hass)

    async def probe(url: str):
        """Return (url, stream_count) - stream_count is -1 if unreachable."""
        try:
            r = await session.get(urljoin(url, "api/streams"), timeout=3)
            if r.status >= 400:
                return url, 0
            data = await r.json()
            if isinstance(data, dict):
                return url, len(data)
            return url, 0
        except Exception:
            return url, -1

    results = await asyncio.gather(*(probe(url) for url in candidates))

    # First candidate that answers and has at least one stream.
    for url, (_url, count) in zip(candidates, results):
        if count > 0:
            return url

    # Nothing had streams. Fall back to any candidate that at least
    # answered, so the user can still see a useful error message.
    for url, (_url, count) in zip(candidates, results):
        if count >= 0:
            return url

    return None


async def list_go2rtc_streams(
    hass: HomeAssistant, session=None, url: Optional[str] = None
) -> list:
    """Return the list of stream names known to the go2rtc server.

    `url` overrides the configured server. That matters when the
    configured instance is not the one that actually holds the cameras.
    """
    try:
        if session is None:
            session = async_get_clientsession(hass)
        r = await session.get(api_streams(hass, url=url), timeout=3)
        if r.status >= 400:
            return []
        streams = await r.json()
    except Exception:
        return []

    if isinstance(streams, dict):
        return sorted(streams.keys())
    return []


async def check_go2rtc_stream(
    hass: HomeAssistant, name: str, session=None, url: Optional[str] = None
) -> Optional[str]:
    """Check whether `name` is a known go2rtc stream.

    Users often write `entity: tapo44` where `tapo44` is the *go2rtc
    stream name* and not a Home Assistant camera entity_id. This helper
    lets the integration recognise that case instead of failing with
    "Camera not found".

    A `media_player.*` entity_id is also accepted: the upstream webrtc
    integration creates one media_player per go2rtc stream, and the
    entity registry knows the stream name it was created from.
    """
    if not name:
        return None

    stream_name = name

    # media_player entity_id -> the go2rtc stream name it was created from
    if name.startswith("media_player."):
        try:
            from homeassistant.helpers import entity_registry as er

            entry = er.async_get(hass).async_get(name)
            if entry and entry.unique_id:
                stream_name = entry.unique_id
        except Exception:
            pass

    streams = await list_go2rtc_streams(hass, session=session, url=url)

    if stream_name in streams:
        return stream_name

    return None


async def find_stream_server(
    hass: HomeAssistant, name: str, session=None
) -> tuple[Optional[str], Optional[str]]:
    """Locate a go2rtc stream by name.

    Returns ``(stream_name, server_url)``. The server URL is the go2rtc
    instance that actually holds the stream.

    Why this is needed: several go2rtc instances can answer HTTP at the
    same time - the HA-managed one on port 11984, a bundled binary, an
    add-on - and most of them have no streams of their own. Connecting to
    the wrong one produces the bare go2rtc error "stream not found" with
    no hint about which instance was used or what the real names are.

    So we search every candidate instance and return the one that really
    has the requested stream.
    """
    if not name:
        return None, None

    stream_name = name

    if name.startswith("media_player."):
        try:
            from homeassistant.helpers import entity_registry as er

            entry = er.async_get(hass).async_get(name)
            if entry and entry.unique_id:
                stream_name = entry.unique_id
        except Exception:
            pass

    # The configured server first - it is the one the card normally uses.
    configured = hass.data.get(DOMAIN)
    if isinstance(configured, Server):
        configured = "http://localhost:1984/"

    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    for url in candidate_urls(hass):
        if url not in candidates:
            candidates.append(url)

    for url in candidates:
        streams = await list_go2rtc_streams(hass, session=session, url=url)
        if stream_name in streams:
            return stream_name, url

    return None, None


async def all_go2rtc_streams(hass: HomeAssistant, session=None) -> dict:
    """Return {server_url: [stream names]} for every reachable instance.

    Used to build an error message that tells the user which instance is
    in use and what the real stream names are.
    """
    out: dict = {}

    configured = hass.data.get(DOMAIN)
    if isinstance(configured, Server):
        configured = "http://localhost:1984/"

    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    for url in candidate_urls(hass):
        if url not in candidates:
            candidates.append(url)

    for url in candidates:
        streams = await list_go2rtc_streams(hass, session=session, url=url)
        if streams:
            out[url] = streams

    return out


async def check_go2rtc(hass: HomeAssistant, url: str = DEFAULT_URL) -> Optional[str]:
    session = async_get_clientsession(hass)
    try:
        r = await session.head(url, timeout=2, allow_redirects=False)
        return url if r.status < 300 else None
    except Exception:
        return None


def api_streams(hass: HomeAssistant, url: Optional[str] = None) -> str:
    if url:
        return urljoin(url, "api/streams")
    entry = hass.data[DOMAIN]
    go_url = "http://localhost:1984/" if isinstance(entry, Server) else entry
    return urljoin(go_url, "api/streams")


async def go2rtc_streams_state(
    hass: HomeAssistant, session=None, url: Optional[str] = None
) -> dict:
    """Return the raw go2rtc stream states.

    Each stream has ``producers`` (connections to the camera) and
    ``consumers`` (clients currently watching). A consumer carries
    ``remote_addr`` and ``user_agent``, which is what makes it possible to
    tell "how many people" from "how many connections".
    """
    try:
        if session is None:
            session = async_get_clientsession(hass)
        r = await session.get(api_streams(hass, url=url), timeout=5)
        if r.status >= 400:
            return {}
        streams = await r.json()
    except Exception:
        return {}

    return streams if isinstance(streams, dict) else {}


def count_viewers(streams: dict) -> dict:
    """Turn raw go2rtc stream states into a viewer summary.

    Two different numbers come out of the same data:

    * ``connections`` - how many client connections are watching. One person
      with two tabs produces two.
    * ``people`` - how many distinct devices. Grouped by source IP and
      User-Agent, so one person watching four cameras still counts once.
    """
    per_stream = {}
    devices = {}

    for name, state in (streams or {}).items():
        state = state or {}
        consumers = state.get("consumers") or []
        producers = state.get("producers") or []

        entries = []
        for c in consumers:
            addr = (c.get("remote_addr") or "?").rsplit(":", 1)[0]
            ua = c.get("user_agent") or "-"
            entries.append({"ip": addr, "ua": ua,
                            "protocol": c.get("protocol") or c.get("format_name") or "?"})
            devices.setdefault((addr, ua), set()).add(name)

        per_stream[name] = {
            "connections": len(consumers),
            "producers": len(producers),
            "viewers": entries,
        }

    people = []
    for (addr, ua), names in sorted(devices.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        people.append({"ip": addr, "ua": ua, "streams": sorted(names)})

    total_conn = sum(s["connections"] for s in per_stream.values())

    return {
        "people": len(people),
        "connections": total_conn,
        "devices": people,
        "streams": per_stream,
    }


# copied from homeassistant.components.hassio.ingress import _websocket_forward
async def websocket_forward(ws_from, ws_to) -> None:
    try:
        async for msg in ws_from:
            if msg.type is aiohttp.WSMsgType.TEXT:
                await ws_to.send_str(msg.data)
            elif msg.type is aiohttp.WSMsgType.BINARY:
                await ws_to.send_bytes(msg.data)
            elif msg.type is aiohttp.WSMsgType.PING:
                await ws_to.ping()
            elif msg.type is aiohttp.WSMsgType.PONG:
                await ws_to.pong()
            elif ws_to.closed:
                await ws_to.close(code=ws_to.close_code, message=msg.extra)  # type: ignore[arg-type]
    except Exception as e:
        _LOGGER.debug(f"WebSocket forward exception: {repr(e)}")


class Server(Thread):
    def __init__(self, binary: str):
        super().__init__(name=DOMAIN, daemon=True)
        self.binary = binary
        self.process = None

    @property
    def available(self):
        return self.process.poll() is None if self.process else False

    def run(self):
        while self.binary:
            self.process = subprocess.Popen(
                [self.binary], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )

            # check alive
            while self.process.poll() is None:
                line = self.process.stdout.readline()
                if line == b"":
                    break
                _LOGGER.debug(line[:-1].decode())

    def stop(self, *args):
        self.binary = None
        self.process.terminate()
