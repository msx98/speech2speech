// ═══════════════════════════════════════════════════════════════════
// TAB SWITCHING
// ═══════════════════════════════════════════════════════════════════
const tabs   = document.querySelectorAll('.tab');
const panels = document.querySelectorAll('.panel');
tabs.forEach(tab => {
  tab.addEventListener('click', () => {
    const id = tab.dataset.panel;
    tabs.forEach(t => t.classList.remove('active'));
    panels.forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('panel-' + id).classList.add('active');
    if (id === 'config' && !clipsLoaded) loadClips();
  });
});

// ═══════════════════════════════════════════════════════════════════
// CHAT
// ═══════════════════════════════════════════════════════════════════
const $chat   = document.getElementById('chat');
const $mic    = document.getElementById('mic-btn');
const $txtin  = document.getElementById('txt-in');
const $send   = document.getElementById('send-btn');
const $wsstat = document.getElementById('ws-status');
const $offln  = document.getElementById('offline');
const $wvwrap = document.getElementById('wv-wrap');
const $wv     = document.getElementById('wv');
const $vadchip= document.getElementById('vad-chip');
const $vadtog = document.getElementById('vad-tog');
const $vadthr = document.getElementById('vad-thr');
const $vadthrv= document.getElementById('vad-thr-val');

let ws = null, connected = false;
let micStream = null, micAudioCtx = null, micAnalyser = null;
let micRecorder = null, isRecording = false;
let drawLoopId = null;
let vadEnabled = false, vadSilTimer = null;
let chatAudioQ = [], chatPlaying = false;
let audioPaused = false;
const VAD_SIL_MS = 1400;

function getBestMime() {
  for (const t of ['audio/webm;codecs=opus','audio/webm','audio/ogg;codecs=opus','audio/mp4','']) {
    try { if (!t || MediaRecorder.isTypeSupported(t)) return t; } catch {}
  }
  return '';
}

async function getMicStream() {
  if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia)
    return navigator.mediaDevices.getUserMedia({ audio: true, video: false });
  const fn = navigator.getUserMedia || navigator.webkitGetUserMedia || navigator.mozGetUserMedia;
  if (fn) return new Promise((res, rej) => fn.call(navigator, { audio: true }, res, rej));
  throw new Error('Mic API unavailable — use http://localhost:3401 or HTTPS');
}

async function openMic() {
  if (micStream) return;
  micStream   = await getMicStream();
  micAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const src   = micAudioCtx.createMediaStreamSource(micStream);
  micAnalyser = micAudioCtx.createAnalyser();
  micAnalyser.fftSize = 256;
  src.connect(micAnalyser);
}

function closeMic() {
  stopDrawLoop();
  if (micAudioCtx) { micAudioCtx.close(); micAudioCtx = null; }
  if (micStream)   { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
  micAnalyser = null;
  $wvwrap.classList.remove('visible');
}

function beginRecording() {
  if (isRecording || !micStream) return;
  const mime = getBestMime();
  micRecorder = new MediaRecorder(micStream, mime ? { mimeType: mime } : {});
  micRecorder.ondataavailable = async e => {
    if (e.data.size > 0 && connected && ws.readyState === WebSocket.OPEN) {
      const buf = await e.data.arrayBuffer();
      ws.send(JSON.stringify({ type: 'audio_chunk', data: ab2b64(buf) }));
    }
  };
  micRecorder.onstop = () => {
    micRecorder = null; isRecording = false;
    if (!vadEnabled) closeMic();
    updateMicBtn();
    if (connected && ws.readyState === WebSocket.OPEN)
      ws.send(JSON.stringify({ type: 'audio_end', ..._generationVoice('chat') }));
    addLoading('user');
  };
  if (connected && ws.readyState === WebSocket.OPEN)
    ws.send(JSON.stringify({ type: 'audio_start' }));
  micRecorder.start(200);
  isRecording = true;
  updateMicBtn();
  $wvwrap.classList.add('visible');
  startDrawLoop();
}

function endRecording() {
  if (!isRecording || !micRecorder) return;
  if (micRecorder.state !== 'inactive') micRecorder.stop();
}

function updateMicBtn() {
  $mic.classList.toggle('rec', isRecording);
  $mic.innerHTML = isRecording ? '&#x23F9;' : '&#x1F3A4;';
}

$mic.addEventListener('click', async () => {
  if (vadEnabled) return;
  if (isRecording) { endRecording(); return; }
  try { await openMic(); } catch(e) { addMsg('err', e.message); return; }
  beginRecording();
});

$vadtog.addEventListener('change', async () => {
  vadEnabled = $vadtog.checked;
  if (vadEnabled) {
    try {
      await openMic();
      $mic.classList.add('vad-on');
      $vadchip.textContent = 'listening';
      $wvwrap.classList.add('visible');
      startDrawLoop();
    } catch(e) {
      addMsg('err', 'Mic: ' + e.message);
      $vadtog.checked = false; vadEnabled = false;
    }
  } else {
    clearTimeout(vadSilTimer); vadSilTimer = null;
    if (isRecording) endRecording();
    $mic.classList.remove('vad-on');
    $vadchip.textContent = 'off'; $vadchip.classList.remove('active');
    closeMic();
  }
});
$vadthr.addEventListener('input', () => { $vadthrv.textContent = $vadthr.value });

function startDrawLoop() {
  if (drawLoopId || !micAnalyser) return;
  const dpr = window.devicePixelRatio || 1;
  $wv.width  = $wv.offsetWidth  * dpr;
  $wv.height = $wv.offsetHeight * dpr;
  const cx = $wv.getContext('2d');
  cx.scale(dpr, dpr);
  const W = $wv.offsetWidth, H = $wv.offsetHeight;
  const buf = new Uint8Array(micAnalyser.frequencyBinCount);
  function loop() {
    drawLoopId = requestAnimationFrame(loop);
    micAnalyser.getByteTimeDomainData(buf);
    cx.fillStyle = '#141720'; cx.fillRect(0,0,W,H);
    cx.strokeStyle = '#6c5ce7'; cx.lineWidth = 2; cx.beginPath();
    const sw = W / buf.length;
    for (let i = 0; i < buf.length; i++) {
      const y = (buf[i] / 128) * (H / 2);
      i === 0 ? cx.moveTo(0, y) : cx.lineTo(i * sw, y);
    }
    cx.lineTo(W, H/2); cx.stroke();
    if (vadEnabled && micAnalyser) {
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += Math.abs(buf[i] - 128);
      const rms = sum / buf.length;
      const thr = parseInt($vadthr.value, 10);
      if (rms > thr) {
        $vadchip.textContent = 'speech \u25CF'; $vadchip.classList.add('active');
        if (vadSilTimer) { clearTimeout(vadSilTimer); vadSilTimer = null; }
        if (!isRecording) beginRecording();
      } else {
        if (isRecording && !vadSilTimer) {
          vadSilTimer = setTimeout(() => {
            vadSilTimer = null; endRecording();
            $vadchip.textContent = 'listening'; $vadchip.classList.remove('active');
          }, VAD_SIL_MS);
        } else if (!isRecording) {
          $vadchip.textContent = 'listening'; $vadchip.classList.remove('active');
        }
      }
    }
  }
  loop();
}

function stopDrawLoop() {
  if (drawLoopId) { cancelAnimationFrame(drawLoopId); drawLoopId = null; }
}

// chat audio playback queue
function enqueueChat(blob) {
  chatAudioQ.push(blob);
  // Enable play buttons on all message bars
  document.querySelectorAll('.play-btn').forEach(function(b) { b.disabled = false; });
  // Only autoplay when VAD is enabled — otherwise user clicks play manually
  if (!audioPaused && vadEnabled && !chatPlaying) playNextChat();
}
function playNextChat() {
  if (!chatAudioQ.length) { chatPlaying = false; return; }
  chatPlaying = true;
  const a = new Audio(URL.createObjectURL(chatAudioQ.shift()));
  a.onended = () => { chatPlaying = false; if (!audioPaused && chatAudioQ.length) playNextChat(); };
  a.onerror = () => { chatPlaying = false; if (!audioPaused && chatAudioQ.length) playNextChat(); };
  if (audioPaused) { chatPlaying = false; return; }
  a.play().catch(() => { chatPlaying = false; if (!audioPaused && chatAudioQ.length) playNextChat(); });
}

function b64ToBlob(b64, mime) {
  const raw = atob(b64);
  const arr = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
  return new Blob([arr], { type: mime });
}

function ab2b64(buf) {
  const bytes = new Uint8Array(buf);
  let s = '';
  for (let i = 0; i < bytes.length; i += 8192)
    s += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(s);
}

// chat DOM helpers
// ── file / blob helpers (for reference images) ──────────────────────
function readFileAsDataURL(file) {
  return new Promise(function(res, rej) {
    var r = new FileReader();
    r.onload = function(e) { res(e.target.result); };
    r.onerror = rej;
    r.readAsDataURL(file);
  });
}
function du2blob(du) {
  var parts = du.split(','), mime = parts[0].match(/:(.*?);/)[1];
  var raw = atob(parts[1]), a = new Uint8Array(raw.length);
  for (var i = 0; i < raw.length; i++) a[i] = raw.charCodeAt(i);
  return new Blob([a], { type: mime });
}
function resizeDataUrl(dataUrl, targetW, targetH) {
  return new Promise(function(resolve) {
    var img = new Image();
    img.onload = function() {
      if (img.naturalWidth === targetW && img.naturalHeight === targetH) {
        resolve(dataUrl);
        return;
      }
      var c = document.createElement('canvas');
      c.width = targetW; c.height = targetH;
      var ctx = c.getContext('2d');
      ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(img, 0, 0, targetW, targetH);
      resolve(c.toDataURL('image/jpeg', 0.92));
    };
    img.src = dataUrl;
  });
}

// ── message bar builder ─────────────────────────────────────────────
// Returns a bottom-bar element: eye, pencil, [abort if streaming], regen, spacer, play-btn, timestamp
function addMsgBar(parentEl, opts) {
  opts = opts || {};
  var bar = document.createElement('div');
  bar.className = 'msg-bar';

  var hideBtn = document.createElement('button');
  hideBtn.className = 'msg-bar-btn hide-btn';
  hideBtn.textContent = '\uD83D\uDC41';
  hideBtn.title = 'Hide from context';
  hideBtn.addEventListener('click', function() {
    var cur = parentEl.getAttribute('data-hidden');
    var hidden = cur === 'true';
    if (hidden) {
      parentEl.removeAttribute('data-hidden');
      hideBtn.textContent = '\uD83D\uDC41';
      hideBtn.title = 'Hide from context';
    } else {
      parentEl.setAttribute('data-hidden', 'true');
      hideBtn.textContent = '\uD83D\uDC41\u200D\uD83D\uDDE8';
      hideBtn.title = 'Show in context';
    }
    syncHistory();
  });
  bar.appendChild(hideBtn);

  // Edit button for text messages
  if (opts.canEdit !== false) {
    var editBtn = document.createElement('button');
    editBtn.className = 'msg-bar-btn edit-btn';
    editBtn.textContent = '\u270F';
    editBtn.title = 'Edit message';
    var editing = false;
    editBtn.addEventListener('click', function() {
      var txtEl = parentEl.querySelector('.txt');
      if (!txtEl) return;
      if (!editing) {
        editing = true;
        editBtn.textContent = '\u2714';
        editBtn.title = 'Save changes';
        var ta = document.createElement('textarea');
        ta.className = 'edit-textarea';
        ta.value = txtEl.textContent;
        ta.style.width = '100%';
        ta.style.boxSizing = 'border-box';
        txtEl.style.display = 'none';
        parentEl.insertBefore(ta, txtEl);
        ta.focus();
      } else {
        var ta = parentEl.querySelector('.edit-textarea');
        if (ta) {
          var newText = ta.value;
          var msgId = parentEl.getAttribute('data-msgid');
          if (msgId && ws && ws.readyState === WebSocket.OPEN && activeConvId) {
            ws.send(JSON.stringify({ type: 'edit_text_msg', conv_id: activeConvId, msg_id: msgId, text: newText }));
          }
          txtEl.textContent = newText;
          txtEl.style.display = '';
          ta.remove();
          syncHistory();
        }
        editing = false;
        editBtn.textContent = '\u270F';
        editBtn.title = 'Edit message';
      }
    });
    bar.appendChild(editBtn);
  }

  // Regen button (hidden during streaming, shown otherwise)
  if (true) {//opts.canRegen !== false) {
    var regenBtn = document.createElement('button');
    regenBtn.className = 'msg-bar-btn regen-btn';
    regenBtn.textContent = '\uD83D\uDD04';
    regenBtn.title = 'Regenerate (ctx up to this message)';
    regenBtn.addEventListener('click', function() { regenAt(parentEl); });
    if (opts.pipelineId) regenBtn.style.display = 'none';
    bar.appendChild(regenBtn);
  }

  // Spacer
  var spacer = document.createElement('span');
  spacer.className = 'msg-bar-spacer';
  spacer.style.cssText = 'flex:1;';
  bar.appendChild(spacer);

  // Abort button — only for streaming bubbles
  var abortBtn = null;
  if (opts.pipelineId) {
    abortBtn = document.createElement('button');
    abortBtn.className = 'msg-bar-btn abort-btn';
    abortBtn.textContent = '\u23F9';
    abortBtn.title = 'Stop generation';
    abortBtn.style.cssText = 'padding:1px 4px;font-size:11px;line-height:1;';
    abortBtn.addEventListener('click', function() {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:'abort_pipeline', pipeline_id:opts.pipelineId}));
    });
    // Insert after edit button (which is bar.children[1]) or hide button (bar.children[0])
    var editOrHide = bar.querySelector('.edit-btn') || bar.children[0];
    bar.insertBefore(abortBtn, editOrHide.nextSibling);
  }







  // Play button
  var playBtn = document.createElement('button');
  playBtn.className = 'msg-bar-btn play-btn';
  playBtn.textContent = '\u25B6';
  playBtn.title = 'Play / Pause';
  playBtn.disabled = true;
  playBtn.addEventListener('click', function() {
    audioPaused = !audioPaused;
    playBtn.textContent = audioPaused ? '\u25B6' : '\u23F8';
    if (!audioPaused && chatAudioQ.length && !chatPlaying) playNextChat();
  });
  bar.appendChild(playBtn);

  // Timestamp
  var ts = document.createElement('span');
  ts.className = 'msg-bar-ts';
  ts.textContent = opts.ts || new Date().toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' });
  bar.appendChild(ts);

  parentEl.appendChild(bar);
  // Return references for cleanup
  return { bar: bar, abortBtn: abortBtn, regenBtn: regenBtn };
}

// ── regenerate helper ───────────────────────────────────────────────
function _abortPipeline() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({type: 'abort_pipeline'}));
  }
}
function regenAt(targetMsg) {
  // Find all messages before targetMsg in the chat, excluding hidden ones
  var msgs = [];
  var current = $chat.firstChild;
  while (current) {
    if (current === targetMsg) break;
    if (current.classList && current.classList.contains('msg') && !current.classList.contains('img') && current.getAttribute('data-hidden') !== 'true') {
      var role = '';
      if (current.classList.contains('user')) role = 'user';
      else if (current.classList.contains('sys')) role = 'assistant';
      var txtEl = current.querySelector('.txt');
      if (role && txtEl && txtEl.textContent) {
        msgs.push({ role: role, content: txtEl.textContent });
      }
    }
    current = current.nextSibling;
  }

  // Build full history from visible messages (without system — server prepends it)
  var wsHistory = [];
  for (var i = 0; i < msgs.length; i++) wsHistory.push(msgs[i]);

  // Remove targetMsg and everything after it from the DOM
  var toRemove = [];
  var n = targetMsg;
  while (n) {
    var next = n.nextSibling;
    toRemove.push(n);
    n = next;
  }
  for (var j = 0; j < toRemove.length; j++) toRemove[j].remove();

  // Send set_conv_history then trigger the pipeline
  $send.disabled = true;
  ws.send(JSON.stringify({ type: 'set_conv_history', conv_id: activeConvId, messages: wsHistory }));
  ws.send(JSON.stringify({ type: 'text_message', text: '', ..._generationVoice('chat'), conv_id: activeConvId }));
  addLoading('sys', { onAbort: _abortPipeline });
}

function addMsg(role, text, msgId) {
  var now = new Date();
  var domId = 'msg-' + now.getTime() + '-' + Math.random().toString(36).slice(2,6);
  var d = document.createElement('div');
  d.className = 'msg ' + role;
  d.id = domId;
  if (msgId) d.setAttribute('data-msgid', msgId);
  var lbl = document.createElement('div');
  lbl.className = 'lbl';
  lbl.textContent = role === 'user' ? 'You' : role === 'sys' ? 'Assistant' : 'Error';
  var txt = document.createElement('div');
  txt.className = 'txt'; txt.textContent = text || '';
  d.appendChild(lbl); d.appendChild(txt);
  addMsgBar(d, { ts: now.toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' }) });
  $chat.appendChild(d);
  $chat.scrollTop = $chat.scrollHeight;
  return txt;
}
function addLoading(role, opts) {
  opts = opts || {};
  removeLoading();
  const d = document.createElement('div');
  d.className = 'msg ' + role; d.id = 'chat-loading';
  // Header row: label + optional abort button
  const header = document.createElement('div');
  header.style.cssText = 'display:flex;align-items:center;gap:6px;';
  const lbl = document.createElement('span');
  lbl.className = 'lbl';
  lbl.style.display = 'inline';
  lbl.textContent = role === 'user' ? 'Recording\u2026' : 'ASSISTANT';
  header.appendChild(lbl);
  // Abort button for loading state
  if (opts.onAbort) {
    var abortBtn = document.createElement('button');
    abortBtn.className = 'msg-bar-btn abort-btn';
    abortBtn.textContent = '\u23F9';
    abortBtn.title = 'Stop generation';
    abortBtn.style.cssText = 'padding:0 4px;font-size:11px;';
    abortBtn.addEventListener('click', function() {
      opts.onAbort();
    });
    header.appendChild(abortBtn);
  }
  d.appendChild(header);
  const dots = document.createElement('div');
  dots.className = 'dots';
  dots.innerHTML = '<span></span><span></span><span></span>';
  d.appendChild(dots);
  $chat.appendChild(d);
  $chat.scrollTop = $chat.scrollHeight;
}
function removeLoading() { document.getElementById('chat-loading')?.remove(); }

// Map from pipeline_id -> {div, txt} — supports concurrent pipelines
const streamBubbles = new Map();
function ensureStreamBubble(pid) {
  if (streamBubbles.has(pid)) return streamBubbles.get(pid);
  var now = new Date();
  var div = document.createElement('div');
  div.className = 'msg sys';
  div.id = 'stream-' + pid;
  var lbl = document.createElement('div');
  lbl.className = 'lbl'; lbl.textContent = 'Assistant';
  var txt = document.createElement('div');
  txt.className = 'txt';
  var txtSpan = document.createElement('span');
  var spnDiv = document.createElement('div');
  spnDiv.className = 'stream-spinner';
  spnDiv.innerHTML = '<div class="spinner"></div>Thinking\u2026';
  txt.appendChild(txtSpan);
  txt.appendChild(spnDiv);
  div.appendChild(lbl); div.appendChild(txt);

  var barRefs = addMsgBar(div, { pipelineId: pid, ts: now.toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' }) });
  // Move abort and regen buttons inside the Assistant label
  if (barRefs.abortBtn) lbl.appendChild(barRefs.abortBtn);
  if (barRefs.regenBtn) lbl.appendChild(barRefs.regenBtn);
  $chat.appendChild(div);
  var bubble = {div, txt: txtSpan, _spinner: spnDiv, _abortBtn: barRefs.abortBtn, _regenBtn: barRefs.regenBtn};
  streamBubbles.set(pid, bubble);
  return bubble;
}
function appendToken(tok, pid) {
  removeLoading();
  var bub = ensureStreamBubble(pid);
  // If user switched conversations, the bubble may no longer be in the DOM
  if (!document.getElementById(bub.div.id)) return;
  bub.txt.textContent += tok;
  $chat.scrollTop = $chat.scrollHeight;
}
function closeStreamBubble(pid, aborted) {
  var bub = streamBubbles.get(pid);
  if (bub) {
    if (bub._spinner) bub._spinner.remove();
    if (bub._abortBtn) bub._abortBtn.remove();
    if (bub._regenBtn) bub._regenBtn.style.display = '';
    if (aborted) {
      // Show "Stopped" indicator on the bubble
      var stopLabel = document.createElement('span');
      stopLabel.className = 'aborted-label';
      stopLabel.textContent = '\u26A0';
      stopLabel.style.cssText = 'color:var(--warn);font-size:12px;margin-left:6px;font-style:italic;';
      bub.txt.parentElement.appendChild(stopLabel);
    }
    streamBubbles.delete(pid);
  }
}

// ── Poll a pending image job for a restored bubble ────────────────
function pollImageJob(bubbleEl, proxyToken) {
  if (!bubbleEl || !proxyToken) return;
  var placeholder = bubbleEl.querySelector('.img-placeholder');
  var genBtn = bubbleEl.querySelector('.img-gen-btn');
  // Already submitted — show generating state immediately
  if (placeholder) placeholder.innerHTML = '<div class="spinner"></div>Generating\u2026';
  if (genBtn) genBtn.disabled = true;
  (async function() {
    try {
      while (true) {
        var poll = await fetch('/api/images/status/' + proxyToken);
        if (!poll.ok) throw new Error('HTTP ' + poll.status);
        var st = await poll.json();
        if (st.status === 'done' && st.b64_json) {
          var imgEl = bubbleEl.querySelector('.img-result');
          if (imgEl && placeholder) {
            imgEl.src = 'data:image/png;base64,' + st.b64_json;
            imgEl.style.display = 'block';
            placeholder.style.display = 'none';
          }
          if (genBtn) {
            genBtn.disabled = false;
            genBtn.textContent = '\u25B6 Generate';
            genBtn.style.background = '';
          }
          if (bubbleEl._imgParams) bubbleEl._imgParams.outputImageData = st.b64_json;
          if (bubbleEl._imgParams && st.gen_params) {
            bubbleEl._imgParams.gen_params = st.gen_params;
            try { sessionStorage.setItem('s2s_gp_' + activeConvId, st.gen_params); } catch(e) {}
          }
          // Patch the conversation message
          if (bubbleEl._imgMsgId && ws && ws.readyState === WebSocket.OPEN && activeConvId) {
            ws.send(JSON.stringify({
              type: 'patch_img_bubble',
              conv_id: activeConvId,
              msg_id: bubbleEl._imgMsgId,
              has_output: true,
              output_b64: st.b64_json
            }));
          }
          // Refresh full LLM context so server knows this bubble now has output
          setTimeout(syncHistory, 100);
          return;
        }
        if (st.status === 'error') {
          if (placeholder) placeholder.innerHTML = '\u274c ' + (st.error || 'Generation failed');
          if (genBtn) { genBtn.disabled = false; genBtn.textContent = '\u2717 Failed'; }
          return;
        }
        if (placeholder) placeholder.innerHTML = '<div class="spinner"></div>Generating\u2026';
        await new Promise(function(res) { setTimeout(res, 2000); });
      }
    } catch (_) {
      if (placeholder) placeholder.innerHTML = 'Click Generate';
      if (genBtn) genBtn.disabled = false;
    }
  })();
}

// WebSocket
let _wsTimer = null, _wsBackoff = 3000;
function connectWS() {
  if (_wsTimer) { clearTimeout(_wsTimer); _wsTimer = null; }
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/ws');
  ws.onopen = () => {
    connected = true;
    $wsstat.textContent = '\u25CF live'; $wsstat.style.color = 'var(--ok)';
    $offln.classList.remove('visible');
    sendKey();
  };
  ws.onclose = ev => {
    connected = false;
    $wsstat.textContent = '\u25CF offline'; $wsstat.style.color = 'var(--err)';
    $offln.classList.add('visible');
    if (ev.code === 1008) return; // Configuration error: retry after fixing and reloading.
    if (_wsTimer) clearTimeout(_wsTimer);
    _wsTimer = setTimeout(connectWS, _wsBackoff);
    _wsBackoff = Math.min(_wsBackoff * 2, 30000);
  };
  ws.onerror = () => {};
  ws.onmessage = ev => {
    try { handleFrame(JSON.parse(ev.data)); } catch(e) { console.error('bad frame', e); }
  };
}

function handleFrame(f) {
  switch (f.type) {
    case 'ready':
      _wsBackoff = 3000;
      // Connection confirmed — no chat message needed
      break;
    case 'stt_text':
      removeLoading(); addMsg('user', f.text); addLoading('sys', { onAbort: _abortPipeline }); break;
    case 'llm_token':
      appendToken(f.text, f.pipeline_id); break;
    case 'tts_sentence':
      enqueueChat(b64ToBlob(f.data, 'audio/wav')); break;
    case 'audio_end':
      removeLoading(); closeStreamBubble(f.pipeline_id, f.aborted); $send.disabled = false; break;
    case 'error':
      removeLoading(); closeStreamBubble(f.pipeline_id);
      addMsg('err', f.message || 'Unknown error'); $send.disabled = false; break;
    // ── Encrypted session frames ──────────────────────────────────
    case 'conv_list':
      onConvList(f.conversations || []);
      break;
    case 'conv_data':
      (async function() {
        // Decrypt each message locally (each may use a different session key)
        var msgs = [];
        var hist = [];
        for (var i = 0; i < (f.messages || []).length; i++) {
          var m = f.messages[i];
          var plain = '[decryption failed]';
          try {
            plain = await decryptServerMsg(m.encrypted_b64, f.conv_id, m.msg_id, m.session_uid);
          } catch (e) {
            console.warn('decrypt failed', m.msg_id, e.message, 'session_uid:', m.session_uid);
          }
          // Check if this is a JSON image bubble
          var imgBubble = null;
          if (plain.charAt(0) === '{') {
            try {
              var parsed = JSON.parse(plain);
              if (parsed && parsed.type === 'img') {
                imgBubble = parsed;
              }
            } catch (_) {}
          }
          if (imgBubble) {
            console.log('Restoring img bubble:', imgBubble.prompt, 'assets:', (imgBubble.assets||[]).length, 'hasOutput:', imgBubble.hasOutput, 'outputImageData len:', (imgBubble.outputImageData||'').length);
            msgs.push({type: 'img', prompt: imgBubble.prompt, model: imgBubble.model,
                       steps: imgBubble.steps, guidance: imgBubble.guidance,
                       seed: imgBubble.seed, size: imgBubble.size,
                       hasOutput: imgBubble.hasOutput,
                       outputImageData: imgBubble.outputImageData || null,
                       assets: imgBubble.assets || [],
                       msg_id: m.msg_id,
                       proxyToken: imgBubble.proxyToken || null});
            // For LLM context, send a text summary instead of the JSON blob
            var ctx = '=== Image Generation Context ===\n';
            ctx += 'Prompt: ' + (imgBubble.prompt || '(empty)') + '\n';
            ctx += 'Model: ' + imgBubble.model + '\n';
            ctx += 'Status: ' + (imgBubble.hasOutput ? 'Generated' : 'Pending') + '\n';
            hist.push({role: 'assistant', content: ctx});
          } else {
            msgs.push({role: m.role, text: plain, msg_id: m.msg_id});
            hist.push({role: m.role, content: plain});
          }
        }
        activeConvId = f.conv_id;
        restoreChatToDOM({msgs: msgs});
        // Start polling any pending image jobs
        setTimeout(function() {
          var pendingBubbles = $chat.querySelectorAll('.msg.img');
          pendingBubbles.forEach(function(b) {
            if (b._proxyToken && !b._imgParams.outputImageData) {
              pollImageJob(b, b._proxyToken);
            }
          });
        }, 100);
        // Send decrypted history back to server for LLM context
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({
            type: 'set_conv_history',
            conv_id: f.conv_id,
            messages: hist
          }));
        }
        renderSessionList();
      })();
      break;
    case 'conv_created':
      activeConvId = f.conv_id;
      $chat.innerHTML = '';
      streamBubbles.clear();
      syncHistory();
      break;
    case 'conv_deleted':
      if (activeConvId === f.conv_id) {
        activeConvId = null;
        $chat.innerHTML = '';
        streamBubbles.clear();
      }
      break;
    case 'conv_renamed':
      renderSessionList();
      break;
    case 'img_bubble_saved':
      // Store the msg_id on the bubble DOM element so generateImage can patch it
      (function() {
        var bubbles = $chat.querySelectorAll('.msg.img');
        var lastBubble = bubbles[bubbles.length - 1];
        if (lastBubble) {
          lastBubble._imgMsgId = f.msg_id;
          // If there's a pending completion patch, fire it now
          if (lastBubble._pendingPatch) {
            var pp = lastBubble._pendingPatch;
            delete lastBubble._pendingPatch;
            ws.send(JSON.stringify({
              type: 'patch_img_bubble', conv_id: activeConvId,
              msg_id: f.msg_id, has_output: pp.has_output,
              output_b64: pp.output_b64 || null
            }));
          }
        }
      })();
      break;
    case 'conv_update':
      // Server notified us of new msg count — update sidebar
      for (var i = 0; i < sessions.length; i++) {
        if (sessions[i].id === f.conv_id) {
          sessions[i].msg_count = f.msg_count;
          break;
        }
      }
      break;
    default:
      console.log('unknown frame:', f.type);
  }
}

// ── Model loading ──────────────────────────────────────────────────
var _loadedModels = [];
var _modelCaps = {};
var _preferredGenModel = localStorage.getItem('img_pref_gen') || 'flux2-lora-9b';
var _preferredEditModel = localStorage.getItem('img_pref_edit') || 'flux2-lora-edit-9b';

function switchToEditModel(bubbleId) {
  var sel = document.getElementById(bubbleId + '-model');
  if (!sel) return;
  // Prefer the saved edit model; fallback to first available edit-capable model
  var found = false;
  for (var i = 0; i < sel.options.length; i++) {
    if (sel.options[i].value === _preferredEditModel) {
      sel.value = _preferredEditModel; found = true; break;
    }
  }
  if (!found) {
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value.includes('edit')) {
        sel.value = sel.options[i].value; found = true; break;
      }
    }
  }
  if (!found) sel.value = _preferredEditModel;
  var ev = new Event('change', { bubbles: true });
  sel.dispatchEvent(ev);
}

function switchToGenModel(bubbleId) {
  var sel = document.getElementById(bubbleId + '-model');
  if (!sel) return;
  // Prefer the saved gen model; fallback to first non-edit model
  var found = false;
  for (var i = 0; i < sel.options.length; i++) {
    if (sel.options[i].value === _preferredGenModel) {
      sel.value = _preferredGenModel; found = true; break;
    }
  }
  if (!found) {
    for (var i = 0; i < sel.options.length; i++) {
      if (!sel.options[i].value.includes('edit')) {
        sel.value = sel.options[i].value; found = true; break;
      }
    }
  }
  if (!found) sel.value = _preferredGenModel;
  var ev = new Event('change', { bubbles: true });
  sel.dispatchEvent(ev);
}

function inferModelCaps(name) {
  var n = name.toLowerCase();
  if (n.includes('edit')) return 'i2i';
  return 't2i';
}

/* ── LoRA config panel ─────────────────────────────────────────── */
var _loraListCache = null;

async function fetchLoraList() {
  if (_loraListCache) return _loraListCache;
  try {
    var r = await fetch('/api/images/loras');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    var d = await r.json();
    _loraListCache = d.loras || [];
  } catch (e) {
    console.warn('Failed to fetch LoRA list:', e);
    _loraListCache = [];
  }
  return _loraListCache;
}

function showLoraPanel(params, bubbleId) {
  fetchLoraList().then(function(allLoras) {
    // Build working copy from params.loras
    var selected = (params.loras || []).slice();
    // Map for quick lookup
    var selectedPaths = {};
    selected.forEach(function(l) { selectedPaths[l.path] = l; });

    // Create overlay
    var overlay = document.createElement('div');
    overlay.className = 'lora-overlay';
    overlay.addEventListener('click', function(e) { if (e.target === overlay) close(); });

    var panel = document.createElement('div');
    panel.className = 'lora-panel';

    // ── Header ──
    var header = document.createElement('div');
    header.className = 'lora-panel-header';
    header.innerHTML = '<span>LoRA Configuration</span><span style="font-size:12px;color:var(--dim)">' + selected.length + ' active</span>';
    panel.appendChild(header);

    // ── Body ──
    var body = document.createElement('div');
    body.className = 'lora-panel-body';

    var searchInput = document.createElement('input');
    searchInput.className = 'lora-search';
    searchInput.placeholder = 'Search LoRAs\u2026';
    body.appendChild(searchInput);

    var listEl = document.createElement('div');
    listEl.id = 'lora-list-' + Date.now();
    body.appendChild(listEl);
    panel.appendChild(body);

    function renderList(filter) {
      listEl.innerHTML = '';
      var filtered = allLoras.filter(function(l) {
        return !filter || l.name.toLowerCase().includes(filter.toLowerCase());
      });
      if (!filtered.length) {
        listEl.innerHTML = '<div class="lora-empty">' + (filter ? 'No LoRAs match "' + filter + '"' : 'No LoRAs found') + '</div>';
        return;
      }
      filtered.forEach(function(lora, idx) {
        var sel = selectedPaths[lora.path];
        var item = document.createElement('div');
        item.className = 'lora-item';
        item.draggable = true;
        item.dataset.idx = idx;

        // Main row: drag handle | name | checkbox
        var row = document.createElement('div');
        row.className = 'lora-item-row';

        var handle = document.createElement('span');
        handle.className = 'lora-handle';
        handle.textContent = '\u283F';
        row.appendChild(handle);

        var name = document.createElement('span');
        name.className = 'lora-name';
        name.textContent = lora.name;
        row.appendChild(name);

        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.className = 'lora-cb';
        cb.checked = !!sel;
        row.appendChild(cb);
        item.appendChild(row);

        // Sub-controls (weight sliders) — shown when selected
        var sub = document.createElement('div');
        sub.className = 'lora-sub-controls';
        sub.style.display = cb.checked ? '' : 'none';
        item.appendChild(sub);

        function buildSubControls() {
          sub.innerHTML = '';
          var table = document.createElement('table');
          var tbody = document.createElement('tbody');
          table.appendChild(tbody);
          sub.appendChild(table);
          var w = sel ? (sel.weight || sel.scale || 1.0) : 1.0;
          var cw = sel ? (sel.clip_weight || w) : w;
          var mw = sel ? (sel.model_weight || w) : w;

          function addSlider(label, key, val, onchange) {
            var tr = document.createElement('tr');
            var td1 = document.createElement('td');
            td1.className = 'lora-label';
            td1.textContent = label;
            var td2 = document.createElement('td');
            var slider = document.createElement('input');
            slider.type = 'range'; slider.min = '0'; slider.max = '2'; slider.step = '0.05';
            slider.value = String(val);
            var td3 = document.createElement('td');
            var valSpan = document.createElement('span');
            valSpan.className = 'lora-val';
            valSpan.textContent = String(val);
            slider.addEventListener('input', function() {
              var v = parseFloat(slider.value);
              valSpan.textContent = String(v);
              onchange(v);
            });
            td2.appendChild(slider);
            td3.appendChild(valSpan);
            tr.appendChild(td1);
            tr.appendChild(td2);
            tr.appendChild(td3);
            tbody.appendChild(tr);
          }

          addSlider('weight', 'weight', w, function(v) {
            if (!sel) {
              sel = {path: lora.path, name: lora.name, weight: v, clip_weight: v, model_weight: v};
              selectedPaths[lora.path] = sel;
              selected.push(sel);
              cb.checked = true;
            }
            sel.weight = v;
          });
          addSlider('clip', 'clip_weight', cw, function(v) {
            if (!sel) {
              sel = {path: lora.path, name: lora.name, weight: v, clip_weight: v, model_weight: v};
              selectedPaths[lora.path] = sel;
              selected.push(sel);
              cb.checked = true;
            }
            sel.clip_weight = v;
          });
          addSlider('model', 'model_weight', mw, function(v) {
            if (!sel) {
              sel = {path: lora.path, name: lora.name, weight: v, clip_weight: v, model_weight: v};
              selectedPaths[lora.path] = sel;
              selected.push(sel);
              cb.checked = true;
            }
            sel.model_weight = v;
          });
        }
        buildSubControls();

        // Checkbox toggle
        cb.addEventListener('change', function() {
          if (cb.checked) {
            sel = {path: lora.path, name: lora.name, weight: 1.0, clip_weight: 1.0, model_weight: 1.0};
            selectedPaths[lora.path] = sel;
            selected.push(sel);
          } else {
            var idx2 = selected.indexOf(sel);
            if (idx2 >= 0) selected.splice(idx2, 1);
            delete selectedPaths[lora.path];
            sel = null;
          }
          sub.style.display = cb.checked ? '' : 'none';
          if (cb.checked) buildSubControls();
          header.innerHTML = '<span>LoRA Configuration</span><span style="font-size:12px;color:var(--dim)">' + selected.length + ' active</span>';
        });

        // Drag & drop
        item.addEventListener('dragstart', function(e) {
          dragSrcIdx = idx; item.classList.add('dragging');
        });
        item.addEventListener('dragend', function() { item.classList.remove('dragging'); });
        item.addEventListener('dragover', function(e) {
          e.preventDefault();
          document.querySelectorAll('#' + listEl.id + ' .lora-item').forEach(function(el) { el.classList.remove('drag-over'); });
          item.classList.add('drag-over');
        });
        item.addEventListener('dragleave', function() { item.classList.remove('drag-over'); });
        item.addEventListener('drop', function(e) {
          e.preventDefault(); item.classList.remove('drag-over');
          if (dragSrcIdx !== null && dragSrcIdx !== idx) {
            var moved = filtered.splice(dragSrcIdx, 1);
            filtered.splice(idx > dragSrcIdx ? idx - 1 : idx, 0, moved[0]);
            dragSrcIdx = null;
            renderList(searchInput.value);
          }
        });

        listEl.appendChild(item);
      });
    }

    searchInput.addEventListener('input', function() { renderList(searchInput.value); });
    renderList('');

    // ── Footer ──
    var footer = document.createElement('div');
    footer.className = 'lora-panel-footer';
    var cancelBtn = document.createElement('button');
    cancelBtn.className = 'lora-cancel-btn';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.addEventListener('click', close);
    var applyBtn = document.createElement('button');
    applyBtn.className = 'lora-apply-btn';
    applyBtn.textContent = 'Apply';
    applyBtn.addEventListener('click', function() {
      params.loras = selected;
      // Also update lora_paths for backward compat
      params.lora_paths = selected.map(function(l) { return l.path; });
      close();
    });
    footer.appendChild(cancelBtn);
    footer.appendChild(applyBtn);
    panel.appendChild(footer);

    overlay.appendChild(panel);
    document.body.appendChild(overlay);

    function close() {
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    }
  });
}

async function loadModels() {
  if (_loadedModels.length) return _loadedModels;
  try {
    var r = await fetch('/api/images/models');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    var d = await r.json();
    var list = d.data || d.models || d;
    if (!Array.isArray(list)) throw new Error('Unexpected format');
    _loadedModels = list.map(function(m) {
      var id = m.id || m.name || m.model || String(m);
      return id;
    }).filter(Boolean);
    _loadedModels.forEach(function(m) { _modelCaps[m] = inferModelCaps(m); });
    if (!_loadedModels.length) throw new Error('Empty model list');
  } catch (e) {
    console.warn('Failed to load models:', e.message);
    // Fallback hardcoded list
    _loadedModels = [
      'sdxl', 'flux2-lora-4b', 'flux2-lora-9b', 'flux2-klein-4b', 'flux2-klein-9b', 'z-image-turbo',
      'flux2-lora-edit-4b', 'flux2-lora-edit-9b', 'flux2-klein-edit-4b', 'flux2-klein-edit-9b', 'qwen-image-edit', 'z-image-turbo-edit',
    ];
    _loadedModels.forEach(function(m) { _modelCaps[m] = inferModelCaps(m); });
  }
  return _loadedModels;
}

// ── Reconnect recovery for in-flight image jobs ────────────────────

// Also restore any saved image gen state for edit-state bubbles (no in-flight job)
function restoreSavedImgBubble() {
  if (!activeConvId) return;
  try {
    var saved = JSON.parse(localStorage.getItem('s2s_imggen_' + activeConvId));
    if (!saved || !saved.params) return;
    // Only restore if there's no image bubble already in the chat
    if ($chat.querySelector('.msg.img')) return;
    // Create the bubble in edit state with saved params
    handleImageCommand(saved.params.prompt || '');
    var lastBubble = $chat.lastChild;
    if (lastBubble && lastBubble.classList.contains('img')) {
      var promptInput = lastBubble.querySelector('.img-prompt-input');
      var modelSelect = lastBubble.querySelector('.img-model-select');
      if (promptInput) promptInput.value = saved.params.prompt || '';
      // Restore the generated output image if available
      if (saved.outputImageData) {
        var imgEl = lastBubble.querySelector('.img-result');
        var placeholder = lastBubble.querySelector('.img-placeholder');
        var genBtn = lastBubble.querySelector('.img-gen-btn');
        if (imgEl && placeholder && genBtn) {
          imgEl.src = 'data:image/png;base64,' + saved.outputImageData;
          imgEl.style.display = 'block';
          placeholder.style.display = 'none';
          genBtn.textContent = '\u2713 Generated';
          genBtn.style.background = 'var(--ok)';
        }
      }
    }
  } catch (_) {}
}

// Kick off model loading + reconnect recovery early
loadModels();

async function generateImage(params, bubbleId) {
  var btn = document.getElementById(bubbleId + '-genbtn');
  var placeholder = document.getElementById(bubbleId + '-placeholder');
  var imgEl = document.getElementById(bubbleId + '-img');
  if (!btn || !placeholder || !imgEl) return;

  btn.disabled = true;
  btn.textContent = '\u23F3 Generating\u2026';
  placeholder.innerHTML = '<div class="spinner"></div>Submitting\u2026';

  try {
    var [width, height] = params.size.split('x').map(Number);
    var token;

    if (params.assets && params.assets.length) {
      // ── Image-to-image via proxy ──────────────────────────────────
      if (!params.assets[0].dataUrl) {
        throw new Error('Image data missing. Please re-upload.');
      }
      var fd = new FormData();
      fd.append('prompt', params.prompt || '');
      fd.append('model', params.model);
      fd.append('width', String(width));
      fd.append('height', String(height));
      fd.append('strength', String(params.strength ?? 0.75));
      if (params.steps != null) fd.append('steps', String(params.steps));
      if (params.guidance != null) fd.append('guidance', String(params.guidance));
      if (params.seed != null) fd.append('seed', String(params.seed));
      if (params.negative_prompt) fd.append('negative_prompt', params.negative_prompt);
      if (params.loras && params.loras.length) fd.append('loras', JSON.stringify(params.loras));
      for (var ai = 0; ai < params.assets.length; ai++) {
        var refData = params.assets[ai].dataUrl;
        // Resize first ref image to target dimensions if different from 1x
        if (ai === 0) {
          refData = await resizeDataUrl(refData, width, height);
        }
        fd.append('image', du2blob(refData), params.assets[ai].name || 'ref.png');
      }
      var r = await fetch('/api/images/edits', { method: 'POST', body: fd });
      if (!r.ok) { var e = await r.text(); throw new Error('Submit failed: ' + e.slice(0, 150)); }
      token = (await r.json()).token;
    } else {
      // ── Text-to-image via proxy ───────────────────────────────────
      var body = { prompt: params.prompt || '', width: width, height: height, size: params.size };
      if (params.model) body.model = params.model;
      if (params.steps != null) body.steps = params.steps;
      if (params.guidance != null) body.guidance = params.guidance;
      if (params.seed != null) body.seed = params.seed;
      if (params.negative_prompt) body.negative_prompt = params.negative_prompt;
      if (params.lora_paths && params.lora_paths.length) body.lora_paths = params.lora_paths;
      if (params.loras && params.loras.length) body.loras = params.loras;
      var r2 = await fetch('/api/images/generations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r2.ok) { var e2 = await r2.text(); throw new Error('Submit failed: ' + e2.slice(0, 150)); }
      token = (await r2.json()).token;
    }

    // Persist proxy token in the conversation message (survives refresh)
    var bubbleEl = document.getElementById(bubbleId);
    if (bubbleEl && bubbleEl._imgMsgId && ws && ws.readyState === WebSocket.OPEN && activeConvId) {
      ws.send(JSON.stringify({
        type: 'patch_img_bubble',
        conv_id: activeConvId,
        msg_id: bubbleEl._imgMsgId,
        params: { proxyToken: token }
      }));
    }

    // Poll for completion
    placeholder.innerHTML = '<div class="spinner"></div>Generating\u2026';
    while (true) {
      await new Promise(function(res) { setTimeout(res, 2000); });
      var poll = await fetch('/api/images/status/' + token);
      if (!poll.ok) throw new Error('Poll failed: HTTP ' + poll.status);
      var st = await poll.json();
      if (st.status === 'done') {
        var imgUrl = 'data:image/png;base64,' + st.b64_json;
        imgEl.src = imgUrl;
        imgEl.style.display = 'block';
        placeholder.style.display = 'none';
        btn.disabled = false;
        btn.textContent = '\u25B6 Generate';
        btn.style.background = '';
        // Store output on the DOM element for persistence
        var bubbleEl = document.getElementById(bubbleId);
        if (bubbleEl) {
          if (bubbleEl._imgParams) {
            bubbleEl._imgParams.outputImageData = st.b64_json;
          }
          // Mark conversation message as having output
          if (ws && ws.readyState === WebSocket.OPEN && activeConvId) {
            if (bubbleEl._imgMsgId) {
              ws.send(JSON.stringify({
                type: 'patch_img_bubble',
                conv_id: activeConvId,
                msg_id: bubbleEl._imgMsgId,
                has_output: true,
                output_b64: st.b64_json
              }));
            } else {
              // img_bubble_saved hasn't arrived yet — queue for later
              bubbleEl._pendingPatch = {has_output: true, output_b64: st.b64_json};
            }
          }
        }
        return;
      }
      if (st.status === 'error') {
        throw new Error(st.error || 'Generation failed');
      }
      // still pending/processing — keep polling
    }
  } catch (e) {
    btn.disabled = false;
    btn.textContent = '\u2717 Retry';
    btn.title = e.message;
    placeholder.innerHTML = '\u274c ' + e.message;
  }
}

function handleImageCommand(rawPrompt) {
  var prompt = (rawPrompt || '').trim();
  var params = {
    prompt: prompt || '',
    model: 'flux2-lora-9b',
    steps: 2,
    guidance: 3.5,
    seed: null,
    size: '1024x1024',
    assets: [],
    strength: 0.75,
    negative_prompt: '',
    lora_paths: [],
  };

  // Trigger model loading (async, populates dropdown when it resolves)
  loadModels();

  // Track this generation for context injection on next text message
  var bubbleId = 'img-bubble-' + Date.now();

  // ── Build the bubble ──────────────────────────────────────────────
  var d = document.createElement('div');
  d.className = 'msg img'; d.id = bubbleId;

  var lbl = document.createElement('div');
  lbl.className = 'lbl'; lbl.textContent = '\uD83C\uDFA8 Image Generation';

  // ── Controls side (left/top) ──────────────────────────────────────
  var controls = document.createElement('div');
  controls.className = 'img-controls';

  // Editable prompt
  var promptInput = document.createElement('textarea');
  promptInput.className = 'img-prompt-input';
  promptInput.id = bubbleId + '-prompt';
  promptInput.placeholder = 'Enter prompt\u2026';
  promptInput.value = params.prompt || '';
  promptInput.addEventListener('input', function() {
    params.prompt = promptInput.value;
  });
  controls.appendChild(promptInput);

  // Model selector (populated async)
  var modelWrap = document.createElement('div');
  modelWrap.className = 'img-model-wrap';
  var modelSelect = document.createElement('select');
  modelSelect.className = 'img-model-select';
  modelSelect.id = bubbleId + '-model';
  modelSelect.innerHTML = '<option>' + params.model + '</option>';
  modelSelect.addEventListener('change', function() {
    params.model = modelSelect.value;
    // Toggle SDXL-only controls (negative prompt, LoRA)
    var sdxlRows = d.querySelectorAll('.sdxl-only');
    var isSdxl = modelSelect.value === 'sdxl';
    for (var i = 0; i < sdxlRows.length; i++) {
      sdxlRows[i].style.display = isSdxl ? '' : 'none';
    }
    // Track preferred model per mode
    if (modelSelect.value.includes('edit')) {
      _preferredEditModel = modelSelect.value;
      localStorage.setItem('img_pref_edit', _preferredEditModel);
    } else {
      _preferredGenModel = modelSelect.value;
      localStorage.setItem('img_pref_gen', _preferredGenModel);
    }
    var capsSpan = document.getElementById(bubbleId + '-caps');
    if (capsSpan) capsSpan.textContent = _modelCaps[params.model] || inferModelCaps(params.model);
  });
  var capsSpan = document.createElement('span');
  capsSpan.className = 'img-model-caps';
  capsSpan.id = bubbleId + '-caps';
  capsSpan.textContent = _modelCaps[params.model] || inferModelCaps(params.model);
  modelWrap.appendChild(modelSelect);
  modelWrap.appendChild(capsSpan);
  controls.appendChild(modelWrap);

  // ── Interactive controls ─────────────────────────────────────────
  var ctrlDiv = document.createElement('div');
  ctrlDiv.className = 'img-params';

  // ── Size control (re-renderable when assets change) ──────────────
  var sizeRow = document.createElement('div');
  sizeRow.className = 'img-ctrl-row';
  var sizeLbl = document.createElement('span');
  sizeLbl.className = 'img-ctrl-label'; sizeLbl.textContent = 'size';

  var sizeInner = document.createElement('div');
  sizeInner.className = 'img-size-inputs';

  function renderSizeControl() {
    sizeInner.innerHTML = '';
    if (params.assets.length && params.assets[0].dataUrl) {
      // Editing: multiplier slider based on first ref image dimensions
      // We need dimensions — try to get them from a loaded image
      (function() {
        var img = new Image();
        img.onload = function() {
          var baseW = img.naturalWidth, baseH = img.naturalHeight;
          var slider = document.createElement('input');
          slider.type = 'range';
          slider.className = 'img-ctrl-slider img-size-slider';
          slider.min = '0.25'; slider.max = '2'; slider.step = '0.25';
          // Parse current size ratio from params
          var curW = parseInt(params.size.split('x')[0], 10) || baseW;
          var curVal = Math.round((curW / baseW) * 100) / 100;
          slider.value = String(Math.max(0.25, Math.min(2, curVal)));

          var resultSpan = document.createElement('span');
          resultSpan.className = 'img-size-result';

          function updateSize() {
            var mult = parseFloat(slider.value);
            var nw = Math.round(baseW * mult);
            var nh = Math.round(baseH * mult);
            params.size = nw + 'x' + nh;
            resultSpan.textContent = nw + '\u00D7' + nh;
          }
          slider.addEventListener('input', updateSize);
          updateSize();

          var sliderRow = document.createElement('div');
          sliderRow.className = 'img-size-slider-row';
          sliderRow.appendChild(slider);
          sliderRow.appendChild(resultSpan);
          sizeInner.appendChild(sliderRow);
        };
        img.src = params.assets[0].dataUrl;
      })();
    } else {
      // Scratch: manual width × height inputs
      var parts = params.size.split('x');
      var w = parseInt(parts[0], 10) || 1024;
      var h = parseInt(parts[1], 10) || 1024;

      var wIn = document.createElement('input');
      wIn.type = 'number'; wIn.className = 'img-size-input';
      wIn.placeholder = 'w'; wIn.min = '64'; wIn.max = '4096'; wIn.step = '64';
      wIn.value = String(w);

      var times = document.createElement('span');
      times.className = 'img-size-times'; times.textContent = '\u00D7';

      var hIn = document.createElement('input');
      hIn.type = 'number'; hIn.className = 'img-size-input';
      hIn.placeholder = 'h'; hIn.min = '64'; hIn.max = '4096'; hIn.step = '64';
      hIn.value = String(h);

      function updateSizeManual() {
        var nw = parseInt(wIn.value, 10) || 1024;
        var nh = parseInt(hIn.value, 10) || 1024;
        nw = Math.max(64, Math.min(4096, nw));
        nh = Math.max(64, Math.min(4096, nh));
        params.size = nw + 'x' + nh;
      }
      wIn.addEventListener('input', updateSizeManual);
      hIn.addEventListener('input', updateSizeManual);

      sizeInner.appendChild(wIn);
      sizeInner.appendChild(times);
      sizeInner.appendChild(hIn);
    }
  }

  sizeRow.appendChild(sizeLbl);
  sizeRow.appendChild(sizeInner);
  ctrlDiv.appendChild(sizeRow);
  renderSizeControl();

  // ── Guidance slider ──────────────────────────────────────────────
  var guideRow = document.createElement('div');
  guideRow.className = 'img-ctrl-row';
  var guideLbl = document.createElement('span');
  guideLbl.className = 'img-ctrl-label'; guideLbl.textContent = 'guidance';
  var guideSlider = document.createElement('input');
  guideSlider.type = 'range'; guideSlider.className = 'img-ctrl-slider';
  guideSlider.min = '1'; guideSlider.max = '20'; guideSlider.step = '0.5';
  guideSlider.value = String(params.guidance);
  var guideVal = document.createElement('span');
  guideVal.className = 'img-ctrl-value'; guideVal.textContent = String(params.guidance);
  guideSlider.addEventListener('input', function() {
    params.guidance = parseFloat(guideSlider.value);
    guideVal.textContent = String(params.guidance);
  });
  guideRow.appendChild(guideLbl);
  guideRow.appendChild(guideSlider);
  guideRow.appendChild(guideVal);
  ctrlDiv.appendChild(guideRow);

  // ── Steps slider ─────────────────────────────────────────────────
  var stepsRow = document.createElement('div');
  stepsRow.className = 'img-ctrl-row';
  var stepsLbl = document.createElement('span');
  stepsLbl.className = 'img-ctrl-label'; stepsLbl.textContent = 'steps';
  var stepsSlider = document.createElement('input');
  stepsSlider.type = 'range'; stepsSlider.className = 'img-ctrl-slider';
  stepsSlider.min = '2'; stepsSlider.max = '20'; stepsSlider.step = '1';
  stepsSlider.value = String(params.steps);
  var stepsVal = document.createElement('span');
  stepsVal.className = 'img-ctrl-value'; stepsVal.textContent = String(params.steps);
  stepsSlider.addEventListener('input', function() {
    params.steps = parseInt(stepsSlider.value, 10);
    stepsVal.textContent = String(params.steps);
  });
  stepsRow.appendChild(stepsLbl);
  stepsRow.appendChild(stepsSlider);
  stepsRow.appendChild(stepsVal);
  ctrlDiv.appendChild(stepsRow);

  // ── Seed control ──────────────────────────────────────────────
  var seedRow = document.createElement('div');
  seedRow.className = 'img-ctrl-row';
  var seedLbl = document.createElement('span');
  seedLbl.className = 'img-ctrl-label'; seedLbl.textContent = 'seed';
  var seedMinus = document.createElement('button');
  seedMinus.textContent = '-'; seedMinus.style.cssText = 'width:28px;flex-shrink:0;border:1px solid var(--border);border-radius:4px;cursor:pointer;background:var(--surface2);color:var(--text)';
  seedMinus.addEventListener('click', function() { if (params.seed == null) params.seed = 42; params.seed = ((params.seed - 12345) * 4005161829) >>> 0; var _sv = document.getElementById(bubbleId).querySelector('.img-ctrl-seed'); if (_sv) _sv.value = String(params.seed); });
  var seedVal = document.createElement('input');
  seedVal.type = 'text'; seedVal.className = 'img-ctrl-seed'; seedVal.style.cssText = 'flex:1;text-align:center;font-family:monospace;border:1px solid var(--border);border-radius:4px;background:var(--surface2);color:var(--text);padding:2px 6px';
  seedVal.value = params.seed == null ? '' : String(params.seed); seedVal.placeholder = 'random';
  seedVal.addEventListener('input', function() { var v = seedVal.value.trim(); params.seed = v === '' ? null : parseInt(v, 10); });
  var seedPlus = document.createElement('button');
  seedPlus.textContent = '+'; seedPlus.style.cssText = 'width:28px;flex-shrink:0;border:1px solid var(--border);border-radius:4px;cursor:pointer;background:var(--surface2);color:var(--text)';
  seedPlus.addEventListener('click', function() { if (params.seed == null) params.seed = 42; params.seed = (1103515245 * params.seed + 12345) >>> 0; var _sv = document.getElementById(bubbleId).querySelector('.img-ctrl-seed'); if (_sv) _sv.value = String(params.seed); });
  seedRow.appendChild(seedLbl);
  seedRow.appendChild(seedMinus);
  seedRow.appendChild(seedVal);
  seedRow.appendChild(seedPlus);
  ctrlDiv.appendChild(seedRow);

  // ── Negative prompt (SDXL only) ─────────────────────────────
  var negRow = document.createElement('div');
  negRow.className = 'img-ctrl-row sdxl-only';
  negRow.style.display = 'none';
  var negLbl = document.createElement('span');
  negLbl.className = 'img-ctrl-label'; negLbl.textContent = 'negative';
  var negInput = document.createElement('textarea');
  negInput.className = 'img-neg-input';
  negInput.placeholder = 'Negative prompt\u2026';
  negInput.style.flex = '1'; negInput.style.resize = 'none'; negInput.rows = 2;
  negInput.addEventListener('input', function() { params.negative_prompt = negInput.value; });
  negRow.appendChild(negLbl);
  negRow.appendChild(negInput);
  ctrlDiv.appendChild(negRow);

  // ── LoRA button (SDXL only) ───────────────────────────────────
  var loraBtn = document.createElement('button');
  loraBtn.className = 'lora-toggle-btn sdxl-only';
  loraBtn.textContent = 'LoRAs';
  loraBtn.style.display = 'none';
  loraBtn.addEventListener('click', function() { showLoraPanel(params, bubbleId); });
  ctrlDiv.appendChild(loraBtn);

  controls.appendChild(ctrlDiv);

  // Reference image assets
  var assetsWrap = document.createElement('div');
  assetsWrap.className = 'img-assets';
  assetsWrap.id = bubbleId + '-assets';
  function renderAssets() {
    assetsWrap.innerHTML = '';
    params.assets.forEach(function(a, idx) {
      var thumb = document.createElement('div');
      thumb.className = 'img-asset-thumb';
      var img = document.createElement('img');
      img.src = a.dataUrl;
      img.alt = a.name;
      var delBtn = document.createElement('button');
      delBtn.className = 'img-asset-del';
      delBtn.textContent = '\u2715';
      delBtn.addEventListener('click', function() {
        params.assets.splice(idx, 1);
        renderAssets();
        renderSizeControl();
        // Auto-switch to gen model when last reference removed
        if (!params.assets.length) switchToGenModel(bubbleId);
      });
      thumb.appendChild(img);
      thumb.appendChild(delBtn);
      assetsWrap.appendChild(thumb);
    });
    // "+" button inline after thumbs, same size/shape as a thumb
    var addBtn = document.createElement('button');
    addBtn.className = 'img-file-btn';
    addBtn.textContent = '+';
    addBtn.addEventListener('click', function() { fileInput.click(); });
    assetsWrap.appendChild(addBtn);
  }
  var fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = 'image/*';
  fileInput.style.display = 'none';
  fileInput.id = bubbleId + '-fileinput';
  fileInput.addEventListener('change', async function() {
    var files = fileInput.files;
    if (!files.length) return;
    var hadAssets = params.assets.length > 0;
    var _pendingGenParams = null;
    for (var fi = 0; fi < files.length; fi++) {
      var f = files[fi];
      var dataUrl = await readFileAsDataURL(f);
      var assetId = 'asset-' + Math.random().toString(36).slice(2,8);
      params.assets.push({ dataUrl: dataUrl, name: f.name, id: assetId });
      // Upload ref image to server for LLM context and store token
      try {
        var fd = new FormData();
        fd.append('image', du2blob(dataUrl), f.name || 'ref.png');
        var uploadResp = await fetch('/api/images/upload-ref', { method: 'POST', body: fd });
        if (uploadResp.ok) {
          var uploadData = await uploadResp.json();
          if (uploadData.token) params.assets[params.assets.length - 1].refToken = uploadData.token;
          if (!hadAssets && uploadData.gen_params) {
            _pendingGenParams = uploadData.gen_params;
          }
        }
      } catch (_) {}
    }
    fileInput.value = '';
    // Restore gen_params from uploaded image metadata
    if (!hadAssets && _pendingGenParams) {
      var _s = typeof _pendingGenParams === 'string' ? JSON.parse(_pendingGenParams) : _pendingGenParams;
      var _bubble = document.getElementById(bubbleId);
      if (_s.prompt) { params.prompt = _s.prompt; var _pi = document.getElementById(bubbleId + '-prompt'); if (_pi) _pi.value = _s.prompt; }
      if (_s.guidance != null) { params.guidance = _s.guidance; var _sl = _bubble.querySelectorAll('.img-ctrl-slider'); if (_sl[0]) _sl[0].value = String(_s.guidance); var _vl = _bubble.querySelectorAll('.img-ctrl-value'); if (_vl[0]) _vl[0].textContent = String(_s.guidance); }
      if (_s.seed != null) { params.seed = _s.seed; var _sv = _bubble.querySelector('.img-ctrl-seed'); if (_sv) _sv.value = String(_s.seed); }
      if (_s.steps != null) { params.steps = _s.steps; var _sl2 = _bubble.querySelectorAll('.img-ctrl-slider'); if (_sl2[1]) _sl2[1].value = String(_s.steps); var _vl2 = _bubble.querySelectorAll('.img-ctrl-value'); if (_vl2[1]) _vl2[1].textContent = String(_s.steps); }
      if (_s.model) { params.model = _s.model; var _ms = document.getElementById(bubbleId + '-model'); if (_ms) _ms.value = _s.model; }
      if (_s.negative_prompt) { params.negative_prompt = _s.negative_prompt; var _ni = _bubble.querySelector('.img-neg-input'); if (_ni) _ni.value = _s.negative_prompt; }
      if (_s.size) params.size = _s.size;
      if (_s.lora_paths) params.lora_paths = _s.lora_paths;
      if (_s.loras) params.loras = _s.loras;
    }
    // Set size to first ref image's natural dimensions when first attached
    if (!hadAssets && params.assets.length && params.assets[0].dataUrl && !_pendingGenParams) {
      try {
        var dims = await new Promise(function(resolve, reject) {
          var img = new Image();
          img.onload = function() { resolve({ w: img.naturalWidth, h: img.naturalHeight }); };
          img.onerror = reject;
          img.src = params.assets[0].dataUrl;
        });
        params.size = dims.w + 'x' + dims.h;
      } catch (_) {}
    }
    renderAssets();
    renderSizeControl();
    // Auto-switch to edit model when first reference added (skip if gen_params restored)
    if (!hadAssets && params.assets.length && !_pendingGenParams) switchToEditModel(bubbleId);
  });
  renderAssets();
  controls.appendChild(assetsWrap);
  controls.appendChild(fileInput);

  // Generate button
  var genBtnWrap = document.createElement('div');
  genBtnWrap.className = 'img-gen-btn-wrap';
  var genBtn = document.createElement('button');
  genBtn.className = 'img-gen-btn';
  genBtn.id = bubbleId + '-genbtn';
  genBtn.textContent = '\u25B6 Generate';
  genBtn.addEventListener('click', function() { generateImage(params, bubbleId); });
  genBtnWrap.appendChild(genBtn);
  controls.appendChild(genBtnWrap);

  // ── Output side (right/bottom) ────────────────────────────────────
  var output = document.createElement('div');
  output.className = 'img-output';

  var placeholder = document.createElement('div');
  placeholder.className = 'img-placeholder';
  placeholder.id = bubbleId + '-placeholder';
  placeholder.textContent = 'Click Generate';
  output.appendChild(placeholder);

  var imgEl = document.createElement('img');
  imgEl.className = 'img-result';
  imgEl.id = bubbleId + '-img';
  imgEl.alt = 'Generated image';
  imgEl.style.display = 'none';
  output.appendChild(imgEl);

  // ── Body wrapper ──────────────────────────────────────────────────
  var body = document.createElement('div');
  body.className = 'img-body';
  body.appendChild(controls);
  body.appendChild(output);

  // ── Assemble top-level ────────────────────────────────────────────
  d.appendChild(lbl);
  d.appendChild(body);
  addMsgBar(d, { canRegen: false, ts: new Date().toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' }) });
  // Store params on the DOM element for session save/restore
  d._imgParams = { prompt: params.prompt, model: params.model, steps: params.steps, guidance: params.guidance, seed: params.seed, size: params.size, assets: params.assets.map(function(a) { return { dataUrl: a.dataUrl, name: a.name, id: a.id, refToken: a.refToken }; }).filter(function(a) { return a.dataUrl; }), outputImageData: null };
  d._imgMsgId = null;
  // Set image gen context for sendText
  var ctx = '=== Image Generation Context ===\n';
  ctx += 'Prompt: ' + (params.prompt || '(empty)') + '\n';
  ctx += 'Model: ' + params.model + '\n';
  ctx += 'Steps: ' + params.steps + '\n';
  ctx += 'Guidance: ' + params.guidance + '\n';
  ctx += 'Seed: ' + (params.seed === null ? 'random' : params.seed) + '\n';
  ctx += 'Size: ' + params.size + '\n';
  ctx += 'Assets: ' + (params.assets.length + ' reference image(s)') + '\n';
  ctx += 'Status: Not yet generated\n';
  $chat.appendChild(d);
  $chat.scrollTop = $chat.scrollHeight;

  // Populate model dropdown once models load
  loadModels().then(function(models) {
    var sel = document.getElementById(bubbleId + '-model');
    if (!sel) return;
    sel.innerHTML = '';
    models.forEach(function(m) {
      var opt = document.createElement('option');
      opt.value = m;
      opt.textContent = m;
      if (m === params.model) opt.selected = true;
      sel.appendChild(opt);
    });
    var capsSpan2 = document.getElementById(bubbleId + '-caps');
    if (capsSpan2) capsSpan2.textContent = _modelCaps[params.model] || inferModelCaps(params.model);
  });

  // Do NOT send anything to the LLM here — the bubble is self-contained.
  // Wire param change auto-sync
  attachImgBubbleSync(bubbleId, params);
  // Persist the bubble to conversation immediately — save everything
  var saveConvId = activeConvId || 'conv-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2,10);
  if (!activeConvId) activeConvId = saveConvId;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      type: 'save_img_bubble',
      conv_id: saveConvId,
      params: {
        prompt: params.prompt,
        model: params.model,
        steps: params.steps,
        guidance: params.guidance,
        seed: params.seed,
        size: params.size,
        negative_prompt: params.negative_prompt,
      },
      assets: params.assets.map(function(a) {
        return { dataUrl: a.dataUrl, name: a.name, id: a.id, refToken: a.refToken };
      }),
      output_b64: null,
      has_output: false
    }));
  }
}

// ── Current image gen context (consumed by sendText) ──
// Not a global — sendText reads from the last image bubble's DOM element.
// Each bubble carries its own data on _imgParams / _imgMsgId / _proxyToken.

async function sendText() {
  const text = $txtin.value.trim();
  if (!connected) return;
  if (!text) {
    // Empty send — trigger LLM continuation without adding a user message
    $send.disabled = true;
    addLoading('sys', { onAbort: _abortPipeline });
    ws.send(JSON.stringify({
      type: 'text_message', text: '', output_image_token: null,
      ref_tokens: null, ..._generationVoice('chat'), conv_id: activeConvId
    }));
    return;
  }

  // Intercept /image commands
  var imgMatch = text.match(/^\/image(?:\\s+(.+))?$/s);
  if (imgMatch) {
    $txtin.value = '';
    handleImageCommand(imgMatch[1] || '');
    return;
  }

  // Intercept /send commands
  var sendMatch = text.match(/^\/send\s+(user|assistant)\s+([\s\S]+)$/);
  if (sendMatch) {
    $txtin.value = '';
    var role = sendMatch[1];
    var msgText = sendMatch[2];
    addMsg(role === 'user' ? 'user' : 'sys', msgText);
    if (ws && ws.readyState === WebSocket.OPEN && activeConvId) {
      ws.send(JSON.stringify({
        type: 'send_as_role',
        conv_id: activeConvId,
        role: role,
        text: msgText
      }));
    }
    // Refresh server context so the LLM sees this message
    setTimeout(syncHistory, 100);
    return;
  }

  // Intercept /trigger — force LLM completion without adding a message
  if (text.trim() === '/trigger') {
    $txtin.value = '';
    $send.disabled = true;
    addLoading('sys', { onAbort: _abortPipeline });
    ws.send(JSON.stringify({
      type: 'text_message', text: '', output_image_token: null,
      ref_tokens: null, ..._generationVoice('chat'), conv_id: activeConvId
    }));
    return;
  }

  addMsg('user', text); $txtin.value = '';
  $send.disabled = true;
  ws.send(JSON.stringify({
    type: 'text_message',
    text: text,
    ..._generationVoice('chat'),
    conv_id: activeConvId
  }));
  addLoading('sys', { onAbort: _abortPipeline });
}
$send.addEventListener('click', sendText);
$txtin.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendText(); }
});

connectWS();
$txtin.focus();

// ═══════════════════════════════════════════════════════════════════
// ENCRYPTED SESSION STORAGE (server-side, no IndexedDB)
// ═══════════════════════════════════════════════════════════════════
// Long-term secret key (32 bytes, base64 in localStorage — never sent to server)
var s2sSecretKey = localStorage.getItem('s2s_crypt_key');
if (!s2sSecretKey) {
  var keyBytes = new Uint8Array(32);
  crypto.getRandomValues(keyBytes);
  s2sSecretKey = btoa(String.fromCharCode.apply(null, keyBytes));
  localStorage.setItem('s2s_crypt_key', s2sSecretKey);
}
// Per-session nonce (32 bytes) — derived session_key = secret XOR nonce
// Store nonces by session_uid so historical msgs can be decrypted locally
var s2sNonces = JSON.parse(localStorage.getItem('s2s_nonces') || '{}');
var s2sSessionUid = 'sess-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2,10);
var s2sNonceBytes = new Uint8Array(32);
crypto.getRandomValues(s2sNonceBytes);
var s2sNonceB64 = btoa(String.fromCharCode.apply(null, s2sNonceBytes));
s2sNonces[s2sSessionUid] = s2sNonceB64;
// Keep only last 100 session nonces (each ~44 chars base64, ~4KB total)
var entries = Object.keys(s2sNonces);
if (entries.length > 100) {
  var toDelete = entries.slice(0, entries.length - 100);
  for (var di = 0; di < toDelete.length; di++) delete s2sNonces[toDelete[di]];
}
localStorage.setItem('s2s_nonces', JSON.stringify(s2sNonces));
// Compute session_key = secret XOR nonce (server sees only this, never the secret)
function xorBytes(a, b) {
  var out = new Uint8Array(32);
  for (var i = 0; i < 32; i++) out[i] = a[i] ^ b[i];
  return out;
}
var _secretRaw = Uint8Array.from(atob(s2sSecretKey), function(c) { return c.charCodeAt(0); });
var _s2sSessionKeyRaw = xorBytes(_secretRaw, s2sNonceBytes);
var s2sSessionKeyB64 = btoa(String.fromCharCode.apply(null, _s2sSessionKeyRaw));
// Import the session key for Web Crypto API (cached)
var _s2sCryptoKey = null;
async function s2sImportKey() {
  if (_s2sCryptoKey) return _s2sCryptoKey;
  if (!window.crypto.subtle) throw new Error('Web Crypto API not available');
  _s2sCryptoKey = await crypto.subtle.importKey('raw', _s2sSessionKeyRaw, 'AES-GCM', false, ['encrypt', 'decrypt']);
  return _s2sCryptoKey;
}
function sendKey() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({type: 'set_key', session_uid: s2sSessionUid, session_key: s2sSessionKeyB64}));
  }
}
// Decrypt a server-returned message using the right session's nonce
async function decryptServerMsg(encryptedB64, convId, msgId, sessionUid) {
  // Recover session key from secret XOR the nonce stored for that session_uid
  var nonceB64 = s2sNonces[sessionUid];
  if (!nonceB64) throw new Error('No nonce for session ' + sessionUid);
  var nonceRaw = Uint8Array.from(atob(nonceB64), function(c) { return c.charCodeAt(0); });
  var sessionKey = xorBytes(_secretRaw, nonceRaw);
  if (!window.crypto.subtle) throw new Error('Web Crypto API not available (insecure context?)');
  var key = await crypto.subtle.importKey('raw', sessionKey, 'AES-GCM', false, ['decrypt']);
  var ct = Uint8Array.from(atob(encryptedB64), function(c) { return c.charCodeAt(0); });
  var enc = new TextEncoder();
  var ivHash = await crypto.subtle.digest('SHA-256', enc.encode(convId + ':' + msgId));
  var iv = new Uint8Array(ivHash).slice(0, 12);
  var aad = enc.encode(convId + ':' + msgId);
  var plain = await crypto.subtle.decrypt({name: 'AES-GCM', iv: iv, additionalData: aad}, key, ct);
  return new TextDecoder().decode(plain);
}
// ── Session state ─────────────────────────────────────────────────
let sessions = [];           // list of {id, name, created, msg_count} from server
let activeConvId = null;     // the active conversation UUID
// `history` is managed on the server — we just track conv metadata

function currentSession() {
  return sessions.find(function(s) { return s.id === activeConvId; }) || sessions[0] || null;
}

// Called when server sends conv_list
function onConvList(list) {
  var activeBefore = activeConvId;
  sessions = list;
  // If our active conv is still in the list, keep it; otherwise pick first
  if (activeBefore && sessions.find(function(s) { return s.id === activeBefore; })) {
    activeConvId = activeBefore;
  } else {
    activeConvId = sessions.length ? sessions[0].id : null;
  }
  renderSessionList();
  syncHistory();
}

async function saveCurrentChat() {
  // No-op: server auto-saves all messages during the pipeline.
  // We just persist _imgGen in localStorage for the current bubble state.

}

// ── Auto-sync image bubble param changes to conversation ──────────
function attachImgBubbleSync(bubbleId, params) {
  var el = document.getElementById(bubbleId);
  if (!el) return;
  var _patchTimer = null;
  function queuePatch() {
    if (_patchTimer) clearTimeout(_patchTimer);
    _patchTimer = setTimeout(function() {
      _patchTimer = null;
      if (el && el._imgMsgId && ws && ws.readyState === WebSocket.OPEN && activeConvId) {
        ws.send(JSON.stringify({
          type: 'patch_img_bubble',
          conv_id: activeConvId,
          msg_id: el._imgMsgId,
          params: {
            prompt: params.prompt,
            model: params.model,
            steps: params.steps,
            guidance: params.guidance,
            seed: params.seed,
            size: params.size,
          },
          assets: params.assets.map(function(a) {
            return { dataUrl: a.dataUrl, name: a.name, id: a.id, refToken: a.refToken };
          })
        }));
      }
    }, 300);
  }
  // Prompt: sync on blur
  var promptInput = document.getElementById(bubbleId + '-prompt');
  if (promptInput) promptInput.addEventListener('blur', queuePatch);
  // Model: sync on change
  var modelSelect = document.getElementById(bubbleId + '-model');
  if (modelSelect) modelSelect.addEventListener('change', queuePatch);
  // Size slider: sync on mouseup (drag end)
  var sizeSlider = el.querySelector('.img-size-slider');
  if (sizeSlider) sizeSlider.addEventListener('mouseup', queuePatch);
  // Manual size inputs: sync on blur
  var sizeInputs = el.querySelectorAll('.img-size-input');
  sizeInputs.forEach(function(inp) { inp.addEventListener('blur', queuePatch); });
  // Steps/guidance sliders: sync on mouseup
  var sliders = el.querySelectorAll('.img-ctrl-slider:not(.img-size-slider)');
  sliders.forEach(function(sl) { sl.addEventListener('mouseup', queuePatch); });
  // Steps/guidance steppy number inputs: sync on blur
  el.querySelectorAll('input[type="number"]').forEach(function(inp) {
    inp.addEventListener('blur', queuePatch);
  });
  // Ref image add/remove: observe .img-assets mutations
  var assetsWrap = el.querySelector('.img-assets');
  if (assetsWrap) {
    var obs = new MutationObserver(function() {
      // After a small delay so the assets array has settled
      setTimeout(queuePatch, 100);
    });
    obs.observe(assetsWrap, { childList: true, subtree: true });
  }
}

function patchCurrentImgBubble(params) {
  var el = document.getElementById(params._bubbleId);
  if (el && el._imgMsgId && ws && ws.readyState === WebSocket.OPEN && activeConvId) {
    ws.send(JSON.stringify({
      type: 'patch_img_bubble',
      conv_id: activeConvId,
      msg_id: el._imgMsgId,
      params: {
        prompt: params.prompt,
        model: params.model,
        steps: params.steps,
        guidance: params.guidance,
        seed: params.seed,
        size: params.size,
      },
      assets: params.assets.map(function(a) {
        return { dataUrl: a.dataUrl, name: a.name, id: a.id, refToken: a.refToken };
      })
    }));
  }
}

function restoreImageBubble(savedMsg) {
  console.log('restoreImageBubble called with:', savedMsg ? savedMsg.prompt : 'null', 'assets:', savedMsg ? (savedMsg.assets||[]).length : 0);
  // Build params from saved state
  var params = {
    prompt: savedMsg.prompt || '',
    model: savedMsg.model || 'flux2-lora-9b',
    steps: savedMsg.steps || 2,
    guidance: savedMsg.guidance || 3.5,
    seed: savedMsg.seed || null,
    size: savedMsg.size || '1024x1024',
    assets: (savedMsg.assets || []).map(function(a) {
      return { dataUrl: a.dataUrl, name: a.name, id: a.id, refToken: a.refToken || null };
    }),
    strength: 0.75,
    negative_prompt: savedMsg.negative_prompt || '',
    lora_paths: savedMsg.lora_paths || [],
  };
  var outputImageData = savedMsg.outputImageData || null;

  loadModels();

  var bubbleId = 'img-bubble-' + Date.now() + '-' + Math.random().toString(36).slice(2,6);


  // ── Build the bubble (mirrors handleImageCommand) ────────────────
  var d = document.createElement('div');
  d.className = 'msg img'; d.id = bubbleId;

  var lbl = document.createElement('div');
  lbl.className = 'lbl'; lbl.textContent = '\uD83C\uDFA8 Image Generation';

  var controls = document.createElement('div');
  controls.className = 'img-controls';

  var promptInput = document.createElement('textarea');
  promptInput.className = 'img-prompt-input';
  promptInput.id = bubbleId + '-prompt';
  promptInput.placeholder = 'Enter prompt\u2026';
  promptInput.value = params.prompt || '';
  promptInput.addEventListener('input', function() {
    params.prompt = promptInput.value;
  });
  controls.appendChild(promptInput);

  var modelWrap = document.createElement('div');
  modelWrap.className = 'img-model-wrap';
  var modelSelect = document.createElement('select');
  modelSelect.className = 'img-model-select';
  modelSelect.id = bubbleId + '-model';
  modelSelect.innerHTML = '<option>' + params.model + '</option>';
  modelSelect.addEventListener('change', function() {
    params.model = modelSelect.value;
    // Toggle SDXL-only controls (negative prompt, LoRA)
    var sdxlRows = d.querySelectorAll('.sdxl-only');
    var isSdxl = modelSelect.value === 'sdxl';
    for (var i = 0; i < sdxlRows.length; i++) {
      sdxlRows[i].style.display = isSdxl ? '' : 'none';
    }
    if (modelSelect.value.includes('edit')) {
      _preferredEditModel = modelSelect.value;
      localStorage.setItem('img_pref_edit', _preferredEditModel);
    } else {
      _preferredGenModel = modelSelect.value;
      localStorage.setItem('img_pref_gen', _preferredGenModel);
    }
    var capsSpan = document.getElementById(bubbleId + '-caps');
    if (capsSpan) capsSpan.textContent = _modelCaps[params.model] || inferModelCaps(params.model);
  });
  var capsSpan = document.createElement('span');
  capsSpan.className = 'img-model-caps';
  capsSpan.id = bubbleId + '-caps';
  capsSpan.textContent = _modelCaps[params.model] || inferModelCaps(params.model);
  modelWrap.appendChild(modelSelect);
  modelWrap.appendChild(capsSpan);
  controls.appendChild(modelWrap);

  // ── Interactive controls ─────────────────────────────────────────
  var ctrlDiv = document.createElement('div');
  ctrlDiv.className = 'img-params';

  // ── Size control ────────────────────────────────────────────────
  var sizeRow = document.createElement('div');
  sizeRow.className = 'img-ctrl-row';
  var sizeLbl = document.createElement('span');
  sizeLbl.className = 'img-ctrl-label'; sizeLbl.textContent = 'size';
  var sizeInner = document.createElement('div');
  sizeInner.className = 'img-size-inputs';
  function renderSizeControl() {
    sizeInner.innerHTML = '';
    if (params.assets.length && params.assets[0].dataUrl) {
      (function() {
        var img = new Image();
        img.onload = function() {
          var baseW = img.naturalWidth, baseH = img.naturalHeight;
          var slider = document.createElement('input');
          slider.type = 'range';
          slider.className = 'img-ctrl-slider img-size-slider';
          slider.min = '0.25'; slider.max = '2'; slider.step = '0.25';
          var curW = parseInt(params.size.split('x')[0], 10) || baseW;
          var curVal = Math.round((curW / baseW) * 100) / 100;
          slider.value = String(Math.max(0.25, Math.min(2, curVal)));
          var resultSpan = document.createElement('span');
          resultSpan.className = 'img-size-result';
          function updateSize() {
            var mult = parseFloat(slider.value);
            var nw = Math.round(baseW * mult);
            var nh = Math.round(baseH * mult);
            params.size = nw + 'x' + nh;
            resultSpan.textContent = nw + '\u00D7' + nh;
          }
          slider.addEventListener('input', updateSize);
          updateSize();
          var sliderRow = document.createElement('div');
          sliderRow.className = 'img-size-slider-row';
          sliderRow.appendChild(slider);
          sliderRow.appendChild(resultSpan);
          sizeInner.appendChild(sliderRow);
        };
        img.src = params.assets[0].dataUrl;
      })();
    } else {
      var parts = params.size.split('x');
      var w = parseInt(parts[0], 10) || 1024;
      var h = parseInt(parts[1], 10) || 1024;
      var wIn = document.createElement('input');
      wIn.type = 'number'; wIn.className = 'img-size-input';
      wIn.placeholder = 'w'; wIn.min = '64'; wIn.max = '4096'; wIn.step = '64';
      wIn.value = String(w);
      var times = document.createElement('span');
      times.className = 'img-size-times'; times.textContent = '\u00D7';
      var hIn = document.createElement('input');
      hIn.type = 'number'; hIn.className = 'img-size-input';
      hIn.placeholder = 'h'; hIn.min = '64'; hIn.max = '4096'; hIn.step = '64';
      hIn.value = String(h);
      function updateSizeManual() {
        var nw = parseInt(wIn.value, 10) || 1024;
        var nh = parseInt(hIn.value, 10) || 1024;
        nw = Math.max(64, Math.min(4096, nw));
        nh = Math.max(64, Math.min(4096, nh));
        params.size = nw + 'x' + nh;
      }
      wIn.addEventListener('input', updateSizeManual);
      hIn.addEventListener('input', updateSizeManual);
      sizeInner.appendChild(wIn);
      sizeInner.appendChild(times);
      sizeInner.appendChild(hIn);
    }
  }
  sizeRow.appendChild(sizeLbl);
  sizeRow.appendChild(sizeInner);
  ctrlDiv.appendChild(sizeRow);
  renderSizeControl();

  // ── Guidance ────────────────────────────────────────────────────
  var guideRow = document.createElement('div');
  guideRow.className = 'img-ctrl-row';
  var guideLbl = document.createElement('span');
  guideLbl.className = 'img-ctrl-label'; guideLbl.textContent = 'guidance';
  var guideSlider = document.createElement('input');
  guideSlider.type = 'range'; guideSlider.className = 'img-ctrl-slider';
  guideSlider.min = '1'; guideSlider.max = '20'; guideSlider.step = '0.5';
  guideSlider.value = String(params.guidance);
  var guideVal = document.createElement('span');
  guideVal.className = 'img-ctrl-value'; guideVal.textContent = String(params.guidance);
  guideSlider.addEventListener('input', function() {
    params.guidance = parseFloat(guideSlider.value);
    guideVal.textContent = String(params.guidance);
  });
  guideRow.appendChild(guideLbl);
  guideRow.appendChild(guideSlider);
  guideRow.appendChild(guideVal);
  ctrlDiv.appendChild(guideRow);

  // ── Steps ───────────────────────────────────────────────────────
  var stepsRow = document.createElement('div');
  stepsRow.className = 'img-ctrl-row';
  var stepsLbl = document.createElement('span');
  stepsLbl.className = 'img-ctrl-label'; stepsLbl.textContent = 'steps';
  var stepsSlider = document.createElement('input');
  stepsSlider.type = 'range'; stepsSlider.className = 'img-ctrl-slider';
  stepsSlider.min = '2'; stepsSlider.max = '10'; stepsSlider.step = '1';
  stepsSlider.value = String(params.steps);
  var stepsVal = document.createElement('span');
  stepsVal.className = 'img-ctrl-value'; stepsVal.textContent = String(params.steps);
  stepsSlider.addEventListener('input', function() {
    params.steps = parseInt(stepsSlider.value, 10);
    stepsVal.textContent = String(params.steps);
  });
  stepsRow.appendChild(stepsLbl);
  stepsRow.appendChild(stepsSlider);
  stepsRow.appendChild(stepsVal);
  ctrlDiv.appendChild(stepsRow);

  // ── Seed control ──────────────────────────────────────────────
  var seedRow = document.createElement('div');
  seedRow.className = 'img-ctrl-row';
  var seedLbl = document.createElement('span');
  seedLbl.className = 'img-ctrl-label'; seedLbl.textContent = 'seed';
  var seedMinus = document.createElement('button');
  seedMinus.textContent = '-'; seedMinus.style.cssText = 'width:28px;flex-shrink:0;border:1px solid var(--border);border-radius:4px;cursor:pointer;background:var(--surface2);color:var(--text)';
  seedMinus.addEventListener('click', function() { if (params.seed == null) params.seed = 42; params.seed = ((params.seed - 12345) * 4005161829) >>> 0; var _sv = document.getElementById(bubbleId).querySelector('.img-ctrl-seed'); if (_sv) _sv.value = String(params.seed); });
  var seedVal = document.createElement('input');
  seedVal.type = 'text'; seedVal.className = 'img-ctrl-seed'; seedVal.style.cssText = 'flex:1;text-align:center;font-family:monospace;border:1px solid var(--border);border-radius:4px;background:var(--surface2);color:var(--text);padding:2px 6px';
  seedVal.value = params.seed == null ? '' : String(params.seed); seedVal.placeholder = 'random';
  seedVal.addEventListener('input', function() { var v = seedVal.value.trim(); params.seed = v === '' ? null : parseInt(v, 10); });
  var seedPlus = document.createElement('button');
  seedPlus.textContent = '+'; seedPlus.style.cssText = 'width:28px;flex-shrink:0;border:1px solid var(--border);border-radius:4px;cursor:pointer;background:var(--surface2);color:var(--text)';
  seedPlus.addEventListener('click', function() { if (params.seed == null) params.seed = 42; params.seed = (1103515245 * params.seed + 12345) >>> 0; var _sv = document.getElementById(bubbleId).querySelector('.img-ctrl-seed'); if (_sv) _sv.value = String(params.seed); });
  seedRow.appendChild(seedLbl);
  seedRow.appendChild(seedMinus);
  seedRow.appendChild(seedVal);
  seedRow.appendChild(seedPlus);
  ctrlDiv.appendChild(seedRow);

  // ── Negative prompt (SDXL only) ─────────────────────────────
  var negRow = document.createElement('div');
  negRow.className = 'img-ctrl-row sdxl-only';
  negRow.style.display = 'none';
  var negLbl = document.createElement('span');
  negLbl.className = 'img-ctrl-label'; negLbl.textContent = 'negative';
  var negInput = document.createElement('textarea');
  negInput.className = 'img-neg-input';
  negInput.placeholder = 'Negative prompt\u2026';
  negInput.style.flex = '1'; negInput.style.resize = 'none'; negInput.rows = 2;
  negInput.value = params.negative_prompt;
  negInput.addEventListener('input', function() { params.negative_prompt = negInput.value; });
  negRow.appendChild(negLbl);
  negRow.appendChild(negInput);
  ctrlDiv.appendChild(negRow);

  // ── LoRA button (SDXL only) ───────────────────────────────────
  var loraBtn = document.createElement('button');
  loraBtn.className = 'lora-toggle-btn sdxl-only';
  loraBtn.textContent = 'LoRAs';
  loraBtn.style.display = 'none';
  loraBtn.addEventListener('click', function() { showLoraPanel(params, bubbleId); });
  ctrlDiv.appendChild(loraBtn);

  controls.appendChild(ctrlDiv);

  // Show SDXL controls if model is already sdxl
  if (params.model === 'sdxl') {
    var sdxlRows = d.querySelectorAll('.sdxl-only');
    for (var i = 0; i < sdxlRows.length; i++) {
      sdxlRows[i].style.display = '';
    }
  }

  // ── Reference image assets ──────────────────────────────────────
  var assetsWrap = document.createElement('div');
  assetsWrap.className = 'img-assets';
  assetsWrap.id = bubbleId + '-assets';
  function renderAssets() {
    assetsWrap.innerHTML = '';
    params.assets.forEach(function(a, idx) {
      var thumb = document.createElement('div');
      thumb.className = 'img-asset-thumb';
      var img = document.createElement('img');
      img.src = a.dataUrl;
      img.alt = a.name;
      var delBtn = document.createElement('button');
      delBtn.className = 'img-asset-del';
      delBtn.textContent = '\u2715';
      delBtn.addEventListener('click', function() {
        params.assets.splice(idx, 1);
        renderAssets();
        renderSizeControl();
        if (!params.assets.length) switchToGenModel(bubbleId);
      });
      thumb.appendChild(img);
      thumb.appendChild(delBtn);
      assetsWrap.appendChild(thumb);
    });
    var addBtn = document.createElement('button');
    addBtn.className = 'img-file-btn';
    addBtn.textContent = '+';
    addBtn.addEventListener('click', function() { fileInput.click(); });
    assetsWrap.appendChild(addBtn);
  }
  var fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = 'image/*';
  fileInput.style.display = 'none';
  fileInput.id = bubbleId + '-fileinput';
  fileInput.addEventListener('change', async function() {
    var files = fileInput.files;
    if (!files.length) return;
    var hadAssets = params.assets.length > 0;
    var _pendingGenParams = null;
    for (var fi = 0; fi < files.length; fi++) {
      var f = files[fi];
      var dataUrl = await readFileAsDataURL(f);
      var assetId = 'asset-' + Math.random().toString(36).slice(2,8);
      params.assets.push({ dataUrl: dataUrl, name: f.name, id: assetId });
      try {
        var fd = new FormData();
        fd.append('image', du2blob(dataUrl), f.name || 'ref.png');
        var uploadResp = await fetch('/api/images/upload-ref', { method: 'POST', body: fd });
        if (uploadResp.ok) {
          var uploadData = await uploadResp.json();
          if (uploadData.token) params.assets[params.assets.length - 1].refToken = uploadData.token;
          if (!hadAssets && uploadData.gen_params) {
            _pendingGenParams = uploadData.gen_params;
          }
        }
      } catch (_) {}
    }
    fileInput.value = '';
    // Restore gen_params from uploaded image metadata
    if (!hadAssets && _pendingGenParams) {
      var _s = typeof _pendingGenParams === 'string' ? JSON.parse(_pendingGenParams) : _pendingGenParams;
      var _bubble = document.getElementById(bubbleId);
      if (_s.prompt) { params.prompt = _s.prompt; var _pi = document.getElementById(bubbleId + '-prompt'); if (_pi) _pi.value = _s.prompt; }
      if (_s.guidance != null) { params.guidance = _s.guidance; var _sl = _bubble.querySelectorAll('.img-ctrl-slider'); if (_sl[0]) _sl[0].value = String(_s.guidance); var _vl = _bubble.querySelectorAll('.img-ctrl-value'); if (_vl[0]) _vl[0].textContent = String(_s.guidance); }
      if (_s.seed != null) { params.seed = _s.seed; var _sv = _bubble.querySelector('.img-ctrl-seed'); if (_sv) _sv.value = String(_s.seed); }
      if (_s.steps != null) { params.steps = _s.steps; var _sl2 = _bubble.querySelectorAll('.img-ctrl-slider'); if (_sl2[1]) _sl2[1].value = String(_s.steps); var _vl2 = _bubble.querySelectorAll('.img-ctrl-value'); if (_vl2[1]) _vl2[1].textContent = String(_s.steps); }
      if (_s.model) { params.model = _s.model; var _ms = document.getElementById(bubbleId + '-model'); if (_ms) _ms.value = _s.model; }
      if (_s.negative_prompt) { params.negative_prompt = _s.negative_prompt; var _ni = _bubble.querySelector('.img-neg-input'); if (_ni) _ni.value = _s.negative_prompt; }
      if (_s.size) params.size = _s.size;
      if (_s.lora_paths) params.lora_paths = _s.lora_paths;
      if (_s.loras) params.loras = _s.loras;
    }
    // Set size to first ref image's natural dimensions when first attached
    if (!hadAssets && params.assets.length && params.assets[0].dataUrl && !_pendingGenParams) {
      try {
        var dims = await new Promise(function(resolve, reject) {
          var img = new Image();
          img.onload = function() { resolve({ w: img.naturalWidth, h: img.naturalHeight }); };
          img.onerror = reject;
          img.src = params.assets[0].dataUrl;
        });
        params.size = dims.w + 'x' + dims.h;
      } catch (_) {}
    }
    renderAssets();
    renderSizeControl();
    // Auto-switch to edit model when first reference added (skip if gen_params restored)
    if (!hadAssets && params.assets.length && !_pendingGenParams) switchToEditModel(bubbleId);
  });
  renderAssets();
  controls.appendChild(assetsWrap);
  controls.appendChild(fileInput);

  // ── Generate button ─────────────────────────────────────────────
  var genBtnWrap = document.createElement('div');
  genBtnWrap.className = 'img-gen-btn-wrap';
  var genBtn = document.createElement('button');
  genBtn.className = 'img-gen-btn';
  genBtn.id = bubbleId + '-genbtn';
  genBtn.textContent = '\u25B6 Generate';
  genBtn.addEventListener('click', function() { generateImage(params, bubbleId); });
  genBtnWrap.appendChild(genBtn);
  controls.appendChild(genBtnWrap);

  // ── Output side ─────────────────────────────────────────────────
  var output = document.createElement('div');
  output.className = 'img-output';
  var placeholder = document.createElement('div');
  placeholder.className = 'img-placeholder';
  placeholder.id = bubbleId + '-placeholder';
  placeholder.textContent = outputImageData ? '' : (savedMsg.hasOutput ? 'Previously generated — click Generate to regenerate' : 'Click Generate');
  if (outputImageData) placeholder.style.display = 'none';
  var imgEl = document.createElement('img');
  imgEl.className = 'img-result';
  imgEl.id = bubbleId + '-img';
  imgEl.alt = 'Generated image';
  imgEl.style.display = outputImageData ? 'block' : 'none';
  if (outputImageData) {
    imgEl.src = 'data:image/png;base64,' + outputImageData;
  }
  output.appendChild(imgEl);
  output.appendChild(placeholder);

  var body = document.createElement('div');
  body.className = 'img-body';
  body.appendChild(controls);
  body.appendChild(output);

  d.appendChild(lbl);
  d.appendChild(body);
  addMsgBar(d, { canRegen: false, ts: new Date().toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' }) });
  d._imgMsgId = savedMsg.msg_id || null;
  d._proxyToken = savedMsg.proxyToken || null;
  d._imgParams = {
    prompt: params.prompt,
    model: params.model,
    steps: params.steps,
    guidance: params.guidance,
    seed: params.seed,
    size: params.size,
    assets: params.assets.map(function(a) {
      return { dataUrl: a.dataUrl, name: a.name, id: a.id, refToken: a.refToken };
    }).filter(function(a) { return a.dataUrl; }),
    outputImageData: outputImageData,
    gen_params: params.gen_params || null,
  };
  $chat.appendChild(d);
  $chat.scrollTop = $chat.scrollHeight;

  // Populate model dropdown once models load
  loadModels().then(function(models) {
    var sel = document.getElementById(bubbleId + '-model');
    if (!sel) return;
    sel.innerHTML = '';
    models.forEach(function(m) {
      var opt = document.createElement('option');
      opt.value = m;
      opt.textContent = m;
      if (m === params.model) opt.selected = true;
      sel.appendChild(opt);
    });
    var capsSpan2 = document.getElementById(bubbleId + '-caps');
    if (capsSpan2) capsSpan2.textContent = _modelCaps[params.model] || inferModelCaps(params.model);
  });


  // Wire param change auto-sync
  attachImgBubbleSync(bubbleId, params);
}

function restoreChatToDOM(session) {
  // Clear chat
  $chat.innerHTML = '';
  streamBubbles.clear();
  // Rebuild messages in order
  session.msgs.forEach(function(m) {
    if (m.type === 'img') {
      restoreImageBubble(m);
    } else {
      addMsg(m.role === 'user' ? 'user' : 'sys', m.text, m.msg_id);
    }
  });
  // Restore image gen context from localStorage
  if (activeConvId) {
    try {
      var saved = JSON.parse(localStorage.getItem('s2s_imggen_' + activeConvId));
      if (saved) {

      }
    } catch (_) {}
  }
}

async function switchSession(id) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  await saveCurrentChat();
  ws.send(JSON.stringify({type: 'load_conv', conv_id: id}));
  closeSidebar();
}

async function newSession() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  await saveCurrentChat();
  ws.send(JSON.stringify({type: 'new_conv'}));
}

async function deleteSession(id) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (sessions.length <= 1) return;
  ws.send(JSON.stringify({type: 'delete_conv', conv_id: id}));
}

async function renameSession(id) {
  var sess = sessions.find(function(s) { return s.id === id; });
  if (!sess || !ws || ws.readyState !== WebSocket.OPEN) return;
  var newName = prompt('Rename conversation:', sess.name);
  if (newName && newName.trim()) {
    ws.send(JSON.stringify({type: 'rename_conv', conv_id: id, name: newName.trim()}));
  }
}

function renderSessionList() {
  var list = document.getElementById('sidebar-list');
  if (!list) return;
  list.innerHTML = '';

  var newBtn = document.createElement('button');
  newBtn.className = 'sess-new';
  newBtn.textContent = '+ New Conversation';
  newBtn.addEventListener('click', newSession);
  list.appendChild(newBtn);

  // Show oldest first
  var sorted = [].concat(sessions).sort(function(a, b) { return a.created - b.created; });
  sorted.forEach(function(s) {
    var item = document.createElement('div');
    item.className = 'sess-item' + (s.id === activeConvId ? ' active' : '');

    var name = document.createElement('span');
    name.className = 'sess-name';
    name.textContent = s.name;
    name.addEventListener('click', function() { switchSession(s.id); });

    var renameBtn = document.createElement('button');
    renameBtn.className = 'sess-btn';
    renameBtn.textContent = '\u270E';
    renameBtn.title = 'Rename';
    renameBtn.addEventListener('click', function(e) { e.stopPropagation(); renameSession(s.id); });

    var delBtn = document.createElement('button');
    delBtn.className = 'sess-btn del';
    delBtn.textContent = '\u2715';
    delBtn.title = 'Delete';
    delBtn.addEventListener('click', function(e) { e.stopPropagation(); deleteSession(s.id); });

    item.appendChild(name);
    item.appendChild(renameBtn);
    item.appendChild(delBtn);
    list.appendChild(item);
  });
}

function openSidebar() {
  document.getElementById('sidebar').classList.add('open');
  document.getElementById('sidebar-overlay').classList.add('open');
  renderSessionList();
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebar-overlay').classList.remove('open');
}

// ── Set server history from current DOM ────────────────────────────
function syncHistory() {
  // Rebuild full history from DOM and send to server for LLM context
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  var h = [{ role: 'system', content: 'You are a helpful, concise voice assistant.' }];
  var el = $chat.firstChild;
  while (el) {
    if (el.classList && el.classList.contains('msg')) {
      if (el.classList.contains('img')) {
        // Image bubble — send text summary
        if (el._imgParams) {
          var bp = el._imgParams;
          var ctx = '=== Image Generation Context ===\n';
          ctx += 'Prompt: ' + (bp.prompt || '(empty)') + '\n';
          ctx += 'Model: ' + bp.model + '\n';
          ctx += 'Status: ' + (bp.outputImageData ? 'Generated' : 'Not yet generated') + '\n';
          h.push({role: 'assistant', content: ctx});
        }
      } else if (el.getAttribute('data-hidden') !== 'true') {
        var role = '';
        if (el.classList.contains('user')) role = 'user';
        else if (el.classList.contains('sys')) role = 'assistant';
        if (role) {
          var txtEl = el.querySelector('.txt');
          if (txtEl && txtEl.textContent) {
            h.push({role: role, content: txtEl.textContent});
          }
        }
      }
    }
    el = el.nextSibling;
  }
  ws.send(JSON.stringify({type: 'set_conv_history', conv_id: activeConvId, messages: h}));
}

// ── Wire sidebar controls ──────────────────────────────────────────
document.getElementById('hamburger').addEventListener('click', openSidebar);
document.getElementById('sidebar-close').addEventListener('click', closeSidebar);
document.getElementById('sidebar-overlay').addEventListener('click', closeSidebar);

// Auto-save after each new message (hook into addMsg)
var _origAddMsg = addMsg;
addMsg = function(role, text) {
  var r = _origAddMsg.apply(this, arguments);
  saveCurrentChat().catch(function() {});
  renderSessionList();
  return r;
};

// Initialize sessions — server sends conv_list on key set, client renders on receipt
(function() {
  // If already connected, send key now
  if (ws && ws.readyState === WebSocket.OPEN) sendKey();
})();

// ═══════════════════════════════════════════════════════════════════
// TTS
// ═══════════════════════════════════════════════════════════════════
const $ttsText   = document.getElementById('tts-text');
const $ttsGen    = document.getElementById('tts-gen');
const $ttsAdd    = document.getElementById('tts-add');
const $ttsQList  = document.getElementById('tts-qlist');
const $ttsEmpty  = document.getElementById('tts-empty');
const $ttsNP     = document.getElementById('tts-nowplay');
const $npTxt     = document.getElementById('np-txt');
const $npAudio   = document.getElementById('np-audio');

// ─ Image generation config ──────────────────────────────────────────
const IMG_BASE = 'http://127.0.0.1:8080';

const TTS_STORE  = 'tts_q_v3';
const POLL_MS    = 2000;
let ttsQueue = [];
let nextId   = 0;

// ─ Single shared audio player ─────────────────────────────────────
// Only one audio source (config preview OR tts now-playing) plays at a time.
let _activeAudio   = null;  // HTMLAudioElement currently playing
let _activePlayBtn = null;  // button that should show pause icon

function stopActiveAudio() {
  if (_activeAudio) {
    _activeAudio.pause();
    _activeAudio.currentTime = 0;
    _activeAudio = null;
  }
  if (_activePlayBtn) {
    _activePlayBtn.textContent = '\u25B6';
    _activePlayBtn.title = 'Preview clip';
    _activePlayBtn = null;
  }
}

function playConfigClip(name, btn) {
  // Toggle: clicking the active button stops it
  if (_activeAudio && _activePlayBtn === btn) {
    stopActiveAudio();
    return;
  }
  stopActiveAudio();
  // Also pause the TTS now-playing bar if it's running
  if (!$npAudio.paused) $npAudio.pause();

  const audio = new Audio('/tts/config/wav/' + encodeURIComponent(name));
  _activeAudio   = audio;
  _activePlayBtn = btn;
  btn.textContent = '\u23F9';
  btn.title = 'Stop';

  const cleanup = () => { if (_activeAudio === audio) stopActiveAudio(); };
  audio.addEventListener('ended', cleanup);
  audio.addEventListener('error', cleanup);

  audio.play().catch(err => { cleanup(); alert('Playback failed: ' + err.message); });
}

// restore in-flight jobs from localStorage
(function restoreQueue() {
  try {
    const saved = JSON.parse(localStorage.getItem(TTS_STORE) || '[]');
    saved.forEach(t => {
      if (t.token && (t.status === 'pending' || t.status === 'processing')) {
        const item = { id: nextId++, text: t.text, token: t.token,
                       status: t.status, profile: t.profile || '', model: t.model || '', blob: null, url: null, err: null };
        ttsQueue.push(item);
        setTimeout(() => pollTTS(item), 500);
      }
    });
    renderTTS();
  } catch(e) {}
})();

function saveTTS() {
  const live = ttsQueue
    .filter(q => q.token && (q.status === 'pending' || q.status === 'processing'))
    .map(q => ({ token: q.token, text: q.text, status: q.status, profile: q.profile, model: q.model }));
  localStorage.setItem(TTS_STORE, JSON.stringify(live));
}

function addToTTS(text, autoPlay) {
  const lines = text.split('\n').filter(l => l.trim());
  if (!lines.length) return;
  const voice = _generationVoice('tts');
  const newItems = lines.map(t => ({
    id: nextId++, text: t.trim(), token: null, profile: voice.profile, model: voice.tts_model,
    status: 'pending', blob: null, url: null, err: null,
    _autoplay: autoPlay
  }));
  ttsQueue.push(...newItems);
  renderTTS();
  newItems.forEach(item => submitTTS(item));
}

async function submitTTS(item) {
  try {
    const fd = new FormData();
    fd.append('text', item.text);
    fd.append('ref', item.profile || '');
    fd.append('model', item.model || '');
    const r = await fetch('/tts/submit', { method: 'POST', body: fd });
    if (!r.ok) throw new Error(r.statusText);
    const d = await r.json();
    item.token = d.token; item.status = 'pending';
    saveTTS(); renderTTS(); pollTTS(item);
  } catch(e) {
    item.status = 'error'; item.err = e.message; renderTTS();
  }
}

async function pollTTS(item) {
  if (!item.token || item.status === 'done' || item.status === 'error') return;
  try {
    const r = await fetch('/tts/status/' + item.token);
    if (!r.ok) throw new Error('poll failed');
    const d = await r.json();
    item.status = d.status; saveTTS();
    if (d.status === 'done' && d.audio) {
      const bytes = Uint8Array.from(atob(d.audio), c => c.charCodeAt(0));
      item.blob = new Blob([bytes], { type: 'audio/wav' });
      item.url  = URL.createObjectURL(item.blob);
      if (item._autoplay) { playTTS(item); item._autoplay = false; }
    }
    if (d.status === 'error') item.err = d.error || 'Error';
    renderTTS();
    if (d.status !== 'done' && d.status !== 'error') {
      // Back off if the job status keeps repeating (stuck queue) so N queued
      // items don't turn into a request storm.
      item._stall = (d.status === item._lastStatus) ? (item._stall || 0) + 1 : 0;
      item._lastStatus = d.status;
      setTimeout(() => pollTTS(item), item._stall > 10 ? 5000 : POLL_MS);
    }
  } catch(e) {
    item.status = 'error'; item.err = e.message; renderTTS();
  }
}

function playTTS(item) {
  if (!item.url) return;
  stopActiveAudio();  // stop any config preview
  $npAudio.src = item.url;
  $npTxt.textContent = item.text;
  $ttsNP.classList.add('visible');
  $npAudio.play().catch(() => {});
}

// If user hits play on the native audio bar, stop any config preview
$npAudio.addEventListener('play', () => stopActiveAudio());

function renderTTS() {
  // Remove existing qi rows
  $ttsQList.querySelectorAll('.qi').forEach(el => el.remove());
  const hasItems = ttsQueue.length > 0;
  $ttsEmpty.style.display = hasItems ? 'none' : '';

  ttsQueue.forEach((item, idx) => {
    const div = document.createElement('div');
    div.className = 'qi' + (item.status === 'error' ? ' qi-error' : '');

    // status dot
    const dot = document.createElement('div');
    dot.className = 'qi-dot ' + item.status;

    // badge
    const badge = document.createElement('div');
    badge.className = 'qi-badge ' + item.status;
    badge.textContent = { pending:'queued', processing:'gen\u2026', done:'done', error:'fail' }[item.status] || item.status;

    // text
    const txt = document.createElement('div');
    txt.className = 'qi-text'; txt.title = item.text; txt.textContent = item.text;

    // actions
    const acts = document.createElement('div');
    acts.className = 'qi-actions';

    if (item.status === 'done' && item.url) {
      const playBtn = document.createElement('button');
      playBtn.className = 'qi-btn'; playBtn.textContent = '\u25B6'; playBtn.title = 'Play';
      playBtn.addEventListener('click', e => { e.stopPropagation(); playTTS(item); });
      const dlA = document.createElement('a');
      dlA.className = 'qi-btn'; dlA.href = item.url;
      dlA.download = 'tts-' + (idx + 1) + '.wav';
      dlA.textContent = '\u2193'; dlA.title = 'Download';
      dlA.addEventListener('click', e => e.stopPropagation());
      acts.appendChild(playBtn); acts.appendChild(dlA);
      div.style.cursor = 'pointer';
      div.addEventListener('click', () => playTTS(item));
    } else if (item.status === 'error') {
      const em = document.createElement('span');
      em.className = 'qi-errmsg'; em.textContent = item.err || 'error'; em.title = item.err;
      acts.appendChild(em);
    }

    div.appendChild(dot); div.appendChild(badge); div.appendChild(txt); div.appendChild(acts);
    $ttsQList.appendChild(div);
  });
}

$ttsGen.addEventListener('click', () => {
  const t = $ttsText.value.trim(); if (!t) return;
  $ttsText.value = ''; addToTTS(t, true);
});
$ttsAdd.addEventListener('click', () => {
  const t = $ttsText.value.trim(); if (!t) return;
  $ttsText.value = ''; addToTTS(t, false);
});
$ttsText.addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $ttsGen.click(); }
});

// ═══════════════════════════════════════════════════════════════════
// CONFIG
// ═══════════════════════════════════════════════════════════════════
const $cfgStatus   = document.getElementById('cfg-status');
const $cfgReload   = document.getElementById('cfg-reload');
const $clipList    = document.getElementById('clip-list');
const $rebuildBtn  = document.getElementById('rebuild-btn');
const $rebuildStat = document.getElementById('rebuild-status');
let clips = [];
let clipsLoaded = false;
let dragSrcIdx = null;

$cfgReload.addEventListener('click', loadClips);

async function loadClips() {
  $cfgStatus.textContent = 'Loading\u2026';
  try {
    const r = await fetch('/tts/config/list');
    const d = await r.json();
    const lastUsed = new Set(d.last_used || []);
    clips = (d.clips || []).map(c => ({ ...c, selected: lastUsed.has(c.name) }));
    clipsLoaded = true;
    $cfgStatus.textContent = clips.length + ' clip' + (clips.length !== 1 ? 's' : '');
    renderClips();
    applyProfileClips();
  } catch(e) {
    $cfgStatus.textContent = 'Load failed';
  }
}

function renderClips() {
  _openEditRow = null; _openEditBtn = null; _openEditClipDiv = null;
  $clipList.innerHTML = '';
  if (!clips.length) {
    $clipList.innerHTML = '<div style="color:var(--dim);font-size:13px;padding:24px;text-align:center">No .wav/.txt pairs found</div>';
    updateRebuild(); return;
  }
  clips.forEach((clip, idx) => {
    const div = document.createElement('div');
    div.className = 'clip-item'; div.draggable = true; div.dataset.idx = idx;

    // drag handle
    const handle = document.createElement('span');
    handle.className = 'clip-handle'; handle.textContent = '\u283F'; handle.title = 'Drag to reorder';

    // checkbox
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.className = 'clip-cb'; cb.checked = clip.selected;
    cb.addEventListener('change', () => { clips[idx].selected = cb.checked; updateRebuild(); _saveCurrentClipsToProfile(); });

    // name
    const name = document.createElement('span');
    name.className = 'clip-name'; name.textContent = clip.name;

    // up/down arrows
    const arrows = document.createElement('div');
    arrows.className = 'clip-arrows';
    const up = document.createElement('button');
    up.className = 'clip-arr'; up.textContent = '\u2191'; up.title = 'Move up';
    up.disabled = idx === 0;
    up.addEventListener('click', () => moveClip(idx, -1));
    const dn = document.createElement('button');
    dn.className = 'clip-arr'; dn.textContent = '\u2193'; dn.title = 'Move down';
    dn.disabled = idx === clips.length - 1;
    dn.addEventListener('click', () => moveClip(idx, 1));
    arrows.appendChild(up); arrows.appendChild(dn);

    div.appendChild(handle); div.appendChild(cb);
    div.appendChild(name); div.appendChild(arrows);

    // info + play action buttons
    const acts = document.createElement('div');
    acts.className = 'clip-acts';

    const editTxtBtn = document.createElement('button');
    editTxtBtn.className = 'clip-act'; editTxtBtn.textContent = '\u270F'; editTxtBtn.title = 'Edit transcription';
    editTxtBtn.addEventListener('click', e => {
      e.stopPropagation();
      openClipEdit(clip, div, editTxtBtn);
    });

    const playBtn = document.createElement('button');
    playBtn.className = 'clip-act'; playBtn.textContent = '\u25B6'; playBtn.title = 'Preview clip';
    playBtn.addEventListener('click', e => {
      e.stopPropagation();
      playConfigClip(clip.name, playBtn);
    });

    const dupBtn = document.createElement('button');
    dupBtn.className = 'clip-act'; dupBtn.textContent = '⧉'; dupBtn.title = 'Duplicate clip';
    dupBtn.addEventListener('click', async e => {
      e.stopPropagation();
      dupBtn.disabled = true; dupBtn.style.opacity = '0.4';
      try {
        const r = await fetch('/tts/config/duplicate-clip', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: clip.name }),
        });
        if (!r.ok) throw new Error(await r.text());
        const d = await r.json();
        clips.splice(idx + 1, 0, { name: d.name, txt_content: d.txt_content, selected: false });
        renderClips();
      } catch(e) {
        alert('Duplicate failed: ' + e.message);
        dupBtn.disabled = false; dupBtn.style.opacity = '';
      }
    });

    acts.appendChild(editTxtBtn); acts.appendChild(dupBtn); acts.appendChild(playBtn);
    div.appendChild(acts);

    // drag & drop
    div.addEventListener('dragstart', e => {
      dragSrcIdx = idx; div.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
    });
    div.addEventListener('dragend', () => div.classList.remove('dragging'));
    div.addEventListener('dragover', e => {
      e.preventDefault(); e.dataTransfer.dropEffect = 'move';
      document.querySelectorAll('.clip-item').forEach(el => el.classList.remove('drag-over'));
      div.classList.add('drag-over');
    });
    div.addEventListener('dragleave', () => div.classList.remove('drag-over'));
    div.addEventListener('drop', e => {
      e.preventDefault(); div.classList.remove('drag-over');
      if (dragSrcIdx !== null && dragSrcIdx !== idx) {
        const [moved] = clips.splice(dragSrcIdx, 1);
        clips.splice(idx, 0, moved);
        renderClips();
      }
    });

    $clipList.appendChild(div);
  });
  updateRebuild();
}

function moveClip(idx, dir) {
  const ni = idx + dir;
  if (ni < 0 || ni >= clips.length) return;
  [clips[idx], clips[ni]] = [clips[ni], clips[idx]];
  renderClips();
}

function updateRebuild() {
  const sel = clips.filter(c => c.selected).length;
  $rebuildBtn.disabled = sel === 0;
  $rebuildBtn.textContent = sel
    ? 'Regenerate Reference (' + sel + ' selected)'
    : 'Regenerate Reference';
}


let _openEditRow = null, _openEditBtn = null, _openEditClipDiv = null;

function closeClipEdit() {
  if (_openEditRow) { _openEditRow.remove(); _openEditRow = null; }
  if (_openEditClipDiv) {
    _openEditClipDiv.style.borderRadius = '';
    _openEditClipDiv.style.marginBottom = '';
    _openEditClipDiv.style.borderColor  = '';
    _openEditClipDiv = null;
  }
  if (_openEditBtn) {
    _openEditBtn.style.color       = '';
    _openEditBtn.style.borderColor = '';
    _openEditBtn = null;
  }
}

function openClipEdit(clip, clipDiv, editBtn) {
  // Toggle
  if (_openEditBtn === editBtn) { closeClipEdit(); return; }
  closeClipEdit();

  _openEditBtn     = editBtn;
  _openEditClipDiv = clipDiv;
  editBtn.style.color       = 'var(--accent)';
  editBtn.style.borderColor = 'var(--accent)';
  clipDiv.style.borderRadius = '8px 8px 0 0';
  clipDiv.style.marginBottom = '0';
  clipDiv.style.borderColor  = 'rgba(108,92,231,.3)';

  const row = document.createElement('div');
  row.className = 'clip-edit-row';
  _openEditRow = row;

  // Wand: re-transcribe from saved WAV
  const wandBtn = document.createElement('button');
  wandBtn.className = 'seg-wand';
  wandBtn.textContent = '\u{1FA84}';
  wandBtn.title = 'Re-transcribe from audio';

  // Textarea — same class as segment editor for visual consistency
  const ta = document.createElement('textarea');
  ta.className = 'seg-ta clip-edit-ta';
  ta.value       = clip.txt_content || '';
  ta.placeholder = 'Transcription\u2026';

  // Action buttons (column, right side)
  const actCol = document.createElement('div');
  actCol.className = 'clip-edit-actions';

  const saveBtn = document.createElement('button');
  saveBtn.className = 'profile-action-btn ok';
  saveBtn.textContent = '\u2713'; saveBtn.title = 'Save';

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'profile-action-btn cancel';
  cancelBtn.textContent = '\u2715'; cancelBtn.title = 'Discard';

  actCol.appendChild(saveBtn); actCol.appendChild(cancelBtn);

  // Wand logic
  let wandAbort = null;
  wandBtn.addEventListener('click', async () => {
    if (wandAbort) {
      wandAbort.abort(); wandAbort = null;
      wandBtn.textContent = '\u{1FA84}'; wandBtn.classList.remove('cancelling');
      ta.disabled = false; ta.style.opacity = '';
      return;
    }
    wandAbort = new AbortController();
    wandBtn.textContent = '\u2715'; wandBtn.classList.add('cancelling');
    ta.disabled = true; ta.style.opacity = '0.35';
    try {
      const r = await fetch('/tts/config/transcribe-clip', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: clip.name }),
        signal: wandAbort.signal,
      });
      if (!r.ok) throw new Error(await r.text());
      const d = await r.json();
      ta.value = d.text;
    } catch(e) {
      if (e.name !== 'AbortError') alert('Transcription failed: ' + e.message);
    } finally {
      wandAbort = null;
      wandBtn.textContent = '\u{1FA84}'; wandBtn.classList.remove('cancelling');
      ta.disabled = false; ta.style.opacity = '';
    }
  });

  // Save logic
  saveBtn.addEventListener('click', async () => {
    const text = ta.value;
    saveBtn.disabled = true; saveBtn.textContent = '\u23F3';
    try {
      const r = await fetch('/tts/config/save-txt', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: clip.name, text }),
      });
      if (!r.ok) throw new Error(await r.text());
      clip.txt_content = text;
      saveBtn.textContent = '\u2713';
      setTimeout(closeClipEdit, 350);
    } catch(e) {
      alert('Save failed: ' + e.message);
      saveBtn.disabled = false; saveBtn.textContent = '\u2713';
    }
  });

  cancelBtn.addEventListener('click', closeClipEdit);

  // Romaji button
  const romajiBtn = document.createElement('button');
  romajiBtn.className = 'seg-wand';
  romajiBtn.textContent = '\uD83C\uDDEC\uD83C\uDDE7'; // 🇬🇧
  romajiBtn.title = 'Convert to romaji';

  let romajiAbort = null;
  romajiBtn.addEventListener('click', async () => {
    if (romajiAbort) {
      romajiAbort.abort(); romajiAbort = null;
      romajiBtn.textContent = '\uD83C\uDDEC\uD83C\uDDE7';
      romajiBtn.classList.remove('cancelling');
      ta.disabled = false; ta.style.opacity = '';
      return;
    }
    const src = ta.value.trim();
    if (!src) return;
    romajiAbort = new AbortController();
    romajiBtn.textContent = '\u2715'; romajiBtn.classList.add('cancelling');
    ta.disabled = true; ta.style.opacity = '0.35';
    try {
      const r = await fetch('/tts/config/to-romaji', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: src }),
        signal: romajiAbort.signal,
      });
      if (!r.ok) throw new Error(await r.text());
      const d = await r.json();
      ta.value = d.text;
    } catch(e) {
      if (e.name !== 'AbortError') alert('Romaji conversion failed: ' + e.message);
    } finally {
      romajiAbort = null;
      romajiBtn.textContent = '\uD83C\uDDEC\uD83C\uDDE7';
      romajiBtn.classList.remove('cancelling');
      ta.disabled = false; ta.style.opacity = '';
    }
  });

  // Left-side button column: wand on top, romaji below
  const leftCol = document.createElement('div');
  leftCol.style.cssText = 'display:flex;flex-direction:column;gap:4px;flex-shrink:0;align-self:center';
  leftCol.appendChild(wandBtn);
  leftCol.appendChild(romajiBtn);

  row.appendChild(leftCol); row.appendChild(ta); row.appendChild(actCol);
  clipDiv.insertAdjacentElement('afterend', row);
  ta.focus(); ta.select();
}

$rebuildBtn.addEventListener('click', async () => {
  const selected = clips.filter(c => c.selected);
  if (!selected.length) return;
  $rebuildBtn.disabled = true;
  $rebuildBtn.textContent = 'Rebuilding\u2026';
  $rebuildStat.textContent = '';
  $rebuildStat.className = '';
  try {
    const fd = new FormData();
    selected.forEach(c => fd.append('names', c.name));
    fd.append('profile_name', activeProfileName === NO_REF_NAME ? 'default' : activeProfileName);
    const r = await fetch('/tts/config/rebuild', { method: 'POST', body: fd });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    if (activeProfileName === NO_REF_NAME) {
      const existing = profiles.find(p => p.name === 'default');
      if (existing) existing.clips = selected.map(c => c.name);
      else profiles.push({ name: 'default', clips: selected.map(c => c.name) });
      _saveProfilesRaw(profiles);
      _setActive('default');
      renderProfileTrigger();
    }
    $rebuildStat.textContent = '\u2713 Done \u2014 ' + d.clips_used + ' clips concatenated';
    $rebuildStat.className = 'ok';
  } catch(e) {
    $rebuildStat.textContent = '\u2717 ' + e.message;
    $rebuildStat.className = 'err';
  } finally {
    updateRebuild();
  }
});


// ═══════════════════════════════════════════════════════════════════
// PROFILES
// ═══════════════════════════════════════════════════════════════════
const PROFILES_KEY   = 'voice_profiles_v1';
const ACTIVE_PRF_KEY = 'voice_active_profile_v1';
const NO_REF_NAME    = '— No Reference —';
const VOICE_MODEL_KEY = 'voice_default_tts_model_v1';
let _voiceModels = [], _serverVoiceModel = '';

function _loadProfilesRaw() {
  try {
    const p = JSON.parse(localStorage.getItem(PROFILES_KEY) || '[]');
    return Array.isArray(p) ? p : [];
  } catch { return []; }
}
function _saveProfilesRaw(arr) {
  localStorage.setItem(PROFILES_KEY,
    JSON.stringify(arr.filter(p => p.name !== NO_REF_NAME)));
}
function _allProfiles() {
  return [{ name: NO_REF_NAME, clips: [] }, ..._loadProfilesRaw()];
}

let profiles = _allProfiles();
let activeProfileName = (() => {
  const n = localStorage.getItem(ACTIVE_PRF_KEY) || NO_REF_NAME;
  return profiles.find(p => p.name === n) ? n : NO_REF_NAME;
})();
function _setActive(name) {
  activeProfileName = name;
  localStorage.setItem(ACTIVE_PRF_KEY, name);
}

const $profileView   = document.getElementById('profile-view');
const $profileEditW  = document.getElementById('profile-edit');
const $profileTrig   = document.getElementById('profile-trigger');
const $profilePanel  = document.getElementById('profile-panel');
const $profilePencil = document.getElementById('profile-pencil');
const $profileInput  = document.getElementById('profile-input');
const $profileCancel = document.getElementById('profile-cancel-btn');
const $profileOk     = document.getElementById('profile-ok-btn');

let _panelOpen = false;
let _editMode  = null; // 'rename' | 'create' | null

function renderProfileTrigger() {
  $profileTrig.querySelector('.pt-name').textContent = activeProfileName;
  _refreshVoiceSelectors();
}

function renderProfilePanel() {
  $profilePanel.innerHTML = '';
  profiles.forEach(p => {
    const row = document.createElement('div');
    row.className = 'profile-opt' + (p.name === activeProfileName ? ' prf-active' : '');

    const delBtn = document.createElement('button');
    delBtn.className = 'profile-opt-del';
    delBtn.textContent = '✕';
    if (p.name === NO_REF_NAME) {
      delBtn.style.visibility = 'hidden';
      delBtn.disabled = true;
    } else {
      delBtn.addEventListener('click', e => { e.stopPropagation(); deleteProfile(p.name); });
    }

    const nameSpan = document.createElement('span');
    nameSpan.className = 'profile-opt-name';
    nameSpan.textContent = p.name;
    nameSpan.addEventListener('click', () => { selectProfile(p.name); closePanel(); });

    row.appendChild(delBtn); row.appendChild(nameSpan);
    $profilePanel.appendChild(row);
  });

  const divider = document.createElement('div');
  divider.className = 'profile-divider';
  $profilePanel.appendChild(divider);

  const createRow = document.createElement('div');
  createRow.className = 'profile-create';
  createRow.textContent = '+ Create Profile';
  createRow.addEventListener('click', () => { closePanel(); enterEditMode('create'); });
  $profilePanel.appendChild(createRow);
}

function openPanel() {
  renderProfilePanel();
  $profilePanel.style.display = 'block';
  _panelOpen = true;
}
function closePanel() {
  $profilePanel.style.display = 'none';
  _panelOpen = false;
}

$profileTrig.addEventListener('click', e => {
  e.stopPropagation();
  _panelOpen ? closePanel() : openPanel();
});
document.addEventListener('click', e => {
  if (_panelOpen && !document.getElementById('profile-dd-wrap').contains(e.target))
    closePanel();
});

function _saveCurrentClipsToProfile() {
  if (activeProfileName === NO_REF_NAME) return;
  const idx = profiles.findIndex(p => p.name === activeProfileName);
  if (idx < 0) return;
  profiles[idx].clips = clips.filter(c => c.selected).map(c => c.name);
  _saveProfilesRaw(profiles);
}

function selectProfile(name) {
  _saveCurrentClipsToProfile();
  _setActive(name);
  profiles = _allProfiles();
  renderProfileTrigger();
  applyProfileClips();
}

function applyProfileClips() {
  const profile = profiles.find(p => p.name === activeProfileName);
  const selectedSet = new Set(profile ? profile.clips : []);
  clips.forEach(c => { c.selected = selectedSet.has(c.name); });
  renderClips();
}

function deleteProfile(name) {
  if (name === NO_REF_NAME) return;
  profiles = profiles.filter(p => p.name !== name);
  _saveProfilesRaw(profiles);
  if (activeProfileName === name) {
    _setActive(NO_REF_NAME);
    renderProfileTrigger();
    applyProfileClips();
  }
  profiles = _allProfiles();
  renderProfilePanel();
  _refreshVoiceSelectors();
}

function enterEditMode(mode) {
  _editMode = mode;
  $profileInput.value  = (mode === 'rename' && activeProfileName !== NO_REF_NAME)
    ? activeProfileName : '';
  $profileInput.placeholder = mode === 'rename' ? 'New name…' : 'Profile name…';
  $profileView.style.display = 'none';
  $profileEditW.style.display = 'flex';
  $profileInput.focus();
  if (mode === 'rename') $profileInput.select();
}
function exitEditMode() {
  _editMode = null;
  $profileEditW.style.display = 'none';
  $profileView.style.display  = 'flex';
}
function confirmEdit() {
  const name = $profileInput.value.trim();
  if (!name || name === NO_REF_NAME) { exitEditMode(); return; }
  profiles = _allProfiles(); // ensure fresh
  const existing = profiles.find(p => p.name === name);
  if (_editMode === 'rename' && activeProfileName !== NO_REF_NAME) {
    if (existing) {
      // target name already exists — just switch to it
      selectProfile(name);
    } else {
      // rename in-place
      const idx = profiles.findIndex(p => p.name === activeProfileName);
      if (idx > 0) {
        profiles[idx].name = name;
        _saveProfilesRaw(profiles);
        _setActive(name);
        profiles = _allProfiles();
        renderProfileTrigger();
      }
    }
  } else {
    // create mode (or pencil while on NO_REF)
    if (existing) {
      selectProfile(name);
    } else {
      profiles.push({ name, clips: [] });
      _saveProfilesRaw(profiles);
      profiles = _allProfiles();
      selectProfile(name);
    }
  }
  exitEditMode();
}

$profilePencil.addEventListener('click', () => enterEditMode('rename'));
$profileCancel.addEventListener('click', exitEditMode);
$profileOk.addEventListener('click', confirmEdit);
$profileInput.addEventListener('keydown', e => {
  if (e.key === 'Enter')  { e.preventDefault(); confirmEdit();  }
  if (e.key === 'Escape') { e.preventDefault(); exitEditMode(); }
});

renderProfileTrigger();

function _activeRef() {
  return (activeProfileName === NO_REF_NAME) ? '' : activeProfileName;
}

// Generation overrides do not change Config. Capture these values when queuing.
function _generationVoice(kind) {
  const actor = document.getElementById(kind + '-voice')?.value || '';
  const model = document.getElementById(kind + '-model')?.value ||
                document.getElementById('config-tts-model')?.value || '';
  const profile = actor === 'none' ? '' : actor.startsWith('profile:') ? actor.slice(8) : _activeRef();
  return { profile, tts_model: model, llm_model: document.getElementById('chat-llm-model').value };
}

function _refreshVoiceSelectors() {
  function populate(select, entries, preferred) {
    if (!select) return;
    const previous = preferred === undefined ? select.value : preferred;
    select.replaceChildren();
    entries.forEach(([value, label]) => {
      const option = document.createElement('option');
      option.value = value; option.textContent = label;
      select.appendChild(option);
    });
    select.value = entries.some(([value]) => value === previous) ? previous : '';
  }
  const actors = [['', 'Use Config (' + (activeProfileName === NO_REF_NAME ? 'No Reference' : activeProfileName) + ')'],
                  ['none', 'No Reference — built-in speaker'],
                  ...profiles.filter(p => p.name !== NO_REF_NAME).map(p => ['profile:' + p.name, p.name])];
  const models = _voiceModels.map(m => [m.id, m.id +
    (m.supports_profiles ? ' — clones profiles' : m.mode === 'voice_design' ? ' — voice design' : '')]);
  ['chat', 'tts'].forEach(kind => {
    populate(document.getElementById(kind + '-voice'), actors);
    populate(document.getElementById(kind + '-model'), [['', 'Use Config'], ...models]);
  });
  populate(document.getElementById('config-tts-model'),
           [['', 'Server default' + (_serverVoiceModel ? ' (' + _serverVoiceModel + ')' : '')], ...models],
           localStorage.getItem(VOICE_MODEL_KEY) || '');
}

document.getElementById('config-tts-model').addEventListener('change', e => {
  localStorage.setItem(VOICE_MODEL_KEY, e.target.value);
});
fetch('/tts/models').then(r => {
  if (!r.ok) throw new Error('Cannot load TTS models');
  return r.json();
}).then(data => {
  _voiceModels = data.models || [];
  _serverVoiceModel = data.default || '';
  _refreshVoiceSelectors();
}).catch(error => console.error(error));

// ═══════════════════════════════════════════════════════════════════
// UPLOAD / SEGMENT EDITOR
// ═══════════════════════════════════════════════════════════════════
const $uploadBtn      = document.getElementById('upload-btn');
const $uploadInput    = document.getElementById('upload-input');
const $recordClipBtn  = document.getElementById('record-clip-btn');
const $recordClipStatus = document.getElementById('record-clip-status');
const $cfgNormal      = document.getElementById('cfg-normal');
const $segView        = document.getElementById('seg-view');
const $segProgressWrap= document.getElementById('seg-progress-wrap');
const $segProgressFill= document.getElementById('seg-progress-fill');
const $segProgressLabel=document.getElementById('seg-progress-label');
const $segNameIn      = document.getElementById('seg-name-in');
const $segCancel      = document.getElementById('seg-cancel');
const $segHdrAdd      = document.getElementById('seg-hdr-add');
const $segToolbar     = document.getElementById('seg-toolbar');
const $segAutoBtn     = document.getElementById('seg-auto-btn');
const $segList2       = document.getElementById('seg-list');
const $segAddRow      = document.getElementById('seg-add-row');
const $segFinish      = document.getElementById('seg-finish');

let _tempId = null;

let clipRecorder = null, clipStream = null, recordedClip = null;
const clipRecordingControls = new Map();
function lockClipRecordingControls(lock) {
  if (lock) {
    for (const id of ['upload-btn', 'mic-btn', 'vad-tog', 'call-start', 'profile-trigger', 'profile-pencil']) {
      const element = document.getElementById(id);
      clipRecordingControls.set(element, element.disabled);
      element.disabled = true;
    }
  } else {
    clipRecordingControls.forEach((disabled, element) => { element.disabled = disabled; });
    clipRecordingControls.clear();
  }
}

async function saveRecordedClip() {
  $recordClipBtn.disabled = true;
  $recordClipBtn.setAttribute('aria-pressed', 'false');
  $recordClipBtn.textContent = 'Saving…';
  $recordClipStatus.textContent = 'Uploading recording…';
  async function post(url, body) {
    const response = await fetch(url, body instanceof FormData
      ? { method: 'POST', body }
      : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }
  try {
    if (!recordedClip.tempId) {
      const form = new FormData();
      form.append('file', recordedClip.file);
      recordedClip.tempId = (await post('/tts/config/upload-temp', form)).temp_id;
    }
    let text = '', transcriptionError = null;
    $recordClipStatus.textContent = 'Transcribing recording…';
    try {
      text = (await post('/tts/config/transcribe-segment', { temp_id: recordedClip.tempId })).text;
    } catch (error) { transcriptionError = error; }
    await post('/tts/config/create-segments', {
      temp_id: recordedClip.tempId, name: recordedClip.name,
      segments: [{ start: null, end: null, text }]
    });
    recordedClip = null;
    await loadClips();
    $recordClipStatus.textContent = transcriptionError
      ? 'Recording saved. Transcription failed; use the recording’s wand button to retry.'
      : 'Recording saved and transcribed.';
  } catch (error) {
    // Re-upload retained audio on retry if the temporary upload was lost.
    if (recordedClip) recordedClip.tempId = null;
    $recordClipStatus.textContent = 'Could not save recording: ' + error.message + '. Click Retry save to try again.';
  } finally {
    lockClipRecordingControls(false);
    $recordClipBtn.disabled = false;
    $recordClipBtn.textContent = recordedClip ? 'Retry save' : '● Record';
  }
}

$recordClipBtn.addEventListener('click', async () => {
  if (clipRecorder?.state === 'recording') {
    $recordClipBtn.disabled = true;
    clipRecorder.stop();
    clipStream?.getTracks().forEach(track => track.stop());
    return;
  }
  if (recordedClip) {
    lockClipRecordingControls(true);
    await saveRecordedClip();
    return;
  }
  $recordClipBtn.disabled = true;
  try {
    if (isRecording || vadEnabled || document.getElementById('call-start').hidden)
      throw new Error('Stop the chat microphone or call before recording a clip');
    if (!window.MediaRecorder) throw new Error('Browser audio recording is unavailable');
    lockClipRecordingControls(true);
    $recordClipStatus.textContent = 'Requesting microphone access…';
    clipStream = await getMicStream();
    const mime = getBestMime();
    const recorder = new MediaRecorder(clipStream, mime ? { mimeType: mime } : {});
    clipRecorder = recorder;
    const chunks = [];
    recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
    recorder.onstop = async () => {
      clipStream?.getTracks().forEach(track => track.stop());
      clipStream = clipRecorder = null;
      const blob = new Blob(chunks, { type: recorder.mimeType });
      if (!blob.size) {
        lockClipRecordingControls(false);
        $recordClipBtn.disabled = false;
        $recordClipBtn.setAttribute('aria-pressed', 'false');
        $recordClipBtn.textContent = '● Record';
        $recordClipStatus.textContent = 'No audio captured. Try recording again.';
        return;
      }
      const name = 'recording_' + Date.now();
      const extension = recorder.mimeType.includes('mp4') ? 'm4a' : recorder.mimeType.includes('ogg') ? 'ogg' : 'webm';
      recordedClip = { name, file: new File([blob], name + '.' + extension, { type: blob.type }) };
      await saveRecordedClip();
    };
    recorder.start(200);
    $recordClipBtn.textContent = '■ Stop recording';
    $recordClipBtn.setAttribute('aria-pressed', 'true');
    $recordClipStatus.textContent = 'Recording… Click Stop recording to save and transcribe.';
  } catch (error) {
    clipStream?.getTracks().forEach(track => track.stop());
    clipStream = clipRecorder = null;
    lockClipRecordingControls(false);
    $recordClipStatus.textContent = error.name === 'NotAllowedError'
      ? 'Microphone access was denied. Allow microphone access and try again.' : error.message;
  } finally { $recordClipBtn.disabled = false; }
});
window.addEventListener('pagehide', () => {
  if (clipRecorder) { clipRecorder.onstop = null; if (clipRecorder.state !== 'inactive') clipRecorder.stop(); }
  clipStream?.getTracks().forEach(track => track.stop());
});

$uploadBtn.addEventListener('click', () => $uploadInput.click());
$uploadInput.addEventListener('change', () => {
  const file = $uploadInput.files[0];
  $uploadInput.value = '';
  if (file) enterUploadMode(file);
});

function showSegEditor(show) {
  const d = show ? '' : 'none';
  $segHdrAdd.style.display   = show ? 'flex'  : 'none';
  $segToolbar.style.display  = show ? 'flex'  : 'none';
  $segList2.style.display    = show ? 'flex'  : 'none';
  $segAddRow.style.display   = show ? 'block' : 'none';
}

function enterUploadMode(file) {
  $cfgNormal.style.display = 'none';
  $segView.style.display   = 'flex';
  $segProgressFill.className = 'seg-progress-fill indeterminate';
  $segProgressFill.style.width = '';
  $segProgressWrap.style.display = 'block';
  $segNameIn.style.display = 'none';
  $segProgressLabel.textContent = 'Uploading\u2026';
  $segFinish.disabled = true;
  $segFinish.textContent = 'Finish and Add';
  $segList2.innerHTML = '';
  _tempId = null;
  showSegEditor(false);   // hide list/+ until upload completes
  uploadAudio(file);
}

function exitUploadMode() {
  $cfgNormal.style.display = 'flex';
  $segView.style.display   = 'none';
  _tempId = null;
}

$segCancel.addEventListener('click', exitUploadMode);
$segHdrAdd.addEventListener('click', addSegRow);
$segAddRow.addEventListener('click', addSegRow);

function uploadAudio(file) {
  const xhr = new XMLHttpRequest();
  xhr.upload.addEventListener('progress', e => {
    if (e.lengthComputable) {
      const pct = e.loaded / e.total * 100;
      $segProgressFill.className = 'seg-progress-fill';
      $segProgressFill.style.width = pct + '%';
      $segProgressLabel.textContent = Math.round(pct) + '%';
    }
  });
  // Upload bytes sent — now server is converting
  xhr.upload.addEventListener('load', () => {
    $segProgressFill.className = 'seg-progress-fill indeterminate';
    $segProgressFill.style.width = '';
    $segProgressLabel.textContent = 'Converting\u2026';
  });
  xhr.addEventListener('load', () => {
    if (xhr.status === 200) {
      const d = JSON.parse(xhr.responseText);
      _tempId = d.temp_id;
      const ts = Math.floor(Date.now() / 1000);
      $segNameIn.value = 'upload_' + ts;
      $segProgressWrap.style.display = 'none';
      $segProgressLabel.textContent = '';
      $segNameIn.style.display = 'block';
      $segFinish.disabled = false;
      showSegEditor(true);    // reveal list/+ now that upload is done
      addSegRow();            // default first row
    } else {
      alert('Upload failed: ' + xhr.responseText);
      exitUploadMode();
    }
  });
  xhr.addEventListener('error', () => { alert('Upload failed'); exitUploadMode(); });
  xhr.open('POST', '/tts/config/upload-temp');
  const fd = new FormData();
  fd.append('file', file);
  xhr.send(fd);
}

// ─ Auto-segment ─────────────────────────────────────────────────────
$segAutoBtn.addEventListener('click', async () => {
  if (!_tempId) return;
  $segAutoBtn.disabled = true;
  $segAutoBtn.textContent = '\u23F3 Segmenting\u2026';
  try {
    const r = await fetch('/tts/config/auto-segment', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ temp_id: _tempId }),
    });
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    if (!d.segments.length) { alert('No speech detected.'); return; }
    $segList2.innerHTML = '';
    d.segments.forEach(s => addSegRow({ start: s.start, end: s.end, text: s.text }));
  } catch(e) {
    alert('Auto-segment failed: ' + e.message);
  } finally {
    $segAutoBtn.disabled = false;
    $segAutoBtn.textContent = '\u{1FA84} Segment';
  }
});

// ─ Segment rows ──────────────────────────────────────────────────────
function secsToMmSs(secs) {
  const m = Math.floor(secs / 60);
  const s = Math.round(secs % 60);
  return [m, s];
}

function addSegRow(prefill = {}) {
  const row = document.createElement('div');
  row.className = 'seg-row';

  // ─ Wand (auto-transcribe this segment) ─
  const wandBtn = document.createElement('button');
  wandBtn.className = 'seg-wand';
  wandBtn.textContent = '\u{1FA84}';
  wandBtn.title = 'Auto-transcribe this segment';

  // ─ Time columns ─
  const timesDiv = document.createElement('div');
  timesDiv.className = 'seg-times';

  ['start', 'end'].forEach(label => {
    const tr = document.createElement('div');
    tr.className = 'seg-time-row';

    const lbl = document.createElement('span');
    lbl.className = 'seg-time-lbl';
    lbl.textContent = label;

    const mmIn = document.createElement('input');
    mmIn.type = 'number'; mmIn.className = 'seg-num';
    mmIn.placeholder = '00'; mmIn.min = '0';
    mmIn.dataset.role = label + '_mm';

    const colon = document.createElement('span');
    colon.className = 'seg-colon'; colon.textContent = ':';

    const ssIn = document.createElement('input');
    ssIn.type = 'number'; ssIn.className = 'seg-num';
    ssIn.placeholder = '00'; ssIn.min = '0'; ssIn.max = '59';
    ssIn.dataset.role = label + '_ss';

    // Prefill from auto-segment results
    if (prefill[label] != null) {
      const [m, s] = secsToMmSs(prefill[label]);
      if (m) mmIn.value = m;
      ssIn.value = s.toString().padStart(2, '0');
    }

    tr.appendChild(lbl); tr.appendChild(mmIn);
    tr.appendChild(colon); tr.appendChild(ssIn);
    timesDiv.appendChild(tr);
  });

  // ─ Transcript textarea ─
  const ta = document.createElement('textarea');
  ta.className = 'seg-ta';
  ta.placeholder = 'Transcript\u2026';
  if (prefill.text) ta.value = prefill.text;

  // ─ Delete button ─
  const del = document.createElement('button');
  del.className = 'seg-del'; del.textContent = '\xd7'; del.title = 'Remove';
  del.addEventListener('click', () => row.remove());

  // ─ Wand logic (wand references ta, so defined after ta) ─
  let wandAbort = null;
  wandBtn.addEventListener('click', async () => {
    if (wandAbort) {
      // Cancel in-flight request
      wandAbort.abort();
      wandAbort = null;
      wandBtn.textContent = '\u{1FA84}';
      wandBtn.classList.remove('cancelling');
      ta.disabled = false; ta.style.opacity = '';
      return;
    }
    if (!_tempId) return;

    const get = role => row.querySelector(`[data-role="${role}"]`);
    const smm = get('start_mm'), sss = get('start_ss');
    const emm = get('end_mm'),   ess = get('end_ss');
    const hasStart = smm.value !== '' || sss.value !== '';
    const hasEnd   = emm.value !== '' || ess.value !== '';
    const start = hasStart ? (parseInt(smm.value)||0)*60 + (parseInt(sss.value)||0) : null;
    const end   = hasEnd   ? (parseInt(emm.value)||0)*60 + (parseInt(ess.value)||0) : null;

    wandAbort = new AbortController();
    wandBtn.textContent = '\u2715';
    wandBtn.classList.add('cancelling');
    ta.disabled = true; ta.style.opacity = '0.35';

    try {
      const r = await fetch('/tts/config/transcribe-segment', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ temp_id: _tempId, start, end }),
        signal: wandAbort.signal,
      });
      if (!r.ok) throw new Error(await r.text());
      const d = await r.json();
      ta.value = d.text;
    } catch(e) {
      if (e.name !== 'AbortError') alert('Transcription failed: ' + e.message);
    } finally {
      wandAbort = null;
      wandBtn.textContent = '\u{1FA84}';
      wandBtn.classList.remove('cancelling');
      ta.disabled = false; ta.style.opacity = '';
    }
  });

  row.appendChild(wandBtn); row.appendChild(timesDiv); row.appendChild(ta); row.appendChild(del);
  $segList2.appendChild(row);
  if (!prefill.text) ta.focus();
}

$segFinish.addEventListener('click', async () => {
  if (!_tempId) return;

  const name = $segNameIn.value.trim() || ('upload_' + Math.floor(Date.now() / 1000));

  const rows = $segList2.querySelectorAll('.seg-row');
  const segments = [];
  for (const row of rows) {
    const get = role => row.querySelector(`[data-role="${role}"]`);
    const startMmEl = get('start_mm'), startSsEl = get('start_ss');
    const endMmEl   = get('end_mm'),   endSsEl   = get('end_ss');

    const hasStart = startMmEl.value !== '' || startSsEl.value !== '';
    const hasEnd   = endMmEl.value   !== '' || endSsEl.value   !== '';

    const start = hasStart
      ? (parseInt(startMmEl.value) || 0) * 60 + (parseInt(startSsEl.value) || 0)
      : null;
    const end   = hasEnd
      ? (parseInt(endMmEl.value)   || 0) * 60 + (parseInt(endSsEl.value)   || 0)
      : null;

    const text = row.querySelector('.seg-ta')?.value.trim() || '';
    segments.push({ start, end, text });
  }

  if (!segments.length) { alert('Add at least one segment.'); return; }

  $segFinish.disabled = true;
  $segFinish.textContent = 'Creating\u2026';

  try {
    const r = await fetch('/tts/config/create-segments', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ temp_id: _tempId, name, segments }),
    });
    if (!r.ok) {
      const e = await r.json().catch(() => ({ detail: r.statusText }));
      throw new Error(e.detail || r.statusText);
    }
    const d = await r.json();
    exitUploadMode();
    await loadClips();
    // Switch to config tab to show the new clips (already there)
  } catch(e) {
    alert('Failed: ' + e.message);
    $segFinish.disabled = false;
    $segFinish.textContent = 'Finish and Add';
  }
});

const chatLlmSelect = document.getElementById('chat-llm-model');
const savedChatLlm = localStorage.getItem('chat_llm_model') || '';
chatLlmSelect.addEventListener('change', () => localStorage.setItem('chat_llm_model', chatLlmSelect.value));
fetch('/studio/llm/models').then(r => {
  if (!r.ok) throw new Error('Cannot load chat models');
  return r.json();
}).then(data => {
  (data.data || []).forEach(model => chatLlmSelect.add(new Option(model.id, model.id)));
  if ([...chatLlmSelect.options].some(o => o.value === savedChatLlm)) chatLlmSelect.value = savedChatLlm;
}).catch(error => { console.warn(error); chatLlmSelect.options[0].textContent = 'Server default (models unavailable)'; });
