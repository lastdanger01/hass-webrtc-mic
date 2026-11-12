#!/usr/bin/env bash
# 一鍵安裝 WebRTC 麥克風對講（全自動）
#
# 用法（在 HA 主機上執行）：
#   curl -sSL https://raw.githubusercontent.com/lastdanger01/hass-webrtc-mic/v1.0.13/patch/install.sh | sudo bash
#
# 它會自動：
#   1. 找到上游 webrtc 整合目錄
#   2. 下載轉換器（不依賴版本綁定的 diff patch）
#   3. 套用麥克風對講改動（任何上游版本都適用）
#   4. 移除過期的預壓縮 .gz 檔（否則瀏覽器拿到舊版）
#   5. 把 manifest 版本改成 -micN（HA 重啟時會自動改寫 Lovelace 資源 URL）
#   6. 驗證結果
#   7. 重啟 Home Assistant
#
# 不需要手動改儀表板資源，也不需要在卡片加任何設定。
set -u

REPO="lastdanger01/hass-webrtc-mic"

# The release to install from. Do NOT hardcode this - a hardcoded tag was
# exactly the bug in v1.0.10: the script asked for a file that only exists
# in a newer release, and every download 404'd.
#   TAG=... curl ... | sudo bash    -> pin a specific release
#   (unset)                        -> use the latest release
TAG="${TAG:-}"

# Set to 0 to skip the Home Assistant restart at the end.
RESTART_HA="${RESTART_HA:-1}"

api_get() { curl -sSL "$1" 2>/dev/null; }

REQUIRED="transform_webrtc.py"

# --- 1. Which release? ----------------------------------------------------
if [ -z "$TAG" ]; then
    TAG=$(api_get "https://api.github.com/repos/$REPO/releases/latest" \
          | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)
    if [ -z "$TAG" ]; then
        TAG=$(api_get "https://api.github.com/repos/$REPO/tags" \
              | sed -n 's/.*"name": *"\(v[0-9][^"]*\)".*/\1/p' | head -1)
    fi
fi
if [ -z "$TAG" ]; then
    echo "[錯誤] 無法確定要安裝的版本（無法取得 release 資訊）。" >&2
    echo "       請改用指定版本：" >&2
    echo "       TAG=v1.0.13 curl -sSL https://raw.githubusercontent.com/$REPO/v1.0.13/patch/install.sh | sudo bash" >&2
    exit 1
fi

# --- 2. Resolve the tag to its commit SHA ----------------------------------
resolve_sha() {
    sha=""
    if command -v git >/dev/null 2>&1; then
        sha=$(git ls-remote "https://github.com/$REPO.git" "refs/tags/$TAG^{}" 2>/dev/null | cut -f1)
        [ -z "$sha" ] && sha=$(git ls-remote "https://github.com/$REPO.git" "refs/tags/$TAG" 2>/dev/null | cut -f1)
    fi
    if [ -z "$sha" ]; then
        sha=$(api_get "https://api.github.com/repos/$REPO/commits/$TAG" \
              | sed -n 's/.*"sha": *"\([0-9a-f]\{40\}\)".*/\1/p' | head -1)
    fi
    echo "$sha"
}

SHA=$(resolve_sha)
if [ -n "$SHA" ]; then
    BASE_URL="https://raw.githubusercontent.com/$REPO/$SHA/patch"
else
    echo "[警告] 無法解析 $TAG 的 commit SHA，改用 tag 路徑。" >&2
    BASE_URL="https://raw.githubusercontent.com/$REPO/$TAG/patch"
fi

CACHE_BUST="?v=$(date +%s)"

echo "=============================================="
echo " WebRTC 麥克風對講 - 全自動安裝"
echo "=============================================="
echo " 版本: $TAG"
echo

# --- 3. Find the integration directory -------------------------------------
find_integration() {
    local cands="
    /config/custom_components/webrtc
    /homeassistant/custom_components/webrtc
    /workspace/custom_components/webrtc
    "
    for d in $cands; do
        if [ -f "$d/www/webrtc-camera.js" ] && [ -f "$d/__init__.py" ]; then
            echo "$d"; return 0
        fi
    done
    # dynamic search
    local found
    found=$(find / -path /proc -prune -o -type f -name 'webrtc-camera.js' -print 2>/dev/null | head -1)
    if [ -n "$found" ]; then
        local dir
        dir=$(dirname "$(dirname "$found")")
        if [ -f "$dir/__init__.py" ]; then
            echo "$dir"; return 0
        fi
    fi
    return 1
}

INTEG="$(find_integration)" || {
    echo "[錯誤] 找不到上游 webrtc 整合"
    echo
    echo "  你還沒裝上游整合。請先裝："
    echo "    HACS -> 整合 -> WebRTC Custom Integration"
    echo "  (https://github.com/AlexxIT/hassio_webrtc)"
    echo
    echo "  裝好之後再跑一次這個指令。"
    exit 1
}

WWW="$INTEG/www"

echo "[1] 找到整合目錄:"
echo "    $INTEG"
echo

# --- 4. Download -----------------------------------------------------------
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "[2] 下載轉換器..."
if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -q -O "$2" "$1"; }
else
    echo "    [錯誤] 需要 curl 或 wget"
    exit 1
fi

MISSING=""
for f in $REQUIRED; do
    if ! fetch "$BASE_URL/$f$CACHE_BUST" "$WORK/$f" 2>/dev/null || [ ! -s "$WORK/$f" ]; then
        MISSING="$MISSING $f"
        rm -f "$WORK/$f"
    fi
done
if [ -n "$MISSING" ]; then
    echo "    [錯誤] 版本 $TAG 缺少下列檔案："
    for f in $MISSING; do echo "       - $f"; done
    echo
    echo "       檔案沒有被修改。"
    exit 1
fi
echo "    OK"
echo

# --- 5. Backup + apply -----------------------------------------------------
echo "[3] 備份原始檔..."
STAMP=$(date +%Y%m%d-%H%M%S)
for f in "$WWW/video-rtc.js" "$WWW/webrtc-camera.js" "$INTEG/__init__.py" "$INTEG/manifest.json"; do
    [ -f "$f" ] && cp -p "$f" "$f.bak-$STAMP"
done
echo "    OK (副檔名 .bak-$STAMP)"
echo

echo "[4] 套用麥克風對講改動..."
PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
    echo "    [錯誤] 需要 python3"
    exit 1
fi

if ! "$PY" "$WORK/transform_webrtc.py" "$INTEG"; then
    echo
    echo "    [錯誤] 套用失敗，檔案已還原。"
    for f in "$WWW/video-rtc.js" "$WWW/webrtc-camera.js" "$INTEG/__init__.py" "$INTEG/manifest.json"; do
        [ -f "$f.bak-$STAMP" ] && mv "$f.bak-$STAMP" "$f"
    done
    exit 1
fi
echo

# --- 6. Stale pre-compressed .gz -------------------------------------------
# The upstream integration can ship pre-compressed .gz siblings next to its
# JS. Caddy prefers the .gz whenever the browser sends Accept-Encoding: gzip,
# so patching the .js alone changes nothing the browser can see. Measured on
# the external HA: the server returned the 2024 file while the .js on disk was
# correct.
echo "[5] 處理預壓縮檔 (.gz)..."
STALE_GZ=""
for gz in "$WWW"/*.gz; do
    [ -f "$gz" ] || continue
    base="${gz%.gz}"
    [ -f "$base" ] || continue
    if command -v gzip >/dev/null 2>&1; then
        inner=$(gzip -cd "$gz" 2>/dev/null | sha256sum 2>/dev/null | cut -d' ' -f1)
        cur=$(sha256sum "$base" 2>/dev/null | cut -d' ' -f1)
        if [ -n "$inner" ] && [ -n "$cur" ] && [ "$inner" != "$cur" ]; then
            STALE_GZ="$STALE_GZ $(basename "$gz")"
        fi
    fi
done
if [ -n "$STALE_GZ" ]; then
    echo "    發現過期的預壓縮檔:$STALE_GZ"
    for g in $STALE_GZ; do
        rm -f "$WWW/$g" && echo "    已移除 $g"
    done
    echo "    （伺服器之後會直接提供新的 .js）"
else
    echo "    OK  沒有過期的預壓縮檔"
fi
echo

# --- 7. Verify -------------------------------------------------------------
echo "[6] 驗證..."
FAIL=0

check() {
    if grep -q "$2" "$3" 2>/dev/null; then
        echo "    OK  $1"
    else
        echo "    [X]  $1"; FAIL=1
    fi
}

check "麥克風按鈕 UI"              "mdi:microphone"       "$WWW/webrtc-camera.js"
check "控制列預設開啟 (ui: true)" "ui: true,"            "$WWW/webrtc-camera.js"
check "PCMA 編碼偏好"             "PCMA"                 "$WWW/video-rtc.js"
check "麥克風重連競態修復"         "this.ws === ws"       "$WWW/video-rtc.js"
check "onopen null 防護"           "if (!this.ws) return;" "$WWW/video-rtc.js"
check "自動播放靜音還原"           "unmuteAutoplay"       "$WWW/video-rtc.js"
check "卡片元素名 (webrtc-camera)" "webrtc-camera"        "$WWW/webrtc-camera.js"
check "WebSocket 路由"             "api/webrtc/ws"        "$WWW/webrtc-camera.js"
check "entity: 支援 stream 名稱"   "_go2rtc_stream_src"   "$INTEG/__init__.py"
check "manifest 版本含 mic"        '"version"'            "$INTEG/manifest.json"

if grep -q '"version": *"[^"]*-mic' "$INTEG/manifest.json" 2>/dev/null; then
    V=$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$INTEG/manifest.json")
    echo "    OK  整合版本 → $V"
    echo "        （HA 重啟時會用這個版本自動改寫 Lovelace 資源 URL）"
else
    echo "    [X]  manifest 版本沒有 mic 後綴"; FAIL=1
fi
echo

if [ $FAIL -ne 0 ]; then
    echo "驗證沒通過，請回報上面的訊息。"
    exit 1
fi

# --- 8. Restart Home Assistant --------------------------------------------
restart_ha() {
    # docker (the common case for HA OS / Supervised)
    if command -v docker >/dev/null 2>&1; then
        local c
        for c in homeassistant home-assistant hassio_supervisor; do
            if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
                echo "    docker restart $c"
                docker restart "$c" >/dev/null 2>&1 && return 0
            fi
        done
    fi
    # systemd
    if command -v systemctl >/dev/null 2>&1; then
        for u in homeassistant hassio home-assistant; do
            if systemctl list-unit-files 2>/dev/null | grep -q "^$u.service"; then
                echo "    systemctl restart $u"
                systemctl restart "$u" >/dev/null 2>&1 && return 0
            fi
        done
    fi
    return 1
}

if [ "$RESTART_HA" = "1" ]; then
    echo "[7] 重啟 Home Assistant..."
    if restart_ha; then
        echo "    OK"
    else
        echo "    [警告] 找不到 HA 的服務，請手動重啟。"
    fi
    echo
else
    echo "[7] 略過重啟 (RESTART_HA=0)"
    echo
fi

cat <<EOF
==============================================
 安裝完成
==============================================

 麥克風按鈕現在會出現在相機卡片的控制列。

 卡片設定只要這樣（不需要加 ui: 或 media: microphone）：

     type: custom:webrtc-camera
     url: tapo44

  entity: 也可以直接填 go2rtc 的 stream 名稱了，
  整合會自己去 go2rtc 查詢。

 瀏覽器記得 Ctrl+Shift+R 強制重新載入一次，
 因為舊的 JS 可能還在瀏覽器快取裡。

 備份檔案（需要還原時用）：
   $WWW/*.bak-$STAMP
   $INTEG/__init__.py.bak-$STAMP
EOF
