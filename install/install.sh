#!/usr/bin/env bash
# webrtc-mic 麥克風對講 —— 一鍵安裝 / 更新
#
# 在 Home Assistant 主機上執行（HA OS 用 Terminal & SSH add-on，
# Supervised / Container 用 ssh 進去跑）：
#
#   curl -sSL https://raw.githubusercontent.com/lastdanger01/hass-webrtc-mic/v1.0.17/install/install.sh | sudo bash
#
# 這一支腳本一次做完所有事：
#
#   1. 找到 HA 的 custom_components 目錄
#   2. 安裝 / 更新獨立的 webrtc_mic 整合（自己的路線，不依賴上游）
#   3. 選配：同時把上游 webrtc 整合也補上麥克風（--with-upstream-patch）
#   4. 一次驗證
#   5. 一次重啟 Home Assistant
#
# 裝完之後卡片只要兩行，不用加 ui: true、不用加 media: microphone、
# 也不用自己去儀表板加 Lovelace 資源：
#
#   type: custom:webrtc-mic-camera
#   url: tapo44
#
# 環境變數：
#   TAG=v1.0.17          指定版本（不指定＝自動取最新 release）
#   WITH_UPSTREAM_PATCH=1 同時 patch 上游 webrtc 整合
#   RESTART_HA=0         跳過重啟 HA
set -u

REPO="lastdanger01/hass-webrtc-mic"
ZIP="webrtc_mic.zip"

# ---------------------------------------------------------------- 版本解析
# 不要寫死 tag：寫死正是 v1.0.10 那個 bug 的原因 —— 腳本去抓一個只存在於
# 更新版本裡的檔案，結果每次下載都 404。
TAG="${TAG:-}"

api_get() { curl -sSL "$1" 2>/dev/null; }

if [ -z "$TAG" ]; then
    TAG=$(api_get "https://api.github.com/repos/$REPO/releases/latest" \
          | sed -n 's/.*"tag_name": *"\\([^"]*\\)".*/\\1/p' | head -1)
    if [ -z "$TAG" ]; then
        TAG=$(api_get "https://api.github.com/repos/$REPO/tags" \
              | sed -n 's/.*"name": *"\\(v[0-9][^"]*\\)".*/\\1/p' | head -1)
    fi
fi
if [ -z "$TAG" ]; then
    echo "[錯誤] 無法確定要安裝的版本（拿不到 release 資訊）。" >&2
    echo "       請改用指定版本：" >&2
    echo "       TAG=v1.0.17 curl -sSL https://raw.githubusercontent.com/$REPO/v1.0.17/install/install.sh | sudo bash" >&2
    exit 1
fi

WITH_UPSTREAM_PATCH="${WITH_UPSTREAM_PATCH:-0}"
RESTART_HA="${RESTART_HA:-1}"

echo "=============================================="
echo " webrtc-mic 麥克風對講 - 安裝 / 更新"
echo "=============================================="
echo " 版本: $TAG"
[ "$WITH_UPSTREAM_PATCH" = "1" ] && echo " 同時 patch 上游 webrtc: 是"
echo

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# cache-buster：避免 CDN／proxy 拿到舊檔
CACHE_BUST="?v=$(date +%s)"

# ---------------------------------------------------------------- 工具
if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -q -O "$2" "$1"; }
else
    echo "[錯誤] 需要 curl 或 wget" >&2
    exit 1
fi

PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done

# ------------------------------------------------- 1. 找 custom_components
echo "[1] 找 custom_components 目錄..."
CC=""
for d in /config/custom_components \
         /homeassistant/custom_components \
         /workspace/custom_components \
         /usr/src/homeassistant/custom_components; do
    if [ -d "$d" ]; then CC="$d"; break; fi
done
if [ -z "$CC" ]; then
    CC=$(find / -path /proc -prune -o -type d -name custom_components -print 2>/dev/null | head -1)
fi
if [ -z "$CC" ]; then
    echo "    [錯誤] 找不到 custom_components 目錄" >&2
    echo "           請確認這是在 Home Assistant 主機上執行。" >&2
    exit 1
fi
echo "    $CC"
echo

# ------------------------------------------------- 2. 下載
echo "[2] 下載 $ZIP ..."
URL="https://github.com/$REPO/releases/download/$TAG/$ZIP"
if ! fetch "$URL" "$WORK/$ZIP" 2>/dev/null || [ ! -s "$WORK/$ZIP" ]; then
    echo "    [錯誤] 下載失敗：$URL" >&2
    exit 1
fi
echo "    OK ($(wc -c < "$WORK/$ZIP") bytes)"
echo

# ------------------------------------------------- 3. 檢查 zip 結構
echo "[3] 檢查壓縮檔..."
if ! command -v unzip >/dev/null 2>&1; then
    echo "    [錯誤] 需要 unzip" >&2
    exit 1
fi
if ! unzip -tq "$WORK/$ZIP" >/dev/null 2>&1; then
    echo "    [錯誤] 壓縮檔損壞" >&2
    exit 1
fi
if ! unzip -l "$WORK/$ZIP" | grep -q "custom_components/webrtc_mic/manifest.json"; then
    echo "    [錯誤] 壓縮檔結構不符（缺 custom_components/webrtc_mic/manifest.json）" >&2
    exit 1
fi
echo "    OK"
echo

# ------------------------------------------------- 4. 安裝 / 更新
echo "[4] 安裝 webrtc_mic..."
STAMP=$(date +%Y%m%d-%H%M%S)
if [ -d "$CC/webrtc_mic" ]; then
    cp -a "$CC/webrtc_mic" "$CC/webrtc_mic.bak-$STAMP"
    echo "    已備份 → webrtc_mic.bak-$STAMP"
    rm -rf "$CC/webrtc_mic"
fi
unzip -q "$WORK/$ZIP" -d "$(dirname "$CC")"
# HACS 不會產生 .gz，但萬一有先清掉：伺服器優先送 .gz 的話瀏覽器會拿到舊檔
find "$CC/webrtc_mic" -name '*.gz' -delete 2>/dev/null || true
echo "    OK → $CC/webrtc_mic"
echo

# ------------------------------------ 5. 選配：同時 patch 上游 webrtc
if [ "$WITH_UPSTREAM_PATCH" = "1" ]; then
    echo "[5] 同時 patch 上游 webrtc 整合..."
    UP=""
    for d in "$CC/webrtc"; do
        if [ -f "$d/www/webrtc-camera.js" ] && [ -f "$d/__init__.py" ]; then UP="$d"; break; fi
    done
    if [ -z "$UP" ]; then
        echo "    [跳過] 沒找到上游 webrtc 整合（$CC/webrtc）"
        echo "           只用 webrtc-mic-camera 的話，這是正常的。"
    else
        if [ -z "$PY" ]; then
            echo "    [跳過] 需要 python3 才能套用補丁"
        else
            if ! fetch "https://raw.githubusercontent.com/$REPO/$TAG/patch/transform_webrtc.py$CACHE_BUST" "$WORK/transform_webrtc.py" 2>/dev/null \
               || [ ! -s "$WORK/transform_webrtc.py" ]; then
                echo "    [跳過] 下載 transform_webrtc.py 失敗（上游保持原樣，不影響 webrtc-mic）"
            elif ! head -1 "$WORK/transform_webrtc.py" | grep -qE 'python|#!'; then
                # 下載到的不是 Python 腳本（例如被 proxy 換成 HTML 錯誤頁）
                echo "    [跳過] transform_webrtc.py 內容不對（不是 Python 腳本），上游保持原樣"
            else
                cp -p "$UP/www/video-rtc.js" "$UP/www/video-rtc.js.bak-$STAMP" 2>/dev/null || true
                cp -p "$UP/www/webrtc-camera.js" "$UP/www/webrtc-camera.js.bak-$STAMP" 2>/dev/null || true
                cp -p "$UP/__init__.py" "$UP/__init__.py.bak-$STAMP" 2>/dev/null || true
                cp -p "$UP/manifest.json" "$UP/manifest.json.bak-$STAMP" 2>/dev/null || true
                if "$PY" "$WORK/transform_webrtc.py" "$UP"; then
                    # 清掉過期的預壓縮 .gz —— 這是上游 patch 最容易失效的原因
                    if command -v gzip >/dev/null 2>&1; then
                        for gz in "$UP"/www/*.gz; do
                            [ -f "$gz" ] || continue
                            base="${gz%.gz}"
                            [ -f "$base" ] || continue
                            inner=$(gzip -cd "$gz" 2>/dev/null | sha256sum 2>/dev/null | cut -d' ' -f1)
                            cur=$(sha256sum "$base" 2>/dev/null | cut -d' ' -f1)
                            if [ -n "$inner" ] && [ -n "$cur" ] && [ "$inner" != "$cur" ]; then
                                rm -f "$gz" && echo "    已移除過期的 $(basename "$gz")"
                            fi
                        done
                    fi
                    echo "    OK → $UP"
                else
                    echo "    [警告] 補丁套用失敗，上游保持原樣（不影響 webrtc-mic）"
                fi
            fi
        fi
    fi
    echo
fi

# ------------------------------------------------- 6. 驗證
echo "[6] 驗證..."
FAIL=0
check() {
    if grep -q "$2" "$3" 2>/dev/null; then
        echo "    OK  $1"
    else
        echo "    [X]  $1"; FAIL=1
    fi
}

CARD="$CC/webrtc_mic/www/webrtc-mic-camera.js"
VRTC="$CC/webrtc_mic/www/video-rtc.js"

check "麥克風按鈕"              "mdi:microphone"          "$CARD"
check "控制列預設開啟 (ui)"     "ui: true,"               "$CARD"
check "PCMA 編碼偏好"          "PCMA"                    "$VRTC"
check "麥克風重連競態修復"      "this.ws === ws"          "$VRTC"
check "onopen null 防護"        "if (!this.ws) return;"   "$VRTC"
check "自動播放靜音還原"        "unmuteAutoplay"          "$VRTC"
check "config.media 同步"       "this.config.media = this.media" "$VRTC"
check "卡片元素名"              "webrtc-mic-camera"       "$CARD"

VER=$(grep -o '"version": *"[^"]*"' "$CC/webrtc_mic/manifest.json" 2>/dev/null | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
echo "    OK  整合版本 → $VER"
echo "        （HA 重啟時會用這個版本自動改寫 Lovelace 資源 URL）"

python3 -m py_compile "$CC/webrtc_mic/__init__.py" 2>/dev/null \
    && echo "    OK  Python 語法" \
    || echo "    [X]  Python 語法"

echo
if [ $FAIL -ne 0 ]; then
    echo "驗證沒通過，請把上面的訊息回報出來。"
    exit 1
fi

# ------------------------------------------------- 7. 重啟 HA
if [ "$RESTART_HA" = "1" ]; then
    echo "[7] 重啟 Home Assistant..."
    restarted=0
    if command -v docker >/dev/null 2>&1; then
        for c in homeassistant home-assistant hassio_supervisor; do
            if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
                echo "    docker restart $c"
                docker restart "$c" >/dev/null 2>&1 && restarted=1 && break
            fi
        done
    fi
    if [ $restarted -eq 0 ] && command -v systemctl >/dev/null 2>&1; then
        for u in homeassistant hassio home-assistant; do
            if systemctl list-unit-files 2>/dev/null | grep -q "^$u.service"; then
                echo "    systemctl restart $u"
                systemctl restart "$u" >/dev/null 2>&1 && restarted=1 && break
            fi
        done
    fi
    if [ $restarted -eq 0 ]; then
        echo "    [警告] 找不到 HA 的服務，請手動重啟。"
    else
        echo "    OK"
    fi
    echo
else
    echo "[7] 略過重啟 (RESTART_HA=0)"
    echo
fi

cat <<'TXT'
==============================================
 安裝完成
==============================================

 卡片設定現在只要這樣：

     type: custom:webrtc-mic-camera
     url: tapo44

 不需要 ui: true，不需要 media: microphone，
 也不需要去儀表板加 Lovelace 資源 ——
 整合自己會註冊，版本號就是 cache-buster。

 ⚠  重要：請用 HTTPS 入口開 HA

   ✅ https://<你的網域>:1234
   ❌ http://<你的網域>:8124

   純 HTTP 不是 secure context，瀏覽器的
   navigator.mediaDevices 會是 undefined，
   麥克風按鈕按下去完全沒有反應。

 瀏覽器記得 Ctrl+Shift+R 強制重新載入。
TXT
