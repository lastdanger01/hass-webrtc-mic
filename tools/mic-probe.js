/**
 * mic-probe.js —— 麥克風對講「相機喇叭沒聲音」的瀏覽器端取樣。
 *
 * 用法：在 HA 頁面（已開啟有麥克風的卡片、且已點過麥克風按鈕）打開
 * DevTools → Console → 貼上整段 → Enter。
 *
 * 它會回答：音軌到底卡在哪一段。
 *   A. media 沒有 'microphone'        → 卡片沒把麥克風加進去
 *   B. micTrack 不存在                → getUserMedia 沒成功 / 沒重連
 *   C. 沒有 sendonly 的 audio transceiver → offer 沒送出音軌
 *   D. outbound-rtp audio bytesSent = 0 → 瀏覽器建立了但沒在送
 *   E. outbound-rtp audio bytesSent 在漲 → 瀏覽器沒問題，問題在 go2rtc/相機端
 */
(async () => {
  const log = console.log;
  const out = [];
  const say = (...a) => { const s = a.join(' '); out.push(s); log(s); };

  // ---- 1. 找出卡片元素（HA 把卡片放在 shadow DOM 裡，要遞迴找） ----
  function findEl(root, tag) {
    const hits = [];
    const walk = (node) => {
      if (!node) return;
      const els = node.querySelectorAll ? node.querySelectorAll('*') : [];
      for (const el of els) {
        const t = (el.tagName || '').toLowerCase();
        if (t === tag) hits.push(el);
        if (el.shadowRoot) walk(el.shadowRoot);
      }
    };
    walk(root);
    return hits;
  }

  const all = findEl(document, 'webrtc-mic-camera');
  say('== 找到 webrtc-mic-camera 元素:', all.length, '個');
  if (!all.length) {
    say('!! 找不到卡片元素 —— 確認這張卡片在畫面上、且元素名是 custom:webrtc-mic-camera');
    return;
  }
  const el = all[0];

  // ---- 2. 麥克風狀態 ----
  say('');
  say('== A. 麥克風狀態');
  say('   media      =', JSON.stringify(el.media));
  say('   config.media=', JSON.stringify(el.config && el.config.media));
  say('   micEnabled =', el.micEnabled);
  say('   micTrack   =', el.micTrack ? (el.micTrack.label || el.micTrack.kind) : null);
  say('   track.enabled =', el.micTrack ? el.micTrack.enabled : null);
  say('   micStream  =', el.micStream ? 'yes' : null);

  if (!String(el.media || '').includes('microphone')) {
    say('   >> A 問題：media 裡沒有 microphone，音軌不會被加進 offer');
  }
  if (!el.micTrack) {
    say('   >> B 問題：沒有 micTrack —— getUserMedia 沒成功，或點麥克風後沒重連');
  }

  // ---- 3. PeerConnection 狀態 ----
  const pc = el.pc;
  say('');
  say('== C. PeerConnection');
  if (!pc) {
    say('   pc = null  —— 卡片現在沒有 WebRTC 連線');
    say('   >> 這代表它跑在 MSE 模式（或連線已斷）。MSE 沒有 peer connection，音軌無處可送。');
    say('   >> 試試在卡片加 mode: webrtc');
    return;
  }
  say('   connectionState =', pc.connectionState);
  say('   signalingState  =', pc.signalingState);
  say('   iceConnectionState =', pc.iceConnectionState);

  const trs = pc.getTransceivers();
  say('   transceivers:');
  let hasSendonlyAudio = false;
  trs.forEach((t, i) => {
    const kind = (t.sender.track && t.sender.track.kind)
      || (t.receiver.track && t.receiver.track.kind) || '?';
    say(`     [${i}] kind=${kind} dir=${t.direction} currentDir=${t.currentDirection} mid=${t.mid}`);
    if (kind === 'audio' && (t.direction === 'sendonly' || t.currentDirection === 'sendonly')) {
      hasSendonlyAudio = true;
    }
  });
  if (!hasSendonlyAudio) {
    say('   >> C 問題：沒有 sendonly 的 audio transceiver —— offer 沒帶音軌出去');
  }

  // ---- 4. offer SDP 的 m= 行 ----
  say('');
  say('== D. local SDP m= 行（看有沒有音訊 m-line、用的什麼編碼）');
  const sdp = (pc.localDescription && pc.localDescription.sdp) || '';
  if (!sdp) {
    say('   (沒有 localDescription)');
  } else {
    sdp.split('\n').filter(l => l.startsWith('m=') || /PCMA|PCMU|opus/.test(l)).forEach(l => say('   ', l.trim()));
    if (/m=audio/.test(sdp)) {
      const mline = sdp.split('\n').find(l => l.startsWith('m=audio')) || '';
      say('   音訊 m-line:', mline.trim());
      say('   方向:', /a=sendonly/.test(sdp) ? 'sendonly 有出現' : '沒看到 a=sendonly');
    } else {
      say('   >> SDP 裡沒有 audio m-line —— 麥克風音軌完全沒進 offer');
    }
  }

  // ---- 5. getStats：出貨量 ----
  say('');
  say('== E. getStats（outbound = 瀏覽器送出去的）');
  let sentBytes = null, recvBytes = null;
  try {
    const stats = await pc.getStats();
    stats.forEach(r => {
      if (r.type === 'outbound-rtp') {
        say('   OUT', r.kind, 'bytesSent=' + r.bytesSent, 'packetsSent=' + r.packetsSent,
            'codec=' + ((r.codec && r.codec.mimeType) || r.codecName || '?'));
        if (r.kind === 'audio') sentBytes = r.bytesSent;
      }
      if (r.type === 'inbound-rtp') {
        say('   IN ', r.kind, 'bytesReceived=' + r.bytesReceived, 'packetsReceived=' + r.packetsReceived,
            'codec=' + ((r.codec && r.codec.mimeType) || r.codecName || '?'));
        if (r.kind === 'audio') recvBytes = r.bytesReceived;
      }
      if (r.type === 'candidate-pair' && r.state === 'succeeded') {
        say('   candidate-pair succeeded');
      }
    });
  } catch (e) {
    say('   getStats 失敗:', e && e.message);
  }

  say('');
  say('== 結論');
  if (sentBytes === null) {
    say('   >> D/E：沒有 outbound audio —— 瀏覽器根本沒在送音軌。');
    say('      回去看 A/B/C 三段的問題。');
  } else if (sentBytes === 0) {
    say('   >> E：有 outbound audio 但 bytesSent=0 —— 建立了傳輸器卻沒在送。');
    say('      通常是 track.enabled=false，或 track 已經 ended。');
  } else {
    say('   >> E：outbound audio bytesSent=' + sentBytes + ' —— 瀏覽器端沒問題！');
    say('      音軌有送出。問題在 go2rtc 端或相機端（跑 diag-backchannel.sh 看 sender bytes）。');
  }
  say('   （請把這段輸出整段貼回去）');
})();
