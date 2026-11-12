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

## Installation

### HACS (recommended)

1. Add this repository to HACS as a **custom repository** (category: *Integration*).
2. Install **WebRTC Camera with Microphone**.
3. Restart Home Assistant.
4. Go to *Settings → Devices & Services → Add Integration* and search for
   *WebRTC Camera with Microphone*.

### Manual

```bash
cd /config
mkdir -p custom_components/webrtc_mic
# copy the contents of custom_components/webrtc_mic/ from this repo
```

Restart Home Assistant, then add the integration from the UI.

## Lovelace card

The card registers itself as a `webrtc-mic-camera` element. Add it to a
dashboard like this:

```yaml
type: custom:webrtc-mic-camera
entity: camera.your_camera
media: video,audio,microphone
```

Without `microphone` in `media`, the microphone button will not do anything.

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
