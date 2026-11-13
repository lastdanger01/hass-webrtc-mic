/**
 * Decisive backchannel test on the internal HA (192.168.31.20) — v2.
 *
 * v1 failed to connect because the test never added go2rtc's remote ICE
 * candidates. The real card does:
 *   pc.addIceCandidate({candidate: msg.value, sdpMid: '0'})
 * so this version does the same, and also buffers local candidates until
 * the signalling WebSocket is open.
 */
const { chromium } = require('/home/lastdanger/node_modules/playwright');
const http = require('http');

const TOKEN = require('fs').readFileSync('/tmp/wstest2/tok', 'utf8').trim();
const G2R = 'http://192.168.31.20:1984';
const HA = 'https://192-168-31-20.sslip.io:8123';
const STREAM = 'kitchen225';

function g2rStreams() {
  return new Promise((resolve, reject) => {
    http.get(G2R + '/api/streams', (res) => {
      let b = '';
      res.on('data', (c) => (b += c));
      res.on('end', () => { try { resolve(JSON.parse(b)); } catch (e) { reject(e); } });
    }).on('error', reject);
  });
}

function pickSenders(streams) {
  const out = {};
  const v = streams[STREAM] || {};
  for (const p of v.producers || []) {
    for (const s of p.senders || []) {
      const codec = (s.codec || {}).codec_name || '?';
      out[codec] = out[codec] || { bytes: 0, packets: 0 };
      out[codec].bytes += s.bytes || 0;
      out[codec].packets += s.packets || 0;
    }
  }
  return out;
}

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
  const page = await ctx.newPage();
  page.on('console', (m) => console.log('  [page]', m.text()));
  page.on('pageerror', (e) => console.log('  [pageerror]', e.message));

  await page.goto(HA + '/manifest.json', { waitUntil: 'domcontentloaded' });

  await page.evaluate(async ({ TOKEN, STREAM }) => {
    const R = { state: 'starting', conn: '', events: [], ice: 0, remoteIce: 0 };
    window.__R = R;

    const signed = await new Promise((resolve, reject) => {
      const w = new WebSocket('wss://192-168-31-20.sslip.io:8123/api/websocket');
      const to = setTimeout(() => reject(new Error('sign timeout')), 15000);
      w.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        if (m.type === 'auth_required') w.send(JSON.stringify({ type: 'auth', access_token: TOKEN }));
        else if (m.type === 'auth_ok') w.send(JSON.stringify({ id: 1, type: 'auth/sign_path', path: '/api/webrtc_mic/ws' }));
        else if (m.id === 1) { clearTimeout(to); w.close(); resolve(m.result.path); }
      };
    });

    const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    const track = mic.getAudioTracks()[0];
    const pc = new RTCPeerConnection({});
    window.__pc = pc;

    const tx = pc.addTransceiver(track, { direction: 'sendonly' });
    const caps = RTCRtpSender.getCapabilities('audio');
    const pcma = caps ? caps.codecs.filter((c) => /PCMA/i.test(c.mimeType)) : [];
    R.events.push('pcma:' + (pcma.length ? pcma.map((c) => c.mimeType).join(',') : 'NONE'));
    if (pcma.length) tx.setCodecPreferences(pcma);
    pc.addTransceiver('video', { direction: 'recvonly' });
    pc.addTransceiver('audio', { direction: 'recvonly' });

    let ws = null;
    const pending = [];
    const sendCand = (c) => {
      if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'webrtc/candidate', value: c }));
      else pending.push(c);
    };
    pc.onicecandidate = (e) => { if (e.candidate) { R.ice++; sendCand(e.candidate.candidate); } };
    pc.onconnectionstatechange = () => { R.conn = pc.connectionState; R.events.push('conn:' + pc.connectionState); };
    pc.oniceconnectionstatechange = () => { R.iceState = pc.iceConnectionState; };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);

    ws = new WebSocket('wss://192-168-31-20.sslip.io:8123' + signed + '&url=' + STREAM);
    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'webrtc/offer', value: offer.sdp }));
      pending.splice(0).forEach((c) => ws.send(JSON.stringify({ type: 'webrtc/candidate', value: c })));
      R.events.push('offer-sent localIce=' + R.ice);
    };
    ws.onmessage = async (ev) => {
      const m = JSON.parse(ev.data);
      if (m.type === 'webrtc/answer') {
        await pc.setRemoteDescription({ type: 'answer', sdp: m.value });
        R.state = 'answered';
      } else if (m.type === 'webrtc/candidate') {
        R.remoteIce++;
        try {
          await pc.addIceCandidate({ candidate: m.value, sdpMid: '0' });
        } catch (e) {
          R.events.push('addIce-fail:' + e.message);
          // retry with the mid the candidate actually carries
          try {
            const mid = /a=mid:(\d+)/.exec(m.value || '');
            await pc.addIceCandidate({ candidate: m.value, sdpMid: mid ? mid[1] : '0' });
            R.events.push('addIce-ok-retry');
          } catch (e2) { R.events.push('addIce-fail2:' + e2.message); }
        }
      } else if (m.type === 'error') {
        R.state = 'error:' + m.value;
      }
    };
    ws.onerror = () => { R.events.push('ws-error'); };
  }, { TOKEN, STREAM });

  await page.waitForTimeout(8000);
  console.log('state:', JSON.stringify(await page.evaluate(() => window.__R)));

  const before = pickSenders(await g2rStreams());
  console.log('go2rtc before:', JSON.stringify(before));

  for (let i = 0; i < 8; i++) {
    await page.waitForTimeout(5000);
    const now = pickSenders(await g2rStreams());
    const parts = Object.keys(now).map((k) => {
      const b = before[k] || { bytes: 0, packets: 0 };
      return `${k} ${b.bytes}->${now[k].bytes} (+${now[k].bytes - b.bytes}B +${now[k].packets - b.packets}p)`;
    });
    const st = await page.evaluate(() => ({ s: window.__R.state, c: window.__R.conn, i: window.__R.iceState }));
    console.log(`  t+${(i + 1) * 5}s ${JSON.stringify(st)} ${parts.join(' | ') || '(no senders)'}`);
  }

  console.log('\n--- browser getStats ---');
  const stats = await page.evaluate(async () => {
    const out = [];
    const s = await window.__pc.getStats();
    s.forEach((r) => {
      if (r.type === 'outbound-rtp') out.push(`OUT ${r.kind} bytesSent=${r.bytesSent} packetsSent=${r.packetsSent} codec=${(r.codec && r.codec.mimeType) || '?'}`);
      if (r.type === 'inbound-rtp') out.push(`IN  ${r.kind} bytesReceived=${r.bytesReceived}`);
      if (r.type === 'candidate-pair' && r.state === 'succeeded') out.push('candidate-pair succeeded');
    });
    return out;
  });
  stats.forEach((s) => console.log('  ' + s));

  console.log('\nevents:', JSON.stringify(await page.evaluate(() => window.__R.events)));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
