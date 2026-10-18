import { LANGUAGES, TranslatorClient } from './translate-client.js';

const ENGINE_NAMES = { local: 'On-prem · Jetson Thor', api: 'Cloud API' };
const STAGES = {
  loading: '온프렘 모델을 GPU에 올리는 중… (첫 실행만)',
  queued: '다른 요청 처리 대기 중…',
  transcribing: '음성 인식 중…',
  translating: '번역 중…',
  synthesizing: '음성 합성 중…',
};

const $ = (selector) => document.querySelector(selector);
const el = {
  language: $('#visitor-language'),
  status: $('#status'),
  engineSwitch: $('#engine-switch'),
  engineChip: $('#engine-chip'),
  engineEyebrow: $('#engine-eyebrow'),
  visitorBadge: $('#visitor-badge'),
  staffSource: $('#staff-source'),
  staffTranslation: $('#staff-translation'),
  visitorSource: $('#visitor-source'),
  visitorTranslation: $('#visitor-translation'),
  staffButton: $('#staff-talk'),
  visitorButton: $('#visitor-talk'),
  clear: $('#clear'),
};

const state = {
  config: null,
  engine: 'local',
  visitorLanguage: 'en',
};

function setStatus(text, kind = '') {
  el.status.textContent = text;
  el.status.dataset.kind = kind;
}

function engineLanguages(engine) {
  const supported = state.config?.engines?.[engine]?.languages;
  return Object.keys(LANGUAGES).filter((code) => !supported || supported.includes(code));
}

function renderLanguages() {
  const codes = engineLanguages(state.engine);
  el.language.replaceChildren(...codes.map((code) => new Option(LANGUAGES[code], code)));
  if (!codes.includes(state.visitorLanguage)) state.visitorLanguage = codes[0] || 'en';
  el.language.value = state.visitorLanguage;
  updateLanguageLabels();
}

function updateLanguageLabels() {
  const name = LANGUAGES[state.visitorLanguage] || state.visitorLanguage;
  $('#visitor-language-name').textContent = name;
  $('#staff-target-name').textContent = name;
}

function setEngine(engine) {
  state.engine = engine;
  for (const button of el.engineSwitch.querySelectorAll('button')) {
    button.setAttribute('aria-checked', String(button.dataset.engine === engine));
  }
  el.engineEyebrow.textContent = ENGINE_NAMES[engine];
  // The on-prem ASR reports the spoken language; the cloud API does not.
  el.visitorBadge.textContent = engine === 'local' ? 'AUTO' : 'API';
  renderLanguages();
}

async function loadConfig() {
  try {
    const response = await fetch('/api/config', { cache: 'no-store' });
    state.config = await response.json();
  } catch (_) {
    state.config = null;
  }
  const engines = state.config?.engines || {};
  const available = Object.keys(engines).filter((key) => engines[key].available);
  const preferred = state.config?.default_engine || 'local';
  const showSwitch = (state.config?.public_engine_choice || state.config?.is_admin) && available.length > 1;

  el.engineSwitch.hidden = !showSwitch;
  for (const button of el.engineSwitch.querySelectorAll('button')) {
    button.disabled = !available.includes(button.dataset.engine);
  }
  setEngine(available.includes(preferred) ? preferred : available[0] || preferred);
  if (!available.length) setStatus('사용 가능한 번역 엔진이 없습니다. 관리자에게 문의하세요.', 'error');
}

el.engineSwitch.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-engine]');
  if (button && !button.disabled && !client.active) setEngine(button.dataset.engine);
});

el.language.addEventListener('change', () => {
  state.visitorLanguage = el.language.value;
  updateLanguageLabels();
});

function elementsFor(direction) {
  return direction === 'staff'
    ? { source: el.staffSource, translation: el.staffTranslation, button: el.staffButton }
    : { source: el.visitorSource, translation: el.visitorTranslation, button: el.visitorButton };
}

function append(target, text) {
  if (!text) return;
  target.classList.remove('partial');
  target.textContent += text;
  target.scrollTop = target.scrollHeight;
}

function adoptDetectedLanguage(session, code) {
  if (session.direction !== 'visitor' || !code || code === 'ko') return;
  if (!engineLanguages(state.engine).includes(code) || code === state.visitorLanguage) return;
  state.visitorLanguage = code;
  el.language.value = code;
  updateLanguageLabels();
  el.visitorBadge.textContent = `${code.toUpperCase()} 감지`;
}

const client = new TranslatorClient({
  onEvent(msg, session) {
    const view = elementsFor(session.direction);
    switch (msg.type) {
      case 'state':
        if (msg.state === 'connecting') setStatus('번역 엔진에 연결 중…', 'working');
        if (msg.state === 'listening') setStatus('듣고 있습니다. 버튼을 누른 채 말씀하세요.', 'live');
        if (msg.state === 'processing') setStatus('마지막 음성을 처리 중…', 'working');
        if (msg.state === 'closed') {
          view.button.classList.remove('recording');
          if (el.status.dataset.kind === 'working') setStatus('대기 중');
        }
        break;
      case 'engine':
        el.engineChip.hidden = false;
        el.engineChip.textContent = ENGINE_NAMES[msg.engine] || msg.label;
        break;
      case 'notice':
        setStatus(msg.message, 'working');
        break;
      case 'processing':
        setStatus(STAGES[msg.stage] || '처리 중…', 'working');
        break;
      case 'speech':
        setStatus(msg.state === 'started' ? '음성 감지됨…' : '번역 중…', 'working');
        break;
      case 'source_partial':
        view.source.textContent = msg.text;
        view.source.classList.add('partial');
        break;
      case 'source_delta':
        append(view.source, msg.text);
        break;
      case 'source_done':
        if (msg.text && (view.source.classList.contains('partial') || !view.source.textContent.trim())) {
          view.source.classList.remove('partial');
          view.source.textContent = msg.text;
        }
        adoptDetectedLanguage(session, msg.language);
        break;
      case 'translation_delta':
        append(view.translation, msg.text);
        break;
      case 'translation_done':
        if (msg.text && !view.translation.textContent.trim()) view.translation.textContent = msg.text;
        break;
      case 'response_done':
        setStatus('번역 완료', 'ok');
        break;
      case 'error':
        setStatus(`오류: ${msg.message}`, 'error');
        break;
      default:
        break;
    }
  },
});

async function startTurn(direction) {
  if (client.active) return;
  const view = elementsFor(direction);
  view.source.textContent = '';
  view.translation.textContent = '';
  view.button.classList.add('recording');
  await client.start({
    direction,
    engine: state.engine,
    target: direction === 'staff' ? state.visitorLanguage : 'ko',
    source: direction === 'staff' ? 'ko' : 'auto',
  });
}

function bindPushToTalk(button, direction) {
  const start = (event) => {
    event.preventDefault();
    startTurn(direction);
  };
  const stop = (event) => {
    event.preventDefault();
    if (client.active?.direction === direction) client.stop();
  };
  button.addEventListener('pointerdown', start);
  button.addEventListener('pointerup', stop);
  button.addEventListener('pointercancel', stop);
  button.addEventListener('pointerleave', (event) => {
    if (event.buttons) stop(event);
  });
}

bindPushToTalk(el.staffButton, 'staff');
bindPushToTalk(el.visitorButton, 'visitor');

el.clear.addEventListener('click', () => {
  client.abort();
  for (const node of [el.staffSource, el.staffTranslation, el.visitorSource, el.visitorTranslation]) {
    node.textContent = '';
  }
  el.visitorBadge.textContent = state.engine === 'local' ? 'AUTO' : 'API';
  setStatus('대기 중');
});

if (!window.isSecureContext && !['localhost', '127.0.0.1'].includes(location.hostname)) {
  setStatus('마이크 사용을 위해 HTTPS로 접속해야 합니다.', 'error');
}
loadConfig();
