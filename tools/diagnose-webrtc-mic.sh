#!/usr/bin/env bash
# 診斷「Custom element not found: webrtc-mic-camera」
# 在 HA 主機上執行（HA Terminal 附加元件 / SSH / Proxmox console 皆可）
# 用法: bash diagnose-webrtc-mic.sh

DOMAIN="webrtc_mic"
CARD="/webrtc_mic/webrtc-mic-camera.js"
CFG="/config"
STORE="$CFG/.storage"

echo "=========================================="
echo " webrtc_mic 卡片診斷"
echo "=========================================="

echo
echo "[1] 整合檔案是否已安裝"
if [ -d "$CFG/custom_components/$DOMAIN" ]; then
  echo "  OK  $CFG/custom_components/$DOMAIN 存在"
  echo "      檔案：$(ls "$CFG/custom_components/$DOMAIN" 2>/dev/null | tr '\n' ' ')"
  if [ -f "$CFG/custom_components/$DOMAIN/www/webrtc-mic-camera.js" ]; then
    echo "      OK  www/webrtc-mic-camera.js 存在 ($(stat -c%s "$CFG/custom_components/$DOMAIN/www/webrtc-mic-camera.js") bytes)"
  else
    echo "      !!  www/webrtc-mic-camera.js 不存在 —— HACS 安裝不完整"
  fi
  if [ -f "$CFG/custom_components/$DOMAIN/manifest.json" ]; then
    echo "      manifest version: $(grep -o '"version"[^,]*' "$CFG/custom_components/$DOMAIN/manifest.json")"
  fi
else
  echo "  !!  $CFG/custom_components/$DOMAIN 不存在 —— HACS 根本沒安裝成功"
fi

echo
echo "[2] config entry 是否已建立（決定 async_setup 有沒有跑）"
if [ -f "$STORE/core.config_entries" ]; then
  if grep -q "\"$DOMAIN\"" "$STORE/core.config_entries"; then
    echo "  OK  有 $DOMAIN 的 config entry"
    grep -o "\"domain\": \"$DOMAIN\"[^}]*}" "$STORE/core.config_entries" | head -1
  else
    echo "  !!  沒有 $DOMAIN 的 config entry"
    echo "      → async_setup 不會執行 → 靜態路徑與 Lovelace 資源都不會註冊"
    echo "      → 這就是 'Custom element not found' 的主因"
    echo "      → 修法：設定 → 裝置與服務 → 新增整合 → 搜尋 'WebRTC Camera with Microphone'"
  fi
else
  echo "  ??  找不到 $STORE/core.config_entries"
fi

echo
echo "[3] Lovelace 資源是否已註冊"
if [ -f "$STORE/lovelace_resources" ]; then
  if grep -q "webrtc_mic/webrtc-mic-camera.js" "$STORE/lovelace_resources"; then
    echo "  OK  已註冊："
    grep -o '"url": "[^"]*webrtc_mic[^"]*"' "$STORE/lovelace_resources"
  else
    echo "  !!  沒有註冊 $CARD"
    echo "      → 資源沒註冊，瀏覽器不會載入卡片 JS"
  fi
else
  echo "  ??  找不到 $STORE/lovelace_resources"
fi

echo
echo "[4] 儀表板模式（YAML 模式不會自動註冊資源）"
if [ -f "$STORE/lovelace_dashboards" ]; then
  echo "  storage 模式的儀表板："
  python3 - "$STORE/lovelace_dashboards" <<'PY' 2>/dev/null || grep -o '"url_path"[^,]*' "$STORE/lovelace_dashboards"
import json,sys
d=json.load(open(sys.argv[1]))
for k,v in (d.get("data") or {}).items():
    print(f"      - {k}: url_path={v.get('url_path')} mode={v.get('mode','storage')} title={v.get('title')}")
PY
  echo "  → 若你用的是 YAML 模式（mode=yaml），必須手動在"
  echo "    設定 → 系統 → 儀表板 → 資源 新增："
  echo "      url: $CARD"
  echo "      type: module"
else
  echo "  ??  找不到 $STORE/lovelace_dashboards"
fi

echo
echo "[5] 總結"
if [ -d "$CFG/custom_components/$DOMAIN" ] && grep -q "\"$DOMAIN\"" "$STORE/core.config_entries" 2>/dev/null \
   && grep -q "webrtc_mic/webrtc-mic-camera.js" "$STORE/lovelace_resources" 2>/dev/null; then
  echo "  設定看起來齊全。若仍報錯，多半是瀏覽器快取："
  echo "    1. 確認網址 query 有變（?v=版本號），沒變就改 manifest.json 的 version 再重啟"
  echo "    2. 瀏覽器 Ctrl+Shift+R（模組資源有 31 天 Cache-Control，硬刷常常無效）"
  echo "    3. 確認 HA 網址是 https://（麥克風與模組載入都需要 secure context）"
else
  echo "  設定不齊全，請依上面標示的 [n] 項目處理後重啟 HA。"
fi
echo
