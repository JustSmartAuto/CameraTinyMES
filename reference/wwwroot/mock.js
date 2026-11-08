const orders = [
  { code: 'MO-20260612-001', product: 'CAM-X200 工业相机', quantity: 500, done: 386, line: '总装一线', status: '生产中' },
  { code: 'MO-20260612-002', product: 'CAM-S80 视觉传感器', quantity: 300, done: 300, line: '总装二线', status: '已完工' },
  { code: 'MO-20260612-003', product: 'CAM-X200 工业相机', quantity: 200, done: 48, line: '总装一线', status: '生产中' }
];

const pages = {
  dashboard: { title: '生产看板', render: renderDashboard },
  orders: { title: '工单管理', render: renderOrders },
  stations: { title: '工位执行', render: renderStations },
  quality: { title: '质量管理', render: renderQuality },
  devices: { title: '设备监控', render: renderDevices },
  trace: { title: '产品追溯', render: renderTrace },
  system: { title: '系统状态', render: renderSystem }
};

function pageHeader(title, subtitle) {
  return `<div class="page-title"><div><h1>${title}</h1><span class="muted">${subtitle}</span></div><button class="win-button" data-action="refresh">刷新</button></div>`;
}

function statusTag(status) {
  const kind = ['生产中', '在线', '合格', '正常', '已完工'].includes(status) ? 'success' : ['待机', '待检'].includes(status) ? 'warn' : 'danger';
  return `<span class="tag ${kind}">${status}</span>`;
}

function renderDashboard() {
  const total = orders.reduce((sum, order) => sum + order.quantity, 0);
  const done = orders.reduce((sum, order) => sum + order.done, 0);
  return `${pageHeader('生产运营看板', '数据更新于 ' + new Date().toLocaleTimeString('zh-CN'))}
    <div class="metrics">
      <div class="metric"><div class="metric-label">今日计划产量 <span>▤</span></div><div class="metric-value">${total.toLocaleString()}</div><small>单位：件</small></div>
      <div class="metric"><div class="metric-label">今日完成产量 <span>▣</span></div><div class="metric-value">${done.toLocaleString()}</div><small>达成率 ${(done / total * 100).toFixed(1)}%</small></div>
      <div class="metric"><div class="metric-label">一次通过率 <span>✓</span></div><div class="metric-value">98.6%</div><small>较昨日 +0.4%</small></div>
      <div class="metric"><div class="metric-label">在线设备 <span>⌁</span></div><div class="metric-value">12 / 14</div><small>2 台设备待机</small></div>
    </div>
    <div class="dashboard-grid">
      <section class="panel"><div class="panel-title">当前工单进度</div><div class="panel-body">${orders.map(order => `<div class="work-order"><div class="order-line"><span><b>${order.code}</b> · ${order.product}</span><span>${order.done} / ${order.quantity}</span></div><div class="progress"><div class="progress-bar" style="width:${order.done / order.quantity * 100}%"></div></div></div>`).join('')}</div></section>
      <section class="panel"><div class="panel-title">工位实时状态</div><div class="panel-body station-list">
        <div class="station"><i class="status-dot online"></i><div class="station-name">OP10 主板装配<small>当前 SN：X2002606120386</small></div><b>运行</b></div>
        <div class="station"><i class="status-dot online"></i><div class="station-name">OP20 整机装配<small>当前 SN：X2002606120379</small></div><b>运行</b></div>
        <div class="station"><i class="status-dot online"></i><div class="station-name">OP30 相机标定<small>当前 SN：X2002606120365</small></div><b>运行</b></div>
        <div class="station"><i class="status-dot warning"></i><div class="station-name">OP40 终检包装<small>等待产品到位</small></div><b>待机</b></div>
      </div></section>
    </div>
    <section class="panel chart-panel"><div class="panel-title">近 7 日计划 / 实际产量</div><div class="panel-body"><div class="bar-chart">${[['06/06',72,65],['06/07',55,58],['06/08',82,76],['06/09',76,80],['06/10',88,81],['06/11',92,86],['06/12',96,73]].map(d => `<div class="bar-group"><div class="bar" style="height:${d[1]}%" title="计划 ${d[1]}"></div><div class="bar actual" style="height:${d[2]}%" title="实际 ${d[2]}"></div><span class="bar-label">${d[0]}</span></div>`).join('')}</div><div class="legend"><span><i></i>计划产量</span><span><i class="actual-key"></i>实际产量</span></div></div></section>`;
}

function orderRows() {
  return orders.map((order, index) => `<tr class="${index === 0 ? 'selected' : ''}"><td>${order.code}</td><td>${order.product}</td><td>${order.line}</td><td>${order.quantity}</td><td>${order.done}</td><td>${statusTag(order.status)}</td><td><button class="win-button" data-action="order-detail" data-code="${order.code}">详情</button></td></tr>`).join('');
}

function renderOrders() {
  return `${pageHeader('生产工单', '工单创建、下发与进度跟踪')}<div class="filter-bar"><label>工单编号<input id="order-filter" placeholder="输入工单编号"></label><label>状态<select><option>全部状态</option><option>生产中</option><option>已完工</option></select></label><button class="win-button" data-action="search-orders">查询</button><button class="win-button" data-action="new-order">新建工单</button></div><div class="table-wrap"><table><thead><tr><th>工单编号</th><th>产品</th><th>计划产线</th><th>计划数量</th><th>完成数量</th><th>状态</th><th>操作</th></tr></thead><tbody id="order-rows">${orderRows()}</tbody></table></div>`;
}

function renderStations() {
  const stations = [['OP10','主板装配','X2002606120386','生产中'],['OP20','整机装配','X2002606120379','生产中'],['OP30','相机标定','X2002606120365','生产中'],['OP40','终检包装','—','待机']];
  return `${pageHeader('工位执行', '总装一线 · 当前工单 MO-20260612-001')}<div class="info-box">提示：Mock 页面不连接 PLC，以下数据仅用于界面演示。</div><div class="table-wrap"><table><thead><tr><th>工位</th><th>工序名称</th><th>当前产品 SN</th><th>状态</th><th>操作</th></tr></thead><tbody>${stations.map(s => `<tr><td>${s[0]}</td><td>${s[1]}</td><td>${s[2]}</td><td>${statusTag(s[3])}</td><td><button class="win-button" data-action="checkout" data-station="${s[0]}">模拟出站</button></td></tr>`).join('')}</tbody></table></div>`;
}

function renderQuality() {
  const rows = [['QC-260612-018','X2002606120381','外观 / 焦距 / 成像','合格'],['QC-260612-017','X2002606120380','外观 / 焦距 / 成像','合格'],['QC-260612-016','X2002606120378','外观 / 焦距 / 成像','待检']];
  return `${pageHeader('质量检验', '检验任务与质量放行')}<div class="metrics"><div class="metric"><div class="metric-label">今日检验</div><div class="metric-value">386</div></div><div class="metric"><div class="metric-label">合格</div><div class="metric-value">381</div></div><div class="metric"><div class="metric-label">不合格</div><div class="metric-value">5</div></div><div class="metric"><div class="metric-label">待检任务</div><div class="metric-value">8</div></div></div><br><div class="table-wrap"><table><thead><tr><th>检验单号</th><th>产品 SN</th><th>检验项目</th><th>结果</th></tr></thead><tbody>${rows.map(r => `<tr>${r.map((c,i) => `<td>${i === 3 ? statusTag(c) : c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

function renderDevices() {
  const devices = [['DEV-001','六轴装配机器人','OP10','在线'],['DEV-002','自动锁螺丝机','OP20','在线'],['DEV-003','Cognex 3D 相机','OP30','在线'],['DEV-004','激光打标机','OP40','待机'],['DEV-005','PLC 主控制器','总装一线','在线']];
  return `${pageHeader('设备监控', '设备连接与运行状态')}<div class="table-wrap"><table><thead><tr><th>设备编号</th><th>设备名称</th><th>所属工位</th><th>状态</th><th>最后通信</th></tr></thead><tbody>${devices.map(d => `<tr><td>${d[0]}</td><td>${d[1]}</td><td>${d[2]}</td><td><i class="status-dot ${d[3] === '在线' ? 'online' : 'warning'}"></i> ${d[3]}</td><td>${new Date().toLocaleTimeString('zh-CN')}</td></tr>`).join('')}</tbody></table></div>`;
}

function renderTrace() {
  return `${pageHeader('产品追溯', '按产品序列号查询完整生产过程')}<div class="filter-bar"><label>产品序列号<input id="sn-input" value="X2002606120386"></label><button class="win-button" data-action="trace-search">查询</button></div><div id="trace-result"></div>`;
}

function renderSystem() {
  const items = [['Web 服务','运行正常'],['数据库','SQLite · 已连接'],['实时通信','SignalR · Mock 已连接'],['PLC 网关','2 / 2 通道在线']];
  return `${pageHeader('系统状态', '服务运行状态与健康检查')}<div class="system-grid">${items.map(item => `<div class="system-item"><h3>${item[0]} ${statusTag('正常')}</h3><p>${item[1]}</p><small>检查时间：${new Date().toLocaleString('zh-CN')}</small></div>`).join('')}</div>`;
}

function showPage(key) {
  const page = pages[key] || pages.dashboard;
  document.querySelectorAll('.nav-item').forEach(button => button.classList.toggle('active', button.dataset.page === key));
  document.getElementById('page-tab').textContent = page.title;
  document.getElementById('page-content').innerHTML = page.render();
}

let toastTimer;
function toast(message) {
  const element = document.getElementById('toast');
  element.textContent = message;
  element.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove('show'), 2500);
}

document.querySelectorAll('.nav-item').forEach(button => button.addEventListener('click', () => showPage(button.dataset.page)));
document.addEventListener('click', event => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const action = button.dataset.action;
  if (action === 'refresh') { showPage(document.querySelector('.nav-item.active').dataset.page); toast('数据已刷新（Mock）'); }
  if (action === 'new-order') document.getElementById('order-dialog').showModal();
  if (action === 'export') toast('生产报表已生成（Mock）');
  if (action === 'order-detail') toast(`工单 ${button.dataset.code}：演示数据`);
  if (action === 'checkout') toast(`${button.dataset.station} 出站成功（Mock）`);
  if (action === 'search-orders') {
    const keyword = document.getElementById('order-filter').value.trim().toLowerCase();
    document.getElementById('order-rows').innerHTML = orders.filter(order => order.code.toLowerCase().includes(keyword)).map((order, index) => `<tr class="${index === 0 ? 'selected' : ''}"><td>${order.code}</td><td>${order.product}</td><td>${order.line}</td><td>${order.quantity}</td><td>${order.done}</td><td>${statusTag(order.status)}</td><td><button class="win-button" data-action="order-detail" data-code="${order.code}">详情</button></td></tr>`).join('') || '<tr><td colspan="7" class="empty-note">未找到匹配工单</td></tr>';
  }
  if (action === 'trace-search') {
    const sn = document.getElementById('sn-input').value.trim();
    document.getElementById('trace-result').innerHTML = `<section class="panel trace-result"><div class="panel-title">追溯结果：${sn || '未输入 SN'}</div><ol class="timeline"><li><b>09:12:35 OP10 主板装配</b><br>检查结果：合格 · 操作员：张工</li><li><b>09:28:16 OP20 整机装配</b><br>检查结果：合格 · 扭矩参数正常</li><li><b>09:46:02 OP30 相机标定</b><br>焦距：12.04 mm · 检查结果：合格</li></ol></section>`;
  }
});

document.getElementById('order-form').addEventListener('submit', event => {
  if (event.submitter?.value === 'cancel') return;
  const data = new FormData(event.currentTarget);
  orders.unshift({ code: data.get('code'), product: data.get('product'), quantity: Number(data.get('quantity')), done: 0, line: data.get('line'), status: '待机' });
  showPage('orders');
  toast('工单创建成功（Mock）');
});

function updateClock() { document.getElementById('current-time').textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false }); }
updateClock();
setInterval(updateClock, 1000);
showPage('dashboard');
