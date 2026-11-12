'use strict';

/* 改善报告工作台前端：数据全部来自 /api/*，无外部依赖。 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const TAG_TEXT = { misjudge: '误判', product: '产品自身NG', external: '外部原因' };
const TAG_ORDER = ['misjudge', 'product', 'external'];
const STATUS_TEXT = { open: '开启中', in_progress: '进行中', done: '已完成', closed: '已关闭' };
const STATION_ORDER = (a, b) => String(a.station).localeCompare(String(b.station), 'zh-CN', { numeric: true });

/* 汇总请求序号：小时快速切换时丢弃过期响应，避免旧数据覆盖新数据 */
let summarySeq = 0;

const state = {
  cfg: null,
  dates: [],
  day: '',
  hours: [],
  urlHadHours: false,
  wantTasks: false,
  stations: [],
  summaryLoading: false,
  current: null,
  detail: null,
  reasonAuto: '',
  reasonDirty: false,
  reasons: {},
  picked: {},
  okPicks: {},
  otherNg: {},
  ticketSel: {},
  tags: {},
  draftUpdatedAt: '',
  lastSaved: null,
  lastSaveError: '',
  saveTimer: null,
  refreshTimer: null,
  clockTimer: null,
  imageCache: new Map(),
  ticketCache: new Map(),
  focusTs: '',
  tasks: [],
  knownTasks: new Map(),
  pollTimer: null,
  reports: [],
  tickets: [],
  ticketId: null,
  stationCollapsed: {},
};

/* ---------- 基础 ---------- */

function esc(text) {
  return String(text == null ? '' : text).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function enc(text) { return encodeURIComponent(String(text)); }

function todayStr() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

function parseHours(text) {
  return String(text || '').split(',').map((x) => x.trim())
    .filter((x) => /^\d+$/.test(x)).map(Number)
    .filter((h) => h >= 0 && h <= 23)
    .filter((h, i, arr) => arr.indexOf(h) === i).sort((a, b) => a - b);
}

function defaultHours(day) {
  const all = Array.from({ length: 24 }, (_, i) => i);
  if (day !== todayStr()) return all;
  return all.slice(0, new Date().getHours() + 1);
}

function timeText(ts) {
  const text = String(ts || '');
  return text.length >= 19 ? text.slice(11, 19) : text;
}

function hhmm(date) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function toast(message, ms = 2600) {
  const node = document.createElement('div');
  node.className = 'toast';
  node.textContent = message;
  document.body.appendChild(node);
  setTimeout(() => node.remove(), ms);
}

/* 任意 API 请求进行中时显示顶部进度条，让界面「在处理」可见 */
let busyCount = 0;
let busyTimer = null;

function busyStart() {
  busyCount += 1;
  if (busyCount > 1) return;
  clearTimeout(busyTimer);
  busyTimer = setTimeout(() => {
    if (busyCount) $('#busyBar').classList.add('on');
  }, 150);
}

function busyEnd() {
  busyCount = Math.max(0, busyCount - 1);
  if (busyCount) return;
  clearTimeout(busyTimer);
  $('#busyBar').classList.remove('on');
}

function loadingHtml(text) {
  return `<div class="loading"><span class="spinner"></span>${esc(text)}</div>`;
}

async function api(path, options = {}) {
  const opts = Object.assign({ headers: {} }, options);
  if (opts.body && typeof opts.body === 'string') {
    opts.headers['Content-Type'] = 'application/json';
  }
  busyStart();
  try {
    const res = await fetch(path, opts);
    const type = res.headers.get('Content-Type') || '';
    const data = type.includes('application/json') ? await res.json() : await res.text();
    if (!res.ok) {
      const error = new Error((data && data.error) || res.statusText);
      error.status = res.status;
      error.data = data;
      throw error;
    }
    return data;
  } finally {
    busyEnd();
  }
}

function setSaveState(text, isError) {
  const node = $('#saveState');
  node.textContent = text;
  node.classList.toggle('error', !!isError);
}

function renderSaveState() {
  if (state.lastSaveError) { setSaveState(state.lastSaveError, true); return; }
  if (!state.lastSaved) { setSaveState('草稿未保存'); return; }
  setSaveState(`已保存 ${hhmm(state.lastSaved)} · 每 5 分钟自动保存`);
}

/* ---------- 启动 ---------- */

async function boot() {
  wireStatic();
  const params = new URLSearchParams(location.search);
  state.urlHadHours = params.has('hours');
  state.wantTasks = params.get('view') === 'tasks';
  try {
    const [cfg, dates] = await Promise.all([api('/api/config'), api('/api/dates')]);
    state.cfg = cfg;
    state.dates = dates.dates;
  } catch (err) {
    setSaveState('载入配置失败：' + err.message, true);
    return;
  }
  const remembered = localStorage.getItem('ctm_day') || '';
  state.day = params.get('date') || remembered || state.cfg.today;
  if (!state.dates.includes(state.day)) state.day = state.cfg.today;
  state.hours = state.urlHadHours ? parseHours(params.get('hours')) : defaultHours(state.day);

  fillStatic();
  renderHours();
  // 先用配置里的工站/相机渲染骨架，等 /api/summary 期间不出现空白
  state.stations = stationSkeleton();
  state.summaryLoading = true;
  renderStations();
  let draft = null;
  try {
    const res = await api('/api/draft?date=' + state.day);
    draft = res.exists ? res.draft : null;
  } catch (err) { /* 草稿读取失败不阻塞 */ }
  if (draft) {
    if (!state.urlHadHours && (draft.hours || []).length) state.hours = parseHours(draft.hours.join(','));
    applyDraft(draft);
  }
  renderHours();
  renderParams();
  await refreshSummary();
  const first = state.stations.length ? state.stations[0].cameras[0].camera : null;
  if (first) await selectCamera(first);
  startAutosave();
  pollTasks(true);

  if (location.pathname.startsWith('/tickets/')) {
    const id = location.pathname.split('/').filter(Boolean).pop();
    switchView('tickets');
    if (id) openTicket(id);
  }
  const view = params.get('view');
  if (view && ['report', 'archive', 'tickets', 'okng'].includes(view)) switchView(view);
}

function fillStatic() {
  const cfg = state.cfg;
  $('#projectName').value = cfg.project_name || '';
  $('#destination').value = cfg.destination || '';
  $('#lineName').value = cfg.line_name || '';
  $('#reviewer').value = cfg.reviewer || '产线工程师';
  $('#showPhysical').checked = false;
  $('#overlayAnnotations').checked = !!cfg.overlay_annotations;
}

function renderParams() {
  $('#dateText').textContent = state.day;
  $('#saveDraft').disabled = !state.hours.length;
  $('#generate').disabled = !state.hours.length;
  renderSaveState();
}

/* ---------- 小时条 ---------- */

function renderHours() {
  const strip = $('#hoursStrip');
  const today = todayStr();
  const limit = state.day === today ? new Date().getHours() : 23;
  strip.innerHTML = Array.from({ length: 24 }, (_, h) => {
    const on = state.hours.includes(h);
    const disabled = h > limit;
    return `<label class="${on ? 'on' : ''} ${disabled ? 'disabled' : ''}" data-hour="${h}">
      <input type="checkbox" ${on ? 'checked' : ''} ${disabled ? 'disabled' : ''}>${h}</label>`;
  }).join('');
  strip.querySelectorAll('label').forEach((node) => {
    node.addEventListener('click', () => {
      const hour = Number(node.dataset.hour);
      if (hour > limit) return;
      state.hours = state.hours.includes(hour)
        ? state.hours.filter((x) => x !== hour)
        : [...state.hours, hour].sort((a, b) => a - b);
      renderHours();
      renderParams();
      scheduleRefresh();
      saveDraftSoon();
    });
  });
}

function setHours(next) {
  state.hours = next.filter((h, i, arr) => arr.indexOf(h) === i).sort((a, b) => a - b);
  renderHours();
  renderParams();
  scheduleRefresh();
  saveDraftSoon();
}

function wireStatic() {
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => switchView(tab.dataset.view));
  });
  document.querySelectorAll('[data-quick]').forEach((button) => {
    button.addEventListener('click', () => {
      const limit = state.day === todayStr() ? new Date().getHours() : 23;
      const all = Array.from({ length: limit + 1 }, (_, i) => i);
      const mode = button.dataset.quick;
      if (mode === 'all') setHours(all);
      if (mode === 'now') setHours(all);
      if (mode === 'clear') setHours([]);
      if (mode === 'ng') {
        const ngHours = (state.summary && state.summary.ng_hours) || [];
        setHours(ngHours.length ? ngHours : all);
      }
    });
  });
  $('#saveDraft').addEventListener('click', () => saveDraft(false));
  $('#generate').addEventListener('click', generate);
  $('#restoreAuto').addEventListener('click', () => {
    if (!state.current) return;
    $('#reasonText').value = state.reasonAuto || '';
    state.reasons[state.current] = $('#reasonText').value;
    state.reasonDirty = true;
    saveDraftSoon();
  });
  $('#saveReason').addEventListener('click', () => { flushReason(); saveDraft(false); });
  $('#reasonText').addEventListener('input', () => {
    if (!state.current) return;
    state.reasons[state.current] = $('#reasonText').value;
    state.reasonDirty = true;
    saveDraftSoon();
  });
  $('#reopenEntry').addEventListener('click', reopenEntry);
  $('#newTicket').addEventListener('click', () => openTicketForm());
  $('#newTicketTop').addEventListener('click', () => openTicketForm());
  $('#refreshReports').addEventListener('click', loadReports);
  $('#ticketStatusFilter').addEventListener('change', loadTickets);
  $('#ticketSearch').addEventListener('input', debounce(loadTickets, 300));
  $('#okngDate').addEventListener('change', () => {
    $('#okngDateInput').value = $('#okngDate').value || todayStr();
    okngFollowToday = $('#okngDateInput').value === todayStr();
    loadOkng();
  });
  $('#okngDateInput').addEventListener('change', () => {
    okngFollowToday = $('#okngDateInput').value === todayStr();
    loadOkng();
  });
  $('#okngRefresh').addEventListener('click', loadOkng);
  $('#okngExport').addEventListener('click', exportOkng);
  $('#okngAuto').addEventListener('change', () => {
    if (okngTimer) { clearInterval(okngTimer); okngTimer = null; }
    if ($('#okngAuto').checked) {
      okngTimer = setInterval(loadOkng, 60000);
      okngScrollToNow();
    }
  });
  scheduleOkngMidnight();
  $('#taskHead').addEventListener('click', () => {
    $('#taskPanel').classList.toggle('collapsed');
    $('#taskToggle').textContent = $('#taskPanel').classList.contains('collapsed') ? '展开' : '收起';
  });
  ['#projectName', '#destination', '#lineName', '#reviewer'].forEach((sel) => {
    $(sel).addEventListener('input', saveDraftSoon);
  });
  $('#showPhysical').addEventListener('change', saveDraftSoon);
  $('#otherNg').addEventListener('input', () => {
    if (!state.detail) return;
    const value = $('#otherNg').value.trim();
    if (value === '') delete state.otherNg[state.detail.station];
    else state.otherNg[state.detail.station] = Math.max(0, Number(value) || 0);
    saveDraftSoon();
  });
  document.addEventListener('keydown', onKey);
}

function debounce(fn, ms) {
  let timer = null;
  return () => { clearTimeout(timer); timer = setTimeout(fn, ms); };
}

/* ---------- 工站 / 汇总 ---------- */

/* 配置里的工站结构（相机名为字符串）→ 渲染用的骨架数据，NG 计数先置 0 */
function stationSkeleton() {
  return (state.cfg?.stations || []).map((station) => ({
    station: station.station,
    station_name: station.station_name,
    ng: 0,
    cameras: (station.cameras || []).map((name) => ({
      camera: name, note: '', total: 0, ok: 0, ng: 0, tags: {},
    })),
  }));
}

function scheduleRefresh() {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(refreshSummary, 300);
}

async function refreshSummary() {
  if (!state.hours.length) {
    state.stations = [];
    state.summaryLoading = false;
    renderStations();
    setSaveState('未选择统计小时', true);
    return;
  }
  const seq = ++summarySeq;
  state.summaryLoading = true;
  renderStations();
  try {
    const data = await api(`/api/summary?date=${state.day}&hours=${state.hours.join(',')}`);
    if (seq !== summarySeq) return;   // 期间又发起新请求，丢弃过期结果
    state.summaryLoading = false;
    state.summary = data;
    state.stations = data.stations.slice().sort(STATION_ORDER);
    (data.warnings || []).forEach((w) => toast(w, 4000));
    renderStations();
    renderParams();
    if (state.current) {
      const exists = state.stations.some((s) => s.cameras.some((c) => c.camera === state.current));
      if (!exists && state.stations.length) {
        await selectCamera(state.stations[0].cameras[0].camera);
      }
    }
  } catch (err) {
    if (seq !== summarySeq) return;
    state.summaryLoading = false;
    renderStations();
    setSaveState('统计失败：' + err.message, true);
  }
}

function renderStations() {
  const box = $('#stationList');
  if (!state.stations.length) {
    box.innerHTML = state.summaryLoading
      ? loadingHtml('正在加载相机数据…')
      : '<div class="hint" style="padding:10px">没有可用工站，请检查小时勾选。</div>';
    return;
  }
  const pending = state.summaryLoading
    ? '<div class="loading"><span class="spinner"></span>正在统计 NG 数据…</div>'
    : '';
  box.innerHTML = pending + state.stations.map((station) => {
    const collapsed = state.stationCollapsed[station.station];
    const cameras = station.cameras.map((cam) => {
      const active = cam.camera === state.current ? 'active' : '';
      const tagged = Object.values(cam.tags || {}).reduce((a, b) => a + b, 0);
      const dot = cam.ng === 0 ? 'dot' : (tagged ? 'dot tagged' : 'dot ng');
      return `<div class="station-item ${active}" data-camera="${esc(cam.camera)}">
        <span class="${dot}"></span>
        <span class="name">${esc(cam.camera)}</span>
        <span class="note">${esc(cam.note || '')}</span>
        <span class="badge ${cam.ng ? 'hot' : ''}">${cam.ng}</span>
      </div>`;
    }).join('');
    return `<div class="station-group">
      <div class="station-head" data-station="${esc(station.station)}">
        <span>${esc(station.station)} ${esc(station.station_name || '')}</span>
        <small>${station.cameras.length} 台 · NG ${station.ng}</small>
      </div>
      <div class="${collapsed ? 'hidden' : ''}">${cameras}</div>
    </div>`;
  }).join('');
  box.querySelectorAll('.station-item').forEach((node) => {
    node.addEventListener('click', () => selectCamera(node.dataset.camera));
  });
  box.querySelectorAll('.station-head').forEach((node) => {
    node.addEventListener('click', () => {
      const key = node.dataset.station;
      state.stationCollapsed[key] = !state.stationCollapsed[key];
      renderStations();
    });
  });
}

async function selectCamera(camera) {
  if (!camera) return;
  flushReason();
  state.current = camera;
  renderStations();
  $('#cardGrid').innerHTML = loadingHtml('正在载入相机数据…');
  try {
    const detail = await api(`/api/station/${enc(camera)}?date=${state.day}`
      + `&hours=${state.hours.join(',')}&limit=${state.cfg.ng_images_per_station}`);
    state.detail = detail;
    state.reasonAuto = detail.auto_reason || '';
    state.tags[camera] = Object.assign({}, detail.tags || {});
    if (!(camera in state.reasons)) state.reasons[camera] = detail.reason || '';
    if (!(camera in state.picked)) state.picked[camera] = (detail.picked || []).slice();
    if (!(camera in state.okPicks)) state.okPicks[camera] = okPickList(detail.ok_pick);
    if (!(state.ticketSel[detail.station])) state.ticketSel[detail.station] = [];
    renderCamHead();
    renderCards();
    renderReason();
    renderOtherNg();
    renderTicketPick();
  } catch (err) {
    $('#cardGrid').innerHTML = `<div class="hint">载入失败：${esc(err.message)}</div>`;
  }
}

function okPickList(raw) {
  if (Array.isArray(raw)) return raw.filter((item) => item && item.ts);
  if (raw && raw.ts) return [raw];
  return [];
}

function cameraOkPicks(camera) {
  return okPickList(state.okPicks[camera]);
}

function setOkPicks(camera, picks) {
  if (picks.length) {
    state.okPicks[camera] = picks;
  } else {
    delete state.okPicks[camera];
  }
}

function okPickIndex(camera, ts, kind) {
  return cameraOkPicks(camera).findIndex(
    (item) => item.ts === ts && (item.kind || '') === (kind || ''));
}

function okImagesPayload() {
  const out = {};
  Object.keys(state.okPicks).forEach((camera) => {
    const picks = cameraOkPicks(camera);
    if (picks.length) out[camera] = picks;
  });
  return out;
}

function renderCamHead() {
  const d = state.detail;
  if (!d) return;
  const tagged = Object.keys(d.tags || {}).length;
  const picks = cameraOkPicks(d.camera);
  const first = picks[0];
  const okText = picks.length
    ? `已选 ${picks.length} 张（首张为参考图：${timeText(first.ts)} · ${first.kind === 'render' ? '渲染图' : '原图'}）`
    : (d.ok ? '自动（OK 记录中位数）' : '无 OK 记录');
  $('#camHead').innerHTML = `
    <h2>${esc(d.camera)}</h2>
    <span class="meta">${esc(d.station)} ${esc(d.station_name || '')} · ${esc(d.note || '')}</span>
    <span class="meta">NG ${d.ng_total} · 已标注 ${tagged} · 入报告 ${(state.picked[d.camera] || []).length}/${d.limit}</span>
    <span class="okbar">OK 参考图：<b>${okText}</b>
      <button class="mini" id="okPickBtn">图库选择…</button>
      ${picks.length ? '<button class="mini ghost" id="okAutoBtn">恢复自动</button>' : ''}</span>
    <span class="count">点击图片可画框标注；1=误判 2=产品自身NG 3=外部原因 0=清除</span>`;
  $('#okPickBtn').addEventListener('click', openGallery);
  $('#okAutoBtn')?.addEventListener('click', () => {
    setOkPicks(d.camera, []);
    renderCamHead();
    saveDraftSoon();
  });
}

/* ---------- 图片卡片 ---------- */

function currentNgRows() {
  const d = state.detail;
  if (!d) return [];
  const rows = d.ng || [];
  const picked = state.picked[d.camera] || [];
  const shown = rows.slice(-80);
  picked.forEach((ts) => {
    if (!shown.some((row) => row.ts === ts)) {
      const found = rows.find((row) => row.ts === ts);
      if (found) shown.unshift(found);
    }
  });
  return shown;
}

function renderCards() {
  const d = state.detail;
  const grid = $('#cardGrid');
  if (!d) return;
  if (!d.ng_total) {
    grid.innerHTML = '<div class="hint" style="padding:8px">该时段没有 NG 记录。</div>';
    return;
  }
  const picked = new Set(state.picked[d.camera] || []);
  const tags = state.tags[d.camera] || {};
  grid.innerHTML = currentNgRows().map((row) => {
    const tag = tags[row.ts] || '';
    return `<div class="card ${picked.has(row.ts) ? 'picked' : ''}" data-ts="${esc(row.ts)}">
      <div class="thumb" data-act="zoom">
        <span class="placeholder">载入中…</span>
        <span class="tag-dot ${tag}"></span>
        <span class="kind hidden"></span>
      </div>
      <div class="bar">
        <span class="time">${timeText(row.ts)}</span>
        <label title="勾选后写入报告"><input type="checkbox" class="pick"
          ${picked.has(row.ts) ? 'checked' : ''}> 入报告</label>
      </div>
      <div class="chips">
        ${TAG_ORDER.map((t) => `<button class="chip ${tag === t ? 'on' : ''}"
          data-tag="${t}">${TAG_TEXT[t]}</button>`).join('')}
      </div>
    </div>`;
  }).join('');

  grid.querySelectorAll('.card').forEach((card) => {
    const ts = card.dataset.ts;
    card.addEventListener('click', () => { state.focusTs = ts; });
    card.querySelector('[data-act="zoom"]').addEventListener('click', () => {
      state.focusTs = ts;
      openAnnotator(d.camera, ts);
    });
    card.querySelectorAll('.chip').forEach((chip) => {
      chip.addEventListener('click', (event) => {
        event.stopPropagation();
        toggleTag(d.camera, ts, chip.dataset.tag);
      });
    });
    card.querySelector('.pick').addEventListener('change', (event) => {
      event.stopPropagation();
      togglePick(d.camera, ts, event.target.checked);
    });
    loadThumb(card, d.camera, ts);
  });
}

async function loadThumb(card, camera, ts) {
  try {
    const info = await imageInfo(camera, ts, 320);
    const thumb = card.querySelector('.thumb');
    if (!info) {
      thumb.innerHTML = '<span class="placeholder">无图像</span>';
      return;
    }
    const img = document.createElement('img');
    img.src = info.url;
    img.alt = timeText(ts);
    img.loading = 'lazy';
    thumb.prepend(img);
    thumb.querySelector('.placeholder')?.remove();
    const kind = thumb.querySelector('.kind');
    if (info.kind === 'image') {
      kind.textContent = '原图';
      kind.classList.remove('hidden');
    }
  } catch (err) { /* 忽略单张失败 */ }
}

async function imageInfo(camera, ts, width, kind) {
  const key = `${camera}|${ts}|${width}|${kind || ''}`;
  if (state.imageCache.has(key)) return state.imageCache.get(key);
  const suffix = kind ? `&kind=${enc(kind)}` : '';
  const res = await fetch(`/api/image/${enc(camera)}/${enc(ts)}?w=${width}${suffix}`);
  if (!res.ok) {
    state.imageCache.set(key, null);
    return null;
  }
  const actualKind = res.headers.get('X-Image-Kind') || 'image';
  const blob = await res.blob();
  const info = { url: URL.createObjectURL(blob), kind: actualKind };
  state.imageCache.set(key, info);
  return info;
}

function togglePick(camera, ts, on) {
  const list = state.picked[camera] || [];
  const limit = state.cfg.ng_images_per_station;
  if (on && list.length >= limit) {
    toast(`每台相机最多 ${limit} 张`);
    renderCards();
    return;
  }
  state.picked[camera] = on ? [...list, ts] : list.filter((x) => x !== ts);
  renderCards();
  renderCamHead();
  saveDraftSoon();
}

async function toggleTag(camera, ts, tag) {
  const current = (state.tags[camera] || {})[ts] || '';
  const next = current === tag ? null : tag;
  try {
    await api('/api/tag', {
      method: 'POST',
      body: JSON.stringify({
        date: state.day, camera, ts, tag: next,
        reviewer: $('#reviewer').value,
      }),
    });
    state.tags[camera] = state.tags[camera] || {};
    if (next) state.tags[camera][ts] = next; else delete state.tags[camera][ts];
    if (state.detail && state.detail.camera === camera) {
      state.detail.tags = state.tags[camera];
      const row = (state.detail.ng || []).find((item) => item.ts === ts);
      if (row) row.tag = next || '';
      renderCamHead();
      renderCards();
      renderStations();
    }
  } catch (err) { toast('打标失败：' + err.message); }
}

function onKey(event) {
  const tag = (event.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
  if (!$('#modal').classList.contains('hidden')) return;
  const d = state.detail;
  if (!d) return;
  const rows = currentNgRows();
  if (!rows.length) return;
  let index = rows.findIndex((row) => row.ts === state.focusTs);
  if (index < 0) index = rows.length - 1;
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
    index = Math.max(0, Math.min(rows.length - 1, index + (event.key === 'ArrowRight' ? 1 : -1)));
    state.focusTs = rows[index].ts;
    event.preventDefault();
    return;
  }
  if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
    const flat = [];
    state.stations.forEach((s) => s.cameras.forEach((c) => flat.push(c.camera)));
    const at = flat.indexOf(d.camera);
    const next = flat[Math.max(0, Math.min(flat.length - 1, at + (event.key === 'ArrowDown' ? 1 : -1)))];
    if (next && next !== d.camera) { selectCamera(next); event.preventDefault(); }
    return;
  }
  const map = { '1': 'misjudge', '2': 'product', '3': 'external' };
  if (map[event.key]) { toggleTag(d.camera, state.focusTs || rows[rows.length - 1].ts, map[event.key]); event.preventDefault(); }
  if (event.key === '0') { toggleTag(d.camera, state.focusTs || rows[rows.length - 1].ts, (state.tags[d.camera] || {})[state.focusTs] || ''); event.preventDefault(); }
}

/* ---------- 原因分析 ---------- */

function flushReason() {
  if (state.current && $('#reasonText').value !== undefined) {
    state.reasons[state.current] = $('#reasonText').value;
  }
}

function renderReason() {
  const d = state.detail;
  const value = state.reasons[d.camera];
  $('#reasonText').value = value !== undefined ? value
    : (d.reason || d.auto_reason || '');
  state.reasonDirty = false;
  $('#reasonHint').textContent = d.ng_total
    ? '（自动预填事实摘要，可直接改写为对外结论）'
    : '（该时段无 NG，可保持“无 NG，维持现状”）';
}

function renderOtherNg() {
  const d = state.detail;
  if (!d) return;
  const block = (state.summary && state.summary.stations || [])
    .find((item) => item.station === d.station);
  const auto = block ? block.other_ng : 0;
  const override = state.otherNg[d.station];
  $('#otherNg').value = override === undefined ? auto : override;
  $('#otherNgHint').textContent = `自动汇总 ${auto}（按快速 tag）`;
}

/* ---------- 工单 ---------- */

async function ticketsFor(station) {
  if (state.ticketCache.has(station)) return state.ticketCache.get(station);
  const data = await api(`/api/tickets?status=open,in_progress&station=${enc(station)}`);
  state.ticketCache.set(station, data.tickets);
  return data.tickets;
}

function renderTicketPick() {
  const d = state.detail;
  const box = $('#ticketPick');
  if (!d) return;
  const selected = state.ticketSel[d.station] || [];
  ticketsFor(d.station).then((tickets) => {
    if (!tickets.length) {
      box.innerHTML = '<div class="hint">暂无未闭环工单</div>';
      return;
    }
    box.innerHTML = tickets.map((t) => `<label class="ticket">
      <input type="checkbox" data-ticket="${t.id}" ${selected.includes(t.id) ? 'checked' : ''}>
      <span><b>#${t.id}</b> ${esc(t.title)}
      <small>${STATUS_TEXT[t.status] || t.status} · ${esc(t.assignee || '未指派')}</small></span>
    </label>`).join('');
    box.querySelectorAll('input[data-ticket]').forEach((node) => {
      node.addEventListener('change', () => {
        const id = Number(node.dataset.ticket);
        const list = state.ticketSel[d.station] || [];
        state.ticketSel[d.station] = node.checked
          ? [...list.filter((x) => x !== id), id]
          : list.filter((x) => x !== id);
        saveDraftSoon();
      });
    });
  }).catch(() => { box.innerHTML = '<div class="hint">工单载入失败</div>'; });
}

function openTicketForm() {
  const stations = state.cfg.stations || [];
  openModal(`
    <div class="head"><span>新建工单</span><span class="spacer"></span>
      <button class="mini" data-close>关闭</button></div>
    <div class="body">
      <form class="ticket-form" id="ticketForm">
        <label>标题<input type="text" name="title" maxlength="120" required></label>
        <label>工站<select name="station">${stations.map((s) =>
          `<option value="${esc(s.station)}">${esc(s.station)} ${esc(s.station_name || '')}</option>`).join('')}
        </select></label>
        <label>分类<select name="category">
          <option value="misjudge">误判类</option><option value="product">产品自身NG</option>
          <option value="external">外部原因</option><option value="other" selected>其他</option>
        </select></label>
        <label>责任人<input type="text" name="assignee" value="视觉相关人员" maxlength="40"></label>
        <label>计划完成<input type="date" name="due_date"></label>
        <label>问题描述<textarea name="body"></textarea></label>
        <label>改善措施<textarea name="measure"></textarea></label>
        <div class="row"><button type="submit" class="primary">创建</button>
          <button type="button" data-close>取消</button></div>
      </form>
    </div>`);
  const form = $('#ticketForm');
  if (state.detail) form.station.value = state.detail.station;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const data = {};
    new FormData(form).forEach((value, key) => { data[key] = value; });
    data.actor = $('#reviewer').value;
    try {
      const res = await api('/api/tickets', { method: 'POST', body: JSON.stringify(data) });
      toast(`工单 #${res.id} 已创建`);
      state.ticketCache.clear();
      closeModal();
      if (state.detail) renderTicketPick();
      if (state.view === 'tickets') loadTickets();
    } catch (err) { toast('创建失败：' + err.message); }
  });
}

/* ---------- 草稿 ---------- */

function payload() {
  return {
    reasons: state.reasons,
    picked_images: state.picked,
    ok_images: okImagesPayload(),
    other_ng: state.otherNg,
    tickets: state.ticketSel,
    project_name: $('#projectName').value,
    destination: $('#destination').value,
    line_name: $('#lineName').value,
    show_physical: $('#showPhysical').checked,
    overlay_annotations: $('#overlayAnnotations').checked,
    reviewer: $('#reviewer').value,
  };
}

function applyDraft(draft) {
  if (!draft) return;
  const p = draft.payload || {};
  state.reasons = Object.assign({}, p.reasons || {});
  state.picked = Object.assign({}, p.picked_images || {});
  state.okPicks = {};
  Object.keys(p.ok_images || {}).forEach((camera) => {
    const picks = okPickList(p.ok_images[camera]);
    if (picks.length) state.okPicks[camera] = picks;
  });
  state.otherNg = Object.assign({}, p.other_ng || {});
  state.ticketSel = {};
  Object.keys(p.tickets || {}).forEach((k) => { state.ticketSel[k] = (p.tickets[k] || []).map(Number); });
  if (p.project_name) $('#projectName').value = p.project_name;
  if (p.destination) $('#destination').value = p.destination;
  if (p.line_name) $('#lineName').value = p.line_name;
  if (p.reviewer) $('#reviewer').value = p.reviewer;
  $('#showPhysical').checked = !!p.show_physical;
  if (p.overlay_annotations !== undefined) {
    $('#overlayAnnotations').checked = !!p.overlay_annotations;
  }
  state.draftUpdatedAt = draft.updated_at || '';
  state.lastSaved = draft.updated_at ? new Date(draft.updated_at) : null;
}

function saveDraftSoon() {
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(() => saveDraft(false), 1800);
}

async function saveDraft(force) {
  if (!state.hours.length) return;
  const body = {
    date: state.day,
    hours: state.hours,
    payload: payload(),
    actor: $('#reviewer').value,
    base_updated_at: state.draftUpdatedAt || null,
    force: !!force,
  };
  try {
    const res = await api('/api/draft', { method: 'POST', body: JSON.stringify(body) });
    state.draftUpdatedAt = res.updated_at;
    state.lastSaved = new Date();
    state.lastSaveError = '';
    renderSaveState();
  } catch (err) {
    if (err.status === 409) {
      const draft = err.data && err.data.draft;
      const who = draft ? `${draft.updated_by || '他人'} ${draft.updated_at || ''}` : '他人';
      if (confirm(`草稿已被 ${who} 更新，是否用当前内容覆盖？`)) {
        return saveDraft(true);
      }
      if (draft) applyDraft(draft);
      renderSaveState();
      return;
    }
    state.lastSaveError = '草稿保存失败：' + err.message;
    renderSaveState();
  }
}

function startAutosave() {
  setInterval(() => saveDraft(false), 5 * 60 * 1000);
  setInterval(() => { if (!state.lastSaveError) renderSaveState(); }, 30000);
}

/* ---------- 生成 ---------- */

async function generate() {
  if (!state.hours.length) { toast('请至少勾选 1 个小时'); return; }
  flushReason();
  await saveDraft(false);
  const body = {
    date: state.day, hours: state.hours,
    project_name: $('#projectName').value,
    destination: $('#destination').value,
    line_name: $('#lineName').value,
    reviewer: $('#reviewer').value,
    show_physical: $('#showPhysical').checked,
    overlay_annotations: $('#overlayAnnotations').checked,
    reasons: state.reasons,
    other_ng: state.otherNg,
    picked_images: state.picked,
    ok_images: okImagesPayload(),
    tickets: state.ticketSel,
  };
  try {
    await api('/api/report/generate', { method: 'POST', body: JSON.stringify(body) });
    toast('已提交生成任务');
    $('#taskPanel').classList.remove('hidden');
    pollTasks(true);
  } catch (err) {
    toast('提交失败：' + err.message, 4000);
  }
}

/* ---------- 后台任务 ---------- */

async function pollTasks(loop) {
  try {
    const data = await api('/api/tasks?active=1&limit=10');
    state.tasks = data.tasks;
    renderTasks();
    const active = state.tasks.some((t) => t.status === 'pending' || t.status === 'running');
    if (active && loop) {
      clearTimeout(state.pollTimer);
      state.pollTimer = setTimeout(() => pollTasks(true), 700);
    } else if (loop) {
      setTimeout(() => pollTasks(false), 1500);
    }
  } catch (err) { /* 服务重启时忽略 */ }
}

function renderTasks() {
  const panel = $('#taskPanel');
  const list = $('#taskList');
  if (!state.tasks.length) {
    if (!state.wantTasks) {
      panel.classList.add('hidden');
      return;
    }
    panel.classList.remove('hidden');
    $('#taskBadge').textContent = '0';
    list.innerHTML = '<div class="hint" style="padding:12px">暂无后台任务</div>';
    return;
  }
  panel.classList.remove('hidden');
  const active = state.tasks.filter((t) => t.status === 'pending' || t.status === 'running').length;
  $('#taskBadge').textContent = active ? String(active) : '完';
  list.innerHTML = state.tasks.map((task) => {
    const percent = Math.round(task.percent || 0);
    const statusClass = task.status === 'partial' ? 'partial'
      : (task.status === 'failed' ? 'failed' : task.status);
    const running = task.status === 'pending' || task.status === 'running';
    const result = task.result || {};
    const links = [];
    if (result.report_id && (task.status === 'done' || task.status === 'partial')) {
      links.push(`<a href="/api/reports/${result.report_id}/file?kind=xlsx">下载 xlsx</a>`);
      links.push(`<a href="/api/reports/${result.report_id}/file?kind=pdf">下载 pdf</a>`);
    }
    if (result.xlsx_path) links.push(`<a href="#" data-open="${result.report_id}" data-kind="xlsx">打开 xlsx</a>`);
    if (result.report_id && task.status === 'partial') {
      links.push(`<a href="#" data-retry="${result.report_id}">重试落盘</a>`);
    }
    return `<div class="task">
      <div class="line"><span class="title">${esc(task.title)}</span>
        <span class="status ${statusClass}">${task.status}</span></div>
      <div class="progress"><span style="width:${percent}%"></span></div>
      <div class="msg">${esc(task.message || '')} ${percent}%</div>
      <div class="acts">${running ? `<a href="#" data-cancel="${task.task_id}">取消</a>` : ''}${links.join('')}</div>
    </div>`;
  }).join('');
  list.querySelectorAll('[data-cancel]').forEach((node) => {
    node.addEventListener('click', async (event) => {
      event.preventDefault();
      try { await api(`/api/tasks/${node.dataset.cancel}/cancel`, { method: 'POST', body: '{}' }); toast('已请求取消'); }
      catch (err) { toast(err.message); }
      pollTasks(true);
    });
  });
  list.querySelectorAll('[data-retry]').forEach((node) => {
    node.addEventListener('click', async (event) => {
      event.preventDefault();
      try {
        await api(`/api/reports/${node.dataset.retry}/export`, { method: 'POST', body: '{}' });
        toast('已提交落盘任务'); pollTasks(true);
      } catch (err) { toast(err.message); }
    });
  });
  list.querySelectorAll('[data-open]').forEach((node) => {
    node.addEventListener('click', async (event) => {
      event.preventDefault();
      try {
        await api('/api/report/open', { method: 'POST',
          body: JSON.stringify({ report_id: Number(node.dataset.open), kind: node.dataset.kind }) });
      } catch (err) { toast(err.message); }
    });
  });
  state.tasks.forEach((task) => {
    const before = state.knownTasks.get(task.task_id);
    state.knownTasks.set(task.task_id, task.status);
    if (before && before !== task.status
        && ['done', 'partial', 'failed', 'canceled'].includes(task.status)) {
      toast(`任务${task.status === 'done' ? '完成' : '结束：' + task.status}`);
      if (state.view === 'archive') loadReports();
    }
  });
}

/* ---------- 存档 ---------- */

function switchView(view) {
  state.view = view;
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.view === view);
  });
  $('#workbench').classList.toggle('hidden', view !== 'report');
  $('#archiveView').classList.toggle('hidden', view !== 'archive');
  $('#ticketsView').classList.toggle('hidden', view !== 'tickets');
  $('#okngView').classList.toggle('hidden', view !== 'okng');
  if (view === 'archive') loadReports();
  if (view === 'tickets') { $('#ticketDetail').classList.add('hidden'); $('#ticketListPanel').classList.remove('hidden'); loadTickets(); }
  if (view === 'okng') loadOkng();
}

async function loadReports() {
  try {
    const data = await api('/api/reports?limit=80');
    state.reports = data.reports;
    const stats = data.stats || {};
    $('#storageStats').textContent =
      `共 ${stats.count || 0} 份 / ${((stats.bytes || 0) / 1048576).toFixed(1)} MB` +
      (data.reports.some((r) => r.draft) ? `（含草稿 ${data.reports.filter((r) => r.draft).length} 份）` : '');
    $('#reportBody').innerHTML = data.reports.map((report) => {
      if (report.draft) {
        return `<tr class="draft-row">
          <td><span class="badge draft">草稿</span></td>
          <td>${esc(report.day)}</td>
          <td>${esc(report.hours || '')}</td>
          <td class="num">—</td>
          <td class="num">—</td>
          <td>—</td><td>—</td>
          <td>${esc(String(report.created_at || '').replace('T', ' ').slice(0, 19))}</td>
          <td>
            <a href="#" data-draft-edit="${esc(report.day)}">继续编辑</a> ·
            <a href="#" data-draft-delete="${esc(report.day)}">删除草稿</a>
          </td></tr>`;
      }
      return `<tr>
      <td>${report.id}</td>
      <td>${esc(report.day)}</td>
      <td>${esc(report.hours || '')}</td>
      <td class="num">${report.stations || 0}</td>
      <td class="num">${report.products || 0} / ${report.ng || 0}</td>
      <td>${(report.xlsx_size || 0) ? (report.xlsx_size / 1024).toFixed(0) + 'K' : '—'}</td>
      <td>${(report.pdf_size || 0) ? (report.pdf_size / 1024).toFixed(0) + 'K' : '—'}</td>
      <td>${esc(String(report.created_at || '').replace('T', ' ').slice(0, 19))}</td>
      <td>
        <a href="/api/reports/${report.id}/file?kind=xlsx">xlsx</a> ·
        <a href="/api/reports/${report.id}/file?kind=pdf">pdf</a> ·
        <a href="#" data-action="export" data-id="${report.id}">重新落盘</a> ·
        <a href="#" data-action="open" data-id="${report.id}">打开文件</a> ·
        <a href="#" data-action="delete" data-id="${report.id}">删除</a>
      </td></tr>`;
    }).join('') || '<tr><td colspan="9" class="hint">暂无存档</td></tr>';
    $$('#reportBody [data-draft-edit]').forEach((node) => {
      node.addEventListener('click', (event) => {
        event.preventDefault();
        window.location.search = '?date=' + encodeURIComponent(node.dataset.draftEdit);
      });
    });
    $$('#reportBody [data-draft-delete]').forEach((node) => {
      node.addEventListener('click', async (event) => {
        event.preventDefault();
        const draftDay = node.dataset.draftDelete;
        if (!confirm(`确认删除 ${draftDay} 的草稿？`)) return;
        try {
          await api(`/api/draft?date=${encodeURIComponent(draftDay)}`, { method: 'DELETE' });
          toast('草稿已删除');
          loadReports();
        } catch (err) { toast(err.message, 4000); }
      });
    });
    $$('#reportBody [data-action]').forEach((node) => {
      node.addEventListener('click', (event) => {
        event.preventDefault();
        reportAction(node.dataset.action, Number(node.dataset.id));
      });
    });
  } catch (err) { toast('存档载入失败：' + err.message); }
}

async function reportAction(action, id) {
  try {
    if (action === 'export') {
      await api(`/api/reports/${id}/export`, { method: 'POST', body: '{}' });
      toast('已提交落盘任务'); pollTasks(true);
    } else if (action === 'open') {
      await api('/api/report/open', { method: 'POST', body: JSON.stringify({ report_id: id, kind: 'xlsx' }) });
    } else if (action === 'delete') {
      if (!confirm(`确认删除报告 #${id}？库中存档与磁盘文件都会删除。`)) return;
      await api(`/api/reports/${id}`, { method: 'DELETE' });
      toast('已删除'); loadReports();
    }
  } catch (err) { toast(err.message, 4000); }
}

/* ---------- 小时 OK/NG 率 ---------- */

let okngTimer = null;
let okngDatesLoaded = false;
let okngFollowToday = null;   // null=未初始化；true=停留在「今天」，跨 0 点自动切换
let okngMidnightTimer = null;

function okngDay() {
  return $('#okngDateInput').value || state.day || state.cfg?.today || todayStr();
}

/** 停留在「今天」时把日期同步到当前日期；返回是否发生了跨天切换。 */
function okngSyncToday() {
  if (!okngFollowToday) return false;
  const today = todayStr();
  if ($('#okngDateInput').value === today) return false;
  $('#okngDateInput').value = today;
  const hasOption = Array.from($('#okngDate').options).some((o) => o.value === today);
  $('#okngDate').value = hasOption ? today : '';
  return true;
}

/** 在下一个本地 0 点切换日期并重新排程，支持页面跨天常开。 */
function scheduleOkngMidnight() {
  if (okngMidnightTimer) clearTimeout(okngMidnightTimer);
  const now = new Date();
  const next = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 0, 0, 1);
  okngMidnightTimer = setTimeout(() => {
    if (okngSyncToday()) {
      okngDatesLoaded = false;  // 新一天需重新拉取有数据的日期列表
      loadOkng();
    }
    scheduleOkngMidnight();
  }, next - now);
}

async function loadOkng() {
  if (!$('#okngDateInput').value) $('#okngDateInput').value = okngDay();
  if (okngFollowToday === null) okngFollowToday = okngDay() === todayStr();
  okngSyncToday();
  if (!okngDatesLoaded) {
    okngDatesLoaded = true;
    try {
      const data = await api('/api/dates');
      $('#okngDate').innerHTML = '<option value="">— 最近有数据日期 —</option>'
        + (data.dates || []).map((d) => `<option value="${d}">${d}</option>`).join('');
      if (okngFollowToday) $('#okngDate').value = todayStr();
    } catch (err) { /* 日期列表失败不阻塞 */ }
  }
  const day = okngDay();
  const table = $('#okngTable');
  if (!table.querySelector('table')) table.innerHTML = loadingHtml('正在加载 OK/NG 数据…');
  try {
    const data = await api('/api/okng?date=' + encodeURIComponent(day));
    renderOkng(day, data.cameras || []);
  } catch (err) {
    table.innerHTML = `<div class="hint" style="padding:20px">加载失败：${esc(err.message)}</div>`;
    $('#okngSummary').textContent = '';
  }
}

/** 导出当前所选日期的 OK/NG 小时报表（xlsx，含合格率与逐小时明细两张表）。 */
async function exportOkng() {
  const day = okngDay();
  const button = $('#okngExport');
  const text = button.textContent;
  button.disabled = true;
  button.textContent = '生成中…';
  busyStart();
  try {
    const res = await fetch(`/api/okng?date=${enc(day)}&format=xlsx`);
    if (!res.ok) {
      let reason = res.statusText;
      try { reason = (await res.json()).error || reason; } catch (e) { /* 非 JSON 错误体 */ }
      throw new Error(reason);
    }
    const url = URL.createObjectURL(await res.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = `OKNG_${day}.xlsx`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast(`已导出 ${day} 的 OK/NG 报表`);
  } catch (err) {
    toast('导出失败：' + err.message, 4000);
  } finally {
    busyEnd();
    button.disabled = false;
    button.textContent = text;
  }
}

function okngClass(rate, ng) {
  if (ng <= 0) return 'full';
  return rate >= 0.8 ? 'warn' : 'bad';
}

// 自动刷新时把横向视图滚到当前小时，避免最新数据在右侧看不见
function okngScrollToNow() {
  if (!$('#okngAuto').checked) return;
  const box = $('#okngTable');
  const head = box && box.querySelector('thead tr');
  if (!head || !box.clientWidth) return;
  const hour = String(new Date().getHours()).padStart(2, '0');
  const cell = Array.from(head.children).find((th) => th.textContent.trim() === hour);
  if (!cell) return;
  // 首列「相机」是 sticky 的，居中计算要扣掉它的宽度
  const camCol = head.querySelector('th.cam-h');
  const padLeft = camCol ? camCol.offsetWidth : 0;
  const delta = cell.getBoundingClientRect().left - box.getBoundingClientRect().left - padLeft;
  box.scrollLeft += delta - (box.clientWidth - padLeft - cell.offsetWidth) / 2;
}

function renderOkng(day, cameras) {
  const hours = Array.from({ length: 24 }, (_, h) => String(h).padStart(2, '0'));
  const rows = [];
  let gOk = 0, gNg = 0;

  const cellHtml = (c) => {
    const judged = c ? c.ok + c.ng : 0;
    if (!judged) return '<td class="none">·</td>';
    const rate = c.ok / judged;
    return `<td class="${okngClass(rate, c.ng)}" title="OK ${c.ok} / NG ${c.ng}">`
      + `${(rate * 100).toFixed(1)}%<span class="cnt">${c.ok}/${c.ng}</span></td>`;
  };

  rows.push('<table><thead><tr><th class="cam-h">相机</th>'
    + hours.map((h) => `<th>${h}</th>`).join('')
    + '<th style="min-width:96px">合计</th></tr></thead><tbody>');
  if (!cameras.length) {
    rows.push('<tr><td class="cam">-</td><td class="none" colspan="25">该日期无判定样本</td></tr>');
  }
  cameras.forEach((cam) => {
    rows.push(`<tr><td class="cam">${esc(cam.camera)}</td>`);
    hours.forEach((h) => rows.push(cellHtml(cam.hours[h])));
    const judged = cam.ok + cam.ng;
    const rate = judged ? cam.ok / judged : null;
    rows.push(`<td class="total-col">${rate === null ? '-' : (rate * 100).toFixed(1) + '%'}`
      + `<span class="cnt">${cam.ok}/${cam.ng}</span></td></tr>`);
  });
  rows.push('</tbody><tfoot><tr><td class="cam">合计</td>');
  hours.forEach((h) => {
    const ok = cameras.reduce((s, cam) => s + ((cam.hours[h] && cam.hours[h].ok) || 0), 0);
    const ng = cameras.reduce((s, cam) => s + ((cam.hours[h] && cam.hours[h].ng) || 0), 0);
    gOk += ok; gNg += ng;
    rows.push(cellHtml(ok + ng ? { ok, ng } : null));
  });
  const gJudged = gOk + gNg;
  rows.push(`<td class="total-col">${gJudged ? ((gOk / gJudged) * 100).toFixed(1) + '%' : '-'}`
    + `<span class="cnt">${gOk}/${gNg}</span></td></tr></tfoot></table>`);
  $('#okngTable').innerHTML = rows.join('');
  $('#okngSummary').textContent = `${day} · ${cameras.length} 台相机 · 判定样本 ${gJudged}`;
  okngScrollToNow();
}

/* ---------- 工单列表 / 详情 ---------- */

async function loadTickets() {
  const status = $('#ticketStatusFilter').value;
  const q = $('#ticketSearch').value.trim();
  try {
    const data = await api(`/api/tickets?status=${enc(status)}&q=${enc(q)}&limit=300`);
    state.tickets = data.tickets;
    const counts = data.counts || {};
    $('#ticketCounts').textContent = Object.keys(STATUS_TEXT).map((k) => `${STATUS_TEXT[k]} ${counts[k] || 0}`).join(' · ');
    $('#ticketBody').innerHTML = data.tickets.map((t) => `<tr data-id="${t.id}" style="cursor:pointer">
      <td>${t.id}</td><td>${esc(t.title)}</td><td>${esc(t.station || '')}</td>
      <td>${esc(t.category || '')}</td>
      <td><span class="status ${t.status}">${STATUS_TEXT[t.status] || t.status}</span></td>
      <td>${esc(t.assignee || '')}</td>
      <td>${esc(String(t.updated_at || '').replace('T', ' ').slice(5, 16))}</td></tr>`).join('')
      || '<tr><td colspan="7" class="hint">暂无工单</td></tr>';
    $$('#ticketBody tr[data-id]').forEach((row) => {
      row.addEventListener('click', () => {
        openTicket(row.dataset.id);
        history.replaceState(null, '', `/tickets/${row.dataset.id}`);
      });
    });
  } catch (err) { toast('工单载入失败：' + err.message); }
}

async function openTicket(id) {
  switchView('tickets');
  $('#ticketListPanel').classList.add('hidden');
  const box = $('#ticketDetail');
  box.classList.remove('hidden');
  box.innerHTML = '<div class="hint">载入中…</div>';
  try {
    const t = await api(`/api/tickets/${id}`);
    const events = (t.events || []).slice().reverse();
    const reports = t.reports || [];
    const actions = [];
    if (t.status === 'open') actions.push(`<button class="mini primary" data-status="in_progress">开始处理</button>`);
    if (t.status === 'in_progress' || t.status === 'open') actions.push(`<button class="mini" data-status="done">标记完成</button>`);
    if (t.status === 'done') actions.push(`<button class="mini" data-status="closed">关闭工单</button>`);
    if (t.status === 'closed') actions.push(`<button class="mini" data-status="open">重新打开</button>`);
    box.innerHTML = `<div class="panel-head">
        <button class="mini" id="backTickets">← 返回</button>
        <h2>#${t.id} ${esc(t.title)}</h2>
        <span class="status ${t.status}">${STATUS_TEXT[t.status] || t.status}</span>
        <span class="spacer"></span>${actions.join(' ')}
      </div>
      <div class="meta">工站 ${esc(t.station || '-')} · 分类 ${esc(t.category || '-')} ·
        责任人 ${esc(t.assignee || '-')} · 计划 ${esc(t.due_date || '-')} ·
        创建 ${esc(t.created_by || '-')} ${esc(String(t.created_at || '').replace('T', ' ').slice(0, 16))}</div>
      <div class="ticket-form">
        <label>问题描述<textarea id="ticketBodyText">${esc(t.body || '')}</textarea></label>
        <label>改善措施<textarea id="ticketMeasureText">${esc(t.measure || '')}</textarea></label>
        <div class="row"><label>责任人<input type="text" id="ticketAssignee" value="${esc(t.assignee || '')}"></label>
          <label>计划完成<input type="date" id="ticketDue" value="${esc(t.due_date || '')}"></label>
          <button class="mini primary" id="saveTicket">保存</button></div>
      </div>
      <h3>关联报告</h3>
      <div>${reports.map((r) => `<div class="ticket">
        <a href="/api/reports/${r.report_id}/file?kind=xlsx">报告 #${r.report_id}</a>
        <span class="hint">${esc(r.day || '')} · ${esc(r.station || '')}</span>
        <button class="mini ghost" data-unlink="${r.report_id}">移除</button></div>`).join('')
        || '<div class="hint">暂无关联报告；在报告工作台勾选工单后生成即可自动关联。</div>'}</div>
      <h3>讨论</h3>
      <ul class="timeline">${events.map((e) => `<li>
        <b>${esc(e.actor || '系统')}</b><time>${esc(String(e.ts || '').replace('T', ' ').slice(0, 16))}</time>
        <div>${esc(eventText(e))}</div></li>`).join('')}</ul>
      <div class="row"><input type="text" id="commentText" placeholder="补充说明…" style="flex:1">
        <button class="mini" id="addComment">发表</button></div>`;
    $('#backTickets').addEventListener('click', () => {
      history.replaceState(null, '', '/report');
      switchView('tickets');
    });
    box.querySelectorAll('[data-status]').forEach((node) => {
      node.addEventListener('click', () => patchTicket(t.id, { status: node.dataset.status }));
    });
    $('#saveTicket').addEventListener('click', () => patchTicket(t.id, {
      body: $('#ticketBodyText').value,
      measure: $('#ticketMeasureText').value,
      assignee: $('#ticketAssignee').value,
      due_date: $('#ticketDue').value || null,
    }));
    $('#addComment').addEventListener('click', async () => {
      const text = $('#commentText').value.trim();
      if (!text) return;
      try {
        await api(`/api/tickets/${t.id}/comments`, {
          method: 'POST', body: JSON.stringify({ text, actor: $('#reviewer').value }) });
        openTicket(t.id);
      } catch (err) { toast(err.message); }
    });
    box.querySelectorAll('[data-unlink]').forEach((node) => {
      node.addEventListener('click', async () => {
        try {
          await api(`/api/tickets/${t.id}/reports/${node.dataset.unlink}`, { method: 'DELETE' });
          openTicket(t.id);
        } catch (err) { toast(err.message); }
      });
    });
  } catch (err) {
    box.innerHTML = `<div class="hint">载入失败：${esc(err.message)}</div>`;
  }
}

function eventText(event) {
  const data = event.payload || event.data || {};
  if (event.kind === 'comment') return data.text || '评论';
  if (data.from || data.to) {
    return `状态：${STATUS_TEXT[data.from] || data.from || '-'} → ${STATUS_TEXT[data.to] || data.to || '-'}`;
  }
  const text = { created: '创建工单', status: '状态变更', assign: '责任人变更',
    measure: '更新改善措施', link_report: '关联报告', unlink_report: '移除报告' }[event.kind];
  if (text) return data.text ? `${text}：${data.text}` : text;
  return JSON.stringify(data);
}

async function patchTicket(id, patch) {
  patch.actor = $('#reviewer').value;
  try {
    await api(`/api/tickets/${id}`, { method: 'PATCH', body: JSON.stringify(patch) });
    toast('已保存');
    openTicket(id);
  } catch (err) { toast(err.message, 4000); }
}

/* ---------- 重新选择日期 ---------- */

async function reopenEntry() {
  try {
    const res = await api('/api/open_entry', {
      method: 'POST', body: JSON.stringify({ date: state.day, hours: state.hours }) });
    toast('已请求桌面端弹出日期选择…', 4000);
    waitEntry(res.stamp);
  } catch (err) { toast(err.message, 4000); }
}

async function waitEntry(stamp) {
  try {
    const data = await api('/api/entry_result');
    if (data.result && data.stamp && data.stamp >= stamp) {
      applyEntry(data.result);
      return;
    }
  } catch (err) { /* 忽略 */ }
  setTimeout(() => waitEntry(stamp), 900);
}

async function applyEntry(result) {
  state.day = result.date || state.day;
  state.hours = parseHours((result.hours || []).join(','));
  if (!state.hours.length) state.hours = defaultHours(state.day);
  localStorage.setItem('ctm_day', state.day);
  $('#dateText').textContent = state.day;
  renderHours();
  renderParams();
  await refreshSummary();
  const first = state.stations.length ? state.stations[0].cameras[0].camera : null;
  if (first) await selectCamera(first);
  saveDraft(false);
}

/* ---------- 图库（OK 参考图选择） ---------- */

function openGallery() {
  const d = state.detail;
  if (!d) return;
  openModal(`<div class="head">
      <span>图库 · ${esc(d.camera)}</span>
      <span class="spacer"></span>
      <span class="hint" style="font-weight:400">可多选（原图/渲染图），首张为 OK 参考图，其余追加进报告</span>
      <button class="mini" data-close>关闭</button>
    </div>
    <div class="body">
      <div class="gallery-filter">
        <label>起始 <input type="time" id="gStart" value="00:00"></label>
        <label>结束 <input type="time" id="gEnd" value="23:59"></label>
        <label>类型 <select id="gKind">
          <option value="all">全部</option>
          <option value="render">渲染图</option>
          <option value="image">原图</option>
        </select></label>
        <button class="mini" id="gReload">筛选</button>
        <button class="mini ghost" id="gClear">清空已选</button>
        <span class="hint" id="gHint">载入中…</span>
      </div>
      <div class="gallery" id="gallery"></div>
    </div>`);
  $('#gReload').addEventListener('click', loadGallery);
  $('#gClear').addEventListener('click', () => {
    setOkPicks(d.camera, []);
    markGallerySelection();
    renderCamHead();
    saveDraftSoon();
    toast('已清空 OK 图选择');
  });
  loadGallery();
}

async function loadGallery() {
  const d = state.detail;
  if (!d) return;
  const grid = $('#gallery');
  const hint = $('#gHint');
  grid.innerHTML = '';
  hint.textContent = '载入中…';
  try {
    const data = await api(`/api/gallery?camera=${enc(d.camera)}&date=${state.day}`
      + `&start=${$('#gStart').value}&end=${$('#gEnd').value}`
      + `&kind=${$('#gKind').value}&limit=300`);
    if (!data.items.length) {
      grid.dataset.total = '';
      hint.textContent = '该时间段没有图片';
      return;
    }
    hint.textContent = `${data.items.length} 张（${data.start}–${data.end}），点图多选`;
    grid.dataset.total = String(data.items.length);
    grid.dataset.range = `${data.start}–${data.end}`;
    grid.innerHTML = data.items.map((item) => `
      <figure class="gallery-item" data-ts="${esc(item.ts)}" data-kind="${esc(item.kind)}">
        <img loading="lazy" src="${item.url}" alt="${timeText(item.ts)}">
        <span class="pick-badge"></span>
        <figcaption><span>${timeText(item.ts)}</span>
          <span>${item.kind === 'render' ? '渲染图' : '原图'}</span></figcaption>
        <div class="ops">
          <button class="mini primary" data-act="use">加入报告</button>
          <button class="mini ghost" data-act="annotate">画框标注</button>
        </div>
      </figure>`).join('');
    grid.querySelectorAll('.gallery-item').forEach((node) => {
      const ts = node.dataset.ts;
      const kind = node.dataset.kind;
      node.querySelector('[data-act="use"]').addEventListener('click', () => {
        toggleOkPick(ts, kind);
        markGallerySelection();
        renderCamHead();
        saveDraftSoon();
      });
      node.querySelector('[data-act="annotate"]').addEventListener('click', () => {
        openAnnotator(d.camera, ts, kind);
      });
    });
    markGallerySelection();
  } catch (err) {
    hint.textContent = `载入失败：${err.message}`;
  }
}

function toggleOkPick(ts, kind) {
  const d = state.detail;
  if (!d) return;
  const picks = cameraOkPicks(d.camera).slice();
  const index = okPickIndex(d.camera, ts, kind);
  if (index >= 0) {
    picks.splice(index, 1);
    toast(`已移出：${timeText(ts)}`);
  } else {
    const limit = state.cfg.ng_images_per_station || 100;
    if (picks.length >= limit) { toast(`每台相机最多 ${limit} 张 OK 图`, 3000); return; }
    picks.push({ ts, kind });
    toast(`已加入第 ${picks.length} 张：${timeText(ts)}`);
  }
  setOkPicks(d.camera, picks);
}

function markGallerySelection() {
  const d = state.detail;
  const grid = $('#gallery');
  if (!d || !grid) return;
  const picks = cameraOkPicks(d.camera);
  grid.querySelectorAll('.gallery-item').forEach((node) => {
    const index = picks.findIndex(
      (item) => item.ts === node.dataset.ts && (item.kind || '') === node.dataset.kind);
    node.classList.toggle('selected', index >= 0);
    const badge = node.querySelector('.pick-badge');
    if (badge) badge.textContent = index >= 0 ? String(index + 1) : '';
    const btn = node.querySelector('[data-act="use"]');
    if (btn) btn.textContent = index >= 0 ? '移出报告' : '加入报告';
  });
  const hint = $('#gHint');
  const total = grid.dataset.total;
  if (hint) {
    hint.textContent = total
      ? `${total} 张（${grid.dataset.range || ''}）· 已选 ${picks.length} 张`
      : `已选 ${picks.length} 张`;
  }
}

/* ---------- 标注 ---------- */

async function openAnnotator(camera, ts, kind) {
  const width = state.cfg.image_max_edge;
  let info;
  try { info = await imageInfo(camera, ts, width, kind); } catch (err) { info = null; }
  if (!info) { toast('该时刻没有可用图像'); return; }
  let ann = { shapes: [] };
  try { ann = await api(`/api/annotation/${enc(camera)}/${enc(ts)}`); } catch (err) { /* 忽略 */ }
  openModal(`<div class="head">
      <span>${esc(camera)} · ${timeText(ts)}</span>
      <span class="spacer"></span>
      <span class="hint" style="font-weight:400">拖动鼠标画框；标注坐标与报告嵌入图一致</span>
      <button class="mini" data-close>关闭</button>
    </div>
    <div class="body">
      <div class="annot-wrap">
        <div class="canvas-box" id="canvasBox">
          <img id="annotImg" src="${info.url}" alt="NG">
          <canvas id="annotCanvas"></canvas>
        </div>
        <div>
          <div class="hint" style="margin-bottom:6px">标注说明（会写入 annotations，供复核）</div>
          <div class="shape-list" id="shapeList"></div>
          <div class="row"><button class="mini ghost" id="clearShapes">清空画框</button></div>
          <label>复核人<input type="text" id="annotReviewer" value="${esc($('#reviewer').value)}"></label>
          <div class="row"><button class="mini primary" id="saveAnnotation">保存标注</button></div>
        </div>
      </div>
    </div>`);
  const img = $('#annotImg');
  const canvas = $('#annotCanvas');
  const shapes = (ann.shapes || []).map((shape) => ({
    label: shape.label || '误判',
    description: shape.description || '',
    points: (shape.points || []).map((p) => [Number(p[0]), Number(p[1])]),
  }));
  let dragStart = null;
  let draftShape = null;

  const fit = () => {
    canvas.width = img.naturalWidth || 1;
    canvas.height = img.naturalHeight || 1;
    draw();
  };
  if (img.complete) fit(); else img.addEventListener('load', fit);

  function draw() {
    const ctx = canvas.getContext('2d');
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    ctx.lineWidth = Math.max(2, Math.round(w / 400));
    ctx.font = `${Math.max(12, Math.round(w / 60))}px "Microsoft YaHei", sans-serif`;
    const all = draftShape ? shapes.concat([draftShape]) : shapes;
    all.forEach((shape) => {
      const p = shape.points || [];
      if (p.length < 2) return;
      const x = Math.min(p[0][0], p[1][0]);
      const y = Math.min(p[0][1], p[1][1]);
      const rw = Math.abs(p[1][0] - p[0][0]);
      const rh = Math.abs(p[1][1] - p[0][1]);
      ctx.strokeStyle = '#ffcc00';
      ctx.fillStyle = 'rgba(255, 204, 0, .14)';
      ctx.fillRect(x, y, rw, rh);
      ctx.strokeRect(x, y, rw, rh);
      const text = shape.label || '误判';
      const tw = ctx.measureText(text).width + 8;
      ctx.fillStyle = 'rgba(0, 0, 0, .7)';
      ctx.fillRect(x, Math.max(0, y - 20), tw, 20);
      ctx.fillStyle = '#ffdd55';
      ctx.fillText(text, x + 4, Math.max(15, y - 5));
    });
  }

  function naturalPoint(event) {
    const rect = canvas.getBoundingClientRect();
    return [
      Math.round((event.clientX - rect.left) * canvas.width / rect.width),
      Math.round((event.clientY - rect.top) * canvas.height / rect.height),
    ];
  }

  canvas.addEventListener('mousedown', (event) => {
    dragStart = naturalPoint(event);
    draftShape = { label: '误判', description: '', points: [dragStart, dragStart] };
  });
  canvas.addEventListener('mousemove', (event) => {
    if (!dragStart) return;
    draftShape.points = [dragStart, naturalPoint(event)];
    draw();
  });
  window.addEventListener('mouseup', function onUp() {
    if (!dragStart) return;
    dragStart = null;
    const p = draftShape.points;
    if (Math.abs(p[1][0] - p[0][0]) > 4 && Math.abs(p[1][1] - p[0][1]) > 4) {
      shapes.push(draftShape);
    }
    draftShape = null;
    draw();
    renderShapeList();
  });

  function renderShapeList() {
    const list = $('#shapeList');
    list.innerHTML = shapes.map((shape, index) => `<div class="shape-item">
      <button class="mini danger del" data-index="${index}">删除</button>
      <input type="text" data-label="${index}" value="${esc(shape.label)}" placeholder="标签">
      <input type="text" data-desc="${index}" value="${esc(shape.description)}" placeholder="说明（可选）">
    </div>`).join('') || '<div class="hint">尚未画框</div>';
    list.querySelectorAll('[data-label]').forEach((input) => {
      input.addEventListener('input', () => { shapes[Number(input.dataset.label)].label = input.value; draw(); });
    });
    list.querySelectorAll('[data-desc]').forEach((input) => {
      input.addEventListener('input', () => { shapes[Number(input.dataset.desc)].description = input.value; });
    });
    list.querySelectorAll('[data-index]').forEach((button) => {
      button.addEventListener('click', () => { shapes.splice(Number(button.dataset.index), 1); draw(); renderShapeList(); });
    });
  }
  renderShapeList();

  $('#clearShapes').addEventListener('click', () => { shapes.length = 0; draw(); renderShapeList(); });
  $('#saveAnnotation').addEventListener('click', async () => {
    try {
      await api('/api/annotation', {
        method: 'POST',
        body: JSON.stringify({
          camera, ts, shapes,
          image_width: img.naturalWidth,
          image_height: img.naturalHeight,
          reviewer: $('#annotReviewer').value,
          description: 'NG 画框标注',
        }),
      });
      toast('标注已保存');
      closeModal();
    } catch (err) { toast('保存失败：' + err.message, 4000); }
  });
}

/* ---------- 弹层 ---------- */

function openModal(html) {
  $('#modalBox').innerHTML = html;
  $('#modal').classList.remove('hidden');
  $$('#modalBox [data-close]').forEach((node) => node.addEventListener('click', closeModal));
}

function closeModal() {
  $('#modal').classList.add('hidden');
  $('#modalBox').innerHTML = '';
}

/* ---------- 启动 ---------- */

document.addEventListener('DOMContentLoaded', boot);
