// Realtime calls use their own socket and conversation, ending on hang-up.
(() => {
  const start = document.getElementById('call-start');
  const mute = document.getElementById('call-mute');
  const end = document.getElementById('call-end');
  const status = document.getElementById('call-status');
  const delaySlider = document.getElementById('call-playback-delay');
  const delayValue = document.getElementById('call-playback-delay-value');
  const delayStorageKey = 'call_playback_delay_ms';
  let hasPlaybackPreference = false;
  let socket, stream, context, processor, source, gain, playbackBus, captureMerger;
  let serverEchoActive = false;
  let active = false, muted = false, responding = false, nextAudio = 0, turn = 0;
  let pendingAudio = [], currentGeneration = null, lastVoiceAt = 0, silenceGapMs = 700, playbackTimer;
  let localSpeechActive = false;
  const discardedGenerations = new Set();
  const playing = new Set();
  const playbackReported = new Set();
  const controls = ['mic-btn', 'vad-tog', 'txt-in', 'send-btn', 'chat-voice', 'chat-model', 'chat-llm-model', 'config-tts-model'];
  const previous = new Map();

  function updatePlaybackDelay(value) {
    silenceGapMs = Math.max(0, Math.min(1000, Math.round(value / 25) * 25));
    delaySlider.value = String(silenceGapMs);
    delaySlider.setAttribute('aria-valuetext', silenceGapMs + ' milliseconds');
    delayValue.value = silenceGapMs + ' ms';
  }
  try {
    const saved = localStorage.getItem(delayStorageKey);
    if (saved !== null && Number.isFinite(Number(saved))) {
      updatePlaybackDelay(Number(saved));
      hasPlaybackPreference = true;
    }
  } catch {}
  delaySlider.addEventListener('input', () => {
    hasPlaybackPreference = true;
    updatePlaybackDelay(Number(delaySlider.value));
    try { localStorage.setItem(delayStorageKey, String(silenceGapMs)); } catch {}
    pumpAudio();
  });

  function useSessionPlaybackDefault(session) {
    if (!hasPlaybackPreference) updatePlaybackDelay(session?.turn_detection?.silence_duration_ms ?? silenceGapMs);
  }

  function send(event) {
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(event));
  }
  function label(text) { status.textContent = text; }
  function detectVoice(voiced, at = performance.now()) {
    if (voiced) {
      lastVoiceAt = at;
      if (!localSpeechActive) interruptPlayback();
    }
    localSpeechActive = voiced;
  }
  function updateEchoStatus(session) {
    serverEchoActive = session?.echo_cancellation?.enabled === true;
    document.getElementById('call-echo-status').textContent = serverEchoActive
      ? 'Server echo filtering' : 'Browser echo cancellation';
  }
  function clearPlayback() {
    pendingAudio = [];
    playing.forEach(s => { try { s.stop(); } catch {} }); playing.clear();
    nextAudio = 0;
  }
  function interruptPlayback() {
    if (currentGeneration) discardedGenerations.add(currentGeneration);
    if (discardedGenerations.size > 64) discardedGenerations.delete(discardedGenerations.values().next().value);
    clearPlayback(); currentGeneration = null;
  }
  function pumpAudio() {
    if (!active || localSpeechActive || performance.now() - lastVoiceAt < silenceGapMs) return;
    while (pendingAudio.length) {
      const message = pendingAudio.shift();
      play(message.audio, message.response_id, message.receivedAt);
    }
  }
  function receiveAudio(message) {
    const id = message.generation_id || message.response_id;
    if (discardedGenerations.has(id)) return;
    if (message.replace_previous || (!message.generation_id && id !== currentGeneration)) {
      clearPlayback(); currentGeneration = id;
    }
    if (message.discarded) {
      discardedGenerations.add(id); clearPlayback(); currentGeneration = null; return;
    }
    if (!currentGeneration) currentGeneration = id;
    if (id !== currentGeneration || !message.delta) return;
    message.receivedAt = performance.now();
    message.audio = decodeAudio(message.delta);
    window.callStatistics.audioReady(message, performance.now());
    pendingAudio.push(message);
    pumpAudio();
  }
  function stop() {
    active = false; turn++;
    if (socket) { socket.onclose = null; socket.close(); socket = null; }
    if (processor) { processor.onaudioprocess = null; processor.disconnect(); processor = null; }
    source?.disconnect(); source = null;
    gain?.disconnect(); gain = null;
    captureMerger?.disconnect(); captureMerger = null;
    playbackBus?.disconnect(); playbackBus = null;
    stream?.getTracks().forEach(t => t.stop()); stream = null;
    clearPlayback(); currentGeneration = null;
    localSpeechActive = false; discardedGenerations.clear();
    serverEchoActive = false;
    document.getElementById('call-echo-status').textContent = '';
    clearInterval(playbackTimer); playbackTimer = null;
    playbackReported.clear();
    context?.close(); context = null;
    muted = responding = false; nextAudio = 0;
    start.hidden = false; start.disabled = false; mute.hidden = end.hidden = true;
    mute.textContent = 'Mute'; mute.setAttribute('aria-pressed', 'false');
    previous.forEach((disabled, element) => { element.disabled = disabled; }); previous.clear();
  }
  function decodeAudio(encoded) {
    const raw = atob(encoded);
    const audio = context.createBuffer(1, raw.length / 2, 24000);
    const data = audio.getChannelData(0);
    const bytes = new DataView(Uint8Array.from(raw, c => c.charCodeAt(0)).buffer);
    for (let i = 0; i < data.length; i++) data[i] = bytes.getInt16(i * 2, true) / 32768;
    return audio;
  }
  function play(audio, responseId, received) {
    const player = context.createBufferSource();
    player.buffer = audio; player.connect(playbackBus);
    const at = Math.max(context.currentTime, nextAudio);
    nextAudio = at + audio.duration;
    playing.add(player);
    player.onended = () => {
      playing.delete(player);
      if (active && !responding && !playing.size) label(muted ? 'Muted' : 'Listening');
    };
    player.start(at); label('Speaking');
    if (!playbackReported.has(responseId)) {
      playbackReported.add(responseId);
      // Estimate the output time from the audio clock and output latency.
      // The server acknowledgement includes transport delay; it is not an
      // acoustic measurement of when the speaker actually makes sound.
      const delay = Math.max(0, at - context.currentTime) +
                    (context.baseLatency || 0) + (context.outputLatency || 0);
      setTimeout(() => {
        if (active && playbackReported.has(responseId)) send({
          type: 'response.playback_started', response_id: responseId,
          receive_to_playback_ms: performance.now() - received
        });
      }, delay * 1000);
    }
  }

  start.addEventListener('click', async () => {
    if (active) return;
    active = true;
    window.callStatistics.reset();
    const attempt = ++turn;
    start.disabled = true; end.hidden = false; label('Connecting…');
    try {
      if (isRecording || vadEnabled || chatPlaying) throw new Error('Stop recording, Auto VAD, and chat playback before starting a call.');
      const options = _generationVoice('chat');
      context = new AudioContext({ sampleRate: 24000 });
      await context.resume();
      playbackBus = context.createGain();
      playbackBus.channelCount = 1;
      playbackBus.channelCountMode = 'explicit';
      playbackBus.connect(context.destination);
      if (!active || attempt !== turn) return;
      const acquired = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false
      });
      if (!active || attempt !== turn) { acquired.getTracks().forEach(t => t.stop()); return; }
      stream = acquired;
      const url = new URL('/v1/realtime', location.href);
      url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
      if (options.tts_model) url.searchParams.set('model', options.tts_model);
      socket = new WebSocket(url);
      socket.onopen = () => send({ type: 'session.update', session: {
        llm_model: options.llm_model,
        voice: options.profile ? 'profile:' + options.profile : 'alloy',
        echo_cancellation: { enabled: true },
        turn_detection: { type: 'server_vad', threshold: 0.005, create_response: true }
      }});
      socket.onmessage = event => {
        if (!active) return;
        const message = JSON.parse(event.data);
        window.callStatistics.event(message, performance.now());
        switch (message.type) {
          case 'session.created':
            useSessionPlaybackDefault(message.session);
            updateEchoStatus(message.session);
            break;
          case 'session.updated':
            useSessionPlaybackDefault(message.session);
            updateEchoStatus(message.session);
            if (processor) break;
            controls.forEach(id => { const el = document.getElementById(id); if (el) { previous.set(el, el.disabled); el.disabled = true; } });
            start.hidden = true; mute.hidden = false; label('Listening');
            source = context.createMediaStreamSource(stream);
            // Paired graph channels share timing: microphone left, rendered
            // assistant audio right. Held/cancelled audio never enters the reference.
            captureMerger = context.createChannelMerger(2);
            processor = context.createScriptProcessor(512, 2, 1);
            lastVoiceAt = performance.now();
            playbackTimer = setInterval(pumpAudio, 10);
            gain = context.createGain(); gain.gain.value = 0;
            source.connect(captureMerger, 0, 0);
            playbackBus.connect(captureMerger, 0, 1);
            captureMerger.connect(processor);
            processor.connect(gain); gain.connect(context.destination);
            processor.onaudioprocess = e => {
              if (!active || muted) return;
              const samples = e.inputBuffer.getChannelData(0);
              let energy = 0;
              for (const sample of samples) energy += sample * sample;
              if (!serverEchoActive) detectVoice(Math.sqrt(energy / samples.length) >= 0.005);
              const count = Math.round(samples.length * 24000 / context.sampleRate);
              function encode(channel) {
                const pcm = new ArrayBuffer(count * 2), view = new DataView(pcm);
                for (let i = 0; i < count; i++) {
                  const sample = channel[Math.min(channel.length - 1, Math.floor(i * context.sampleRate / 24000))];
                  view.setInt16(i * 2, Math.max(-1, Math.min(1, sample)) * 32767, true);
                }
                return ab2b64(pcm);
              }
              const packet = { type: 'input_audio_buffer.append', audio: encode(samples), captured_at: performance.now() };
              if (serverEchoActive) packet.playback_reference = encode(e.inputBuffer.getChannelData(1));
              send(packet);
            };
            break;
          case 'input_audio_buffer.activity':
            if (serverEchoActive && !muted) {
              const now = performance.now();
              const captured = Number(message.captured_at);
              detectVoice(message.voiced === true, Number.isFinite(captured) && captured >= 0 && captured <= now ? captured : now);
            }
            break;
          case 'input_audio_buffer.speech_started':
            lastVoiceAt = performance.now(); interruptPlayback();
            label('Hearing you…'); break;
          case 'input_audio_buffer.speech_stopped': responding = true; label('Thinking…'); break;
          case 'conversation.item.input_audio_transcription.completed':
            if (message.transcript) addMsg('user', message.transcript);
            break;
          case 'conversation.item.input_audio_transcription.partial':
            if (message.transcript) label('Hearing: ' + message.transcript);
            break;
          case 'response.created':
            if (!message.provisional) { responding = true; label('Thinking…'); }
            break;
          case 'response.committed': responding = true; break;
          case 'response.output_audio.delta': receiveAudio(message); break;
          case 'response.timing':
            window.dispatchEvent(new CustomEvent('studio:call-timing', { detail: message }));
            break;
          case 'response.output_audio_transcript.done':
            if (message.transcript) addMsg('sys', message.transcript);
            break;
          case 'response.done':
            responding = false;
            if (!playing.size) label(muted ? 'Muted' : 'Listening');
            break;
          case 'error':
          case 'conversation.item.input_audio_transcription.failed':
            addMsg('err', message.error?.message || 'Call failed');
            stop(); label('Call ended with an error'); break;
        }
      };
      socket.onerror = () => { stop(); label('Could not connect call'); };
      socket.onclose = () => { stop(); label('Call disconnected'); };
    } catch (error) {
      if (attempt !== turn) return;
      stop(); label(error.message);
    }
  });
  mute.addEventListener('click', () => {
    muted = !muted;
    localSpeechActive = false;
    stream?.getAudioTracks().forEach(track => { track.enabled = !muted; });
    send({ type: 'input_audio_buffer.clear' });
    mute.textContent = muted ? 'Unmute' : 'Mute';
    mute.setAttribute('aria-pressed', String(muted));
    label(muted ? 'Muted' : responding ? 'Thinking…' : playing.size ? 'Speaking' : 'Listening');
  });
  end.addEventListener('click', () => { stop(); label('Call ended'); });
  window.addEventListener('pagehide', stop);
})();
