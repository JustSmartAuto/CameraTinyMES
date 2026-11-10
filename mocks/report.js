/* 改善分析报告工作台 Mock — 交互逻辑（数据来自 localStorage 假数据） */
(function () {
  const M = MockData;
  const $ = (s) => document.querySelector(s);
  const pad = M.pad;

  const state = {
    day: M.today(),
    hours: new Set(),
    activeCamera: null,
    draft: null,
    dirty: false,
    saveTimer: null,
    autoCountdown: 300,
    speed: 1,
    limit: 8,
    genTimers: [],
    view: 'write'
  };

  // ---------------- 通用 ----------------
  function toast(msg) {
    const t = $('#toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.remove('show'), 2200);
  }

  function currentHour() { return new Date().getHours(); }
  function isToday(day) { return day === M.today(); }

  function keyOf(camera, ts) { return camera + '|' + ts; }

  function tagOf(camera, ts) {
    return state.draft.tags[keyOf(camera, ts)] || null;
  }

  function boxesOf(camera, ts) {
    return state.draft.boxes[keyOf(camera, ts)] || [];
  }

  function fmtTime(ts) { return ts.slice(11, 19); }

  // ---------------- 入口对话框 ----------------
  const entry = {
    selectedHours: new Set(),
    init() {
      const sel = $('#entryDate');
      sel.innerHTML = M.dates().map(d =>
        `<option value="${d}">${d}${d === M.today() ? '（今天）' : ''}</option>`
      ).join('');
      sel.value = state.day;
      sel.addEventListener('change', () => this.setDay(sel.value));
      this.setDay(sel.value);
      $('#entryConfirm').addEventListener('click', () => this.confirm());
      $('#entryCancel').addEventListener('click', () => {
        $('#entryOverlay').classList.add('hidden');
        if (!state.draft) history.length > 1 ? history.back() : (location.href = 'index.html');
      });
      $('#entryHours').addEventListener('click', (e) => {
        const chip = e.target.closest('.hour-chip');
        if (!chip || chip.classList.contains('disabled')) return;
        const h = +chip.dataset.h;
        if (this.selectedHours.has(h)) this.selectedHours.delete(h);
        else this.selectedHours.add(h);
        this.renderHours();
      });
      document.querySelectorAll('#entryOverlay [data-quick]').forEach(b =>
        b.addEventListener('click', () => this.quick(b.dataset.quick))
      );
    },
    setDay(day) {
      const max = isToday(day) ? currentHour() : 23;
      const old = this.selectedHours;
      this.selectedHours = new Set();
      for (let h = 0; h <= max; h++) this.selectedHours.add(h);
      // 保留用户已选（若仍合法）
      if (old && old.size) {
        const kept = [...old].filter(h => h <= max);
        if (kept.length) this.selectedHours = new Set(kept);
      }
      this.renderHours();
    },
    renderHours() {
      const day = $('#entryDate').value;
      const max = isToday(day) ? currentHour() : 23;
      const wrap = $('#entryHours');
      wrap.innerHTML = '';
      for (let h = 0; h < 24; h++) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'hour-chip' + (this.selectedHours.has(h) ? ' on' : '') + (h > max ? ' disabled' : '');
        b.dataset.h = h;
        b.textContent = pad(h);
        wrap.appendChild(b);
      }
      $('#entryHint').textContent = `已选 ${this.selectedHours.size} 小时`;
    },
    quick(type) {
      const day = $('#entryDate').value;
      const max = isToday(day) ? currentHour() : 23;
      if (type === 'all') this.selectedHours = new Set(Array.from({ length: max + 1 }, (_, i) => i));
      if (type === 'upto') this.selectedHours = new Set(Array.from({ length: max + 1 }, (_, i) => i));
      if (type === 'clear') this.selectedHours = new Set();
      if (type === 'ng') {
        // 有 NG 的小时（任一台相机）
        const set = new Set();
        for (const st of M.STATIONS) {
          for (const cam of st.cameras) {
            const stats = M.hourStats(day, cam);
            for (let h = 0; h <= max; h++) if (stats[h].ng > 0) set.add(h);
          }
        }
        this.selectedHours = set;
      }
      this.renderHours();
    },
    confirm() {
      if (!this.selectedHours.size) { toast('至少勾选 1 个小时'); return; }
      state.day = $('#entryDate').value;
      state.hours = new Set([...this.selectedHours].sort((a, b) => a - b));
      $('#entryOverlay').classList.add('hidden');
      start();
    },
    open() {
      $('#entryDate').value = state.day;
      this.setDay(state.day);
      // 回填当前小时
      if (state.hours.size) this.selectedHours = new Set(state.hours);
      this.renderHours();
      $('#entryOverlay').classList.remove('hidden');
    }
  };

  // ---------------- 启动 ----------------
  function start() {
    const saved = M.getDraft(state.day);
    state.draft = saved || M.emptyDraft(state.day, [...state.hours]);
    state.lastSaved = saved && saved.updatedAt ? saved.updatedAt : null;
    state.draft.day = state.day;
    state.draft.hours = [...state.hours].sort((a, b) => a - b);
    // 兼容旧草稿缺字段
    ['reasons', 'picked', 'removed', 'tags', 'boxes', 'otherNg', 'tickets', 'header'].forEach(k => {
      if (!state.draft[k]) state.draft[k] = k === 'header' ? {
        project: '锦恒SAB项目', destination: '锦州', line: 'Line1', includePhysical: false
      } : {};
    });
    state.activeCamera = state.draft._activeCamera || firstCameraWithNG() || M.STATIONS[0].cameras[0];
    $('#app').hidden = false;
    $('#archiveView').hidden = true;
    state.view = 'write';
    renderAll();
    startAutoSave();
    if (saved) toast(`已恢复 ${saved.updatedAt ? new Date(saved.updatedAt).toLocaleTimeString() : ''} 的草稿`);
  }

  function firstCameraWithNG() {
    for (const st of M.STATIONS) {
      for (const cam of st.cameras) {
        const ev = M.ngEvents(state.day, cam, [...state.hours]);
        if (ev.length) return cam;
      }
    }
    return null;
  }

  // ---------------- 计算 ----------------
  function cameraEval(cam) {
    const events = M.ngEvents(state.day, cam, [...state.hours]);
    const summary = M.summary(state.day, [...state.hours]).find(r => r.camera === cam);
    const removed = new Set(state.draft.removed[cam] || []);
    const shown = events.filter(e => !removed.has(e.ts));
    let misjudge = 0, product = 0, external = 0;
    for (const e of events) {
      const t = tagOf(cam, e.ts);
      if (t === 'misjudge') misjudge++;
      else if (t === 'product') product++;
      else if (t === 'external') external++;
    }
    const H = events.length;
    const J = Math.min(H, misjudge + external);
    const I = Math.max(0, H - J);
    const override = state.draft.otherNg[cam];
    return {
      camera: cam, total: summary ? summary.total : 0, H, I, J,
      misjudge, product, external, events, shown,
      autoOther: J, otherOverride: typeof override === 'number' ? override : null,
      hasTag: misjudge + product + external > 0
    };
  }

  function stationAgg(cameras) {
    const rows = cameras.map(cameraEval);
    return {
      total: rows.reduce((s, r) => s + r.total, 0),
      H: rows.reduce((s, r) => s + r.H, 0),
      rows
    };
  }

  function factTextFor(cam) {
    const r = cameraEval(cam);
    const info = M.cameraInfo(cam);
    const hours = [...state.hours].sort((a, b) => a - b);
    const rate = r.total ? (r.H / r.total * 100) : 0;
    const times = r.events.map(e => fmtTime(e.ts));
    const span = times.length ? `${times[0]}~${times[times.length - 1]}` : '—';
    // 连续 NG：间隔 <= 120 秒算连续
    let run = 1, maxRun = 1;
    for (let i = 1; i < r.events.length; i++) {
      const a = new Date(r.events[i - 1].ts).getTime();
      const b = new Date(r.events[i].ts).getTime();
      if (b - a <= 120000) { run++; maxRun = Math.max(maxRun, run); }
      else run = 1;
    }
    const extra = maxRun >= 3 ? `；存在连续 ${maxRun} 件 NG，疑似工装/来料异常` : '';
    return `视觉NG ${r.H} 件（不良率 ${rate.toFixed(1)}%）；检测项：${info.note || '—'}；
统计时段：${M.hoursText(hours)}；NG 时段：${span}；配方：SAB_MAIN.jobx${extra}。`;
  }

  // ---------------- 渲染 ----------------
  function renderAll() {
    $('#dateView').textContent = state.day;
    renderHeaderInputs();
    renderHours();
    renderStations();
    renderStationDetail();
    updateSaveState();
    $('#reviewerLabel').textContent = '操作人：' + (state.draft.reviewer || '产线工程师');
  }

  function renderHeaderInputs() {
    const h = state.draft.header;
    $('#hdrProject').value = h.project || '';
    $('#hdrDestination').value = h.destination || '';
    $('#hdrLine').value = h.line || '';
    $('#hdrPhysical').checked = !!h.includePhysical;
  }

  function renderHours() {
    const wrap = $('#mainHours');
    wrap.innerHTML = '';
    const max = isToday(state.day) ? currentHour() : 23;
    for (let h = 0; h < 24; h++) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'hour-chip' + (state.hours.has(h) ? ' on' : '') + (h > max ? ' disabled' : '');
      b.dataset.h = h;
      b.textContent = pad(h);
      wrap.appendChild(b);
    }
    $('#hoursSummary').textContent = `已选 ${state.hours.size} 小时`;
    $('#generateBtn').disabled = state.hours.size === 0;
  }

  function renderStations() {
    const list = $('#stationList');
    list.innerHTML = '';
    let camCount = 0;
    for (const st of M.STATIONS) {
      const agg = stationAgg(st.cameras);
      camCount += st.cameras.length;
      const g = document.createElement('div');
      g.className = 'st-group';
      const head = document.createElement('div');
      head.className = 'st-group-head';
      head.innerHTML = `<span>${st.station}</span><span style="font-weight:400">${st.name}</span><span class="count">NG ${agg.H}</span>`;
      g.appendChild(head);
      for (const row of agg.rows) {
        const item = document.createElement('div');
        item.className = 'st-item' + (row.camera === state.activeCamera ? ' active' : '');
        const t = tagOf(row.camera, row.events[0]?.ts);
        const hasReason = !!(state.draft.reasons[row.camera] || '').trim();
        const dotCls = hasReason ? 'done' : (row.hasTag ? 'product' : 'empty');
        item.innerHTML = `
          <span class="dot ${dotCls}" title="${hasReason ? '原因已写' : row.hasTag ? '已打 tag' : '未处理'}"></span>
          <span class="cam">${row.camera}</span>
          <span class="sub">${row.H ? 'NG ' + row.H : '无 NG'}</span>`;
        item.addEventListener('click', () => {
          state.activeCamera = row.camera;
          state.draft._activeCamera = row.camera;
          scheduleSave(500);
          renderStations();
          renderStationDetail();
        });
        g.appendChild(item);
      }
      list.appendChild(g);
    }
    $('#camCount').textContent = `${camCount} 台相机`;
  }

  function renderStationDetail() {
    const cam = state.activeCamera;
    const info = M.cameraInfo(cam);
    const r = cameraEval(cam);
    $('#curStation').textContent = info.station;
    $('#curCamera').textContent = `${cam} · ${info.model || ''}`;
    $('#reasonWho').textContent = `责任人：${state.draft.reviewer || '产线工程师'}`;

    // 统计
    const jShown = r.otherOverride !== null ? r.otherOverride : r.autoOther;
    const iShown = Math.max(0, r.H - jShown);
    $('#statRow').innerHTML = `
      <div class="stat"><div class="k">生产总数</div><div class="v">${r.total.toLocaleString()}</div></div>
      <div class="stat"><div class="k">NG 数量</div><div class="v" style="color:var(--red)">${r.H}</div></div>
      <div class="stat"><div class="k">视觉NG</div><div class="v">${iShown}</div></div>
      <div class="stat"><div class="k">其他NG</div><div class="v" style="color:var(--blue)">${jShown}</div></div>
      <div class="stat"><div class="k">不良率</div><div class="v">${(r.total ? r.H / r.total * 100 : 0).toFixed(2)}<small>%</small></div></div>`;

    // 卡片（均匀抽样展示，最多 limit 张）
    const total = r.shown.length;
    let show = r.shown;
    if (show.length > state.limit) {
      const arr = [];
      for (let i = 0; i < state.limit; i++) {
        arr.push(show[Math.round(i * (show.length - 1) / (state.limit - 1))]);
      }
      show = arr;
    }
    const cards = $('#cards');
    cards.innerHTML = '';
    if (!show.length) {
      cards.innerHTML = `<div class="empty" style="grid-column:1/-1"><strong>该相机在所选时段内无 NG</strong>报告将写「无 NG，维持现状」</div>`;
    }
    for (const e of show) {
      const tag = tagOf(cam, e.ts);
      const boxes = boxesOf(cam, e.ts);
      const card = document.createElement('div');
      card.className = 'card';
      card.innerHTML = `
        <div class="card-img" data-open="1">
          <img src="${M.frame(cam, e.ts)}" alt="${cam} ${e.ts}" loading="lazy" />
          <span class="corner ${tag || ''}"></span>
          ${boxes.length ? `<span class="annot">已标注 ${boxes.length}</span>` : ''}
        </div>
        <div class="card-meta"><span class="t">${fmtTime(e.ts)}</span><span>NG</span></div>
        <div class="card-tags">
          <button class="chip ${tag === 'misjudge' ? 'on-misjudge' : ''}" data-tag="misjudge">误判</button>
          <button class="chip ${tag === 'product' ? 'on-product' : ''}" data-tag="product">产品自身NG</button>
          <button class="chip ${tag === 'external' ? 'on-external' : ''}" data-tag="external">外部原因</button>
        </div>
        <div class="card-foot">
          <button class="btn sm" data-annot="1">标注</button>
          <button class="btn sm" data-remove="1">移除</button>
        </div>`;
      card.querySelector('[data-open]').addEventListener('click', () => openAnnot(cam, e.ts));
      card.querySelector('[data-annot]').addEventListener('click', () => openAnnot(cam, e.ts));
      card.querySelector('[data-remove]').addEventListener('click', () => {
        const list = state.draft.removed[cam] || (state.draft.removed[cam] = []);
        if (!list.includes(e.ts)) list.push(e.ts);
        scheduleSave(400);
        renderStations();
        renderStationDetail();
        toast('已从报告中移除该图');
      });
      card.querySelectorAll('[data-tag]').forEach(btn => {
        btn.addEventListener('click', () => {
          const k = keyOf(cam, e.ts);
          const next = btn.dataset.tag;
          if (state.draft.tags[k] === next) delete state.draft.tags[k];
          else state.draft.tags[k] = next;
          scheduleSave(500);
          renderStations();
          renderStationDetail();
        });
      });
      cards.appendChild(card);
    }
    const removedN = (state.draft.removed[cam] || []).length;
    $('#cardsNote').textContent = r.H
      ? `共 ${r.H} 件 NG，展示 ${show.length} 张（报告上限 ${state.limit} 张，自动均匀抽样）${removedN ? `，已移除 ${removedN} 张` : ''}。页面保留的图即报告 M..X 列内容。`
      : '';

    // 右栏
    const fact = factTextFor(cam);
    $('#factText').textContent = fact;
    const reason = state.draft.reasons[cam];
    const ta = $('#reasonInput');
    if (document.activeElement !== ta) ta.value = reason !== undefined ? reason : fact;
    $('#reasonCount').textContent = `${ta.value.length} 字`;

    const autoOther = r.autoOther;
    const inp = $('#otherNgInput');
    if (document.activeElement !== inp) inp.value = r.otherOverride !== null ? r.otherOverride : autoOther;
    $('#otherNgHint').textContent = r.otherOverride !== null
      ? `人工调整（tag 汇总 ${autoOther}）`
      : `自动汇总：误判 ${r.misjudge} + 外部 ${r.external} = ${autoOther}`;

    renderTicketPick(info, cam);
    $('#newTicketLink').href = `tickets.html?new=1&station=${encodeURIComponent(info.station)}&camera=${encodeURIComponent(cam)}`;
  }

  function renderTicketPick(info, cam) {
    const wrap = $('#ticketPick');
    const all = M.tickets().filter(t => t.status === 'open' || t.status === 'in_progress');
    const related = all.filter(t => !t.station || t.station === info.station);
    const picked = state.draft.tickets[cam] || (state.draft.tickets[cam] = []);
    if (!related.length) {
      wrap.innerHTML = `<div style="color:var(--muted);font-size:11.5px">本站暂无未闭环工单</div>`;
      return;
    }
    wrap.innerHTML = related.map(t => `
      <label>
        <input type="checkbox" data-tid="${t.id}" ${picked.includes(t.id) ? 'checked' : ''} />
        <span>
          <span class="tp-title">#${t.id} ${t.title}</span>
          <span class="tp-meta"><span class="badge ${t.status === 'open' ? 'open' : 'progress'}">${t.status === 'open' ? '开启中' : '处理中'}</span>
          ${t.assignee || ''}${t.due_date ? ' · 计划 ' + t.due_date.slice(5) : ''}</span>
        </span>
      </label>`).join('');
    wrap.querySelectorAll('input[data-tid]').forEach(cb => {
      cb.addEventListener('change', () => {
        const id = +cb.dataset.tid;
        const arr = state.draft.tickets[cam] || (state.draft.tickets[cam] = []);
        const i = arr.indexOf(id);
        if (cb.checked && i < 0) arr.push(id);
        if (!cb.checked && i >= 0) arr.splice(i, 1);
        scheduleSave(400);
      });
    });
  }

  // ---------------- 草稿保存 ----------------
  function scheduleSave(delay) {
    state.dirty = true;
    updateSaveState();
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(() => saveNow('change'), delay || 1200);
  }

  function saveNow(kind) {
    if (!state.draft) return;
    state.draft.hours = [...state.hours].sort((a, b) => a - b);
    state.draft._activeCamera = state.activeCamera;
    const ts = M.saveDraft(state.draft);
    state.dirty = false;
    state.lastSaved = ts;
    state.autoCountdown = 300 / state.speed;
    updateSaveState();
    if (kind === 'manual') toast('草稿已保存到数据库（Mock）');
  }

  function updateSaveState() {
    const n = $('#saveState');
    if (state.dirty) {
      n.className = 'save-state warn';
      n.innerHTML = `有未保存修改 · 自动保存 <b>${fmtCountdown()}</b> 后`;
    } else if (state.lastSaved) {
      const d = new Date(state.lastSaved);
      n.className = 'save-state';
      n.innerHTML = `已保存 <b>${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}</b> · 下次自动保存 ${fmtCountdown()} 后`;
    } else {
      n.className = 'save-state';
      n.textContent = '尚未保存草稿';
    }
  }

  function fmtCountdown() {
    const s = Math.max(0, Math.ceil(state.autoCountdown));
    return `${pad(Math.floor(s / 60))}:${pad(s % 60)}`;
  }

  let autoTimer = null;
  function startAutoSave() {
    clearInterval(autoTimer);
    state.autoCountdown = 300 / state.speed;
    autoTimer = setInterval(() => {
      state.autoCountdown -= 1;
      if (state.autoCountdown <= 0) {
        saveNow('auto');
        if (!state.dirty) toast('已自动保存草稿（每 5 分钟）');
      }
      updateSaveState();
    }, 1000);
  }

  // ---------------- 标注 ----------------
  const annot = {
    camera: null, ts: null, boxes: [], drawing: null, imgReady: false,
    open(cam, ts) {
      this.camera = cam; this.ts = ts;
      const key = keyOf(cam, ts);
      this.boxes = JSON.parse(JSON.stringify(state.draft.boxes[key] || []));
      const img = $('#annotImg');
      $('#annotMeta').textContent = `${cam} · ${ts.replace('T', ' ')}`;
      const desc = this.boxes.length ? (this.boxes[0].desc || '') : '';
      $('#annotDesc').value = desc;
      img.onload = () => {
        this.imgReady = true;
        this.resize();
        this.draw();
      };
      img.src = M.frame(cam, ts);
      if (img.complete) { this.imgReady = true; this.resize(); this.draw(); }
      this.renderList();
      $('#annotOverlay').classList.remove('hidden');
    },
    resize() {
      const img = $('#annotImg');
      const c = $('#annotCanvas');
      const rect = img.getBoundingClientRect();
      c.width = Math.round(rect.width);
      c.height = Math.round(rect.height);
    },
    scale() {
      const img = $('#annotImg');
      return img.naturalWidth ? img.getBoundingClientRect().width / img.naturalWidth : 1;
    },
    draw(temp) {
      const c = $('#annotCanvas');
      const g = c.getContext('2d');
      g.clearRect(0, 0, c.width, c.height);
      const s = this.scale();
      const paint = (b, color, dashed) => {
        g.strokeStyle = color;
        g.lineWidth = 2;
        g.setLineDash(dashed ? [5, 4] : []);
        g.strokeRect(b.x * s, b.y * s, b.w * s, b.h * s);
        if (b.label) {
          g.fillStyle = color;
          g.font = '600 12px Consolas, monospace';
          g.fillText(b.label, b.x * s + 3, Math.max(12, b.y * s - 5));
        }
      };
      this.boxes.forEach((b, i) => paint(b, i === 0 ? '#00d66f' : '#ffd166'));
      if (temp) paint(temp, '#4ea1ff', true);
    },
    bind() {
      const c = $('#annotCanvas');
      const pos = (e) => {
        const r = c.getBoundingClientRect();
        return { x: e.clientX - r.left, y: e.clientY - r.top };
      };
      c.addEventListener('mousedown', (e) => {
        if (!this.imgReady) return;
        const p = pos(e);
        this.drawing = { x: p.x, y: p.y, w: 0, h: 0 };
      });
      c.addEventListener('mousemove', (e) => {
        if (!this.drawing) return;
        const p = pos(e);
        this.drawing.w = p.x - this.drawing.x;
        this.drawing.h = p.y - this.drawing.y;
        const norm = this.normalize(this.drawing);
        this.draw(norm);
      });
      window.addEventListener('mouseup', (e) => {
        if (!this.drawing) return;
        const norm = this.normalize(this.drawing);
        this.drawing = null;
        if (norm.w < 6 || norm.h < 6) { this.draw(); return; }
        const s = this.scale();
        this.boxes.push({
          label: $('#annotLabel').value,
          desc: $('#annotDesc').value,
          x: Math.round(norm.x / s), y: Math.round(norm.y / s),
          w: Math.round(norm.w / s), h: Math.round(norm.h / s)
        });
        this.draw();
        this.renderList();
      });
      $('#annotClear').addEventListener('click', () => {
        this.boxes = [];
        this.draw();
        this.renderList();
      });
      $('#annotSave').addEventListener('click', () => this.save());
      document.querySelectorAll('[data-close="annot"]').forEach(b =>
        b.addEventListener('click', () => $('#annotOverlay').classList.add('hidden'))
      );
    },
    normalize(b) {
      const x = Math.min(b.x, b.x + b.w);
      const y = Math.min(b.y, b.y + b.h);
      return { x, y, w: Math.abs(b.w), h: Math.abs(b.h) };
    },
    renderList() {
      const box = $('#boxList');
      if (!this.boxes.length) { box.innerHTML = '<span>暂无框</span>'; return; }
      box.innerHTML = '';
      this.boxes.forEach((b, i) => {
        const d = document.createElement('div');
        d.className = 'bx';
        d.innerHTML = `<span>框 ${i + 1} · ${b.label} (${b.x},${b.y},${b.w}×${b.h})</span>`;
        const del = document.createElement('button');
        del.className = 'btn ghost sm';
        del.textContent = '删除';
        del.addEventListener('click', () => {
          this.boxes.splice(i, 1);
          this.draw();
          this.renderList();
        });
        d.appendChild(del);
        box.appendChild(d);
      });
    },
    save() {
      const key = keyOf(this.camera, this.ts);
      const desc = $('#annotDesc').value.trim();
      this.boxes.forEach(b => b.desc = desc);
      if (this.boxes.length) state.draft.boxes[key] = this.boxes;
      else delete state.draft.boxes[key];
      scheduleSave(300);
      $('#annotOverlay').classList.add('hidden');
      renderStationDetail();
      toast('标注已保存（Mock 写入草稿，真实环境写入 annotations）');
    }
  };

  function openAnnot(cam, ts) { annot.open(cam, ts); }

  // ---------------- 生成 ----------------
  const STAGES = [
    '按小时聚合 C11 统计',
    '读取 NG / OK 图像（含 AV1 解码）',
    '生成工作簿与单元格样式',
    '注入 DISPIMG 内嵌图',
    '渲染 PDF（A3 横版 · 米黄底）',
    '写入数据库（report_files 分块）',
    '落盘 xlsx / pdf'
  ];

  function generate() {
    if (!state.hours.size) { toast('请至少勾选 1 个小时'); return; }
    saveNow('manual');
    $('#genOverlay').classList.remove('hidden');
    $('#genProgress').hidden = false;
    $('#genResult').hidden = true;
    $('#genClose').hidden = true;
    $('#genCancel').hidden = false;
    $('#genOpenXlsx').hidden = true;
    $('#genArchive').hidden = true;
    $('#genFill').style.width = '0%';
    $('#genHint').textContent = '生成中，请勿关闭页面';
    const stages = $('#stageList');
    stages.innerHTML = STAGES.map((s, i) =>
      `<div class="stage" data-i="${i}"><span class="ico">○</span><span>${s}</span></div>`).join('');
    state.genTimers.forEach(t => clearTimeout(t));
    state.genTimers = [];
    let i = 0;
    const step = () => {
      if (i > 0) {
        const prev = stages.querySelector(`[data-i="${i - 1}"]`);
        prev.classList.add('done');
        prev.querySelector('.ico').textContent = '✓';
      }
      if (i >= STAGES.length) { finish(); return; }
      const cur = stages.querySelector(`[data-i="${i}"]`);
      cur.classList.add('active');
      cur.querySelector('.ico').textContent = '◐';
      $('#genFill').style.width = Math.round((i + 1) / STAGES.length * 100) + '%';
      const t = setTimeout(() => { i++; step(); }, 420 + Math.random() * 360);
      state.genTimers.push(t);
    };
    step();
  }

  function finish() {
    const rows = M.summary(state.day, [...state.hours]);
    const total = rows.reduce((s, r) => s + r.total, 0);
    const ng = rows.reduce((s, r) => s + r.ng, 0);
    const d = new Date();
    const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
    const xlsx = `视觉改善分析报告_${stamp}.xlsx`;
    const pdf = `视觉改善分析报告_${stamp}.pdf`;
    const rep = M.addReport({
      created_at: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`,
      day: state.day,
      hoursText: M.hoursText([...state.hours].sort((a, b) => a - b)),
      total, ng,
      xlsx_kb: 1800 + Math.round(ng * 8.5),
      pdf_kb: 900 + Math.round(ng * 5.2),
      saved: true, xlsx_name: xlsx, pdf_name: pdf
    });
    $('#genProgress').hidden = true;
    $('#genResult').hidden = false;
    $('#genHint').textContent = '完成';
    $('#genCancel').hidden = true;
    $('#genClose').hidden = false;
    $('#genOpenXlsx').hidden = false;
    $('#genArchive').hidden = false;
    $('#resId').textContent = '#' + rep.id;
    $('#resXlsx').textContent = xlsx;
    $('#resPdf').textContent = pdf;
    $('#resDb').textContent = `reports #${rep.id} + report_files（xlsx ${Math.round(rep.xlsx_kb / 1024 * 10) / 10}MB / pdf ${Math.round(rep.pdf_kb / 1024 * 10) / 10}MB）`;
    $('#resXlsx').onclick = (e) => { e.preventDefault(); toast('Mock：真实环境会调用 os.startfile 打开 xlsx'); };
    $('#resPdf').onclick = (e) => { e.preventDefault(); toast('Mock：真实环境会打开 PDF'); };
    state.generated = rep;
  }

  function cancelGen() {
    state.genTimers.forEach(t => clearTimeout(t));
    state.genTimers = [];
    $('#genOverlay').classList.add('hidden');
    toast('已取消生成');
  }

  // ---------------- 存档 ----------------
  function renderArchive() {
    const rows = M.reports();
    const tb = $('#archiveRows');
    tb.innerHTML = '';
    if (!rows.length) {
      tb.innerHTML = `<tr><td colspan="9"><div class="empty">暂无存档</div></td></tr>`;
    }
    rows.forEach(r => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>#${r.id}</td>
        <td class="mono">${r.created_at}</td>
        <td>${r.day}</td>
        <td style="max-width:260px">${r.hoursText}</td>
        <td>${r.total.toLocaleString()} / <b style="color:var(--red)">${r.ng}</b></td>
        <td>${(r.xlsx_kb / 1024).toFixed(1)} MB</td>
        <td>${(r.pdf_kb / 1024).toFixed(1)} MB</td>
        <td><span class="badge ${r.saved ? 'open' : 'progress'}">${r.saved ? '已落盘' : '仅存档'}</span></td>
        <td>
          <button class="btn ghost sm" data-xlsx>下载xlsx</button>
          <button class="btn ghost sm" data-pdf>下载pdf</button>
          <button class="btn ghost sm" data-del style="color:var(--red)">删除</button>
        </td>`;
      tr.querySelector('[data-xlsx]').onclick = () => toast('Mock：从数据库流式下载 xlsx');
      tr.querySelector('[data-pdf]').onclick = () => toast('Mock：从数据库流式下载 pdf');
      tr.querySelector('[data-del]').onclick = () => {
        if (!confirm(`删除存档 #${r.id}？将同时删除库记录与落盘文件。`)) return;
        M.removeReport(r.id);
        renderArchive();
        toast('已删除存档 #' + r.id);
      };
      tb.appendChild(tr);
    });

    const drafts = M.draftDays();
    const box = $('#draftList');
    if (!drafts.length) {
      box.innerHTML = `<div style="color:var(--muted);font-size:12px">暂无未完成草稿</div>`;
    } else {
      box.innerHTML = drafts.map(d => {
        const dr = M.getDraft(d);
        const t = dr && dr.updatedAt ? new Date(dr.updatedAt).toLocaleString() : '—';
        return `<div class="row-between" style="padding:6px 0;border-bottom:1px solid var(--line-soft)">
          <div><b>${d}</b> <span style="color:var(--muted);font-size:11.5px">更新于 ${t} · ${dr && dr.hours ? dr.hours.length : 0} 小时</span></div>
          <div><button class="btn sm" data-open-day="${d}">继续编写</button>
          <button class="btn sm" data-del-day="${d}" style="color:var(--red)">删除</button></div>
        </div>`;
      }).join('');
      box.querySelectorAll('[data-open-day]').forEach(b => b.onclick = () => {
        state.day = b.dataset.openDay;
        const dr = M.getDraft(state.day);
        state.hours = new Set(dr && dr.hours && dr.hours.length ? dr.hours : defaultHours(state.day));
        switchView('write');
        start();
      });
      box.querySelectorAll('[data-del-day]').forEach(b => b.onclick = () => {
        if (!confirm('删除该日草稿？')) return;
        M.deleteDraft(b.dataset.delDay);
        renderArchive();
      });
    }
  }

  function defaultHours(day) {
    const max = isToday(day) ? currentHour() : 23;
    return Array.from({ length: max + 1 }, (_, i) => i);
  }

  function switchView(v) {
    state.view = v;
    $('#app').hidden = v !== 'write';
    $('#archiveView').hidden = v !== 'archive';
    document.querySelectorAll('.nav-tab').forEach(a => a.classList.remove('active'));
    const id = v === 'write' ? '#writeTab' : '#archiveTab';
    const tab = document.querySelector(id);
    if (tab) tab.classList.add('active');
    if (v === 'archive') renderArchive();
  }

  // ---------------- 事件绑定 ----------------
  function bind() {
    $('#changeDateBtn').addEventListener('click', () => entry.open());
    document.querySelectorAll('[data-quick]').forEach(b => {
      if (b.closest('#entryOverlay')) return;
      b.addEventListener('click', () => {
        const type = b.dataset.quick;
        const max = isToday(state.day) ? currentHour() : 23;
        if (type === 'all' || type === 'upto') state.hours = new Set(Array.from({ length: max + 1 }, (_, i) => i));
        if (type === 'clear') state.hours = new Set();
        if (type === 'ng') {
          const set = new Set();
          for (const st of M.STATIONS) for (const cam of st.cameras) {
            const stats = M.hourStats(state.day, cam);
            for (let h = 0; h <= max; h++) if (stats[h].ng > 0) set.add(h);
          }
          state.hours = set;
        }
        scheduleSave(300);
        renderHours();
        renderStations();
        renderStationDetail();
      });
    });

    $('#mainHours').addEventListener('click', (e) => {
      const chip = e.target.closest('.hour-chip');
      if (!chip || chip.classList.contains('disabled')) return;
      const h = +chip.dataset.h;
      if (state.hours.has(h)) state.hours.delete(h); else state.hours.add(h);
      scheduleSave(400);
      renderHours();
      renderStations();
      renderStationDetail();
    });

    $('#reasonInput').addEventListener('input', (e) => {
      state.draft.reasons[state.activeCamera] = e.target.value;
      $('#reasonCount').textContent = `${e.target.value.length} 字`;
      scheduleSave(1200);
      // 左侧状态点
      renderStations();
    });

    $('#refillFactBtn').addEventListener('click', () => {
      const t = factTextFor(state.activeCamera);
      $('#reasonInput').value = t;
      state.draft.reasons[state.activeCamera] = t;
      $('#reasonCount').textContent = `${t.length} 字`;
      scheduleSave(600);
    });

    $('#restoreFactBtn').addEventListener('click', () => {
      const t = factTextFor(state.activeCamera);
      $('#reasonInput').value = t;
      state.draft.reasons[state.activeCamera] = t;
      scheduleSave(600);
      toast('已恢复自动事实摘要');
    });

    $('#otherNgInput').addEventListener('input', (e) => {
      const v = e.target.value.trim();
      if (v === '') {
        delete state.draft.otherNg[state.activeCamera];
      } else {
        state.draft.otherNg[state.activeCamera] = Math.max(0, Math.min(9999, +v));
      }
      scheduleSave(800);
      renderStationDetail();
    });

    $('#resetOtherNg').addEventListener('click', () => {
      delete state.draft.otherNg[state.activeCamera];
      scheduleSave(300);
      renderStationDetail();
    });

    ['hdrProject', 'hdrDestination', 'hdrLine'].forEach(id => {
      $(`#${id}`).addEventListener('input', (e) => {
        const key = id === 'hdrProject' ? 'project' : id === 'hdrDestination' ? 'destination' : 'line';
        state.draft.header[key] = e.target.value;
        scheduleSave(800);
      });
    });
    $('#hdrPhysical').addEventListener('change', (e) => {
      state.draft.header.includePhysical = e.target.checked;
      scheduleSave(400);
    });

    $('#saveDraftBtn').addEventListener('click', () => saveNow('manual'));
    $('#generateBtn').addEventListener('click', generate);
    $('#speedBtn').addEventListener('click', () => {
      state.speed = state.speed === 1 ? 30 : 1;
      $('#speedBtn').textContent = `加速演示 ${state.speed}×`;
      state.autoCountdown = 300 / state.speed;
      if (state.speed === 30) toast('自动保存已加速到 10 秒一次（演示用）');
    });

    $('#genCancel').addEventListener('click', cancelGen);
    $('#genClose').addEventListener('click', () => $('#genOverlay').classList.add('hidden'));
    $('#genArchive').addEventListener('click', () => {
      $('#genOverlay').classList.add('hidden');
      switchView('archive');
    });
    $('#genOpenXlsx').addEventListener('click', () => toast('Mock：真实环境会打开 xlsx 供发送前检查'));

    document.querySelectorAll('.nav-tab').forEach(a => {
      if (a.id === 'archiveTab') {
        a.addEventListener('click', (e) => { e.preventDefault(); switchView('archive'); });
      }
      if (a.id === 'writeTab') {
        a.addEventListener('click', (e) => {
          e.preventDefault();
          if (state.draft) switchView('write'); else entry.open();
        });
      }
    });

    annot.bind();
  }

  // ---------------- 初始化 ----------------
  function init() {
    entry.init();
    bind();
    // URL 参数模拟"桌面端确认后打开网页"
    const q = new URLSearchParams(location.search);
    const day = q.get('date');
    const hours = q.get('hours');
    if (q.get('archive')) {
      $('#entryOverlay').classList.add('hidden');
      switchView('archive');
      return;
    }
    if (day) {
      state.day = day;
      state.hours = new Set(hours ? hours.split(',').map(Number) : defaultHours(day));
      $('#entryOverlay').classList.add('hidden');
      start();
    } else {
      entry.open();
    }
  }

  window.MockReport = { state, saveNow, switchView };
  document.addEventListener('DOMContentLoaded', init);
})();
