import { LANGUAGES, TranslatorClient } from './translate-client.js';

const $ = (selector) => document.querySelector(selector);
const ENGINE_NAMES = { local: '온프렘 GPU', api: '클라우드 API' };
const STATE_NAMES = { unloaded: '미로딩', loading: '로딩 중', ready: '준비됨', error: '오류' };
const STAGE_NAMES = {
  model_load: '모델 로드 (콜드 스타트)',
  queue_wait: 'GPU 대기',
  partial_drain: '부분 자막 마무리 대기',
  partial_asr: '부분 자막 ASR (누적)',
  asr: '최종 ASR',
  llm_ttft: 'LLM 첫 토큰',
  llm: 'LLM 번역 전체',
  tts_first: 'TTS 첫 오디오 청크',
  tts: 'TTS 전체',
  upstream_connect: '업스트림 연결',
  session_init: '세션 초기화',
};
const SUMMARY_COLUMNS = [
  ['connect_ms', '연결'],
  ['release_to_source_ms', '→ 자막'],
  ['release_to_translation_ms', '→ 번역'],
  ['release_to_audio_ms', '→ 첫 음성'],
  ['release_to_done_ms', '→ 완료'],
];

const state = {
  overview: null,
  consoleEngine: 'local',
  consoleLanguage: 'en',
  history: { util: [], power: [] },
  openTurn: null,
  lastClient: null,
};

// ------------------------------------------------------------------ helpers

function fmtMs(value) {
  if (value == null) return '–';
  if (Math.abs(value) >= 1000) return `${(value / 1000).toFixed(2)} s`;
  return `${Math.round(value)} ms`;
}

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child == null) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function kv(target, rows) {
  target.replaceChildren(
    ...rows.filter(([, v]) => v != null && v !== '').flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]),
  );
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  // A 401 from the login call is a wrong password; elsewhere it means the session expired.
  if (response.status === 401 && path !== '/api/admin/login') {
    showLogin();
    throw new Error('unauthorized');
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function setSegmented(container, value) {
  for (const button of container.querySelectorAll('button')) {
    button.setAttribute('aria-checked', String(button.dataset.engine === value));
  }
}

// -------------------------------------------------------------------- auth

function showLogin() {
  $('#login-view').hidden = false;
  $('#dashboard').hidden = true;
  $('#logout').hidden = true;
  stopPolling();
}

function showDashboard() {
  $('#login-view').hidden = true;
  $('#dashboard').hidden = false;
  $('#logout').hidden = false;
  startPolling();
}

$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('#login-error').textContent = '';
  try {
    await api('/api/admin/login', {
      method: 'POST',
      body: JSON.stringify({ password: $('#password').value }),
    });
    $('#password').value = '';
    showDashboard();
  } catch (error) {
    $('#login-error').textContent = error.message;
  }
});

$('#logout').addEventListener('click', async () => {
  await fetch('/api/admin/logout', { method: 'POST' });
  showLogin();
});

// ----------------------------------------------------------------- polling

let timers = [];

function startPolling() {
  stopPolling();
  refreshOverview();
  refreshSystem();
  timers = [setInterval(refreshOverview, 5000), setInterval(refreshSystem, 2000)];
}

function stopPolling() {
  timers.forEach(clearInterval);
  timers = [];
}

async function refreshOverview() {
  try {
    state.overview = await api('/api/admin/overview');
  } catch (_) {
    return;
  }
  renderSettings();
  renderLocal();
  renderAggregate();
  renderTurns();
}

async function refreshSystem() {
  let sys;
  try {
    sys = await api('/api/admin/system');
  } catch (_) {
    return;
  }
  const gpu = sys.gpus?.[0] || {};
  $('#host-line').textContent = [sys.host, sys.board].filter(Boolean).join(' · ');
  $('#gpu-name').textContent = gpu.name ? `${gpu.name} · driver ${gpu.driver}` : 'GPU 정보 없음';

  push(state.history.util, gpu.utilization_pct ?? 0);
  push(state.history.power, gpu.power_w ?? 0);
  $('#g-util').textContent = gpu.utilization_pct != null ? `${gpu.utilization_pct}%` : '–';
  $('#g-power').textContent = gpu.power_w != null ? `${gpu.power_w.toFixed(1)} W` : '–';
  spark($('#spark-util'), state.history.util, 100);
  spark($('#spark-power'), state.history.power, Math.max(10, ...state.history.power));

  const mem = sys.memory || {};
  $('#g-mem').textContent = mem.total_gb ? `${mem.used_gb} / ${mem.total_gb} GB` : '–';
  $('#g-mem-bar').style.width = mem.total_gb ? `${(mem.used_gb / mem.total_gb) * 100}%` : '0';
  $('#g-clock').textContent = gpu.clock_mhz != null ? `${gpu.clock_mhz} MHz` : '–';
  $('#g-temp').textContent = [
    gpu.temperature_c != null ? `GPU ${gpu.temperature_c}°C` : null,
    sys.cpu_temp_c != null ? `CPU ${sys.cpu_temp_c}°C` : null,
  ].filter(Boolean).join(' · ');

  kv($('#sys-info'), [
    ['CPU 부하 (1/5분)', sys.load_avg?.every((v) => v != null) ? `${sys.load_avg[0].toFixed(2)} / ${sys.load_avg[1].toFixed(2)} (${sys.cpu_count} cores)` : null],
    ['서버 프로세스 RSS', sys.process_rss_gb != null ? `${sys.process_rss_gb} GB` : null],
    ['GPU 상태', sys.local_busy ? '번역 처리 중' : '유휴'],
  ]);
}

function push(list, value) {
  list.push(value);
  if (list.length > 60) list.shift();
}

function spark(svg, values, max) {
  if (!values.length) return;
  const step = 120 / Math.max(1, values.length - 1);
  const points = values.map((v, i) => `${(i * step).toFixed(1)},${(31 - (v / max) * 29).toFixed(1)}`).join(' ');
  svg.innerHTML = `<polyline points="${points}" />`;
}

// ---------------------------------------------------------------- settings

function renderSettings() {
  const { runtime, api: apiInfo, engines } = state.overview;
  setSegmented($('#default-engine'), runtime.default_engine);
  for (const button of $('#default-engine').querySelectorAll('button')) {
    button.disabled = !engines[button.dataset.engine]?.available;
  }
  $('#public-choice').checked = runtime.public_engine_choice;
  kv($('#api-info'), [
    ['클라우드 API', apiInfo.configured ? '설정됨' : '미설정 (DASHSCOPE_API_KEY)'],
    ['모델', apiInfo.model],
    ['리전', apiInfo.region],
  ]);
  for (const button of $('#console-engine').querySelectorAll('button')) {
    button.disabled = !engines[button.dataset.engine]?.available;
  }
  if (!engines[state.consoleEngine]?.available) {
    const fallback = Object.keys(engines).find((e) => engines[e].available);
    if (fallback) setConsoleEngine(fallback);
  } else if (!state.consoleReady) {
    setConsoleEngine(state.consoleEngine); // narrow languages to the engine's list
  }
  state.consoleReady = true;
}

async function saveSettings(patch) {
  try {
    await api('/api/admin/settings', { method: 'POST', body: JSON.stringify(patch) });
  } finally {
    refreshOverview();
  }
}

$('#default-engine').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-engine]');
  if (button && !button.disabled) saveSettings({ default_engine: button.dataset.engine });
});
$('#public-choice').addEventListener('change', (event) => saveSettings({ public_engine_choice: event.target.checked }));

function renderLocal() {
  const local = state.overview.local;
  const pill = $('#local-state');
  pill.textContent = local.enabled ? (STATE_NAMES[local.state] || local.state) : '비활성';
  pill.dataset.state = local.state;
  const load = local.load_ms || {};
  const loadText = Object.keys(load).length
    ? Object.entries(load).map(([k, v]) => `${k.toUpperCase()} ${fmtMs(v)}`).join(' · ')
    : null;
  const shortName = (path) => path?.split('/').pop();
  kv($('#local-info'), [
    ['ASR', shortName(local.models.asr)],
    ['번역 LLM', shortName(local.models.llm)],
    ['TTS', shortName(local.models.tts)],
    ['디바이스', [local.device, local.cuda_device].filter(Boolean).join(' · ')],
    ['PyTorch', local.torch],
    ['GPU 텐서 메모리', local.gpu_allocated_gb != null ? `${local.gpu_allocated_gb} GB (예약 ${local.gpu_reserved_gb} GB)` : null],
    ['로딩 시간', loadText],
    ['로드 시각', local.loaded_at ? new Date(local.loaded_at * 1000).toLocaleString() : null],
    ['처리한 턴', local.turns_served],
  ]);
  $('#local-error').textContent = local.error || '';
  $('#local-load').disabled = !local.enabled || local.state === 'ready' || local.state === 'loading';
  $('#local-unload').disabled = local.state !== 'ready' || local.busy;
}

$('#local-load').addEventListener('click', async () => {
  try {
    await api('/api/admin/local/load', { method: 'POST' });
  } catch (error) {
    $('#local-error').textContent = error.message;
  }
  refreshOverview();
});

$('#local-unload').addEventListener('click', async () => {
  try {
    await api('/api/admin/local/unload', { method: 'POST' });
  } catch (error) {
    $('#local-error').textContent = error.message;
  }
  refreshOverview();
});

$('#clear-metrics').addEventListener('click', async () => {
  await api('/api/admin/metrics', { method: 'DELETE' });
  refreshOverview();
});

// ------------------------------------------------------------- aggregates

function renderAggregate() {
  const aggregate = state.overview.aggregate || {};
  const engines = Object.keys(aggregate);
  const table = $('#aggregate');
  if (!engines.length) {
    table.replaceChildren(h('tbody', {}, h('tr', {}, h('td', { class: 'muted' }, '아직 기록된 턴이 없습니다.'))));
    return;
  }
  const stageNames = [...new Set(engines.flatMap((e) => Object.keys(aggregate[e].stages || {})))];
  table.replaceChildren(
    h('thead', {}, h('tr', {},
      h('th', {}, '엔진'), h('th', { class: 'num' }, '턴 (오류)'),
      SUMMARY_COLUMNS.map(([, label]) => h('th', { class: 'num' }, label)),
    )),
    h('tbody', {}, engines.map((engine) => {
      const row = aggregate[engine];
      return h('tr', {},
        h('td', {}, h('span', { class: `engine-tag ${engine}` }, ENGINE_NAMES[engine] || engine)),
        h('td', { class: 'num' }, `${row.turns} (${row.errors})`),
        SUMMARY_COLUMNS.map(([key]) => h('td', { class: 'num' },
          `${fmtMs(row[key]?.p50)} / `, h('span', { class: 'muted' }, fmtMs(row[key]?.p90)))),
      );
    })),
    stageNames.length ? h('tfoot', {}, h('tr', {}, h('td', { colspan: 2 + SUMMARY_COLUMNS.length, class: 'stage-summary' },
      '단계별 p50 — ',
      engines.map((engine) => h('span', { class: 'stage-group' },
        h('strong', {}, `${ENGINE_NAMES[engine] || engine}: `),
        Object.entries(aggregate[engine].stages || {})
          .map(([k, v]) => `${STAGE_NAMES[k] || k} ${fmtMs(v)}`).join(' · '))),
    ))) : null,
  );
}

// ------------------------------------------------------------- turn table

function renderTurns() {
  const turns = state.overview.turns || [];
  const table = $('#turns');
  if (!turns.length) {
    table.replaceChildren(h('tbody', {}, h('tr', {}, h('td', { class: 'muted' }, '아직 기록된 턴이 없습니다.'))));
    return;
  }
  const body = h('tbody');
  for (const turn of turns) {
    const open = state.openTurn === turn.id;
    body.append(h('tr', {
      class: `turn-row${open ? ' open' : ''}${turn.error ? ' failed' : ''}`,
      onclick: () => { state.openTurn = open ? null : turn.id; renderTurns(); },
    },
      h('td', {}, new Date(turn.started_at * 1000).toLocaleTimeString()),
      h('td', {}, h('span', { class: `engine-tag ${turn.engine}` }, ENGINE_NAMES[turn.engine] || turn.engine)),
      h('td', {}, turn.direction === 'staff' ? '직원' : '민원인'),
      h('td', {}, `${turn.info?.detected_language || turn.source} → ${turn.target}`),
      h('td', { class: 'num' }, `${turn.audio_in_s}s`),
      SUMMARY_COLUMNS.map(([key]) => h('td', { class: 'num' }, fmtMs(turn[key]))),
      h('td', {}, turn.error ? h('span', { class: 'error-text' }, turn.error) : (turn.info?.cold_start ? '콜드 스타트' : 'OK')),
    ));
    if (open) {
      body.append(h('tr', { class: 'turn-detail' }, h('td', { colspan: 6 + SUMMARY_COLUMNS.length }, turnDetail(turn))));
    }
  }
  table.replaceChildren(
    h('thead', {}, h('tr', {},
      h('th', {}, '시각'), h('th', {}, '엔진'), h('th', {}, '방향'), h('th', {}, '언어'),
      h('th', { class: 'num' }, '발화'),
      SUMMARY_COLUMNS.map(([, label]) => h('th', { class: 'num' }, label)),
      h('th', {}, '상태'),
    )),
    body,
  );
}

function turnDetail(turn, client) {
  const dl = h('dl', { class: 'kv metrics-kv' });
  kv(dl, [
    ...Object.entries(turn.stages || {}).map(([k, v]) => [STAGE_NAMES[k] || k, fmtMs(v)]),
    ['LLM 속도', turn.info?.llm_tok_per_s ? `${turn.info.llm_tok_per_s} tok/s (${turn.info.llm_tokens} tok)` : null],
    ['TTS RTF', turn.info?.tts_rtf != null ? `${turn.info.tts_rtf} (${turn.info.tts_sentences}문장)` : null],
    ['부분 자막 실행', turn.info?.partial_asr_runs],
    ['번역 음성 길이', turn.audio_out_s ? `${turn.audio_out_s}s` : null],
    ['문자 수 (원문/번역)', `${turn.source_chars} / ${turn.translation_chars}`],
    ['모델', turn.info?.llm_model ? `${turn.info.asr_model} · ${turn.info.llm_model} · ${turn.info.tts_model}` : turn.info?.model],
    ['브라우저: 누름 → 준비', client ? fmtMs(client.press_to_ready_ms) : null],
    ['브라우저: 해제 → 스피커 재생', client ? fmtMs(client.release_to_playback_ms) : null],
    ['클라이언트', turn.client],
  ]);
  return h('div', { class: 'detail-grid' }, timeline(turn), dl);
}

// ---------------------------------------------------------------- timeline

function timeline(turn) {
  const m = turn.marks || {};
  const total = Math.max(m.done || 0, 1);
  const rows = [
    ['연결', 0, m.ready, 'connect'],
    ['발화 (마이크)', m.audio_in_first ?? m.ready, m.release, 'speech'],
    ['원문 자막', m.source_first, m.source_done ?? m.source_first, 'source'],
    ['번역 텍스트', m.translation_first, m.translation_done ?? m.translation_first, 'translation'],
    ['번역 음성', m.audio_out_first, m.done, 'audio'],
  ];
  const wrap = h('div', { class: 'timeline' });
  for (const [label, start, end, kind] of rows) {
    const row = h('div', { class: 'tl-row' }, h('span', { class: 'tl-label' }, label));
    const track = h('div', { class: 'tl-track' });
    if (start != null && end != null) {
      const left = (start / total) * 100;
      const width = Math.max(0.6, ((end - start) / total) * 100);
      track.append(h('span', { class: `tl-bar ${kind}`, style: `left:${left}%;width:${width}%` }));
      row.append(track, h('span', { class: 'tl-value' }, fmtMs(end - start)));
    } else {
      row.append(track, h('span', { class: 'tl-value muted' }, '–'));
    }
    wrap.append(row);
  }
  if (m.release != null) {
    const marker = h('div', { class: 'tl-release', style: `left:calc(var(--tl-label) + (100% - var(--tl-label) - var(--tl-value)) * ${m.release / total})` });
    marker.title = '버튼 해제';
    wrap.append(marker);
  }
  wrap.append(h('div', { class: 'tl-axis' }, h('span', {}, '0'), h('span', {}, '빨간 점선 = 버튼 해제'), h('span', {}, fmtMs(total))));
  return wrap;
}

// ------------------------------------------------------------ test console

function setConsoleEngine(engine) {
  state.consoleEngine = engine;
  setSegmented($('#console-engine'), engine);
  const supported = state.overview?.engines?.[engine]?.languages;
  const codes = Object.keys(LANGUAGES).filter((c) => !supported || supported.includes(c));
  if (!codes.includes(state.consoleLanguage)) state.consoleLanguage = codes[0];
  $('#console-language').replaceChildren(...codes.map((c) => new Option(LANGUAGES[c], c)));
  $('#console-language').value = state.consoleLanguage;
}

$('#console-engine').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-engine]');
  if (button && !button.disabled && !client.active) setConsoleEngine(button.dataset.engine);
});
$('#console-language').addEventListener('change', (event) => { state.consoleLanguage = event.target.value; });

const consoleStatus = (text) => { $('#c-status').textContent = text; };
let pendingMetrics = null;

const client = new TranslatorClient({
  onEvent(msg, session) {
    const source = $('#c-source');
    const translation = $('#c-translation');
    switch (msg.type) {
      case 'state':
        if (msg.state === 'listening') consoleStatus('듣는 중…');
        if (msg.state === 'processing') consoleStatus('처리 중…');
        if (msg.state === 'closed') {
          $('#c-staff').classList.remove('recording');
          $('#c-visitor').classList.remove('recording');
          state.lastClient = msg.client;
          if (pendingMetrics) showConsoleMetrics(pendingMetrics, msg.client);
          pendingMetrics = null;
          refreshOverview();
        }
        break;
      case 'processing': consoleStatus(`처리 중: ${msg.stage}`); break;
      case 'notice': consoleStatus(msg.message); break;
      case 'source_partial': source.textContent = msg.text; source.classList.add('partial'); break;
      case 'source_delta': source.classList.remove('partial'); source.textContent += msg.text; break;
      case 'source_done':
        if (msg.text) { source.classList.remove('partial'); source.textContent = msg.text; }
        break;
      case 'translation_delta': translation.textContent += msg.text; break;
      case 'translation_done': if (msg.text && !translation.textContent.trim()) translation.textContent = msg.text; break;
      case 'response_done': consoleStatus('완료'); break;
      case 'error': consoleStatus(`오류: ${msg.message}`); break;
      case 'metrics': pendingMetrics = msg.metrics; break;
      default: break;
    }
  },
});

function showConsoleMetrics(turn, clientTimings) {
  const container = $('#c-timeline');
  container.classList.remove('empty');
  container.replaceChildren(timeline(turn));
  const detail = turnDetail(turn, clientTimings).querySelector('dl');
  const headline = [
    ['해제 → 첫 음성 (서버)', fmtMs(turn.release_to_audio_ms)],
    ['해제 → 스피커 재생 (브라우저)', fmtMs(clientTimings?.release_to_playback_ms)],
    ['해제 → 완료', fmtMs(turn.release_to_done_ms)],
  ];
  const head = h('dl', { class: 'kv metrics-kv headline' });
  kv(head, headline);
  $('#c-metrics').replaceChildren(...head.childNodes, ...detail.childNodes);
}

function bindConsoleButton(button, direction) {
  button.addEventListener('pointerdown', (event) => {
    event.preventDefault();
    if (client.active) return;
    $('#c-source').textContent = '';
    $('#c-translation').textContent = '';
    button.classList.add('recording');
    client.start({
      direction,
      engine: state.consoleEngine,
      target: direction === 'staff' ? state.consoleLanguage : 'ko',
      source: direction === 'staff' ? 'ko' : 'auto',
    });
  });
  const stop = (event) => {
    event.preventDefault();
    if (client.active?.direction === direction) client.stop();
  };
  button.addEventListener('pointerup', stop);
  button.addEventListener('pointercancel', stop);
  button.addEventListener('pointerleave', (event) => { if (event.buttons) stop(event); });
}

bindConsoleButton($('#c-staff'), 'staff');
bindConsoleButton($('#c-visitor'), 'visitor');

// -------------------------------------------------------------------- init

(async () => {
  const config = await fetch('/api/config', { cache: 'no-store' }).then((r) => r.json()).catch(() => null);
  if (config && !config.admin_enabled) {
    showLogin();
    $('#login-error').textContent = '서버에 ADMIN_PASSWORD가 설정되지 않아 관리자 기능이 꺼져 있습니다.';
    $('#login-form').querySelector('button').disabled = true;
    return;
  }
  setConsoleEngine(config?.default_engine || 'local');
  if (config?.is_admin) showDashboard();
  else showLogin();
})();
