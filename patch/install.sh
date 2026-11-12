#!/usr/bin/env bash
# 一鍵安裝 WebRTC 麥克風對講補丁（patch 上游 webrtc 整合的兩個 JS）
#
# 用法（在 HA 主機上執行）：
#   curl -sSL https://raw.githubusercontent.com/lastdanger01/hass-webrtc-mic/v1.0.9/patch/install.sh | sudo bash
#
# 它會自動：
#   1. 找到上游 webrtc 整合的 www 目錄
#   2. 下載兩個補丁
#   3. 套用（失敗會整組不動，並保留 .bak）
#   4. 驗證結果
#   5. 提示接下來要做什麼
set -u

REPO="lastdanger01/hass-webrtc-mic"
TAG="v1.0.9"

# Resolve the tag to its commit SHA at run time. raw.githubusercontent.com
# caches by URL and a force-pushed tag can keep serving the previous file
# for a while, so we download from the immutable SHA instead. Resolving it
# here (instead of hardcoding it) keeps this script correct even when the
# tag is later re-pointed at a new commit.
resolve_sha() {
    sha=""
    if command -v git >/dev/null 2>&1; then
        sha=$(git ls-remote "https://github.com/$REPO.git" "refs/tags/$TAG^{}" 2>/dev/null | cut -f1)
        [ -z "$sha" ] && sha=$(git ls-remote "https://github.com/$REPO.git" "refs/tags/$TAG" 2>/dev/null | cut -f1)
    fi
    if [ -z "$sha" ] && command -v curl >/dev/null 2>&1; then
        sha=$(curl -sSL "https://api.github.com/repos/$REPO/commits/$TAG" 2>/dev/null \
              | sed -n 's/.*"sha": *\"\([0-9a-f]\{40\}\)".*/\1/p' | head -1)
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

# extra cache buster
CACHE_BUST="?v=$(date +%s)"

echo "=============================================="
echo " WebRTC 麥克風對講補丁 - 安裝"
echo "=============================================="
echo

# --- 1. 找到 www 目錄 ---------------------------------------------------
find_www() {
  local cands="
    /config/custom_components/webrtc/www
    /homeassistant/custom_components/webrtc/www
    /workspace/custom_components/webrtc/www
  "
  for d in $cands; do
    if [ -f "$d/video-rtc.js" ] && [ -f "$d/webrtc-camera.js" ]; then
      echo "$d"; return 0
    fi
  done
  # 容器內（HA 官方 docker）的路徑
  if [ -f /homeassistant/custom_components/webrtc/www/video-rtc.js ]; then
    echo "/homeassistant/custom_components/webrtc/www"; return 0
  fi
  # 動態搜尋
  local found
  found=$(find / -path /proc -prune -o -type f -name 'webrtc-camera.js' -print 2>/dev/null | head -1)
  if [ -n "$found" ]; then
    echo "$(dirname "$found")"; return 0
  fi
  return 1
}

WWW="$(find_www)" || {
  echo "[錯誤] 找不到上游 webrtc 整合的 www 目錄"
  echo
  echo "  你還沒裝上游整合。請先裝："
  echo "    HACS -> 整合 -> WebRTC Custom Integration"
  echo "  (https://github.com/AlexxIT/hassio_webrtc)"
  echo
  echo "  裝好之後再跑一次這個指令。"
  exit 1
}

echo "[1] 找到整合目錄:"
echo "    $WWW"
echo

# --- 2. 下載補丁 --------------------------------------------------------
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "[2] 下載補丁..."
if command -v curl >/dev/null 2>&1; then
  DL="curl -sSL"
elif command -v wget >/dev/null 2>&1; then
  DL="wget -q -O"
else
  echo "    [錯誤] 需要 curl 或 wget"
  exit 1
fi

if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -q -O "$2" "$1"; }
else
  echo "    [錯誤] 需要 curl 或 wget"
  exit 1
fi

for f in video-rtc.js.patch video-rtc.js.v361.patch video-rtc.js.v360.patch \
         webrtc-camera.js.patch webrtc-camera.js.v361.patch webrtc-camera.js.v360.patch \
         video-rtc.js.racefix.patch \
         apply_patch.py; do
  if ! fetch "$BASE_URL/$f$CACHE_BUST" "$WORK/$f"; then
    echo "    [錯誤] 下載 $f 失敗"
    exit 1
  fi
  if [ ! -s "$WORK/$f" ]; then
    echo "    [錯誤] $f 是空的（下載失敗）"
    exit 1
  fi
done
echo "    OK"
echo

# --- 3. 檢查是否已經套過 -------------------------------------------------
if grep -q "mdi:microphone" "$WWW/webrtc-camera.js" 2>/dev/null; then
  echo "[3] 偵測到麥克風按鈕已經存在。"
  if grep -q "this.ws === ws" "$WWW/video-rtc.js" 2>/dev/null; then
    echo "    競態修復也在，略過。"
    echo
  else
    echo "    但缺少 v1.0.9 的競態修復，補上（麥克風按了沒聲音就是這個原因）..."
    cd "$WWW" || exit 1
    if command -v python3 >/dev/null 2>&1; then PY=python3
    elif command -v python >/dev/null 2>&1; then PY=python
    else echo "    [錯誤] 需要 python3"; exit 1; fi
    OUT=$($PY "$WORK/apply_patch.py" "$WORK/video-rtc.js.racefix.patch" video-rtc.js 2>&1)
    RC=$?
    echo "$OUT" | sed 's/^/      /'
    if [ $RC -eq 0 ]; then
      echo "    OK  競態修復已補上"
    else
      echo "    [錯誤] 競態修復套用失敗，檔案未被修改。"
      exit 1
    fi
    echo
  fi
else
  echo "[3] 套用補丁..."
  cd "$WWW" || exit 1
  if command -v python3 >/dev/null 2>&1; then
    PY=python3
  elif command -v python >/dev/null 2>&1; then
    PY=python
  else
    echo "    [錯誤] 需要 python3"
    exit 1
  fi

  # 上游有三個常見版本（master、v3.6.1 release、v3.6.0 release），各有一份補丁。
  # 逐一嘗試，全部 hunk 成功才算數；任一組失敗都不動檔案。
  APPLIED=0
  for variant in "" ".v361" ".v360"; do
    echo "    嘗試補丁版本: ${variant:-master}"
    OUT=$($PY "$WORK/apply_patch.py" \
          "$WORK/video-rtc.js${variant}.patch" video-rtc.js \
          "$WORK/webrtc-camera.js${variant}.patch" webrtc-camera.js 2>&1)
    RC=$?
    echo "$OUT" | sed 's/^/      /'
    if [ $RC -eq 0 ]; then
      APPLIED=1
      break
    fi
    echo "      （不匹配，換下一個版本）"
  done

  if [ $APPLIED -eq 0 ]; then
    echo
    echo "    [錯誤] 補丁套用失敗，檔案完全沒有被修改。"
    echo "    可能是上游版本不同。請把上面的錯誤訊息回報給提供補丁的人。"
    exit 1
  fi
  echo
fi

# --- 4. 驗證 ------------------------------------------------------------
echo "[4] 驗證..."
FAIL=0

if grep -q "mdi:microphone" "$WWW/webrtc-camera.js" 2>/dev/null; then
  echo "    OK  麥克風按鈕 UI"
else
  echo "    [X]  麥克風按鈕 UI 沒找到"; FAIL=1
fi

if grep -q "PCMA" "$WWW/video-rtc.js" 2>/dev/null; then
  echo "    OK  PCMA 編碼偏好"
else
  echo "    [X]  PCMA 編碼偏好 沒找到"; FAIL=1
fi

# v1.0.9: 麥克風重連競態的修復
if grep -q "this.ws === ws" "$WWW/video-rtc.js" 2>/dev/null; then
  echo "    OK  麥克風重連競態修復"
else
  echo "    [X]  麥克風重連競態修復 沒找到（會造成麥克風無聲）"; FAIL=1
fi

if grep -q "if (!this.ws) return;" "$WWW/video-rtc.js" 2>/dev/null; then
  echo "    OK  onopen null 防護"
else
  echo "    [X]  onopen null 防護 沒找到"; FAIL=1
fi

if grep -q "webrtc-camera" "$WWW/webrtc-camera.js" 2>/dev/null; then
  echo "    OK  卡片元素名 (webrtc-camera)"
else
  echo "    [X]  卡片元素名 異常"; FAIL=1
fi

if grep -q "api/webrtc/ws" "$WWW/webrtc-camera.js" 2>/dev/null; then
  echo "    OK  WebSocket 路由 (/api/webrtc/ws)"
else
  echo "    [X]  WebSocket 路由 異常"; FAIL=1
fi
echo

if [ $FAIL -ne 0 ]; then
  echo "驗證沒通過，請回報上面的訊息。"
  exit 1
fi

echo "=============================================="
echo " 安裝完成"
echo "=============================================="
echo
echo " 接下來："
echo
echo " 1. 重啟 Home Assistant"
echo
echo " 2. HA -> 設定 -> 系統 -> 儀表板 -> 右上角「資源」"
echo "    刪掉舊的 WebRTC Camera 那筆（如果有的話）"
echo
echo " 3. 瀏覽器重新載入儀表板"
echo
echo " 4. 卡片設定用 url:（不是 entity:）："
echo
echo "    type: custom:webrtc-camera"
echo "    url: tapo44"
echo "    media: video,audio,microphone"
echo "    ui: true"
echo
echo "    media 一定要包含 microphone，少了這三個字麥克風不會啟用。"
echo "    ui 一定要開，麥克風按鈕在 ui 控制列裡。"
echo
echo "    為什麼不能用 entity:？"
echo "      上游的 entity: 只接受真正的 camera entity_id"
echo "      （像 camera.tapo_c200_mainstream），填 go2rtc 的 stream 名稱"
echo "      會直接報 \"Can't get URL for tapo44\"。"
echo "      url: 不做驗證，直接把名稱當成 go2rtc 的 src 丟出去。"
echo
