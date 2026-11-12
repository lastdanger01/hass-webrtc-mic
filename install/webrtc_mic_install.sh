#!/usr/bin/env bash
# 一鍵安裝 / 更新 webrtc_mic 獨立整合（不需要終端機以外的任何手動設定）
#
# 在 HA 主機上執行（HA OS 用 Terminal & SSH，Supervised/Container 用 ssh 進去）：
#   curl -sSL https://raw.githubusercontent.com/lastdanger01/hass-webrtc-mic/v1.0.14/install/webrtc_mic_install.sh | sudo bash
#
# 它會自動：
#   1. 找到 HA 的 custom_components 目錄
#   2. 下載 webrtc_mic.zip
#   3. 備份現有的 webrtc_mic
#   4. 覆蓋安裝
#   5. 重啟 Home Assistant
#
# 裝完之後：
#   - Lovelace 資源由整合自己自動註冊，不用手動改儀表板
#   - go2rtc 自動偵測
#   - 卡片只要 type + url 兩行，ui: true 已內建預設
set -u

REPO="lastdanger01/hass-webrtc-mic"
ZIP="webrtc_mic.zip"

# Which release to install. Do NOT hardcode a tag here: a hardcoded tag was
# exactly the bug in v1.0.10 of the patch installer - the script asked for a
# file that only exists in a newer release and every download 404'd.
#   TAG=v1.0.15 curl ... | sudo bash   -> pin a specific release
#   (unset)                            -> use the latest release
TAG="${TAG:-}"

api_get() { curl -sSL "$1" 2>/dev/null; }

if [ -z "$TAG" ]; then
    TAG=$(api_get "https://api.github.com/repos/$REPO/releases/latest" \
          | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)
    if [ -z "$TAG" ]; then
        TAG=$(api_get "https://api.github.com/repos/$REPO/tags" \
              | sed -n 's/.*"name": *"\(v[0-9][^"]*\)".*/\1/p' | head -1)
    fi
fi
if [ -z "$TAG" ]; then
    echo "[錯誤] 無法確定要安裝的版本。" >&2
    echo "       請改用指定版本：TAG=v1.0.15 curl ... | sudo bash" >&2
    exit 1
fi

URL="https://github.com/$REPO/releases/download/$TAG/$ZIP"

echo "=============================================="
echo " webrtc_mic 獨立整合 - 安裝 / 更新"
echo "=============================================="
echo " 版本: $TAG"
echo

# --- 1. 找 custom_components 目錄 ---
echo "[1] 找 custom_components 目錄..."
CC=""
for d in /config/custom_components /homeassistant/custom_components /workspace/custom_components /usr/src/homeassistant/custom_components; do
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

# --- 2. 下載 ---
echo "[2] 下載 $ZIP ..."
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$WORK/$ZIP" || { echo "    [錯誤] 下載失敗" >&2; exit 1; }
elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$WORK/$ZIP" "$URL" || { echo "    [錯誤] 下載失敗" >&2; exit 1; }
else
    echo "    [錯誤] 需要 curl 或 wget" >&2; exit 1
fi
echo "    OK ($(wc -c < "$WORK/$ZIP") bytes)"
echo

# --- 3. 檢查 zip 結構 ---
echo "[3] 檢查壓縮檔..."
if command -v unzip >/dev/null 2>&1; then
    if ! unzip -tq "$WORK/$ZIP" >/dev/null 2>&1; then
        echo "    [錯誤] 壓縮檔損壞" >&2; exit 1
    fi
    if ! unzip -l "$WORK/$ZIP" | grep -q "custom_components/webrtc_mic/manifest.json"; then
        echo "    [錯誤] 壓縮檔結構不符（缺 custom_components/webrtc_mic/manifest.json）" >&2
        exit 1
    fi
    echo "    OK"
else
    echo "    [錯誤] 需要 unzip" >&2; exit 1
fi
echo

# --- 4. 備份 + 安裝 ---
echo "[4] 安裝..."
STAMP=$(date +%Y%m%d-%H%M%S)
if [ -d "$CC/webrtc_mic" ]; then
    cp -a "$CC/webrtc_mic" "$CC/webrtc_mic.bak-$STAMP"
    echo "    已備份現有整合 → webrtc_mic.bak-$STAMP"
    rm -rf "$CC/webrtc_mic"
fi
unzip -q "$WORK/$ZIP" -d "$(dirname "$CC")"
echo "    OK → $CC/webrtc_mic"
echo

# --- 5. 清掉舊的快取 .gz（上游 webrtc 才有的坑，但保險起見）---
find "$CC/webrtc_mic" -name '*.gz' -delete 2>/dev/null || true

# --- 6. 驗證 ---
echo "[5] 驗證..."
VER=$(grep -o '"version": *"[^"]*"' "$CC/webrtc_mic/manifest.json" | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
UI=$(grep -c 'ui: true,' "$CC/webrtc_mic/www/webrtc-mic-camera.js" 2>/dev/null || echo 0)
MIC=$(grep -c 'mdi:microphone' "$CC/webrtc_mic/www/webrtc-mic-camera.js" 2>/dev/null || echo 0)
SYNC=$(grep -c 'this.config.media = this.media' "$CC/webrtc_mic/www/video-rtc.js" 2>/dev/null || echo 0)
echo "    整合版本: $VER"
echo "    ui 預設:  $UI"
echo "    麥克風按鈕: $MIC 處"
echo "    config.media 同步: $SYNC"
if [ "$UI" -lt 1 ] || [ "$MIC" -lt 1 ]; then
    echo "    [錯誤] 驗證未通過" >&2; exit 1
fi
echo "    OK"
echo

# --- 7. 重啟 ---
echo "[6] 重啟 Home Assistant..."
restarted=0
if command -v docker >/dev/null 2>&1; then
    for c in homeassistant home-assistant hassio_supervisor; do
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
            echo "    docker restart $c"; docker restart "$c" >/dev/null 2>&1 && restarted=1 && break
        fi
    done
fi
if [ $restarted -eq 0 ] && command -v systemctl >/dev/null 2>&1; then
    for u in homeassistant hassio home-assistant; do
        if systemctl list-unit-files 2>/dev/null | grep -q "^$u.service"; then
            echo "    systemctl restart $u"; systemctl restart "$u" >/dev/null 2>&1 && restarted=1 && break
        fi
    done
fi
if [ $restarted -eq 0 ]; then
    echo "    [警告] 找不到 HA 服務，請手動重啟"
else
    echo "    OK"
fi
echo

cat <<TXT
==============================================
 安裝完成
==============================================

 卡片設定現在只要這樣：

     type: custom:webrtc-mic-camera
     url: tapo44

 不需要 ui: true，不需要 media: microphone，
 也不需要去儀表板加 Lovelace 資源。

 ⚠  重要：請用 HTTPS 入口開 HA

   ✅ https://ndm9514770.duckdns.org:1234
   ❌ http://ndm9514770.duckdns.org:8124

   純 HTTP 不是 secure context，瀏覽器的
   navigator.mediaDevices 會是 undefined，
   麥克風按鈕按下去完全沒有反應。

   瀏覽器記得 Ctrl+Shift+R 強制重新載入。
TXT
