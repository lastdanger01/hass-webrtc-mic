# WebRTC Camera with Microphone

A fork of [AlexxIT/WebRTC](https://github.com/AlexxIT/WebRTC) that adds a working
**microphone button** to the camera card, plus the fixes needed to make
two-way audio actually work on Tapo and similar cameras.

> This is an **independent integration** (`webrtc_mic`). It does not replace or
> conflict with the upstream `webrtc` integration - different domain,
> different HTTP routes, different WebSocket path. You can run both.

## What it adds over upstream

Upstream supports two-way audio at the protocol level since v3.4.0
(`media: video,audio,microphone`), but the card has no microphone button and
the implementation has gaps. This fork fixes:

| Change | Why it matters |
|---|---|
| Microphone button in the card UI (`mdi:microphone-off` / `mdi:microphone`) | Upstream has no way to turn the mic on or off from the card |
| `setPCMACodecPreference()` on the sendonly transceiver | Browsers pick Opus by default; Tapo backchannel only accepts **PCMA**. Without this the push fails silently |
| Microphone track survives reconnects | Upstream `ondisconnect()` calls `sender.track.stop()` on every sender, which kills the mic track, so the mic dies on each reconnect |
| `unmuteAutoplay()` restores sound after an autoplay block | Upstream mutes the video permanently when autoplay with sound is blocked, with no recovery path |
| Mute only on a real `NotAllowedError` | Upstream mutes on any `play()` failure; an `AbortError` (a normal interrupt) silently kills camera audio |
| `microphone: false` config option | Lets you hide the button entirely |
| Media player with progress bar, volume, seek, pause/resume | Upstream ships a minimal media player |

## Two ways to install

| | Simpler | Full fork |
|---|---|---|
| What changes | **2 JS files** (patched in place) | 7 files, new integration domain |
| Card element | `custom:webrtc-camera` (upstream) | `custom:webrtc-mic-camera` |
| Side by side with upstream | not possible (same files) | yes |
| Files | `patch/` in this repo | HACS install of `webrtc_mic` |

> **Check which one your card uses first.** The patch modifies the *upstream*
> `webrtc` integration, so it only affects cards written as
> `type: custom:webrtc-camera`. A card written as
> `type: custom:webrtc-mic-camera` uses the fork and is unaffected by it.

**Most people want the simple one.** It patches the two upstream JS files
in place - that is all the microphone feature needs. See `patch/README.md`.

> The only reason this repo is a separate integration is to run **alongside**
> the upstream `webrtc` integration without conflicting domains and routes.
> If you do not need both, use the patch.

### Simple path (recommended)

1. Install the upstream [WebRTC Custom Integration](https://github.com/AlexxIT/hassio_webrtc)
   (HACS -> WebRTC Custom Integration).
2. On the HA host, run **one command**:

   ```bash
   curl -sSL https://raw.githubusercontent.com/lastdanger01/hass-webrtc-mic/v1.0.7/patch/install.sh | sudo bash
   ```

   It finds the integration directory, downloads the patches, applies them,
   verifies the result and prints the next steps. If the patch does not
   match your upstream version, it changes **nothing**.

3. Restart HA, then delete the *WebRTC Camera* Lovelace resource and
   reload the browser (the card is cached hard).

Prefer to do it by hand? Download `patch/webrtc-mic-patch.zip` from the
releases - it contains `apply_patch.py`, the patches for both common
upstream versions (v3.6.1 and master) and the full instructions.

Your card uses the **upstream** element name:

```yaml
type: custom:webrtc-camera
url: tapo44
media: video,audio,microphone
ui: true
```

> Use `url:`, not `entity:`. The upstream integration only accepts a real
> camera entity_id in `entity:`; `url:` takes a go2rtc stream name directly.

## Installation (full fork)

### HACS (recommended)

1. Add this repository to HACS as a **custom repository** (category: *Integration*).
2. Install **WebRTC Camera with Microphone**.
3. Restart Home Assistant.
4. Go to *Settings → Devices & Services → Add Integration* and search for
   *WebRTC Camera with Microphone*.

That is all - **no URL to type**. The integration auto-detects your go2rtc
server by probing the well-known locations (the URL configured in the
official *go2rtc* integration, the HA-managed go2rtc port, `localhost`,
the docker host gateway, the Frigate add-on hostnames and the LAN address
of your HA host) and uses the first one that answers.

If auto-detection cannot find your go2rtc (for example it runs on a
different machine than Home Assistant), set it explicitly:

```yaml
webrtc_mic:
  url: http://<go2rtc-host>:1984/
```

The integration will **not** silently start its own bundled go2rtc - a
bundled instance has no streams configured, so every camera card would
fail with *"Can't get URL for ..."*. If you really want it, tick
*Use a bundled go2rtc* when adding the integration.

### YAML (optional, only if auto-detection picks the wrong server)

```yaml
webrtc_mic:
  url: http://192.168.1.10:1984/
```

Restart Home Assistant. Leave this out in the normal case - it is not
required.

### If go2rtc is not reachable at all

The integration still sets up and serves the card, so you get a working
dashboard instead of a *Custom element not found* card. The stream itself
starts working as soon as go2rtc becomes reachable; add the integration
from the UI afterwards to point it at a specific server.

### Manual

```bash
cd /config
mkdir -p custom_components/webrtc_mic
# copy the contents of custom_components/webrtc_mic/ from this repo
```

Restart Home Assistant, then add the integration from the UI.

## Troubleshooting: "Custom element not found: webrtc-mic-camera"

This means the browser could not load the card JavaScript. Check in order:

1. **Is the integration set up?** The card JS is served from `async_setup`,
   which only runs once the integration is set up (via YAML or a config
   entry). Check *Settings → Devices & Services* for *WebRTC Camera with
   Microphone*. Adding the integration from the UI is enough - it no longer
   needs a working go2rtc URL.
2. **Is the Lovelace resource registered?** In *Settings → System → Dashboards
   → Resources* there must be an entry for `/webrtc_mic/webrtc-mic-camera.js`.
   The integration adds it automatically in storage mode. If you manage
   dashboards in **YAML mode**, add it by hand:
   ```yaml
   url: /webrtc_mic/webrtc-mic-camera.js
   type: module
   ```
3. **Browser cache.** Module resources are served with a long
   `Cache-Control`, so `Ctrl+Shift+R` often does nothing. Change the query
   string of the resource URL (`?v=...`) to force a refetch, or bump the
   `version` in `manifest.json` and restart Home Assistant.
4. **HTTPS.** The microphone only works in a secure context. Use
   `https://<your-ha-host>`.

A diagnostic script is included at `tools/diagnose-webrtc-mic.sh` - run it on
the Home Assistant host to check all of the above at once.

## Lovelace card

The card registers itself as a `webrtc-mic-camera` element. Add it to a
dashboard like this:

```yaml
type: custom:webrtc-mic-camera
entity: camera.your_camera
media: video,audio,microphone
```

Without `microphone` in `media`, the microphone button will not do anything.

### `entity` vs `url`

`entity` accepts a Home Assistant camera entity_id:

```yaml
type: custom:webrtc-mic-camera
entity: camera.your_camera
```

It also accepts a **go2rtc stream name** - the integration looks the name
up in the go2rtc stream list, so this works too:

```yaml
type: custom:webrtc-mic-camera
entity: tapo44
```

If the value is neither, use `url` with the stream name or a full RTSP URL:

```yaml
type: custom:webrtc-mic-camera
url: tapo44
```

To hide the button:

```yaml
type: custom:webrtc-mic-camera
entity: camera.your_camera
microphone: false
```

The integration registers the Lovelace resource
`/webrtc_mic/webrtc-mic-camera.js` automatically, so you do not need to add it
by hand.

## Browser requirements

- The browser only grants microphone access on **HTTPS** or **localhost**.
- Microphone permissions must be granted for the origin you are using.

## Media player (optional)

The fork ships an extended `media_player` platform that exposes a progress
bar, volume, seek and pause/resume over the go2rtc backchannel.

```yaml
media_player:
  - platform: webrtc_mic
    name: Camera Speaker
    stream: camera.your_camera
    audio: camera.your_camera/mic
    # Optional: a plain HTTP server that serves the audio to go2rtc.
    # It must not use a client-bound auth signature, or go2rtc gets a 401.
    media_server: "http://192.168.1.10:8124"
    # Optional: dashboard song title -> file name in the media library
    music_library:
      "Song Title": "song-title.mp3"
```

## Known limitations

- Only cameras exposed by go2rtc with a working backchannel are supported.
- Only the WebRTC mode supports the microphone.
- The browser blocks microphone access over plain HTTP on a non-localhost
  origin.

## Credits

Based on [AlexxIT/WebRTC](https://github.com/AlexxIT/WebRTC) by AlexxIT
(MIT licensed). This fork is not affiliated with or endorsed by the upstream
author.
