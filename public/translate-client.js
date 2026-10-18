// Shared push-to-talk client used by the demo page and the admin console.
// One WebSocket per turn: PCM16 16 kHz up, transcripts + PCM16 audio down.

export const LANGUAGES = {
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

const WORKLET_URL = '/audio-worklet.js';
const MAX_BUFFERED_PACKETS = 16000 / 320 * 30; // 30 s of 20 ms packets

export class TranslatorClient {
  constructor({ onEvent = () => {} } = {}) {
    this.onEvent = onEvent;
    this.audioContext = null;
    this.workletLoaded = false;
    this.playbackCursor = 0;
    this.session = null;
  }

  get active() {
    return this.session;
  }

  async ensureAudioContext() {
    if (!this.audioContext) this.audioContext = new AudioContext();
    if (this.audioContext.state === 'suspended') await this.audioContext.resume();
    return this.audioContext;
  }

  wsUrl({ target, engine, direction, source }) {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const params = new URLSearchParams({ target, direction, source: source || 'auto' });
    if (engine) params.set('engine', engine);
    return `${protocol}//${location.host}/api/ws/translate?${params}`;
  }

  async start({ direction, target, engine, source }) {
    if (this.session) return null;
    const session = {
      direction,
      target,
      engine,
      socket: null,
      stream: null,
      sourceNode: null,
      workletNode: null,
      pending: [],
      ready: false,
      stopping: false,
      closed: false,
      client: { pressedAt: performance.now() },
    };
    this.session = session;
    this.playbackCursor = 0;
    this.emit(session, { type: 'state', state: 'connecting' });

    const socket = new WebSocket(this.wsUrl({ target, engine, direction, source }));
    socket.binaryType = 'arraybuffer';
    session.socket = socket;
    socket.addEventListener('open', () => { session.client.openAt = performance.now(); });
    socket.addEventListener('message', (event) => this.handleMessage(session, event));
    socket.addEventListener('close', () => this.cleanup(session));
    socket.addEventListener('error', () => this.emit(session, { type: 'error', message: 'WebSocket 연결 오류' }));

    // Open the microphone in parallel with the connection and buffer until the
    // engine is ready, so the first syllables are not lost.
    try {
      await this.startMicrophone(session);
    } catch (error) {
      this.emit(session, { type: 'error', message: `마이크 오류: ${error.message}` });
      this.abort();
    }
    return session;
  }

  async handleMessage(session, event) {
    if (typeof event.data !== 'string') return;
    const msg = JSON.parse(event.data);

    if (msg.type === 'ready') {
      session.ready = true;
      session.client.readyAt = performance.now();
      if (msg.max_audio_seconds) {
        session.maxTimer = setTimeout(() => this.stop(), msg.max_audio_seconds * 1000);
      }
      for (const packet of session.pending) socket(session)?.send(packet);
      session.pending = [];
      if (session.stopping) this.sendFinish(session);
      else this.emit(session, { type: 'state', state: 'listening' });
    }

    if (msg.type === 'audio') this.play(session, msg.audio, msg.sample_rate || 24000);
    this.emit(session, msg);
  }

  async startMicrophone(session) {
    const ctx = await this.ensureAudioContext();
    if (!this.workletLoaded) {
      await ctx.audioWorklet.addModule(WORKLET_URL);
      this.workletLoaded = true;
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      video: false,
    });
    if (session.stopping || this.session !== session) {
      // Released before the mic opened: nothing was recorded.
      stream.getTracks().forEach((track) => track.stop());
      return;
    }

    const source = ctx.createMediaStreamSource(stream);
    const worklet = new AudioWorkletNode(ctx, 'pcm-16k-processor');
    const silent = ctx.createGain();
    silent.gain.value = 0;
    worklet.port.onmessage = (event) => {
      if (session.stopping) return;
      if (session.ready && session.socket.readyState === WebSocket.OPEN) {
        session.socket.send(event.data);
      } else if (session.pending.length < MAX_BUFFERED_PACKETS) {
        session.pending.push(event.data);
      }
    };
    source.connect(worklet);
    worklet.connect(silent);
    silent.connect(ctx.destination);

    session.stream = stream;
    session.sourceNode = source;
    session.workletNode = worklet;
    session.client.micAt = performance.now();
  }

  stopMicrophone(session) {
    try { session.workletNode?.disconnect(); } catch (_) {}
    try { session.sourceNode?.disconnect(); } catch (_) {}
    session.stream?.getTracks().forEach((track) => track.stop());
    session.stream = session.workletNode = session.sourceNode = null;
  }

  // Button released: stop the mic and let the engine finish the turn.
  stop() {
    const session = this.session;
    if (!session || session.stopping) return;
    session.stopping = true;
    session.client.releaseAt = performance.now();
    clearTimeout(session.maxTimer);
    this.stopMicrophone(session);
    this.emit(session, { type: 'state', state: 'processing' });

    if (session.ready) this.sendFinish(session);
    else if (!session.pending.length && session.socket.readyState !== WebSocket.CONNECTING) session.socket.close();
    // Otherwise 'ready' will flush the buffer and send finish.

    session.completionTimer = setTimeout(() => {
      if (session.socket.readyState === WebSocket.OPEN) {
        this.emit(session, { type: 'error', message: '처리 시간이 초과되었습니다. 서버 상태를 확인하세요.' });
        session.socket.close();
      }
    }, 300000);
  }

  sendFinish(session) {
    if (session.socket.readyState === WebSocket.OPEN) {
      session.socket.send(JSON.stringify({ type: 'finish' }));
    }
  }

  abort() {
    const session = this.session;
    if (!session) return;
    session.stopping = true;
    try { session.socket.close(); } catch (_) {}
    this.cleanup(session);
  }

  cleanup(session) {
    if (session.closed) return;
    session.closed = true;
    clearTimeout(session.maxTimer);
    clearTimeout(session.completionTimer);
    this.stopMicrophone(session);
    if (this.session === session) this.session = null;
    this.emit(session, { type: 'state', state: 'closed', client: clientTimings(session) });
  }

  play(session, base64, sampleRate) {
    if (!base64 || !this.audioContext) return;
    const ctx = this.audioContext;
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    const pcm = new Int16Array(bytes.buffer);
    const floats = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i += 1) floats[i] = pcm[i] / 32768;

    const buffer = ctx.createBuffer(1, floats.length, sampleRate);
    buffer.copyToChannel(floats, 0);
    const node = ctx.createBufferSource();
    node.buffer = buffer;
    node.connect(ctx.destination);
    this.playbackCursor = Math.max(this.playbackCursor, ctx.currentTime + 0.02);
    node.start(this.playbackCursor);
    if (!session.client.playAt) {
      session.client.playAt = performance.now() + (this.playbackCursor - ctx.currentTime) * 1000;
    }
    this.playbackCursor += buffer.duration;
  }

  emit(session, msg) {
    this.onEvent(msg, session);
  }
}

function socket(session) {
  return session.socket.readyState === WebSocket.OPEN ? session.socket : null;
}

function clientTimings(session) {
  const c = session.client;
  const since = (a, b) => (a != null && b != null ? Math.round(b - a) : null);
  return {
    press_to_ready_ms: since(c.pressedAt, c.readyAt),
    press_to_mic_ms: since(c.pressedAt, c.micAt),
    release_to_playback_ms: since(c.releaseAt, c.playAt),
  };
}
