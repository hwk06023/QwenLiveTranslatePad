const LANGUAGES = {
  en: 'English',
  ja: '日本語',
  zh: '中文',
  yue: '粵語',
  es: 'Español',
  fr: 'Français',
  de: 'Deutsch',
  it: 'Italiano',
  pt: 'Português',
  ru: 'Русский',
  ar: 'العربية',
  th: 'ไทย',
  vi: 'Tiếng Việt',
  id: 'Bahasa Indonesia',
  ms: 'Bahasa Melayu',
  tr: 'Türkçe',
  hi: 'हिन्दी',
};

const state = {
  active: null,
  audioContext: null,
  playbackCursor: 0,
  visitorLanguage: 'en',
};

const $ = (selector) => document.querySelector(selector);
const languageSelect = $('#visitor-language');
const statusEl = $('#status');
const staffSource = $('#staff-source');
const staffTranslation = $('#staff-translation');
const visitorSource = $('#visitor-source');
const visitorTranslation = $('#visitor-translation');
const staffButton = $('#staff-talk');
const visitorButton = $('#visitor-talk');
const clearButton = $('#clear');

for (const [code, name] of Object.entries(LANGUAGES)) {
  const option = document.createElement('option');
  option.value = code;
  option.textContent = name;
  languageSelect.appendChild(option);
}

languageSelect.value = state.visitorLanguage;
languageSelect.addEventListener('change', () => {
  state.visitorLanguage = languageSelect.value;
  $('#visitor-language-name').textContent = LANGUAGES[state.visitorLanguage] || state.visitorLanguage;
  $('#staff-target-name').textContent = LANGUAGES[state.visitorLanguage] || state.visitorLanguage;
});
languageSelect.dispatchEvent(new Event('change'));

function setStatus(text, kind = '') {
  statusEl.textContent = text;
  statusEl.dataset.kind = kind;
}

function wsUrl(target) {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${location.host}/api/ws/translate?target=${encodeURIComponent(target)}`;
}

async function ensureAudioContext() {
  if (!state.audioContext) {
    state.audioContext = new AudioContext();
  }
  if (state.audioContext.state === 'suspended') {
    await state.audioContext.resume();
  }
  return state.audioContext;
}

function base64ToInt16(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return new Int16Array(bytes.buffer);
}

async function playPCM(base64, sampleRate = 24000) {
  if (!base64) return;
  const ctx = await ensureAudioContext();
  const pcm = base64ToInt16(base64);
  const floatData = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i += 1) {
    floatData[i] = pcm[i] / 32768;
  }

  const buffer = ctx.createBuffer(1, floatData.length, sampleRate);
  buffer.copyToChannel(floatData, 0);
  const source = ctx.createBufferSource();
  source.buffer = buffer;
  source.connect(ctx.destination);

  const now = ctx.currentTime;
  state.playbackCursor = Math.max(state.playbackCursor, now + 0.02);
  source.start(state.playbackCursor);
  state.playbackCursor += buffer.duration;
}

function resetDirection(direction) {
  if (direction === 'staff') {
    staffSource.textContent = '';
    staffTranslation.textContent = '';
  } else {
    visitorSource.textContent = '';
    visitorTranslation.textContent = '';
  }
}

function addText(el, text) {
  if (!text) return;
  el.textContent += text;
  el.scrollTop = el.scrollHeight;
}

async function startTranslation(direction) {
  if (state.active) return;

  const target = direction === 'staff' ? state.visitorLanguage : 'ko';
  const sourceEl = direction === 'staff' ? staffSource : visitorSource;
  const translationEl = direction === 'staff' ? staffTranslation : visitorTranslation;
  const button = direction === 'staff' ? staffButton : visitorButton;

  resetDirection(direction);
  state.playbackCursor = 0;
  button.classList.add('recording');
  setStatus('번역 서버에 연결 중…', 'working');

  const socket = new WebSocket(wsUrl(target));
  const session = {
    direction,
    target,
    socket,
    stream: null,
    sourceNode: null,
    workletNode: null,
    ready: false,
    stopping: false,
  };
  state.active = session;

  socket.addEventListener('message', async (event) => {
    const msg = JSON.parse(event.data);

    if (msg.type === 'ready') {
      session.ready = true;
      setStatus('듣고 있습니다. 버튼을 누른 채 말씀하세요.', 'live');
      try {
        await startMicrophone(session);
      } catch (error) {
        setStatus(`마이크 오류: ${error.message}`, 'error');
        stopTranslation();
      }
      return;
    }

    if (msg.type === 'source_delta') addText(sourceEl, msg.text);
    if (msg.type === 'source_done' && msg.text && !sourceEl.textContent.trim()) sourceEl.textContent = msg.text;
    if (msg.type === 'translation_delta') addText(translationEl, msg.text);
    if (msg.type === 'translation_done' && msg.text && !translationEl.textContent.trim()) translationEl.textContent = msg.text;
    if (msg.type === 'audio') await playPCM(msg.audio, msg.sample_rate || 24000);
    if (msg.type === 'speech') setStatus(msg.state === 'started' ? '음성 감지됨…' : '번역 중…', 'working');
    if (msg.type === 'response_done') setStatus('번역 완료', 'ok');
    if (msg.type === 'error') setStatus(`오류: ${msg.message}`, 'error');
    if (msg.type === 'session_finished') setStatus('통역 세션 종료', 'ok');
  });

  socket.addEventListener('close', () => {
    cleanupSession(session);
  });

  socket.addEventListener('error', () => {
    setStatus('WebSocket 연결 오류', 'error');
  });
}

async function startMicrophone(session) {
  const ctx = await ensureAudioContext();
  await ctx.audioWorklet.addModule('/audio-worklet.js');

  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
    video: false,
  });

  const source = ctx.createMediaStreamSource(stream);
  const worklet = new AudioWorkletNode(ctx, 'pcm-16k-processor');
  const silent = ctx.createGain();
  silent.gain.value = 0;

  worklet.port.onmessage = (event) => {
    if (session.socket.readyState === WebSocket.OPEN && !session.stopping) {
      session.socket.send(event.data);
    }
  };

  source.connect(worklet);
  worklet.connect(silent);
  silent.connect(ctx.destination);

  session.stream = stream;
  session.sourceNode = source;
  session.workletNode = worklet;
}

function stopMicrophone(session) {
  if (session.workletNode) {
    try { session.workletNode.disconnect(); } catch (_) {}
  }
  if (session.sourceNode) {
    try { session.sourceNode.disconnect(); } catch (_) {}
  }
  if (session.stream) {
    for (const track of session.stream.getTracks()) track.stop();
  }
  session.stream = null;
  session.workletNode = null;
  session.sourceNode = null;
}

function stopTranslation() {
  const session = state.active;
  if (!session || session.stopping) return;
  session.stopping = true;
  stopMicrophone(session);
  setStatus('마지막 음성을 처리 중…', 'working');

  if (session.socket.readyState === WebSocket.OPEN) {
    session.socket.send(JSON.stringify({ type: 'finish' }));
  } else if (session.socket.readyState < WebSocket.CLOSING) {
    session.socket.close();
  }

  window.setTimeout(() => {
    if (session.socket.readyState === WebSocket.OPEN) session.socket.close();
  }, 8000);
}

function cleanupSession(session) {
  stopMicrophone(session);
  staffButton.classList.remove('recording');
  visitorButton.classList.remove('recording');
  if (state.active === session) state.active = null;
}

function bindPushToTalk(button, direction) {
  const start = (event) => {
    event.preventDefault();
    startTranslation(direction);
  };
  const stop = (event) => {
    event.preventDefault();
    if (state.active?.direction === direction) stopTranslation();
  };

  button.addEventListener('pointerdown', start);
  button.addEventListener('pointerup', stop);
  button.addEventListener('pointercancel', stop);
  button.addEventListener('pointerleave', (event) => {
    if (event.buttons) stop(event);
  });
}

bindPushToTalk(staffButton, 'staff');
bindPushToTalk(visitorButton, 'visitor');

clearButton.addEventListener('click', () => {
  if (state.active) stopTranslation();
  staffSource.textContent = '';
  staffTranslation.textContent = '';
  visitorSource.textContent = '';
  visitorTranslation.textContent = '';
  setStatus('대기 중');
});

if (!window.isSecureContext && location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {
  setStatus('마이크 사용을 위해 HTTPS로 접속해야 합니다.', 'error');
} else {
  setStatus('대기 중');
}
