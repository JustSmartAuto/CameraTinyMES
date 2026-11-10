/* 改善报告 Mock — 假数据、浏览器本地持久化（不连真实数据库） */
(function () {
  const LS = {
    draft: 'ctm.mock.drafts.v1',
    tickets: 'ctm.mock.tickets.v1',
    reports: 'ctm.mock.reports.v1',
    reviewer: 'ctm.mock.reviewer'
  };

  // ---------- 基础工具 ----------
  const pad = (n) => String(n).padStart(2, '0');

  function hash(str) {
    let h = 2166136261;
    for (let i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }

  function mulberry32(seed) {
    let a = seed >>> 0;
    return function () {
      a = (a + 0x6D2B79F5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function todayStr() {
    const d = new Date();
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  function shiftDay(day, delta) {
    const d = new Date(day + 'T12:00:00');
    d.setDate(d.getDate() + delta);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  function dates() {
    const out = [];
    const t = todayStr();
    for (let i = 0; i < 7; i++) out.push(shiftDay(t, -i));
    return out;
  }

  // ---------- 产线配置（与 cameras.json 结构对应） ----------
  const STATIONS = [
    { station: 'OP10', name: '上料 / 定位', cameras: ['OP10-28', 'OP10-29'] },
    { station: 'OP20', name: '焊缝检测', cameras: ['OP20-58', 'OP20-59'] },
    { station: 'OP30', name: '外观检测', cameras: ['OP30-122'] },
    { station: 'OP40', name: '下料复检', cameras: ['OP40-9'] }
  ];

  const CAMERA_META = {
    'OP10-28': { model: 'In-Sight 2800', note: '定位孔间距', rate: 0.042, hue: 188 },
    'OP10-29': { model: 'In-Sight 2800', note: '定位孔偏移', rate: 0.026, hue: 204 },
    'OP20-58': { model: 'In-Sight 3800', note: '焊缝宽度', rate: 0.034, hue: 34 },
    'OP20-59': { model: 'In-Sight 3800', note: '焊缝气孔', rate: 0.021, hue: 22 },
    'OP30-122': { model: 'In-Sight 2800', note: '表面划痕', rate: 0.051, hue: 210 },
    'OP40-9': { model: 'In-Sight 2801', note: '总成外观', rate: 0.018, hue: 168 }
  };

  function cameraInfo(id) {
    for (const st of STATIONS) {
      if (st.cameras.includes(id)) {
        return { ...CAMERA_META[id], id, station: st.station, stationName: st.name };
      }
    }
    return { id, station: 'OP??', stationName: '', note: '', rate: 0.03, model: '', hue: 200 };
  }

  // 每台相机每小时的产量 / NG 数（确定性伪随机，刷新不变）
  function hourStats(day, camera) {
    const rng = mulberry32(hash(day + '|' + camera));
    const base = 78 + Math.floor(rng() * 34);
    const meta = cameraInfo(camera);
    const out = [];
    for (let h = 0; h < 24; h++) {
      // 夜班 0-7 点产量略低，白天满产
      const factor = (h >= 8 && h <= 20) ? 1 : (h >= 0 && h <= 7 ? 0.55 : 0.72);
      const total = Math.max(0, Math.round(base * factor + (rng() - 0.5) * 12));
      // 让部分小时集中出现异常，便于体验"仅NG时段"
      const bump = (h === 10 || h === 14 || h === 19) ? 1.9 : 1;
      const raw = total * meta.rate * bump * (0.6 + rng() * 0.9);
      const ng = Math.min(total, Math.round(raw));
      out.push({ total, ng });
    }
    return out;
  }

  function summary(day, hours) {
    const rows = [];
    for (const st of STATIONS) {
      for (const cam of st.cameras) {
        const stats = hourStats(day, cam);
        let total = 0, ng = 0;
        for (const h of hours) { total += stats[h].total; ng += stats[h].ng; }
        rows.push({
          camera: cam, station: st.station, stationName: st.name,
          total, ng, ok: total - ng, rate: total ? ng / total : 0
        });
      }
    }
    return rows;
  }

  // NG 事件时间戳（含秒），只返回所勾选小时内的
  function ngEvents(day, camera, hours) {
    const stats = hourStats(day, camera);
    const events = [];
    const meta = cameraInfo(camera);
    for (const h of hours) {
      const n = stats[h].ng;
      if (!n) continue;
      const rng = mulberry32(hash(day + '|' + camera + '|' + h));
      for (let i = 0; i < n; i++) {
        const sec = Math.floor(rng() * 3600);
        events.push({
          hour: h,
          sec,
          ts: `${day}T${pad(h)}:${pad(Math.floor(sec / 60))}:${pad(sec % 60)}`,
          value: +(1 - meta.rate * (0.3 + rng())).toFixed(3)
        });
      }
    }
    events.sort((a, b) => a.ts.localeCompare(b.ts));
    return events;
  }

  function fmtHour(h) { return `${pad(h)}:00`; }

  function hoursText(hours) {
    if (!hours.length) return '未选择';
    const sorted = [...hours].sort((a, b) => a - b);
    const segs = [];
    let start = sorted[0], prev = sorted[0];
    for (let i = 1; i < sorted.length; i++) {
      const h = sorted[i];
      if (h === prev + 1) { prev = h; continue; }
      segs.push(start === prev ? fmtHour(start) : `${fmtHour(start)}-${pad(prev + 1)}:00`);
      start = prev = h;
    }
    segs.push(start === prev ? fmtHour(start) : `${fmtHour(start)}-${pad(prev + 1)}:00`);
    return `${segs.join('、')}（共 ${hours.length} 小时）`;
  }

  // ---------- 假图像（canvas 生成，离线可用） ----------
  const frameCache = new Map();

  function frame(camera, ts) {
    const key = camera + '|' + ts;
    if (frameCache.has(key)) return frameCache.get(key);
    const seed = hash(key);
    const rng = mulberry32(seed);
    const meta = cameraInfo(camera);
    const W = 640, H = 480;
    const c = document.createElement('canvas');
    c.width = W; c.height = H;
    const g = c.getContext('2d');

    // 背景（暗场金属）
    const bg = g.createLinearGradient(0, 0, 0, H);
    bg.addColorStop(0, '#20262b');
    bg.addColorStop(0.5, '#2c3339');
    bg.addColorStop(1, '#191e22');
    g.fillStyle = bg;
    g.fillRect(0, 0, W, H);

    // 噪点
    for (let i = 0; i < 900; i++) {
      const x = rng() * W, y = rng() * H;
      const a = 0.02 + rng() * 0.06;
      g.fillStyle = `rgba(255,255,255,${a})`;
      g.fillRect(x, y, 1.5, 1.5);
    }

    // 工件轮廓
    g.strokeStyle = 'rgba(150,170,185,.28)';
    g.lineWidth = 2;
    g.strokeRect(56 + rng() * 10, 60 + rng() * 12, 520, 330);

    // 焊缝：亮带 + 羽化边
    const seamY = 218 + (rng() - 0.5) * 40;
    const seamH = 26 + rng() * 18;
    const warm = 30 + (meta.hue % 40);
    const seam = g.createLinearGradient(0, seamY - seamH, 0, seamY + seamH);
    seam.addColorStop(0, 'rgba(70,78,84,0)');
    seam.addColorStop(0.35, `hsla(${warm}, 55%, 62%, .75)`);
    seam.addColorStop(0.55, `hsla(${warm - 6}, 60%, 76%, .95)`);
    seam.addColorStop(1, 'rgba(60,66,72,0)');
    g.fillStyle = seam;
    g.beginPath();
    g.moveTo(48, seamY);
    for (let x = 48; x <= W - 48; x += 16) {
      g.lineTo(x, seamY + Math.sin(x / 41 + seed % 7) * 6 + (rng() - 0.5) * 3);
    }
    g.lineTo(W - 48, seamY + seamH);
    g.lineTo(48, seamY + seamH);
    g.closePath();
    g.fill();

    // 飞溅 / 缺陷
    const spotN = 3 + Math.floor(rng() * 6);
    for (let i = 0; i < spotN; i++) {
      const x = 70 + rng() * 500;
      const y = seamY - 30 + rng() * 70;
      const r = 1.5 + rng() * 4.5;
      g.fillStyle = `rgba(255,238,200,${0.18 + rng() * 0.5})`;
      g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2); g.fill();
    }

    // 检测框（模拟 A0 render 叠加层）
    if (rng() > 0.35) {
      const bx = 180 + rng() * 180, by = seamY - 40 + rng() * 20;
      const bw = 90 + rng() * 130, bh = 46 + rng() * 40;
      g.strokeStyle = 'rgba(0, 214, 111, .9)';
      g.lineWidth = 2;
      g.strokeRect(bx, by, bw, bh);
      g.fillStyle = 'rgba(0, 214, 111, .9)';
      g.font = '600 13px Consolas, monospace';
      g.fillText('NG', bx + 4, by - 6);
    }

    // 相机名 / 时间 / 配方
    g.fillStyle = 'rgba(255,255,255,.72)';
    g.font = '600 14px Consolas, monospace';
    g.fillText(camera, 14, 24);
    const time = ts.replace('T', ' ');
    g.fillText(time, 14, H - 16);
    g.fillStyle = 'rgba(0, 214, 111, .9)';
    g.textAlign = 'right';
    g.fillText('SAB_MAIN.jobx', W - 14, H - 16);
    g.textAlign = 'left';

    const url = c.toDataURL('image/jpeg', 0.82);
    frameCache.set(key, url);
    return url;
  }

  // ---------- localStorage 读写 ----------
  function readJSON(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (e) { return fallback; }
  }
  function writeJSON(key, value) {
    localStorage.setItem(key, JSON.stringify(value));
  }

  // ---------- 草稿 ----------
  function emptyDraft(day, hours) {
    return {
      day,
      hours: hours || [],
      reviewer: localStorage.getItem(LS.reviewer) || '产线工程师',
      header: {
        project: '锦恒SAB项目',
        destination: '锦州',
        line: 'Line1',
        includePhysical: false
      },
      reasons: {},
      picked: {},
      removed: {},
      tags: {},
      boxes: {},
      otherNg: {},
      tickets: {},
      updatedAt: null
    };
  }

  function getDraft(day) {
    const all = readJSON(LS.draft, {});
    return all[day] || null;
  }

  function saveDraft(draft) {
    const all = readJSON(LS.draft, {});
    draft.updatedAt = new Date().toISOString();
    all[draft.day] = draft;
    writeJSON(LS.draft, all);
    return draft.updatedAt;
  }

  function deleteDraft(day) {
    const all = readJSON(LS.draft, {});
    delete all[day];
    writeJSON(LS.draft, all);
  }

  function draftDays() {
    return Object.keys(readJSON(LS.draft, {})).sort().reverse();
  }

  // ---------- 工单 ----------
  function seedTickets() {
    const t = todayStr();
    const d1 = shiftDay(t, -1);
    return [
      {
        id: 15, title: 'OP10-1 来料表面划痕误报', status: 'open', priority: 'P1',
        category: 'product', station: 'OP10', camera: 'OP10-28',
        assignee: '王工', due_date: shiftDay(t, 6), created_at: `${t} 08:42`, updated_at: `${t} 09:15`,
        body: '上料后首件经常出现条状划痕判定 NG，目视确认多数为来料自带轻微拉丝，非本工序造成。',
        measure: '1) 与供应商确认来料表面粗糙度上限；2) 调整 C11 判定阈值 lower=0.72；3) 连续跟踪 3 天。',
        events: [
          { ts: `${t} 08:42`, actor: '产线工程师', kind: 'created', text: '由 11月10日 改善报告创建' },
          { ts: `${t} 09:15`, actor: '王工', kind: 'comment', text: '已取样 5 件送检，等待来料报告。' }
        ],
        reports: [1]
      },
      {
        id: 14, title: 'OP10 定位孔图像偶发模糊', status: 'open', priority: 'P2',
        category: 'misjudge', station: 'OP10', camera: 'OP10-29',
        assignee: '陈工', due_date: '', created_at: `${d1} 16:20`, updated_at: `${t} 10:02`,
        body: '换班后前 10 分钟图像对焦不稳，导致定位孔边缘发虚，出现误判 NG。',
        measure: '检查镜头锁紧环与气源压力；增加开机自检拍图。',
        events: [
          { ts: `${d1} 16:20`, actor: '产线工程师', kind: 'created', text: '创建工单' },
          { ts: `${t} 10:02`, actor: '陈工', kind: 'status', text: 'open → in_progress' },
          { ts: `${t} 10:03`, actor: '陈工', kind: 'assign', text: '责任人设为 陈工' }
        ],
        reports: [2]
      },
      {
        id: 12, title: 'OP20-58 焊缝反光导致误判', status: 'in_progress', priority: 'P0',
        category: 'misjudge', station: 'OP20', camera: 'OP20-58',
        assignee: '张工', due_date: shiftDay(t, 5), created_at: `${d1} 11:05`, updated_at: `${t} 08:30`,
        body: '午间侧光较强时，焊缝高光被误判为气孔，集中在 11:00-14:00。',
        measure: '1) 加装遮光罩；2) 光源角度由 45° 调至 60°；3) 复测 200 件验证误判率。',
        events: [
          { ts: `${d1} 11:05`, actor: '产线工程师', kind: 'created', text: '由 11月09日 改善报告创建' },
          { ts: `${d1} 13:40`, actor: '张工', kind: 'status', text: 'open → in_progress' },
          { ts: `${t} 08:30`, actor: '张工', kind: 'comment', text: '遮光罩已到货，今天安装。' }
        ],
        reports: [1, 2]
      },
      {
        id: 11, title: 'OP30-122 划痕阈值偏严', status: 'done', priority: 'P1',
        category: 'misjudge', station: 'OP30', camera: 'OP30-122',
        assignee: '李工', due_date: shiftDay(t, -3), created_at: `${shiftDay(t, -4)} 09:10`, updated_at: `${shiftDay(t, -2)} 17:48`,
        body: '表面细微拉丝被判定为划痕，客户抽检判定为良品。',
        measure: '阈值由 0.86 调至 0.78，增加长度过滤（<=3px 忽略）。',
        events: [
          { ts: `${shiftDay(t, -4)} 09:10`, actor: '产线工程师', kind: 'created', text: '创建工单' },
          { ts: `${shiftDay(t, -3)} 10:20`, actor: '李工', kind: 'status', text: 'open → in_progress' },
          { ts: `${shiftDay(t, -2)} 17:48`, actor: '李工', kind: 'status', text: 'in_progress → done（验证 300 件，误判 0）' }
        ],
        reports: [1]
      },
      {
        id: 9, title: '上下料机械手定位偏移', status: 'closed', priority: 'P0',
        category: 'external', station: 'OP40', camera: 'OP40-9',
        assignee: '设备科', due_date: shiftDay(t, -5), created_at: `${shiftDay(t, -8)} 14:22`, updated_at: `${shiftDay(t, -5)} 09:05`,
        body: '机械手取料位置偏移，工件进入视野偏左，造成 OP40 连续 NG。',
        measure: '重新示教取料点位，增加到位检测；每班点检一次。',
        close_reason: 'fixed',
        events: [
          { ts: `${shiftDay(t, -8)} 14:22`, actor: '产线工程师', kind: 'created', text: '创建工单' },
          { ts: `${shiftDay(t, -6)} 09:00`, actor: '设备科', kind: 'comment', text: '已重新示教，观察中。' },
          { ts: `${shiftDay(t, -5)} 09:05`, actor: '产线工程师', kind: 'status', text: 'done → closed（fixed）' }
        ],
        reports: [1]
      },
      {
        id: 7, title: '相机 IP 与其他设备冲突', status: 'closed', priority: 'P2',
        category: 'other', station: '', camera: '',
        assignee: 'IT', due_date: shiftDay(t, -12), created_at: `${shiftDay(t, -15)} 10:00`, updated_at: `${shiftDay(t, -12)} 16:30`,
        body: '两台相机配置了相同网段地址，导致间歇断连。',
        measure: '重排 IP 规划表，写入设备台账。',
        close_reason: 'wontfix',
        events: [
          { ts: `${shiftDay(t, -15)} 10:00`, actor: '产线工程师', kind: 'created', text: '创建工单' }
        ],
        reports: []
      }
    ];
  }

  function tickets() {
    const data = readJSON(LS.tickets, null);
    if (data && Array.isArray(data) && data.length) return data;
    const seed = seedTickets();
    writeJSON(LS.tickets, seed);
    return seed;
  }

  function saveTickets(list) { writeJSON(LS.tickets, list); }

  function addTicket(fields) {
    const list = tickets();
    const id = list.reduce((m, t) => Math.max(m, t.id), 0) + 1;
    const now = new Date();
    const ts = `${todayStr()} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
    const t = {
      id, status: 'open', priority: 'P2', events: [], reports: [],
      created_at: ts, updated_at: ts, close_reason: '',
      body: '', measure: '', assignee: '视觉相关人员', due_date: '', station: '', camera: '',
      category: 'other', ...fields
    };
    t.events = [{ ts, actor: t.assignee || '视觉相关人员', kind: 'created', text: '创建工单（Mock）' }];
    list.push(t);
    saveTickets(list);
    return t;
  }

  function updateTicket(id, patch, event) {
    const list = tickets();
    const t = list.find(x => x.id === id);
    if (!t) return null;
    Object.assign(t, patch);
    const now = new Date();
    t.updated_at = `${todayStr()} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
    if (event) {
      event.ts = t.updated_at;
      t.events.push(event);
    }
    saveTickets(list);
    return t;
  }

  // ---------- 报告存档 ----------
  function seedReports() {
    const t = todayStr();
    return [
      {
        id: 3, created_at: `${t} 09:12:41`, day: shiftDay(t, -1), hoursText: '00:00-24:00（共 24 小时）',
        total: 4862, ng: 127, xlsx_kb: 2841, pdf_kb: 1637, saved: true,
        xlsx_name: '视觉改善分析报告_20261110091241.xlsx', pdf_name: '视觉改善分析报告_20261110091241.pdf'
      },
      {
        id: 2, created_at: `${shiftDay(t, -1)} 18:04:22`, day: shiftDay(t, -2), hoursText: '08:00-21:00（共 13 小时）',
        total: 2974, ng: 61, xlsx_kb: 1902, pdf_kb: 1105, saved: true,
        xlsx_name: '视觉改善分析报告_20261109180422.xlsx', pdf_name: '视觉改善分析报告_20261109180422.pdf'
      },
      {
        id: 1, created_at: `${shiftDay(t, -2)} 17:51:08`, day: shiftDay(t, -3), hoursText: '00:00-24:00（共 24 小时）',
        total: 4650, ng: 143, xlsx_kb: 2610, pdf_kb: 1540, saved: true,
        xlsx_name: '视觉改善分析报告_20261108175108.xlsx', pdf_name: '视觉改善分析报告_20261108175108.pdf'
      }
    ];
  }

  function reports() {
    const data = readJSON(LS.reports, null);
    if (data && Array.isArray(data) && data.length) return data;
    const seed = seedReports();
    writeJSON(LS.reports, seed);
    return seed;
  }

  function addReport(rep) {
    const list = reports();
    const id = list.reduce((m, r) => Math.max(m, r.id), 0) + 1;
    const item = { id, ...rep };
    list.unshift(item);
    writeJSON(LS.reports, list);
    return item;
  }

  function removeReport(id) {
    writeJSON(LS.reports, reports().filter(r => r.id !== id));
  }

  function resetAll() {
    [LS.draft, LS.tickets, LS.reports].forEach(k => localStorage.removeItem(k));
  }

  window.MockData = {
    LS, pad, hash, todayStr, shiftDay, dates, today: todayStr,
    STATIONS, cameraInfo, hourStats, summary, ngEvents, hoursText, fmtHour,
    frame, getDraft, saveDraft, deleteDraft, draftDays, emptyDraft,
    tickets, addTicket, updateTicket, saveTickets,
    reports, addReport, removeReport, resetAll
  };
})();
