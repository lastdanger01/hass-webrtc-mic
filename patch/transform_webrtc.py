#!/usr/bin/env python3
"""
transform_webrtc.py - 以「內容錨點」把麥克風對講改動套進上游 webrtc 整合。

為什麼不用 diff patch？
    原本的做法是每個上游版本各出一份 diff patch（video-rtc.js.patch /
    .v361.patch / .v360.patch）。結果是補丁跟上游檔案綁死：上游檔只要被
    改過（哪怕只改一行），patch 就套不進去。實務上這是安裝失敗最常見的原因。

這支腳本以「程式碼內容」當錨點，不看行號，所以任何上游版本都能套。
同一段程式碼在不同版本有不同寫法時（實測有 `media.includes()` 與
`media.indexOf(...) >= 0` 兩種），會列出多個候選錨點逐一嘗試。

用法:
    python3 transform_webrtc.py <整合目錄>

整合目錄必須包含:
    <dir>/www/video-rtc.js
    <dir>/www/webrtc-camera.js
    <dir>/__init__.py
    <dir>/manifest.json

設計原則:
    * 全部改動套用成功才寫檔；任一項失敗就整組不動（不留半套的檔案）。
    * 已套用過的改動會被偵測到並跳過（可重複執行）。
    * 會自動把 manifest.json 的 version 加上 -micN 後綴，讓上游的
      init_resource() 用新版本號改寫 Lovelace 資源 URL → 瀏覽器自動
      拿到新檔，不需要手動改儀表板資源。
"""

import os
import re
import shutil
import sys


# --------------------------------------------------------------------------
# 改動定義：每項 = (說明, [(候選舊文, 新文), ...])
# 候選舊文只要有一個命中就算成功。
# --------------------------------------------------------------------------

VIDEO_RTC_CHANGES = [
    (
        "constructor: 麥克風狀態欄位",
        [(
            """        this.onmessage = null;
""",
            """        this.onmessage = null;

        /**
         * [info] Browser microphone stream (kept across reconnects).
         * @type {MediaStream|null}
         */
        this.micStream = null;

        /**
         * [info] Browser microphone track used for the camera backchannel.
         * @type {MediaStreamTrack|null}
         */
        this.micTrack = null;

        /**
         * [info] Microphone enabled state.
         * @type {boolean}
         */
        this.micEnabled = false;

        /**
         * [info] True when the browser blocked autoplay with sound and we
         * muted the element ourselves. A user gesture can undo this.
         * @type {boolean}
         */
        this.autoMuted = false;
""",
        )],
    ),
    (
        "play(): 只在真的 autoplay 被擋時才靜音",
        [(
            """    play() {
        this.video.play().catch(() => {
            if (!this.video.muted) {
                this.video.muted = true;
                this.video.play().catch(er => {
                    console.warn(er);
                });
            }
        });
    }
""",
            """    play() {
        this.video.play().catch(er => {
            // Only a real autoplay block (NotAllowedError) justifies muting.
            // AbortError just means a new load/pause interrupted the request -
            // muting there silences the camera audio for no reason.
            if (er && er.name === 'NotAllowedError' && !this.video.muted) {
                // Autoplay with sound was blocked by the browser. Remember that
                // we muted it ourselves so a later user gesture can restore it.
                this.autoMuted = true;
                this.video.muted = true;
                this.video.play().catch(e2 => {
                    console.warn(e2);
                });
            }
        });
    }

    /**
     * Restore sound after the browser auto-muted the element because autoplay
     * with audio was blocked. Must be called from a user gesture handler.
     * @return {boolean} true if the sound was restored
     */
    unmuteAutoplay() {
        if (!this.video || !this.autoMuted) return false;
        this.autoMuted = false;
        this.video.muted = false;
        this.video.play().catch(() => {
            // still blocked - fall back to muted again
            this.autoMuted = true;
            this.video.muted = true;
        });
        return true;
    }
""",
        ), (
            # v3.6.0 already checks NotAllowedError - only the unmute helper is
            # missing there.
            """    play() {
        this.video.play().catch(er => {
            const isAutoplayError = er.name === 'NotAllowedError';
            if (!this.video.muted && isAutoplayError) {
                this.video.muted = true;
                this.video.play().catch(er => {
                    console.warn(er);
                });
            }
        });
    }
""",
            """    play() {
        this.video.play().catch(er => {
            const isAutoplayError = er.name === 'NotAllowedError';
            if (!this.video.muted && isAutoplayError) {
                this.autoMuted = true;
                this.video.muted = true;
                this.video.play().catch(er => {
                    console.warn(er);
                });
            }
        });
    }

    /**
     * Restore sound after the browser auto-muted the element because autoplay
     * with audio was blocked. Must be called from a user gesture handler.
     * @return {boolean} true if the sound was restored
     */
    unmuteAutoplay() {
        if (!this.video || !this.autoMuted) return false;
        this.autoMuted = false;
        this.video.muted = false;
        this.video.play().catch(() => {
            // still blocked - fall back to muted again
            this.autoMuted = true;
            this.video.muted = true;
        });
        return true;
    }
""",
        )],
    ),
    (
        "setPCMACodecPreference / enableMicrophone / disableMicrophone",
        [(
            """    /**
     * Set video source (WebSocket URL). Support relative path.
""",
            """    /**
     * Prefer PCMA/8000 for the microphone. Tapo cameras only accept PCMA on
     * the backchannel and go2rtc has no Opus decoder for it.
     * @param transceiver {RTCRtpTransceiver}
     */
    setPCMACodecPreference(transceiver) {
        try {
            const caps = RTCRtpSender.getCapabilities('audio');
            if (!caps) return;
            const pcma = caps.codecs.filter(c => /PCMA/i.test(c.mimeType));
            if (pcma.length) transceiver.setCodecPreferences(pcma);
        } catch (e) {
            console.warn(e);
        }
    }

    /**
     * Request microphone permission and renegotiate the peer connection so the
     * sendonly audio transceiver reaches the camera backchannel.
     * @return {Promise<void>}
     */
    async enableMicrophone() {
        if (!this.micTrack) {
            const media = await navigator.mediaDevices.getUserMedia({audio: true});
            this.micStream = media;
            this.micTrack = media.getAudioTracks()[0];
        }
        this.micTrack.enabled = true;
        this.micEnabled = true;
        if (!this.media.includes('microphone')) this.media += ',microphone';
        // Keep config.media in sync as well. nextStream() resets
        // `this.media` from `this.config.media`, so without this the
        // microphone would be dropped again when the user switches streams.
        if (!this.config.media || !this.config.media.includes('microphone')) {
            this.config.media = this.media;
        }
        this.ondisconnect();
        setTimeout(() => this.onconnect(), 100);
    }

    /**
     * Mute the microphone without dropping the peer connection.
     * @return {Promise<void>}
     */
    async disableMicrophone() {
        this.micEnabled = false;
        if (this.micTrack) this.micTrack.enabled = false;
    }

    /**
     * Set video source (WebSocket URL). Support relative path.
""",
        )],
    ),
    (
        "onconnect: WebSocket 關閉競態修復",
        [(
            """        this.ws = new WebSocket(this.wsURL);
        this.ws.binaryType = 'arraybuffer';
        this.ws.addEventListener('open', () => this.onopen());
        this.ws.addEventListener('close', () => this.onclose());
""",
            """        // Keep a reference to this socket. The `close` event fires
        // asynchronously, so by the time it arrives `this.ws` may already
        // point at a newer socket - enableMicrophone() closes the old one
        // and reconnects to add the sendonly audio transceiver. Acting on the
        // stale event would kill the new socket, so the microphone track
        // would never reach the camera.
        const ws = this.ws = new WebSocket(this.wsURL);
        ws.binaryType = 'arraybuffer';
        ws.addEventListener('open', () => this.onopen());
        ws.addEventListener('close', () => {
            if (this.ws === ws) this.onclose();
        });
""",
        )],
    ),
    (
        "ondisconnect: 不要停掉麥克風 track",
        [(
            """                if (sender.track) sender.track.stop();
""",
            """                if (sender.track && sender.track !== this.micTrack) sender.track.stop();
""",
        )],
    ),
    (
        "onopen: this.ws 可能已被關閉的防護",
        [(
            """        this.wsState = WebSocket.OPEN;

        this.ws.addEventListener('message', ev => {
""",
            """        this.wsState = WebSocket.OPEN;

        // The socket may have been closed or replaced between the `open`
        // event firing and this handler running. Without this guard the card
        // dies with "null is not an object (evaluating
        // 'this.ws.addEventListener')" and stops sending the microphone.
        if (!this.ws) return;

        this.ws.addEventListener('message', ev => {
""",
        )],
    ),
    (
        "createOffer: 用 micTrack 並偏好 PCMA",
        [(
            """            if (this.media.includes('microphone')) {
                const media = await navigator.mediaDevices.getUserMedia({audio: true});
                media.getTracks().forEach(track => {
                    pc.addTransceiver(track, {direction: 'sendonly'});
                });
            }
""",
            """            if (this.media.includes('microphone') && this.micTrack) {
                const track = this.micTrack;
                track.enabled = this.micEnabled;
                const transceiver = pc.addTransceiver(track, {direction: 'sendonly'});
                this.setPCMACodecPreference(transceiver);
            }
""",
        ), (
            """            if (this.media.indexOf('microphone') >= 0) {
                const media = await navigator.mediaDevices.getUserMedia({audio: true});
                media.getTracks().forEach(track => {
                    pc.addTransceiver(track, {direction: 'sendonly'});
                });
            }
""",
            """            if (this.media.includes('microphone') && this.micTrack) {
                const track = this.micTrack;
                track.enabled = this.micEnabled;
                const transceiver = pc.addTransceiver(track, {direction: 'sendonly'});
                this.setPCMACodecPreference(transceiver);
            }
""",
        )],
    ),
]


CARD_CHANGES = [
    (
        "setConfig: 預設開啟控制列（麥克風按鈕）",
        [(
            """        this.config = Object.assign({
            mode: config.mse === false ? 'webrtc' : config.webrtc === false ? 'mse' : this.mode,
            media: this.media,
            streams: [{url: config.url, entity: config.entity}],
""",
            """        this.config = Object.assign({
            mode: config.mse === false ? 'webrtc' : config.webrtc === false ? 'mse' : this.mode,
            media: this.media,
            // The control bar holds the microphone button, and upstream skips
            // the whole bar unless the card sets `ui: true`. Default it on so
            // the microphone exists without any card configuration. An
            // explicit `ui:` in the card still wins, because Object.assign
            // copies the card config over these defaults.
            ui: true,
            streams: [{url: config.url, entity: config.entity}],
""",
        )],
    ),
    (
        "CSS: 麥克風按鈕樣式",
        [(
            """                .volume {
                    display: none;
                }
""",
            """                .volume {
                    display: none;
                }
                .microphone {
                    display: none;
                    color: #ff5252;
                }
                .microphone.active {
                    color: #00c853;
                }
""",
        )],
    ),
    (
        "控制列: 麥克風按鈕",
        [(
            """                    <span class="space"></span>
                    <ha-icon class="play" icon="mdi:play"></ha-icon>
""",
            """                    <span class="space"></span>
                    <ha-icon class="microphone" icon="mdi:microphone-off"></ha-icon>
                    <ha-icon class="play" icon="mdi:play"></ha-icon>
""",
        )],
    ),
    (
        "麥克風按鈕顯示 + 點擊還原被自動靜音的音量",
        [(
            """        const ui = this.querySelector('.ui');
        ui.addEventListener('click', ev => {
""",
            """        const ui = this.querySelector('.ui');
        const mic = this.querySelector('.microphone');
        if (this.config.microphone !== false) {
            mic.style.display = 'block';
        }

        // The browser blocks autoplay with sound and silently mutes the video
        // element. Any user gesture on the card is a chance to restore sound.
        this.addEventListener('click', () => {
            if (this.unmuteAutoplay()) {
                this.querySelector('.volume').icon = this.video.muted ? 'mdi:volume-mute' : 'mdi:volume-high';
            }
        });
        ui.addEventListener('click', ev => {
""",
        )],
    ),
    (
        "點擊麥克風: 開/關麥克風",
        [(
            """            } else if (icon === 'mdi:rectangle') {
                document.exitPictureInPicture().catch(console.warn);
            } else if (ev.target.className === 'stream') {
""",
            """            } else if (icon === 'mdi:rectangle') {
                document.exitPictureInPicture().catch(console.warn);
            } else if (icon === 'mdi:microphone-off') {
                mic.icon = 'mdi:microphone';
                mic.classList.add('active');
                // a click is a user gesture: restore sound blocked by autoplay policy
                this.unmuteAutoplay();
                this.enableMicrophone().catch(err => {
                    console.warn(err);
                    mic.icon = 'mdi:microphone-off';
                    mic.classList.remove('active');
                });
            } else if (icon === 'mdi:microphone') {
                this.disableMicrophone().catch(console.warn);
                mic.classList.remove('active');
                mic.icon = 'mdi:microphone-off';
            } else if (ev.target.className === 'stream') {
""",
        )],
    ),
]


# __init__.py: entity: 接受 go2rtc 的 stream 名稱
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

INIT_HELPERS = '''async def _go2rtc_stream_names(hass: HomeAssistant) -> list:
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

INIT_WS_ANCHOR = "async def ws_connect(hass: HomeAssistant, params: dict) -> str:"


def apply_changes(content, changes, path):
    """套用所有改動；回傳 (新內容, 套用清單, 跳過清單, 失敗清單)。

    已套用過的改動會被偵測到並標記為 skipped。判斷方式是精確比對改動後
    的完整文字（不能用「第一個非空行」當特徵 —— 例如 play() 的新舊版本
    都以 `    play() {` 開頭，會把還沒套用的改動誤判成已套用）。
    """
    applied = []
    skipped = []
    failed = []

    for desc, candidates in changes:
        # 已經是改動後的樣子 → 略過
        if any(new in content for _, new in candidates):
            skipped.append(desc)
            continue

        done = False
        for old, new in candidates:
            if old in content:
                content = content.replace(old, new, 1)
                applied.append(desc)
                done = True
                break
        if not done:
            failed.append(desc)

    return content, applied, skipped, failed


def bump_manifest_version(manifest_path, suffix="-mic1"):
    """把 manifest.json 的 version 加上後綴。

    上游的 init_resource() 用整合的 version 當 Lovelace 資源的 cache-buster，
    所以版本一改，HA 重啟時就會自動改寫資源 URL → 瀏覽器自動拿到新檔，
    使用者不需要手動去儀表板改資源。
    """
    import json

    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    version = str(manifest.get("version", "v0"))
    if version.endswith(suffix):
        return version, False

    # 已存在 -micN 則遞增
    m = re.match(r"^(.*)-mic(\d+)$", version)
    if m:
        new_version = "%s-mic%d" % (m.group(1), int(m.group(2)) + 1)
    else:
        new_version = version + suffix

    manifest["version"] = new_version
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return new_version, True


def bump_card_import(content, new_ver):
    """把卡片 JS 的 video-rtc.js import 版本號換掉。

    這是必要的：瀏覽器會把 video-rtc.js 當成另一個資源快取，換了外層
    資源 URL 不會讓它重新抓。版本號必須跟著改。
    """
    import_ver = new_ver.lstrip("v")
    new_import = "import {VideoRTC} from './video-rtc.js?v=%s';" % import_ver
    pattern = re.compile(r"import \{VideoRTC\} from '\./video-rtc\.js\?v=[^']*';")
    if not pattern.search(content):
        return content, False
    return pattern.sub(new_import, content, count=1), True


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    integ = sys.argv[1]
    www = os.path.join(integ, "www")
    paths = {
        "video-rtc.js": os.path.join(www, "video-rtc.js"),
        "webrtc-camera.js": os.path.join(www, "webrtc-camera.js"),
        "__init__.py": os.path.join(integ, "__init__.py"),
        "manifest.json": os.path.join(integ, "manifest.json"),
    }

    for name, p in paths.items():
        if not os.path.exists(p):
            print("  [錯誤] 找不到 %s：%s" % (name, p))
            return 1

    originals = {}
    for name, p in paths.items():
        with open(p, encoding="utf-8") as f:
            originals[name] = f.read()

    results = {}
    all_failed = []

    # --- video-rtc.js ---
    content, a, s, f = apply_changes(
        originals["video-rtc.js"], VIDEO_RTC_CHANGES, paths["video-rtc.js"])
    results["video-rtc.js"] = (content, a, s, f)
    all_failed += [("video-rtc.js", x) for x in f]

    # --- webrtc-camera.js ---
    content, a, s, f = apply_changes(
        originals["webrtc-camera.js"], CARD_CHANGES, paths["webrtc-camera.js"])
    results["webrtc-camera.js"] = (content, a, s, f)
    all_failed += [("webrtc-camera.js", x) for x in f]

    # --- __init__.py ---
    init = originals["__init__.py"]
    if INIT_OLD in init:
        init = init.replace(INIT_OLD, INIT_NEW, 1)
        if INIT_WS_ANCHOR in init:
            init = init.replace(INIT_WS_ANCHOR, INIT_HELPERS, 1)
        results["__init__.py"] = (init, ["entity: 接受 go2rtc stream 名稱"], [], [])
    elif INIT_WS_ANCHOR in init and "_go2rtc_stream_src" in init:
        results["__init__.py"] = (init, [], ["entity: 接受 go2rtc stream 名稱"], [])
    else:
        results["__init__.py"] = (init, [], [], ["entity: 接受 go2rtc stream 名稱"])
        all_failed.append(("__init__.py", "entity: 接受 go2rtc stream 名稱"))

    if all_failed:
        print("  [錯誤] 以下改動無法套用（檔案未修改）：")
        for fname, desc in all_failed:
            print("     - %s: %s" % (fname, desc))
        return 1

    # --- manifest 版本（cache-buster）---
    new_ver, changed = bump_manifest_version(paths["manifest.json"])
    with open(paths["manifest.json"], encoding="utf-8") as f:
        manifest_content = f.read()

    # --- 卡片 import 版本 ---
    card_content = results["webrtc-camera.js"][0]
    card_content, imp_changed = bump_card_import(card_content, new_ver)
    results["webrtc-camera.js"] = (
        card_content, results["webrtc-camera.js"][1],
        results["webrtc-camera.js"][2], results["webrtc-camera.js"][3])

    # --- 寫入 ---
    for name in ("video-rtc.js", "webrtc-camera.js", "__init__.py"):
        content = results[name][0]
        if content == originals[name]:
            continue
        with open(paths[name], "w", encoding="utf-8") as f:
            f.write(content)
        print("  [修改] %s" % name)
        for desc in results[name][1]:
            print("         + %s" % desc)
        for desc in results[name][2]:
            print("         = %s（已存在，略過）" % desc)

    print("  [修改] manifest.json version → %s" % new_ver)
    print("         （上游 init_resource() 會用這個版本改寫 Lovelace 資源 URL）")
    print("  [修改] webrtc-camera.js 的 video-rtc.js import → v=%s" % new_ver.lstrip("v"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
