/* 工单管理 Mock — 类 GitHub Issue 列表 + 详情抽屉（localStorage 持久化） */
(function () {
  const M = MockData;
  const $ = (s) => document.querySelector(s);

  const STATUS = {
    open: { label: '开启中', cls: 'open', ico: '开' },
    in_progress: { label: '处理中', cls: 'progress', ico: '进' },
    done: { label: '已完成', cls: 'done', ico: '完' },
    closed: { label: '已关闭', cls: 'closed', ico: '关' }
  };
  const CATEGORY = {
    misjudge: { label: '误判优化', dot: 'misjudge' },
    product: { label: '产品自身NG', dot: 'product' },
    external: { label: '外部原因', dot: 'external' },
    other: { label: '其他', dot: 'empty' }
  };
  const CLOSE_REASONS = [
    { key: 'fixed', t: '已修复', d: '措施已验证有效，问题不再发生' },
    { key: 'duplicate', t: '重复', d: '与既有工单重复，合并处理' },
    { key: 'invalid', t: '误报', d: '经复核并非真实问题' },
    { key: 'wontfix', t: '不处理', d: '评估后决定不采取行动' }
  ];

  const state = {
    tab: 'open',
    q: '',
    station: '',
    category: '',
    assignee: '',
    priority: '',
    sort: 'updated',
    drawer: null,     // { type:'detail', id } | { type:'new', prefill }
    closeId: null,
    closeReason: 'fixed'
  };

  function toast(msg) {
    const t = $('#toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.remove('show'), 2200);
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  function all() { return M.tickets(); }

  function isRecurring(t) {
    return t.status === 'done' && all().some(o =>
      o.id !== t.id && o.station === t.station && o.category === t.category &&
      (o.status === 'open' || o.status === 'in_progress')
    );
  }

  function linkCount(t) { return (t.reports || []).length; }

  // ---------------- 过滤 / 排序 ----------------
  function filtered() {
    let list = all();
    const counts = { open: 0, in_progress: 0, done: 0, closed: 0 };
    list.forEach(t => counts[t.status]++);

    if (state.tab !== 'all') list = list.filter(t => t.status === state.tab);
    if (state.q) {
      const q = state.q.toLowerCase();
      list = list.filter(t => (`#${t.id} ${t.title} ${t.body || ''} ${t.measure || ''}`).toLowerCase().includes(q));
    }
    if (state.station) list = list.filter(t => t.station === state.station);
    if (state.category) list = list.filter(t => t.category === state.category);
    if (state.assignee) list = list.filter(t => t.assignee === state.assignee);
    if (state.priority) list = list.filter(t => t.priority === state.priority);

    const pRank = { P0: 0, P1: 1, P2: 2, P3: 3 };
    if (state.sort === 'priority') {
      list.sort((a, b) => (pRank[a.priority] - pRank[b.priority]) || b.updated_at.localeCompare(a.updated_at));
    } else if (state.sort === 'created') {
      list.sort((a, b) => b.created_at.localeCompare(a.created_at));
    } else {
      list.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    }
    return { list, counts };
  }

  // ---------------- 渲染：头部统计 / tabs / 列表 ----------------
  function renderStats() {
    const list = all();
    const openN = list.filter(t => t.status === 'open' || t.status === 'in_progress').length;
    const doneN = list.filter(t => t.status === 'done').length;
    const closedN = list.filter(t => t.status === 'closed').length;
    const urgent = list.filter(t => (t.status === 'open' || t.status === 'in_progress') && (t.priority === 'P0' || t.priority === 'P1')).length;
    $('#statOpen').textContent = openN;
    $('#statDone').textContent = doneN;
    $('#statClosed').textContent = closedN;
    $('#statUrgent').textContent = urgent;
  }

  function renderTabs(counts) {
    const tabs = [
      ['open', '开启中'], ['in_progress', '处理中'], ['done', '已完成'], ['closed', '已关闭'], ['all', '全部']
    ];
    $('#statusTabs').innerHTML = tabs.map(([k, label]) => {
      const n = k === 'all' ? Object.values(counts).reduce((s, v) => s + v, 0) : counts[k];
      return `<div class="status-tab ${state.tab === k ? 'on' : ''}" data-tab="${k}">${label}<span class="n">${n}</span></div>`;
    }).join('');
    $('#statusTabs').querySelectorAll('[data-tab]').forEach(el => {
      el.addEventListener('click', () => { state.tab = el.dataset.tab; render(); });
    });
  }

  function renderFilters() {
    const stations = [...new Set(all().map(t => t.station).filter(Boolean))];
    const assignees = [...new Set(all().map(t => t.assignee).filter(Boolean))];
    const s = $('#fStation'), a = $('#fAssignee');
    if (s.options.length === 1) {
      stations.sort().forEach(st => s.insertAdjacentHTML('beforeend', `<option value="${esc(st)}">${esc(st)}</option>`));
    }
    if (a.options.length === 1) {
      assignees.sort().forEach(x => a.insertAdjacentHTML('beforeend', `<option value="${esc(x)}">${esc(x)}</option>`));
    }
  }

  function renderList() {
    const { list } = filtered();
    const box = $('#issueList');
    if (!list.length) {
      box.innerHTML = `<div class="empty"><strong>没有符合条件的工单</strong>切换状态页签、清空筛选，或新建一张工单</div>`;
      return;
    }
    box.innerHTML = list.map(t => {
      const st = STATUS[t.status];
      const cat = CATEGORY[t.category] || CATEGORY.other;
      return `
        <div class="issue" data-id="${t.id}">
          <span class="issue-ico ${t.status}">${st.ico}</span>
          <div class="issue-main">
            <div class="issue-title">
              <span class="id">#${t.id}</span>
              <span>${esc(t.title)}</span>
              <span class="badge ${st.cls}">${st.label}</span>
              <span class="badge ${t.priority.toLowerCase()}">${t.priority}</span>
              ${isRecurring(t) ? '<span class="recur">可能复发</span>' : ''}
            </div>
            <div class="issue-meta">
              <span class="dot ${cat.dot}"></span>${cat.label}
              <span class="sep">·</span>${t.station ? esc(t.station) : '全线'}${t.camera ? ' / ' + esc(t.camera) : ''}
              <span class="sep">·</span>责任人 ${esc(t.assignee || '未指派')}
              ${t.due_date ? `<span class="sep">·</span>计划 ${esc(t.due_date)}` : ''}
              ${t.close_reason ? `<span class="sep">·</span>关闭原因 ${closeReasonLabel(t.close_reason)}` : ''}
            </div>
          </div>
          <div class="issue-right">
            更新 <b>${esc(t.updated_at)}</b>
            <div class="reports">关联报告 ${linkCount(t)} 份</div>
          </div>
        </div>`;
    }).join('');
    box.querySelectorAll('.issue').forEach(el => {
      el.addEventListener('click', () => openDetail(+el.dataset.id));
    });
  }

  function closeReasonLabel(k) {
    const hit = CLOSE_REASONS.find(r => r.key === k);
    return hit ? hit.t : (k || '');
  }

  function render() {
    const { counts } = filtered();
    renderStats();
    renderFilters();
    renderTabs(counts);
    renderList();
  }

  // ---------------- 抽屉：详情 ----------------
  function openDetail(id) {
    state.drawer = { type: 'detail', id };
    renderDrawer();
    $('#drawerOverlay').classList.remove('hidden');
  }

  function closeDrawer() {
    state.drawer = null;
    $('#drawerOverlay').classList.add('hidden');
  }

  function statusActions(t) {
    const btn = (act, label, cls) => `<button class="btn sm ${cls || ''}" data-act="${act}">${label}</button>`;
    if (t.status === 'open') return btn('start', '开始处理') + btn('close', '关闭', 'danger');
    if (t.status === 'in_progress') return btn('done', '完成') + btn('close', '关闭', 'danger');
    if (t.status === 'done') return btn('reopen', '重新打开') + btn('close', '归档关闭', 'danger');
    return btn('reopen', '重新打开');
  }

  function timelineHTML(t) {
    const icoMap = { created: '创', status: '态', comment: '评', assign: '人', link: '链', edit: '改' };
    return (t.events || []).slice().reverse().map(e => `
      <div class="tl-item">
        <span class="tl-ico ${e.kind}">${icoMap[e.kind] || '·'}</span>
        <div>
          <div class="tl-text">${esc(e.text)}</div>
          <div class="tl-meta">${esc(e.actor || '系统')} · ${esc(e.ts)}</div>
        </div>
      </div>`).join('') || '<div class="tl-meta" style="color:var(--muted);font-size:12px">暂无记录</div>';
  }

  function reportsHTML(t) {
    const reports = M.reports();
    if (!t.reports || !t.reports.length) {
      return '<span style="color:var(--muted);font-size:12px">尚未关联报告</span>';
    }
    return t.reports.map(rid => {
      const r = reports.find(x => x.id === rid);
      if (!r) return `<span class="report-chip gone">报告 #${rid} 已清理（记录保留关联）</span>`;
      return `<a class="report-chip" href="report.html?archive=1">#${rid} · 统计 ${esc(r.day)}</a>`;
    }).join('');
  }

  function renderDetail(t) {
    const st = STATUS[t.status];
    const cat = CATEGORY[t.category] || CATEGORY.other;
    const stations = M.STATIONS.map(s => s.station);
    const cams = [...new Set(M.STATIONS.flatMap(s => s.cameras))];

    $('#drawer').className = 'drawer';
    $('#drawer').innerHTML = `
      <div class="drawer-head">
        <span class="issue-ico ${t.status}" style="margin-top:2px">${st.ico}</span>
        <div style="flex:1;min-width:0">
          <div class="kicker">工单 #${t.id} · 创建于 ${esc(t.created_at)}</div>
          <input id="dTitle" class="input title-input" value="${esc(t.title)}" />
        </div>
        <button class="btn sm" data-act="close-drawer">关闭</button>
      </div>
      <div class="drawer-body">
        <div class="detail-grid">
          <div>
            <div class="field">
              <label>问题描述</label>
              <textarea id="dBody" class="textarea" rows="4">${esc(t.body)}</textarea>
            </div>
            <div class="field">
              <label>改善措施（写入报告「原因分析及改造解决方案」）</label>
              <textarea id="dMeasure" class="textarea" rows="5">${esc(t.measure)}</textarea>
            </div>

            <div class="section-sub">
              <div class="row-between" style="display:flex;align-items:center;justify-content:space-between">
                <div class="section-title">时间线</div>
                <span class="hint" style="color:var(--muted);font-size:11.5px">状态流转全部留痕，不覆盖历史</span>
              </div>
              <div class="timeline">${timelineHTML(t)}</div>
              <div class="comment-box">
                <textarea id="dComment" class="textarea" rows="2" placeholder="添加评论，例如：已取样送检 / 遮光罩已安装…"></textarea>
                <button class="btn" id="addComment">评论</button>
              </div>
            </div>

            <div class="section-sub">
              <div class="row-between" style="display:flex;align-items:center;justify-content:space-between">
                <div class="section-title">关联报告</div>
                <button class="btn ghost sm" id="toggleLinks">＋ 关联报告</button>
              </div>
              <div class="link-list">${reportsHTML(t)}</div>
              <div id="linkPanel" hidden>
                <div style="color:var(--muted);font-size:11.5px;margin-bottom:4px">勾选引用本工单的报告（ticket_reports 双向关联）</div>
                ${M.reports().map(r => `
                  <label><input type="checkbox" data-rid="${r.id}" ${t.reports.includes(r.id) ? 'checked' : ''} />
                    #${r.id} · 统计 ${esc(r.day)} · 生成 ${esc(r.created_at)}</label>`).join('')}
                <div style="margin-top:6px"><button class="btn sm primary" id="confirmLinks">保存关联</button></div>
              </div>
            </div>
          </div>

          <aside class="detail-side">
            <div class="field">
              <label>状态</label>
              <div><span class="badge ${st.cls}">${st.label}</span>
                ${t.close_reason ? `<span style="color:var(--muted);font-size:11.5px">（${closeReasonLabel(t.close_reason)}）</span>` : ''}</div>
            </div>
            <div class="field">
              <label>优先级</label>
              <select id="dPriority" class="select">
                ${['P0', 'P1', 'P2', 'P3'].map(p => `<option value="${p}" ${t.priority === p ? 'selected' : ''}>${p}</option>`).join('')}
              </select>
            </div>
            <div class="field">
              <label>类别</label>
              <select id="dCategory" class="select">
                ${Object.entries(CATEGORY).map(([k, v]) => `<option value="${k}" ${t.category === k ? 'selected' : ''}>${v.label}</option>`).join('')}
              </select>
            </div>
            <div class="field">
              <label>工站</label>
              <select id="dStation" class="select">
                <option value="">全线</option>
                ${stations.map(s => `<option value="${s}" ${t.station === s ? 'selected' : ''}>${s}</option>`).join('')}
              </select>
            </div>
            <div class="field">
              <label>相机</label>
              <select id="dCamera" class="select">
                <option value="">不限</option>
                ${cams.map(c => `<option value="${c}" ${t.camera === c ? 'selected' : ''}>${c}</option>`).join('')}
              </select>
            </div>
            <div class="field">
              <label>责任人</label>
              <input id="dAssignee" class="input" value="${esc(t.assignee)}" />
            </div>
            <div class="field">
              <label>计划完成日</label>
              <input id="dDue" class="input" type="date" value="${esc(t.due_date)}" />
            </div>
            <div class="field" style="margin-bottom:0">
              <label>时间</label>
              <div style="color:var(--muted);font-size:11.5px;line-height:1.7">
                创建 ${esc(t.created_at)}<br />更新 ${esc(t.updated_at)}
              </div>
            </div>
          </aside>
        </div>
      </div>
      <div class="drawer-foot">
        <div class="actions">
          ${statusActions(t)}
          <button class="btn sm" id="copyLink">复制链接</button>
        </div>
        <span class="hint">状态操作即时生效；标题/正文/属性改动需点「保存修改」</span>
        <button class="btn primary" id="saveTicket">保存修改</button>
      </div>`;

    bindDetail(t);
  }

  function bindDetail(t) {
    const d = $('#drawer');
    d.querySelector('[data-act="close-drawer"]').addEventListener('click', closeDrawer);

    d.querySelectorAll('[data-act]').forEach(btn => {
      const act = btn.dataset.act;
      if (act === 'close-drawer') return;
      btn.addEventListener('click', () => doAction(t, act));
    });

    $('#addComment').addEventListener('click', () => {
      const ta = $('#dComment');
      const text = ta.value.trim();
      if (!text) { toast('请输入评论内容'); return; }
      M.updateTicket(t.id, {}, { actor: '产线工程师', kind: 'comment', text });
      toast('评论已添加');
      render();
      renderDrawer();
    });

    $('#saveTicket').addEventListener('click', () => {
      const patch = {
        title: $('#dTitle').value.trim() || t.title,
        body: $('#dBody').value,
        measure: $('#dMeasure').value,
        priority: $('#dPriority').value,
        category: $('#dCategory').value,
        station: $('#dStation').value,
        camera: $('#dCamera').value,
        assignee: $('#dAssignee').value.trim(),
        due_date: $('#dDue').value
      };
      const changed = Object.keys(patch).filter(k => String(patch[k] || '') !== String(t[k] || ''));
      M.updateTicket(t.id, patch, changed.length
        ? { actor: '产线工程师', kind: 'edit', text: '更新：' + changed.map(k => LABELS[k] || k).join('、') }
        : null);
      toast(changed.length ? '已保存修改' : '没有改动');
      render();
      renderDrawer();
    });

    $('#toggleLinks').addEventListener('click', () => {
      const p = $('#linkPanel');
      p.hidden = !p.hidden;
    });

    $('#confirmLinks').addEventListener('click', () => {
      const ids = [...d.querySelectorAll('#linkPanel input[data-rid]:checked')].map(x => +x.dataset.rid);
      M.updateTicket(t.id, { reports: ids }, {
        actor: '产线工程师', kind: 'link',
        text: ids.length ? `关联报告：${ids.map(i => '#' + i).join('、')}` : '清空报告关联'
      });
      toast('关联已更新');
      render();
      renderDrawer();
    });

    $('#copyLink').addEventListener('click', () => {
      const url = `http://127.0.0.1:18110/tickets/${t.id}`;
      if (navigator.clipboard) navigator.clipboard.writeText(url).catch(() => { });
      toast('已复制：' + url);
    });
  }

  const LABELS = {
    title: '标题', body: '问题描述', measure: '改善措施', priority: '优先级',
    category: '类别', station: '工站', camera: '相机', assignee: '责任人', due_date: '计划完成日'
  };

  function doAction(t, act) {
    if (act === 'start') {
      M.updateTicket(t.id, { status: 'in_progress' }, { actor: '产线工程师', kind: 'status', text: 'open → in_progress' });
      toast('已开始处理');
    } else if (act === 'done') {
      M.updateTicket(t.id, { status: 'done' }, { actor: '产线工程师', kind: 'status', text: 'in_progress → done（待验证归档）' });
      toast('已标记完成');
    } else if (act === 'reopen') {
      M.updateTicket(t.id, { status: 'open', close_reason: '' }, { actor: '产线工程师', kind: 'status', text: `${t.status} → open（重新打开）` });
      toast('已重新打开');
    } else if (act === 'close') {
      state.closeId = t.id;
      state.closeReason = 'fixed';
      $('#closeTitle').textContent = `关闭工单 #${t.id}`;
      renderCloseOpts();
      $('#closeOverlay').classList.remove('hidden');
      return;
    }
    render();
    renderDrawer();
  }

  function renderCloseOpts() {
    $('#closeOpts').innerHTML = CLOSE_REASONS.map(r => `
      <label class="close-opt ${state.closeReason === r.key ? 'on' : ''}" data-reason="${r.key}">
        <input type="radio" name="cr" ${state.closeReason === r.key ? 'checked' : ''} />
        <span><span class="t">${r.t}</span><br /><span class="d">${r.d}</span></span>
      </label>`).join('');
    $('#closeOpts').querySelectorAll('[data-reason]').forEach(el => {
      el.addEventListener('click', () => { state.closeReason = el.dataset.reason; renderCloseOpts(); });
    });
  }

  // ---------------- 抽屉：新建 ----------------
  function openNew(prefill) {
    state.drawer = { type: 'new', prefill: prefill || {} };
    renderDrawer();
    $('#drawerOverlay').classList.remove('hidden');
  }

  function renderNew(pf) {
    const stations = M.STATIONS.map(s => s.station);
    const cams = [...new Set(M.STATIONS.flatMap(s => s.cameras))];
    $('#drawer').className = 'drawer narrow';
    $('#drawer').innerHTML = `
      <div class="drawer-head">
        <span class="issue-ico open">＋</span>
        <div style="flex:1">
          <div class="kicker">新建工单</div>
          <div style="font-size:15px;font-weight:700;margin-top:2px">改善措施工单</div>
        </div>
        <button class="btn sm" data-act="close-drawer">关闭</button>
      </div>
      <div class="drawer-body">
        <div class="field">
          <label>标题（一句话问题）</label>
          <input id="nTitle" class="input" placeholder="例如：OP20-58 焊缝反光导致误判" value="${esc(pf.title || '')}" />
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
          <div class="field">
            <label>类别</label>
            <select id="nCategory" class="select">
              ${Object.entries(CATEGORY).map(([k, v]) => `<option value="${k}" ${(pf.category || 'misjudge') === k ? 'selected' : ''}>${v.label}</option>`).join('')}
            </select>
          </div>
          <div class="field">
            <label>优先级</label>
            <select id="nPriority" class="select">
              ${['P0', 'P1', 'P2', 'P3'].map(p => `<option value="${p}" ${(pf.priority || 'P2') === p ? 'selected' : ''}>${p}</option>`).join('')}
            </select>
          </div>
          <div class="field">
            <label>工站</label>
            <select id="nStation" class="select">
              <option value="">全线</option>
              ${stations.map(s => `<option value="${s}" ${pf.station === s ? 'selected' : ''}>${s}</option>`).join('')}
            </select>
          </div>
          <div class="field">
            <label>相机</label>
            <select id="nCamera" class="select">
              <option value="">不限</option>
              ${cams.map(c => `<option value="${c}" ${pf.camera === c ? 'selected' : ''}>${c}</option>`).join('')}
            </select>
          </div>
          <div class="field">
            <label>责任人</label>
            <input id="nAssignee" class="input" value="${esc(pf.assignee || '视觉相关人员')}" />
          </div>
          <div class="field">
            <label>计划完成日</label>
            <input id="nDue" class="input" type="date" value="${esc(pf.due_date || '')}" />
          </div>
        </div>
        <div class="field">
          <label>问题描述</label>
          <textarea id="nBody" class="textarea" rows="4" placeholder="现象、发生时段、已排除项…">${esc(pf.body || '')}</textarea>
        </div>
        <div class="field">
          <label>改善措施</label>
          <textarea id="nMeasure" class="textarea" rows="4" placeholder="1) … 2) … 3) 验证方式与数量">${esc(pf.measure || '')}</textarea>
        </div>
        <div class="mock-note" style="margin:0">
          从报告工作台新建时会自动预填工站 / 相机 / 时段 NG 数 / 图片时间戳；
          若同站同类已有未闭环工单，真实实现会提示「追加关联到 #xx」避免重复开单。
        </div>
      </div>
      <div class="drawer-foot">
        <span class="hint">Mock 仅写 localStorage</span>
        <button class="btn" data-act="close-drawer">取消</button>
        <button class="btn primary" id="createTicket">创建工单</button>
      </div>`;

    $('#drawer').querySelectorAll('[data-act="close-drawer"]').forEach(b => b.addEventListener('click', closeDrawer));
    $('#createTicket').addEventListener('click', () => {
      const title = $('#nTitle').value.trim();
      if (!title) { toast('请填写标题'); return; }
      const t = M.addTicket({
        title,
        category: $('#nCategory').value,
        priority: $('#nPriority').value,
        station: $('#nStation').value,
        camera: $('#nCamera').value,
        assignee: $('#nAssignee').value.trim() || '视觉相关人员',
        due_date: $('#nDue').value,
        body: $('#nBody').value,
        measure: $('#nMeasure').value,
        status: 'open'
      });
      toast(`工单 #${t.id} 已创建`);
      state.tab = 'open';
      render();
      openDetail(t.id);
    });
  }

  function renderDrawer() {
    if (!state.drawer) return;
    if (state.drawer.type === 'detail') {
      const t = all().find(x => x.id === state.drawer.id);
      if (!t) { closeDrawer(); return; }
      renderDetail(t);
    } else {
      renderNew(state.drawer.prefill);
    }
  }

  // ---------------- 事件 ----------------
  function bind() {
    $('#newBtn').addEventListener('click', () => openNew({}));
    $('#resetBtn').addEventListener('click', () => {
      if (!confirm('清空所有 Mock 演示数据（草稿 / 工单 / 报告存档）并恢复初始样例？')) return;
      M.resetAll();
      location.reload();
    });

    $('#searchInput').addEventListener('input', (e) => { state.q = e.target.value.trim(); renderList(); });
    $('#fStation').addEventListener('change', (e) => { state.station = e.target.value; render(); });
    $('#fCategory').addEventListener('change', (e) => { state.category = e.target.value; render(); });
    $('#fAssignee').addEventListener('change', (e) => { state.assignee = e.target.value; render(); });
    $('#fPriority').addEventListener('change', (e) => { state.priority = e.target.value; render(); });
    $('#fSort').addEventListener('change', (e) => { state.sort = e.target.value; render(); });

    $('#closeCancel').addEventListener('click', () => {
      $('#closeOverlay').classList.add('hidden');
      state.closeId = null;
    });
    $('#closeConfirm').addEventListener('click', () => {
      const id = state.closeId;
      if (!id) return;
      const reason = state.closeReason;
      const t = all().find(x => x.id === id);
      M.updateTicket(id, { status: 'closed', close_reason: reason }, {
        actor: '产线工程师', kind: 'status',
        text: `${t ? t.status : ''} → closed（${closeReasonLabel(reason)}）`
      });
      $('#closeOverlay').classList.add('hidden');
      state.closeId = null;
      toast('工单已关闭');
      render();
      renderDrawer();
    });

    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      if (!$('#closeOverlay').classList.contains('hidden')) {
        $('#closeOverlay').classList.add('hidden');
        state.closeId = null;
      } else if (!$('#drawerOverlay').classList.contains('hidden')) {
        closeDrawer();
      }
    });

    $('#drawerOverlay').addEventListener('mousedown', (e) => {
      if (e.target === $('#drawerOverlay')) closeDrawer();
    });
  }

  // ---------------- 初始化 ----------------
  function init() {
    bind();
    render();
    const q = new URLSearchParams(location.search);
    if (q.get('new')) {
      openNew({
        station: q.get('station') || '',
        camera: q.get('camera') || '',
        category: q.get('category') || 'misjudge'
      });
    } else if (q.get('id')) {
      const t = all().find(x => x.id === +q.get('id'));
      if (t) openDetail(t.id);
    }
  }

  window.MockTickets = { state, render, openDetail, openNew };
  document.addEventListener('DOMContentLoaded', init);
})();
