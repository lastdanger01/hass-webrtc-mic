#!/usr/bin/env bash
# 診斷麥克風對講「相機喇叭沒聲音」。
#
# 在 Home Assistant 主機上跑（HA OS 用 Terminal & SSH add-on）：
#   bash diag-backchannel.sh
#
# 它會回答一個問題：瀏覽器的音軌卡在哪一段？
#   A. go2rtc 的 stream 根本沒有 backchannel（相機不支援 / source 不是 tapo://）
#   B. 有 backchannel，但編碼對不上（不是 PCMA/8000）
#   C. 編碼對得上，但位元組不漲（瀏覽器沒送 / go2rtc 沒接）
set -u

echo "=============================================="
echo " 麥克風對講診斷 (backchannel)"
echo "=============================================="
echo

PY=""
for c in python3 python; do
  command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
done
if [ -z "$PY" ]; then
  echo "[錯誤] 需要 python3" >&2
  exit 1
fi

# ---------------------------------------------------------------- 找 go2rtc
echo "[1] 找 go2rtc ..."
GURL=""
for d in /config /homeassistant /workspace; do
  f="$d/.storage/core.config_entries"
  [ -f "$f" ] || continue
  found=$("$PY" - "$f" <<'PYEOF'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for e in data.get("data", {}).get("entries", []):
    if e.get("domain") in ("webrtc_mic", "webrtc", "go2rtc"):
        u = e.get("data", {}).get("url")
        if u and "11984" not in u:
            print(u)
            break
PYEOF
)
  [ -n "$found" ] && { GURL="$found"; echo "    config entry → $GURL"; break; }
done
[ -z "$GURL" ] && { GURL="http://localhost:1984/"; echo "    預設 → $GURL"; }
echo

# ---------------------------------------------------------------- stream 清單
echo "[2] go2rtc streams + 每個 producer 的 medias（找 sendonly = backchannel）..."
"$PY" - "$GURL" <<'PYEOF'
import json, sys, urllib.request
base = sys.argv[1].rstrip("/") + "/"
try:
    d = json.load(urllib.request.urlopen(base + "api/streams", timeout=5))
except Exception as e:
    print("    [錯誤] 無法讀取 streams:", e)
    sys.exit(0)

if not d:
    print("    [錯誤] go2rtc 沒有任何 stream")
    sys.exit(0)

for name, v in d.items():
    print(f"\n  ── stream: {name}")
    for p in (v.get("producers") or []):
        url = p.get("url", "")
        print(f"     producer  format={p.get('format_name', '?')}  url={url}")
        medias = p.get("medias") or []
        has_bc = False
        for m in medias:
            kind = m.get("kind", "?")
            direction = m.get("direction", "?")
            codecs = ",".join(
                f"{c.get('name','?')}/{c.get('clock_rate','?')}"
                for c in (m.get("codecs") or [])
            )
            mark = ""
            if kind == "audio" and direction == "sendonly":
                mark = "   <<< BACKCHANNEL (相機喇叭)"
                has_bc = True
            print(f"        media {kind:5} {direction:9} [{codecs}]{mark}")
        if not medias:
            print("        (producer 尚未連線，沒有 medias)")
        if not has_bc:
            print("        !! 這個 producer 沒有 sendonly audio")
            print("           → 相機喇叭不可能有聲音。檢查 source 是否為 tapo://")
            print("           → 若為 rtsp:// 或 ffmpeg://，該 source 不支援 backchannel。")
PYEOF
echo

# ---------------------------------------------------------------- 位元組取樣
echo "[3] 取樣 15 秒：backchannel 的 sender bytes 有沒有在漲？"
"$PY" - "$GURL" <<'PYEOF'
import json, sys, time, urllib.request
base = sys.argv[1].rstrip("/") + "/"

def snap():
    try:
        d = json.load(urllib.request.urlopen(base + "api/streams", timeout=4))
    except Exception:
        return {}
    out = {}
    for name, v in d.items():
        for p in (v.get("producers") or []):
            for s in (p.get("senders") or []):
                codec = (s.get("codec") or {}).get("codec_name", "")
                out[(name, codec, s.get("id", ""))] = (
                    s.get("bytes", 0), s.get("packets", 0)
                )
    return out

prev = snap()
if not prev:
    print("    (目前沒有任何 sender —— 瀏覽器還沒連上或沒開麥克風)")
else:
    for k, v in prev.items():
        print(f"    起始 {k[0]:12} codec={k[1]:8} bytes={v[0]} packets={v[1]}")

print("    取樣中（請在這 15 秒內對著麥克風說話）...")
for i in range(3):
    time.sleep(5)
    cur = snap()
    moved = False
    for k, v in cur.items():
        if k in prev:
            pb, pp = prev[k]
            if v[0] != pb:
                moved = True
                print(f"    ↑ {k[0]:12} codec={k[1]:8} bytes {pb} → {v[0]}  (+{v[0]-pb}, +{v[1]-pp} pkts)")
    if not moved:
        pass
    prev = cur

print()
print("    結論：")
print("      有 ↑ 累加 → 音軌確實送到相機（若喇叭仍沒聲音，是相機端問題）")
print("      完全沒動   → 音軌沒送出來（看瀏覽器端 offer 與 mode）")
PYEOF
echo

# ---------------------------------------------------------------- go2rtc.yaml
echo "[4] go2rtc.yaml 的 stream 設定（source 協定很重要）..."
for f in /config/go2rtc.yaml /homeassistant/go2rtc.yaml /workspace/go2rtc.yaml; do
  [ -f "$f" ] || continue
  echo "    $f"
  grep -nE "^\s*[a-zA-Z0-9_-]+:" "$f" | head -30
  echo "    ── tapo:// / rtsp:// / ffmpeg:// 出現情形 ──"
  grep -oE "(tapo|rtsp|ffmpeg|http|webrtc|kasa|vigi)://" "$f" | sort | uniq -c
  break
done
echo
echo "=============================================="
echo " 診斷結束。把上面全部輸出貼回來。"
echo "=============================================="
