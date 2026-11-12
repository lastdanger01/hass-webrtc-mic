# WebRTC 麥克風對講補丁

## 一鍵安裝（全自動）

```bash
curl -sSL https://raw.githubusercontent.com/lastdanger01/hass-webrtc-mic/v1.0.13/patch/install.sh | sudo bash
```

安裝器會自動完成：

1. 找到上游 webrtc 整合目錄
2. 套用麥克風對講改動（**任何上游版本都適用**）
3. 移除過期的預壓縮 `.gz` 檔
4. 把 `manifest.json` 版本改成 `-micN`（HA 重啟時自動改寫 Lovelace 資源 URL）
5. 驗證十項功能
6. 重啟 Home Assistant

**不需要**手動改儀表板資源，**不需要**在卡片加 `ui: true` 或 `media: microphone`。

卡片只要這樣：

```yaml
type: custom:webrtc-camera
url: tapo44
```

`entity:` 也可以直接填 go2rtc 的 stream 名稱，整合會自己去 go2rtc 查。

## 為什麼用「文字轉換」而不是 diff patch

原本的做法是每個上游版本各出一份 diff patch（`video-rtc.js.patch` /
`.v361.patch` / `.v360.patch`）。結果是補丁跟上游檔案綁死：上游檔只要被
改過（哪怕只改一行），patch 就套不進去 —— 這是安裝失敗最常見的原因。

`transform_webrtc.py` 改用**內容錨點**（不靠行號），同一段程式碼在不同版本
有不同寫法時（實測有 `media.includes()` 與 `media.indexOf(...) >= 0` 兩種）
會列出多個候選錨點逐一嘗試。已對 upstream master / v3.6.1 / v3.6.0 三版
實測通過，13 項改動全部套用成功。

## 這個版本的四個根因

| 症狀 | 根因 | 修法 |
|---|---|---|
| 卡片顯示 `Custom element not found` | 整合沒有 `CONFIG_SCHEMA`，config flow 在 go2rtc 不可連線時不建立 config entry，沒有 entry 就沒有 `async_setup()` | 加 `CONFIG_SCHEMA`，可從 YAML 直接 setup |
| `Can't get URL for tapo44` | 上游 `entity:` 只認真正的 camera entity_id | 加 go2rtc stream 名稱 fallback |
| 麥克風按鈕不出現 | 上游 `renderCustomUI()` 第一行 `if (!this.config.ui) return;` | 預設開啟 `ui: true` |
| **patch 成功但瀏覽器沒麥克風** | 上游 www 帶著**預先壓縮的 `.gz` 兄弟檔**，Caddy 優先送 `.gz`，那份是舊版 | 移除過期的 `.gz` |
| 按了沒聲音 | `close` 事件非同步，`this.ws` 可能已換成新的 socket | 保留 socket 參照 + `if (!this.ws) return;` 防護 |

## 驗證方式

安裝器對磁碟檔 grep 十項特徵。但**磁碟正確不代表瀏覽器載得到** ——
一定要用帶 `Accept-Encoding: gzip` 的請求驗一次伺服器實際回傳的內容：

```bash
curl -sk -H 'Accept-Encoding: gzip' http://<ha>/webrtc/webrtc-camera.js -o /tmp/x
```

若回應有 `Content-Encoding: gzip`，把內容解壓縮後比對 sha256，
確認是不是跟磁碟上的檔一致。
