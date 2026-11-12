#!/usr/bin/env bash
# Diagnose a "Can't get URL for <stream>" error on the webrtc_mic card.
#
# Run this on the Home Assistant host:
#   bash diagnose-webrtc-mic.sh
#
# It checks, in order:
#   1. integration files present
#   2. config entry + the go2rtc URL it uses
#   3. which go2rtc server that is, and whether it answers
#   4. which streams that go2rtc knows about
#   5. whether your card's entity/url matches one of them
#   6. Lovelace resource registered
set -u

echo "=============================================="
echo " webrtc_mic diagnosis"
echo "=============================================="
echo

DOMAIN_DIR=""
for d in /config /homeassistant /workspace; do
  if [ -d "$d/custom_components/webrtc_mic" ]; then
    DOMAIN_DIR="$d/custom_components/webrtc_mic"
    break
  fi
done

if [ -z "$DOMAIN_DIR" ]; then
  echo "[1] integration files: NOT FOUND"
  echo "    looked in /config, /homeassistant, /workspace"
  echo "    -> install it via HACS first"
  exit 1
fi

echo "[1] integration files: OK"
echo "    $DOMAIN_DIR"
if [ -f "$DOMAIN_DIR/manifest.json" ]; then
  echo "    version: $(grep -o '"version"[^,]*' "$DOMAIN_DIR/manifest.json" | head -1 | cut -d'"' -f4)"
fi
echo

# --- find the go2rtc url the integration actually uses -------------------
GO_URL=""
if [ -f /config/.storage/core.config_entries ]; then
  GO_URL=$(python3 - <<'PY' 2>/dev/null
import json
d = json.load(open('/config/.storage/core.config_entries'))
for e in d['data']['entries']:
    if e['domain'] == 'webrtc_mic':
        url = (e.get('data') or {}).get('url')
        bundled = (e.get('data') or {}).get('bundled_go2rtc')
        if url:
            print(url)
        elif bundled:
            print('BUNDLED')
PY
)
fi

echo "[2] config entry:"
if [ -z "$GO_URL" ]; then
  echo "    no webrtc_mic config entry found"
  echo "    -> add it: Settings -> Devices & Services -> Add Integration"
  echo "       -> 'WebRTC Camera with Microphone'"
  echo
  GO_URL="http://localhost:1984/"
else
  echo "    go2rtc url: $GO_URL"
fi
echo

if [ "$GO_URL" = "BUNDLED" ]; then
  echo "[3] *** THE INTEGRATION IS RUNNING ITS OWN BUNDLED go2rtc ***"
  echo "    A bundled go2rtc has NO streams configured, so every camera"
  echo "    card fails with \"Can't get URL for ...\"."
  echo
  echo "    FIX: point the integration at your real go2rtc server."
  echo "    Add this to configuration.yaml:"
  echo "        webrtc_mic:"
  echo "          url: http://<your-go2rtc-host>:1984/"
  echo "    then restart Home Assistant."
  echo
  exit 2
fi

# --- probe the go2rtc server --------------------------------------------
BASE="${GO_URL%/}"
STREAMS_URL="$BASE/api/streams"

echo "[3] go2rtc reachability:"
if command -v curl >/dev/null 2>&1; then
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$BASE/" 2>/dev/null)
else
  CODE="no-curl"
fi
echo "    $BASE/ -> $CODE"
if [ "$CODE" = "000" ] || [ "$CODE" = "no-curl" ]; then
  echo "    -> NOT reachable from this host"
  echo "    -> check the URL, firewall and that go2rtc is running"
  echo
fi
echo

echo "[4] streams known by that go2rtc:"
if command -v curl >/dev/null 2>&1; then
  curl -s --max-time 5 "$STREAMS_URL" 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print('    (could not parse - the URL may be wrong or need auth)')
    sys.exit(0)
if not isinstance(d, dict) or not d:
    print('    *** EMPTY ***')
    print('    -> the integration is talking to a go2rtc with no streams.')
    print('    -> that go2rtc has no config; point it at your real one.')
else:
    for k in sorted(d):
        print('    -', k)
" 2>/dev/null
fi
echo

echo "[5] which entity/url does your card use?"
for f in /config/.storage/lovelace /config/lovelace.yaml /config/dashboards; do
  [ -e "$f" ] && echo "    (found $f - search it for 'webrtc-mic-camera')"
done
echo "    -> every value you put in 'entity:' or 'url:' must be one of the"
echo "       stream names listed in [4], or a camera entity_id."
echo "       a typo like 'cemara.tapo44' will fail with the same error."
echo

echo "[6] Lovelace resource:"
if [ -f /config/.storage/lovelace_resources ]; then
  python3 - <<'PY' 2>/dev/null
import json
d = json.load(open('/config/.storage/lovelace_resources'))
found = False
for i in d['data']['items']:
    u = i.get('url', '')
    if 'webrtc_mic' in u:
        print('    ', u)
        found = True
if not found:
    print('    *** webrtc_mic resource NOT registered ***')
    print('    -> the integration was not set up (see [2])')
PY
else
  echo "    (no lovelace_resources storage - UI mode dashboards)"
fi
echo

echo "=============================================="
echo " done"
echo "=============================================="
