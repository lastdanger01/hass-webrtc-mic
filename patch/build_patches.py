#!/usr/bin/env python3
"""
build_patches.py - 產生「版本無關」的補丁檔。

上游 webrtc 整合有三個常見版本（master / v3.6.1 / v3.6.0）。原本的做法是
每個版本各出一份補丁，結果是「補丁跟上游檔案綁死」：
上游檔只要被改過（哪怕只改一行），補丁就套不進去。

這支腳本把改動拆成兩份**內容錨點在三個版本間逐行相同**的補丁：

  webrtc-camera.js.uidefault.patch
      setConfig 的預設值加上 `ui: true`。
      上游的麥克風按鈕住在 renderCustomUI()，而它的第一行是
      `if (!this.config.ui) return;` —— 卡片沒有 ui:true 就整段跳過，
      按鈕從來沒被建立過。改成預設開啟，之後就不再需要手動加設定。

  init.py.entity-stream.patch
      entity: 參數接受 go2rtc 的 stream 名稱。
      上游的 entity: 分支只認真正的 camera entity_id，填 stream 名會直接
      斷言失敗並回報 "Can't get URL for <name>"。

用法:
    python3 build_patches.py <上游原始檔目錄>
    例: python3 build_patches.py /tmp/up/master
        -> 產生 webrtc-camera.js.uidefault.patch / init.py.entity-stream.patch
"""

import difflib
import os
import sys


# --- 改動 1: webrtc-camera.js 預設開啟控制列（含麥克風按鈕）---------------

CARD_OLD = """        this.config = Object.assign({
            mode: config.mse === false ? 'webrtc' : config.webrtc === false ? 'mse' : this.mode,
            media: this.media,
            streams: [{url: config.url, entity: config.entity}],
"""

CARD_NEW = """        this.config = Object.assign({
            mode: config.mse === false ? 'webrtc' : config.webrtc === false ? 'mse' : this.mode,
            media: this.media,
            // The control bar holds the microphone button, and upstream skips
            // the whole bar unless the card sets `ui: true`. Default it on so
            // the microphone exists without any card configuration. An
            // explicit `ui:` in the card still wins, because Object.assign
            // copies the card config over these defaults.
            ui: true,
            streams: [{url: config.url, entity: config.entity}],
"""


# --- 改動 2: __init__.py 的 entity 分支接受 go2rtc stream 名稱 ------------

INIT_OLD = """    if entity_id := params.get("entity"):
        src = await async_get_stream_source(hass, entity_id)
        if src is None:
            # build link to MJPEG stream
            if state := hass.states.get(entity_id):
                if token := state.attributes.get("access_token"):
                    src = f"{get_url(hass)}/api/camera_proxy_stream/{entity_id}?token={token}"
        assert src, f"Can't get URL for {entity_id}"
        query = {"src": src, "name": entity_id}
"""

INIT_NEW = '''    if entity_id := params.get("entity"):
        src = None
        try:
            src = await async_get_stream_source(hass, entity_id)
        except Exception:
            # Not a camera entity. Users often put a go2rtc *stream name*
            # here (e.g. "tapo44"). Accept that as well instead of failing
            # with "Camera not found".
            src = None

        if src is None:
            # build link to MJPEG stream
            if state := hass.states.get(entity_id):
                if token := state.attributes.get("access_token"):
                    src = f"{get_url(hass)}/api/camera_proxy_stream/{entity_id}?token={token}"

        if src is None:
            # go2rtc stream name fallback
            src = await _go2rtc_stream_src(hass, entity_id)

        if src is None:
            streams = await _go2rtc_stream_names(hass)
            raise Exception(
                f"Can't get URL for {entity_id}. "
                f"Available go2rtc streams: {streams or 'none'}. "
                f"Put the stream name in the card's `url:` option."
            )
        query = {"src": src, "name": entity_id}
'''

HELPERS = '''async def _go2rtc_stream_names(hass: HomeAssistant) -> list:
    """Return the go2rtc stream names known to the configured server."""
    try:
        session = async_get_clientsession(hass)
        async with session.get(utils.api_streams(hass), timeout=5) as r:
            if r.status != 200:
                return []
            data = await r.json()
        if isinstance(data, dict):
            return sorted(data.keys())
        return []
    except Exception:
        return []


async def _go2rtc_stream_src(hass: HomeAssistant, name: str) -> str | None:
    """Resolve a go2rtc stream name to a source URL.

    Users often write `entity: tapo44` where `tapo44` is the go2rtc stream
    name and not a Home Assistant camera entity_id.
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

    if stream_name in await _go2rtc_stream_names(hass):
        return stream_name

    return None


async def ws_connect(hass: HomeAssistant, params: dict) -> str:'''

WS_ANCHOR = "async def ws_connect(hass: HomeAssistant, params: dict) -> str:"


def build(upstream_dir):
    www = os.path.join(upstream_dir, "custom_components", "webrtc", "www")
    card_path = os.path.join(www, "webrtc-camera.js")
    init_path = os.path.join(upstream_dir, "custom_components", "webrtc", "__init__.py")

    out = {}

    # --- card ---
    with open(card_path, encoding="utf-8") as f:
        card = f.read()
    if CARD_OLD not in card:
        raise SystemExit("[錯誤] 找不到 webrtc-camera.js 的 setConfig 錨點")
    card_new = card.replace(CARD_OLD, CARD_NEW, 1)
    out["webrtc-camera.js.uidefault.patch"] = ''.join(difflib.unified_diff(
        card.splitlines(keepends=True), card_new.splitlines(keepends=True),
        fromfile="webrtc-camera.js", tofile="webrtc-camera.js", n=3))

    # --- init ---
    with open(init_path, encoding="utf-8") as f:
        init = f.read()
    if INIT_OLD not in init:
        raise SystemExit("[錯誤] 找不到 __init__.py 的 entity 錨點")
    if WS_ANCHOR not in init:
        raise SystemExit("[錯誤] 找不到 __init__.py 的 ws_connect 錨點")
    init_new = init.replace(INIT_OLD, INIT_NEW, 1)
    init_new = init_new.replace(WS_ANCHOR, HELPERS, 1)
    out["init.py.entity-stream.patch"] = ''.join(difflib.unified_diff(
        init.splitlines(keepends=True), init_new.splitlines(keepends=True),
        fromfile="__init__.py", tofile="__init__.py", n=3))

    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    upstream = sys.argv[1]
    outdir = sys.argv[2] if len(sys.argv) > 2 else "."
    os.makedirs(outdir, exist_ok=True)

    patches = build(upstream)
    for name, text in patches.items():
        path = os.path.join(outdir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        print("  已產生 %s (%d bytes, %d hunks)" % (
            name, len(text), text.count("\n@@")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
