# MES 制造执行系统（前端反编译说明）

本目录是一个已经构建、压缩并拆包的 MES 前端站点，页面标题为 **MES 制造执行系统**。系统面向离散制造试点产线，覆盖工单、排产、生产执行、质量放行、追溯、异常安灯、PLC 联调和系统运维。

> 本文通过分析 `index.html` 及 `assets/` 中的 JavaScript/CSS 产物整理。当前目录不包含原始 Vue 源码、后端代码、依赖清单或源码映射（Source Map），因此本文记录的是可确认的前端行为，不等同于完整源码恢复。

## 1. 技术栈与构建形态

从打包产物可以确认：

- Vue 3，单页应用，挂载到 `#app`。
- Vue Router，使用 HTML5 History 路由。
- Element Plus 组件库及其中文/英文语言包。
- Axios，默认请求超时为 30 秒。
- Microsoft SignalR 客户端，打包版本为 `8.0.17`。
- Vite 构建产物，采用动态 `import()` 按页面拆包，并分批预加载常用页面。
- 自定义中英文翻译机制，不依赖独立的 i18n 页面配置文件。

目录中只有静态文件，没有 `package.json`，所以不能在此目录直接执行 `npm run dev` 或重新构建。

## 2. 目录结构

```text
wwwroot/
├── index.html                    # 页面入口，加载主 JS 和全局 CSS
├── favicon.ico
├── assets/
│   ├── index-*.js               # 应用启动、路由、认证、权限、双语资源
│   ├── BasicLayout-*.js         # 主布局、菜单、页签、状态栏、AI 助手
│   ├── LoginView-*.js           # 登录页
│   ├── Home-*.js                # 首页
│   ├── *List-*.js               # 各业务列表/管理页面
│   ├── *Report-*.js             # 报表页面
│   ├── *Center-*.js             # 质量、异常、安灯、SPC 等业务中心
│   ├── *-vendor-*.js            # Vue、Element Plus、Axios 等共享依赖
│   └── *.css                    # 全局及页面级样式
└── README.md
```

文件名中的哈希用于缓存控制。更新文件时必须同步修改 `index.html` 和 JS 内部的引用，不能只替换同名业务文件。

## 3. 启动、认证与请求流程

### 页面启动

1. 浏览器打开 `index.html`。
2. 加载 `assets/index-qVka3JPp.js` 及共享依赖。
3. 注册 Vue Router 和 Element Plus，挂载根组件。
4. 未登录用户访问业务页面时跳转到 `/login`，并通过 `redirect` 查询参数保存原地址。
5. 登录成功后访问 `/home` 或原目标页面。

### 登录与权限

- 登录接口：`POST /api/Auth/login`。
- 后端返回访问令牌、过期时间和用户信息后，前端保存到 `localStorage`。
- Axios 请求自动附加：`Authorization: Bearer <accessToken>`。
- 收到 HTTP `401` 时清除认证状态并返回登录页。
- 收到 HTTP `403` 时返回首页。
- 菜单和路由根据用户权限显示或拦截；拥有 `*:view` 对应的 `*:edit` 权限时也允许查看。
- `*` 表示全部权限。

主要权限包括：

```text
system:view/edit    audit:view
production:view/edit process:view/edit
execution:view/edit quality:view/edit
trace:view          device:view/edit
integration:view/edit monitor:view
```

前端权限控制仅用于界面和导航，真正的数据安全仍必须由后端接口保证。

### 浏览器存储

| 键 | 存储位置 | 用途 |
|---|---|---|
| `mes.auth.token` | localStorage | 访问令牌 |
| `mes.auth.expiresAt` | localStorage | 令牌过期时间 |
| `mes.auth.user` | localStorage | 当前用户及权限 |
| `mes.auth.lastUsername` | localStorage | 记住用户名 |
| `mes.locale` | localStorage | 中文/英文语言选择 |
| `mes.view-tabs` | sessionStorage | 已打开页面页签 |
| `mes.aiAssistant.messages` | sessionStorage | 最近 AI 会话，最多保留 18 条 |
| `mes.aiAssistant.drawer` | sessionStorage | AI 抽屉开关状态 |

## 4. 页面与业务功能

### 首页与通用界面

- `/home`：设备总数、在线数、待机数，最近工单生产进度，以及近 7 天计划/实际产量。
- 左侧分级菜单支持折叠，并按权限动态显示。
- 顶部显示当前位置、用户、角色/岗位和中英文切换。
- 业务页面以多页签方式打开，支持关闭当前、关闭其他、关闭全部；首页固定。
- 底部每 20 秒检查一次后端、Ready 探针和数据库状态，也可手动刷新。
- 内置只读 AI 助手，可查询工单、质量、PLC 联调、异常和追溯问题，并跳转到相关页面；能否使用由后端开关控制。

### 基础资料与生产管理

| 路由 | 功能 |
|---|---|
| `/system/resources/workshops` | 车间维护，支持启停和级联停用 |
| `/system/resources/lines` | 产线维护、互斥产线、关联工位和工艺路线启停 |
| `/production/workstations` | 工位及其工序、PLC 绑定 |
| `/production/operations` | 工序基础数据 |
| `/production/process-routes` | 工艺路线、工序顺序、主/辅工序、供料目标 |
| `/production/process/parameter-specs` | 测试参数规范、草稿/发布版本、上下限和目标值 |
| `/production/process/recipes` | 配方及参数目标值 |
| `/production/process/first-station-code-policies` | 首站编码、外部码、SN 复用和自动建档策略 |
| `/production/process/product-raw-material-bindings` | 产品、工序与原料扫码顺序绑定 |
| `/production/products` | 产品维护 |
| `/production/materials` | 物料及安全库存维护 |
| `/production/boms` | 产品总 BOM、工序 BOM、按工艺生成 BOM |
| `/production/work-order` | 工单创建、编辑、下发、激活、完工及转料 |
| `/production/work-order-schedules` | 按产线和日期拖拽排产、调序 |
| `/production/production-lots` | 生产批次 |
| `/production/material-lots` | 物料批次及可用数量 |
| `/production/issue-orders` | 发料单和物料批次发放 |

### 生产执行与追溯

| 路由 | 功能 |
|---|---|
| `/execution/station-console` | 工位上下文、SN 扫码、CheckIn/CheckOut、参数、检验、缺陷和物料消耗 |
| `/execution/packaging-workbench` | 创建箱包、录入产品码、称重、打印箱包码、包装历史 |
| `/execution/trace-search` | 按条件检索追溯记录，查看工单过站和主/辅站信息 |
| `/execution/sn-trace` | 单件位置、过站、检验、物料、谱系、NC、返工、首件完整追溯 |
| `/execution/genealogy-trace` | 父件与组件 SN 的装配谱系查询 |

工位执行接口的核心路径为：

```text
POST /api/Execution/check-in
POST /api/Execution/check-out
GET  /api/Execution/stations/{id}/dashboard
GET  /api/Execution/stations/{id}/active-session
GET  /api/Execution/stations/{id}/continue-current-unit
POST /api/Execution/sessions/{id}/cancel
GET  /api/Execution/trace/{sn}
```

页面逻辑支持活动会话恢复、继续当前件、取消会话、超时未出站处理，以及首件、检验、NC 等生产阻塞提示。

### 质量管理

| 路由 | 功能 |
|---|---|
| `/quality/defect-codes` | 缺陷代码 |
| `/quality/quality-control-plans` | 质量控制计划、检验模式和检查项 |
| `/quality/inspection-tasks` | 检验任务池、提交检验、取消任务 |
| `/quality/inspections` | 检验记录 |
| `/quality/nc-records` | 不合格评审与处置 |
| `/quality/rework-tasks` | 返工任务创建、开始、完成和取消 |
| `/quality/rework-policies` | 工位返修策略 |
| `/quality/quality-holds` | 质量冻结、恢复和处理 |
| `/quality/incoming-release` | 来料放行 |
| `/quality/final-release` | 最终放行/驳回 |
| `/quality/first-articles` | 首件检验、放行、驳回和重置 |
| `/quality/first-article-policies` | 首站、末站、指定工位/工序首件策略 |
| `/quality/spc-center` | SPC 趋势、规格限、Cp/Cpk 和异常监控 |

### 监控、异常与报表

| 路由 | 功能 |
|---|---|
| `/monitor/line-process-overview` | 各工位当前生产、阻塞位置和最近过站，支持导出 |
| `/monitor/line-dashboard` | 工单进度、工位状态、节拍和异常，支持实时刷新 |
| `/monitor/exceptions` | 异常确认、升级、恢复与结案 |
| `/monitor/andon-center` | 人工安灯、停线和处理 |
| `/reports/production-daily` | 生产日报 |
| `/reports/work-order-performance` | 工单达成、生产明细和 NG 检验历史导出 |
| `/reports/quality-daily` | 质量日报 |
| `/reports/defect-pareto` | 缺陷 Pareto 分析 |
| `/reports/rework-scrap-trend` | 返工/报废趋势 |
| `/reports/incoming-quality` | 来料质量报表 |
| `/reports/final-release` | 最终放行报表 |
| `/reports/spc-summary` | SPC 汇总报表 |

报表接口统一位于 `/api/Reports/`，页面查询与对应的 `/export` 接口分离，文件以二进制流下载。

### 设备与 PLC 自动化

| 路由 | 功能 |
|---|---|
| `/device/ledger` | 设备台账、状态及按产线批量切换状态 |
| `/device/models` | 设备型号、适用工序、默认参数/质量模板 |
| `/device/bindings` | 工位设备绑定、主设备、虚拟码规则 |
| `/integration/plcs` | PLC 连接参数、启停和采集控制 |
| `/integration/tag-mapping` | 工艺参数到 PLC 点位的映射 |
| `/integration/workstation-io-tags` | 产线/工位握手标签及点位配置 |
| `/integration/dispatch-jobs` | 工单、换型、配方下发进度与失败重试 |
| `/integration/code-rules` | SN/批次等编码规则及分段预览 |

PLC 配置包含协议、CPU 系列、IP、端口、Rack/Slot 或 UnitId、超时和采集开关。点位支持地址类型、位偏移、字符串长度、字节序、比例和偏移量等配置。页面文字明确提到 PLC/PLCSim 和 102 端口，但具体协议实现位于后端，无法由当前前端产物确认。

实时监控使用 SignalR：

```text
/hubs/monitoring
```

SignalR 不可用或断连时，看板会退化为定时轮询。

### 系统与运维

| 路由 | 功能 |
|---|---|
| `/system/user` | 用户、MES 工号绑定、启停、密码重置 |
| `/system/roles` | 角色及权限 |
| `/system/posts` | 岗位及岗位附加权限 |
| `/system/events` | 系统事件和审计日志 |
| `/system/status` | 版本、环境、数据库、Live/Ready 健康状态 |
| `/system/parameters` | 运行时参数、覆盖值、恢复默认值 |
| `/system/data-dictionaries` | 英文业务值与中文显示名称映射 |
| `/system/logs` | 查看或下载 Serilog 日志 |
| `/system/ops/test-reports` | 生成 MES-PLC 交互 Excel 报告 |
| `/system/ops/sqlite-export` | SQLite 转 SQL Server 脚本包 |
| `/system/ops/mysql-settings` | 外部 MySQL 数据源配置与连接测试 |

AI 助手接口：

```text
POST /api/AiAssistant/ask
GET  /api/AiAssistant/status
```

## 5. 后端接口约定

前端没有配置独立的 API 基础地址，所有请求均使用相对路径，因此默认前端与后端同源。主要接口前缀包括：

```text
/api/Auth
/api/Execution
/api/Monitoring
/api/Reports
/api/WorkOrders
/api/ProcessRoutes
/api/Workstations
/api/Products
/api/Materials
/api/Boms
/api/Plcs
/api/TagMappings
/api/WorkstationIoTags
/api/PlcDispatchJobs
/api/system
/health/live
/health/ready
/hubs/monitoring
```

列表接口通常使用 `page`、`pageSize` 和筛选参数；写操作使用 REST 风格的 `POST`、`PUT`、`PATCH`、`DELETE`。部分导出请求返回 Blob。

当前目录没有后端实现，故无法仅凭前端确认数据库结构、令牌签名方式、业务校验的完整规则和 PLC 协议细节。界面和运维文字涉及 SQLite、SQL Server 及外部 MySQL；后端很可能采用 ASP.NET Core，但这属于基于 SignalR、健康检查和页面文本的推断。

## 6. 部署与本地预览

### 仅查看静态页面

在本目录运行：

```bash
python -m http.server 8080
```

访问 `http://localhost:8080/`。此方式只能验证静态资源加载；登录和业务数据需要后端，且直接刷新深层路由可能返回 404。

### 正式部署

将整个目录作为站点根目录部署，并满足：

1. 站点必须部署在域名根路径，因为资源引用为 `/assets/...`。
2. 非静态文件路径回退到 `index.html`，支持 Vue History 路由。
3. 将 `/api/`、`/health/` 反向代理到 MES 后端。
4. 将 `/hubs/` 反向代理到后端，并允许 WebSocket，以支持 SignalR。
5. 若后端直接托管 `wwwroot`，通常无需另配跨域；前后端分域时则必须修改请求配置并处理 CORS、认证和 WebSocket。

Nginx 配置示例（后端地址需按实际环境修改）：

```nginx
server {
    listen 80;
    server_name mes.example.com;
    root /var/www/mes;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /health/ {
        proxy_pass http://127.0.0.1:5000;
    }

    location /hubs/ {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }
}
```

## 7. 二次分析与源码恢复建议

当前产物保留了页面组件名、路由名、接口路径、字段名和大量中文文本，适合继续恢复功能文档或重写前端。不过变量名已经压缩，模块导出也被重命名，不能无损还原原始 TypeScript、注释和工程配置。

建议按以下边界重建源码：

```text
src/
├── api/           # 按业务域封装 Axios 请求
├── router/        # 路由、登录守卫、权限守卫
├── stores/        # 认证、语言及全局状态
├── layouts/       # BasicLayout 与通用布局组件
├── views/         # 与现有路由一一对应的页面
├── components/    # 表格、报表、对话框等共享组件
└── locales/       # 从中英文资源表拆分语言文件
```

优先恢复 `index-*.js` 中的认证、权限和路由，再恢复各业务 API 模块，最后重写页面组件。不要直接修改压缩产物作为长期维护方案。

## 8. 已知限制

- 缺少后端服务时，只能显示登录页或静态界面，无法完成真实业务流程。
- 未提供测试账号，本文不包含任何默认账号或密码。
- 未发现 Source Map，无法精确映射回原源码文件。
- 标签映射模块中的导入/导出函数仍直接抛出“未实现”错误。
- 生产环境应评估将 Bearer 令牌保存在 `localStorage` 的 XSS 风险，并确保后端执行完整鉴权和审计。
