# WebRTC Camera 麥克風對講補丁（Tapo PCMA backchannel）

把 AlexxIT/WebRTC Custom Integration 的瀏覽器端 JS 補上「麥克風 → 相機喇叭」對講能力。

## 這份補丁做了什麼

原始上游版本**沒有**麥克風對講功能，UI 連麥克風按鈕都沒有。補丁加了三件事：

### 1. `video-rtc.js` — 麥克風擷取 + PCMA 強制編碼

- 新增 `micStream` / `micTrack` / `micEnabled` 三個狀態欄位
- 新增 `setPCMACodecPreference(transceiver)`：從 `RTCRtpSender.getCapabilities('audio')` 篩出 PCMA 並 `setCodecPreferences()`
  - **這是最關鍵的一段**。瀏覽器預設挑 Opus，但 Tapo 相機 backchannel 只吃 PCMA/8000，不指定就完全沒聲音。
- 新增 `enableMicrophone()`：`getUserMedia({audio: true})` 拿音軌，並**重建 PeerConnection**（`ondisconnect()` → 100ms → `onconnect()`）
  - transceiver 必須在 `createOffer()` 之前就存在，無法中途加入，所以只能重建
- 新增 `disableMicrophone()`：只設 `track.enabled = false`，不斷線
- `createOffer()` 改為使用預先取得的 `this.micTrack`（原本是每次 createOffer 都重新 getUserMedia）
- `ondisconnect()` 清理時排除 `this.micTrack`，避免把麥克風軌一併 stop 掉（否則下次要重新授權）

### 2. `webrtc-camera.js` — 麥克風按鈕 UI

- 新增 `.microphone` / `.microphone.active` CSS（紅/綠）
- 新增 `<ha-icon class="microphone" icon="mdi:microphone-off">` 圖示
- 點擊處理：`mdi:microphone-off` → 開啟；`mdi:microphone` → 關閉
- 開啟時同步呼叫 `unmuteAutoplay()`（解決 autoplay 政策導致相機麥克風聽不到的問題）

### 3. `video-rtc.js` — autoplay 靜音修復（另一個獨立問題）

- 新增 `autoMuted` 旗標、`unmuteAutoplay()` 方法
- `play()` 只在 `NotAllowedError` 時才靜音（`AbortError` 不該靜音，會無故關掉相機收音）
- `webrtc-camera.js` 在卡片任何點擊與麥克風點擊時呼叫 `unmuteAutoplay()`

## 怎麼套用

### 前置條件

1. HA 已安裝 **AlexxIT WebRTC Custom Integration**（HACS）
2. 瀏覽器能連到 HA 的 **HTTPS 或 localhost**（麥克風權限要求安全來源）
3. go2rtc 正常運作，且相機 stream 用 **`tapo://`** 私有協定（ONVIF 的 AudioOutput 不能用對講）

### 步驟

```bash
# 1. 備份
cd /config/custom_components/webrtc/www
cp video-rtc.js video-rtc.js.bak
cp webrtc-camera.js webrtc-camera.js.bak

# 2. 套用補丁
patch -p0 < /path/to/video-rtc.js.patch
patch -p0 < /path/to/webrtc-camera.js.patch

# 3. 清掉資源快取並重載
#    HA 設定 → 系統 → Dashboard → 右上角「資源」→ 清除「WebRTC Camera」
#    或直接重啟 HA
```

### 驗證

打開 HA 儀表板，攝影機卡片左上角應該出現紅色麥克風圖示。點一下：

- 圖示變綠 = 麥克風已開啟
- 對著相機講話，另一台裝置（或相機端）應該聽得到

開啟瀏覽器 F12 → Console 確認沒有 `Failed to cast media` 之類的錯誤。

## 版本相容性

| 項目 | 值 |
|---|---|
補丁支援三個上游版本，各自有一份對應的 patch：

| 上游版本 | 補丁檔 | 實測 hunk |
|---|---|---|
| master | `video-rtc.js.patch` / `webrtc-camera.js.patch` | 4/4 + 4/4 |
| v3.6.1（tag） | `*.v361.patch` | 10/10 + 8/8 |
| v3.6.0（tag） | `*.v360.patch` | 12/12 + 8/8 |

`install.sh` 會依序嘗試並自動挑選套用成功的那一份；三個都不符就**完全不動檔案**。

> 2026-11-12 重新產生：上游 master 已把 `indexOf` 改成 `includes`，舊補丁套不上去（hunk 4/4 失敗）。之後又補上 v3.6.1 與 v3.6.0 兩個 release tag 的版本 —— **HACS addon 裝的是 release tag，不是 master**，只有 master 版補丁會失敗。

**如果對方的上游版本跟這裡不同**，`patch` 會失敗（hunk 不匹配）。那時要把 `video-rtc.js` / `webrtc-camera.js` 換成這個資料夾裡的 `.orig` 基準版本再套 —— 但那會把對方自己的修改洗掉，所以**先 diff 一下看看對方有沒有改過**。

## 先確認你用的是哪個整合

這個補丁是**改上游 `webrtc` 整合的兩個 JS**，所以：

- 卡片元素名是 **`custom:webrtc-camera`**（上游的）
- 如果你的卡片寫的是 `custom:webrtc-mic-camera`，那是另一個獨立整合
  （本 repo 的 fork），**這個補丁對它沒有作用**

先看你的卡片 YAML 開頭寫哪一個：

| 卡片 YAML | 你需要什麼 |
|---|---|
| `type: custom:webrtc-camera` | **這個補丁**（本頁說明） |
| `type: custom:webrtc-mic-camera` | fork 整合，本 repo 的 release（`webrtc_mic.zip`） |

> 兩者可以並存：元素名不同、路由不同（`/api/webrtc/ws` vs `/api/webrtc_mic/ws`）。

## 卡片參數：`url:` 才是對的做法

上游的 `webrtc` 整合對 `entity:` 與 `url:` 的處理**完全不同**：

```python
if entity_id := params.get("entity"):
    src = await async_get_stream_source(hass, entity_id)   # 必須是 camera entity
    assert src, f"Can't get URL for {entity_id}"            # ← 失敗就是這句
elif src := params.get("url"):
    query = {"src": src}                                    # ← 完全不驗證
```

- **`entity:`** 要求一個真正的 Home Assistant camera entity_id
  （例如 `camera.tapo_c200_mainstream`）。填 go2rtc 的 stream 名稱
  （例如 `tapo44`）會直接報 `Can't get URL for tapo44`。
- **`url:`** 不做任何驗證，直接把值當成 go2rtc 的 src 丟出去。
  **填 go2rtc stream 名稱（`tapo44`）或完整 RTSP 網址都可以。**

> 所以對講卡片請用 **`url:`**，不要用 `entity:`。

## 重要的架構說明

這個補丁只改**瀏覽器端**。以下三件事仍然必須在 HA 端自己設定，補丁不管：

1. **go2rtc stream 必須用 `tapo://`**
   ```yaml
   streams:
     tapo85:
       - tapo://admin:<sha256密碼大寫>@192.168.31.85
   ```
   密碼是相機**雲端帳密**的 SHA256 大寫，不是 RTSP 帳密。

2. **相機 media_player entity**（HA → 相機喇叭播歌，跟麥克風對講是不同機制）
   ```yaml
   # custom_components/webrtc/media_player.py
   # 透過 HA WebSocket API 或 config entry 建立
   ```

3. **HA 的 `internal_url` / `external_url` 必須是 https**
   HA 8123 是 HTTPS-only 的話，`internal_url` 寫 `http://` 會讓 `media_source://` 路徑整個爆掉。

## 已知限制

- **Tapo backchannel 是單通道** —— 真人麥克風、廣播器、播歌會互搶通道，同時用會互相干擾。
- **瀏覽器必須是 WebRTC 完整支援的** —— Chrome 63+ / Safari 11.1+。
- **HA 的 `lovelace_resources` 記錄的 URL 版本號**不會自動跟著 patch 更新；若改了檔案但沒反應，清一次資源快取。
