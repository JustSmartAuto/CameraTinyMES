# 计划：将小时 OK/NG 率看板合并到主软件网页端

## Context

`utils/okng_hourly.py` 是独立部署的 BeOS 风格看板（自带 HTTP 服务），已实现按相机逐小时 OK/NG 率统计。用户要求把该功能合并进主体软件的网页端（`ReportServer` + `webui` 单页工作台），产线用户在同一工作台内即可查看，无需另开端口；独立脚本保留（供其他工控机单独部署）。

判定口径（已验证可用）：每台相机用 `cameras.json` 配置的结果单元格与通过/不通过标记（`config.result_specs`），数值等价兜底（`1.0`≡`1`），`samples.ok` 列优先，未配置的相机回退全局 OK/NG 文本。

## 改动

### 1. 新建 `src/cameratinymes/okng.py`（共享判定/聚合逻辑）

把 [utils/okng_hourly.py](d:/JustStupid/CameraHelper相机显示_2606120922/CameraTinyMES/utils/okng_hourly.py) 中 `hourly_okng`、`_judge_text`、`_as_float`、`_token_nums` 迁入为包内模块（约 80 行，纯函数，输入 `storage, day, cameras`），供 web.py 与独立脚本共用。

### 2. `src/cameratinymes/web.py` — 新增 API

- import `okng`；在 `_route_get`（web.py:645 路由表）加 `elif path == "/api/okng": self._api_okng(query)`。
- 新增 `_api_okng`：复用现有日期解析助手 `self._day(query)`（web.py:775 已有同款用法），调 `okng.hourly_okng(self.app.storage, day, self.app.cameras)`，返回 `{"date": ..., "cameras": [...]}`。
- 日期下拉复用现有 `/api/dates`（web.py:695，已含 result 单元格日期并集 + today），token 认证由 `_dispatch` 统一覆盖，无需额外处理。

### 3. `webui/index.html` — 新增页签与视图容器

- topbar 加 `<div class="tab" data-view="okng">OK/NG率</div>`（index.html:13-15 处）。
- 加 `<main class="okng hidden" id="okngView">`：面板头（日期下拉 + 刷新 + 自动刷新60s + 图例）+ 表格容器，结构与 `#archiveView`（index.html:70）一致。

### 4. `webui/style.css` — 看板样式

矩阵表样式：相机列与表头 sticky、单元格三色（全OK绿 / 有NG黄 / NG≥20%红）、合计列/行，配色与间距沿用 style.css 现有变量与类（参考 `.grid` 表格类）。

### 5. `webui/app.js` — 视图注册与渲染

- `switchView`（app.js:859-869）注册 `okngView` 的 hidden 切换；切到该页时调 `loadOkng()`（模式同 `loadReports` app.js:871）。
- 新增「OK/NG 看板」小节：`loadOkng()` 用现有 `api()`（app.js:92）请求 `/api/okng?date=`，渲染 相机 × 24h + 合计 矩阵（悬停 title 显示 OK/NG 明细）；日期下拉来自 `/api/dates`（`data.dates`）；自动刷新 60s。
- 风格随主工作台设计语言（BeOS 风格仅保留在独立脚本页面）。

### 6. `utils/okng_hourly.py` — 改为复用共享逻辑

删除重复的 `hourly_okng` 等，改为 `from cameratinymes.okng import hourly_okng`（该脚本已有 sys.path 注入，行为不变：BeOS 页面 + 自有端口）。

## 验证

1. `python -c "import py_compile; ..."` 语法检查改动文件。
2. 用 `uv run python web_only.py`（仅起 ReportServer，连中心库 192.168.10.64）启动，浏览器打开工作台：
   - 切到「OK/NG率」页签，确认 20 台相机（含 OP40-141、OP20-57）与 24h 矩阵、合计行列渲染正常；
   - 日期下拉切换、刷新、自动刷新可用；控制台无报错。
3. 重启 `utils/okng_hourly.py` 确认独立版仍正常（重构后回归）。
