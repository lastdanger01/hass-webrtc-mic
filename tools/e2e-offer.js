/**
 * End-to-end check of the webrtc_mic WebSocket proxy on the internal HA.
 *
 * It acts exactly like the card does:
 *   1. sign the path /api/webrtc_mic/ws (and /api/webrtc/ws for comparison)
 *   2. build a real offer: sendonly mic transceiver (PCMA preferred)
 *      + recvonly video + recvonly audio
 *   3. send it over the signed WebSocket
 *   4. print the offer's m= lines and go2rtc's answer m= lines
 *
 * The answer tells us whether go2rtc accepted the audio backchannel
 * (an m=audio sendonly line with PCMA/8000 in the answer).
 */
const { chromium } = require('/home/lastdanger/node_modules/playwright');

const HA = 'https://192-168-31-20.sslip.io:8123';
const TOKEN = require('fs').readFileSync('/tmp/wstest2/tok', 'utf8').trim();

const probe = async (page, wsPath, label) => {
  const out = await page.evaluate(async ({ wsPath, TOKEN, HA }) => {
    const log = [];
    const say = (s) => log.push(s);

    // --- sign the path over HA's own WebSocket API ---
    const signed = await new Promise((resolve, reject) => {
      const w = new WebSocket('wss://192-168-31-20.sslip.io:8123/api/websocket');
      const to = setTimeout(() => reject(new Error('sign timeout')), 15000);
      w.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        if (m.type === 'auth_required') {
          w.send(JSON.stringify({ type: 'auth', access_token: TOKEN }));
        } else if (m.type === 'auth_ok') {
          w.send(JSON.stringify({ id: 1, type: 'auth/sign_path', path: wsPath }));
        } else if (m.id === 1) {
          clearTimeout(to);
          w.close();
          resolve(m.result.path);
        }
      };
      w.onerror = () => reject(new Error('ws error'));
    });
    say('signed: ' + signed.slice(0, 60) + '...');

    // --- real microphone (fake device) ---
    const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    const track = mic.getAudioTracks()[0];
    say('mic track: ' + track.label + ' readyState=' + track.readyState);

    const pc = new RTCPeerConnection({});
    const tx = pc.addTransceiver(track, { direction: 'sendonly' });

    // same as the fork's setPCMACodecPreference()
    const caps = RTCRtpSender.getCapabilities('audio');
    const pcma = caps ? caps.codecs.filter((c) => /PCMA/i.test(c.mimeType)) : [];
    say('PCMA capabilities: ' + (pcma.length ? JSON.stringify(pcma.map((c) => c.mimeType)) : 'NONE'));
    if (pcma.length) tx.setCodecPreferences(pcma);

    pc.addTransceiver('video', { direction: 'recvonly' });
    pc.addTransceiver('audio', { direction: 'recvonly' });

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const sdp = offer.sdp;

    say('--- OFFER m= lines ---');
    sdp.split('\n').filter((l) => l.startsWith('m=')).forEach((l) => say('  ' + l.trim()));
    say('--- OFFER rtpmap ---');
    sdp.split('\n').filter((l) => /a=rtpmap/.test(l)).forEach((l) => say('  ' + l.trim()));

    // --- send the offer through the integration's proxy ---
    const answer = await new Promise((resolve, reject) => {
      const to = setTimeout(() => reject(new Error('no answer in 25s')), 25000);
      const w = new WebSocket('wss://192-168-31-20.sslip.io:8123' + signed + '&url=kitchen225');
      w.onopen = () => w.send(JSON.stringify({ type: 'webrtc/offer', value: sdp }));
      w.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        say('<- type=' + m.type);
        if (m.type === 'webrtc/answer') { clearTimeout(to); w.close(); resolve(m.value); }
        else if (m.type === 'error') { clearTimeout(to); w.close(); reject(new Error(m.value)); }
      };
      w.onerror = () => { clearTimeout(to); reject(new Error('ws error')); };
    });

    say('--- ANSWER m= lines ---');
    answer.split('\n').filter((l) => l.startsWith('m=')).forEach((l) => say('  ' + l.trim()));
    say('--- ANSWER rtpmap ---');
    answer.split('\n').filter((l) => /a=rtpmap/.test(l)).forEach((l) => say('  ' + l.trim()));
    const hasBack = /m=audio[^]*?a=sendonly/.test(answer) || /a=sendonly/.test(answer);
    say('ANSWER has sendonly (backchannel): ' + hasBack);

    pc.close();
    mic.getTracks().forEach((t) => t.stop());
    return log.join('\n');
  }, { wsPath, TOKEN, HA });

  console.log('\n============ ' + label + ' ============');
  console.log(out);
  console.log('');
};

(async () => {
  const browser = await chromium.launch({
    args: [
      '--ignore-certificate-errors',
      '--use-fake-device-for-media-stream',
      '--use-fake-ui-for-media-stream',
      '--autoplay-policy=no-user-gesture-required',
    ],
  });
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true });

  const TARGETS = process.env.ONLY === 'fork'
    ? [['/api/webrtc_mic/ws', 'FORK  /api/webrtc_mic/ws']]
    : [['/api/webrtc_mic/ws', 'FORK  /api/webrtc_mic/ws'],
       ['/api/webrtc/ws', 'UPSTR /api/webrtc/ws']];
  for (const [p, label] of TARGETS) {
    // fresh page per target: the HA login redirect destroys the execution
    // context of a page that is still navigating
    const page = await ctx.newPage();
    page.on('console', (m) => console.log('  [page]', m.text()));
    try {
      await page.goto(HA + '/', { waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(3000);
      await probe(page, p, label);
    } catch (e) {
      console.log('\n============ ' + label + ' ============');
      console.log('FAILED: ' + e.message);
    }
    await page.close();
  }

  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
