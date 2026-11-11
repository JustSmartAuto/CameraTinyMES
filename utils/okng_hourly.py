"""独立的「小时 OK/NG 率」网页看板（不启动采集主程序）。

读取数据目录下 config/database.json 配置的数据库（auto = MariaDB 优先、
失败回退 SQLite，与主程序一致），按小时聚合各相机的 OK/NG 数量与合格率，
以 BeOS 风格（assets/07-beos.css 设计令牌）在浏览器中展示。

判定/聚合逻辑与主软件网页端共用（cameratinymes.okng）。

用法：
    uv run python utils/okng_hourly.py
    uv run python utils/okng_hourly.py --port 18140 --data-dir D:/JustSmart/CameraTinyMES
    uv run python utils/okng_hourly.py --host 0.0.0.0   # 允许局域网访问

访问 http://127.0.0.1:18130/
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1]
try:
    import cameratinymes  # noqa: F401
except ImportError:  # 未安装为包时直接使用源码目录
    sys.path.insert(0, str(ROOT / "src"))

from cameratinymes.config import (
    DatabaseConfig, default_data_dir, load_config, load_database_config,
)
from cameratinymes.okng import hourly_okng
from cameratinymes.storage import MariaDBStorage, SQLiteStorage, open_storage

log = logging.getLogger("okng_hourly")

DEFAULT_PORT = 18130


# ---------- 数据查询 ----------

def recent_days(storage, limit: int = 60) -> list[str]:
    """有采样记录的日期（倒序），用于日期下拉。"""
    rows = storage._fetchall(
        "SELECT DISTINCT substr(ts, 1, 10) AS d FROM samples"
        " ORDER BY d DESC LIMIT ?", (int(limit),))
    return [str(row[0]) for row in rows if row[0]]


# ---------- 页面（BeOS 风格，令牌来自 assets/07-beos.css） ----------

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>小时 OK/NG 率 — CameraTinyMES</title>
<style>
:root {
  --bg: #2a5556;
  --chrome: #cfcfcf;
  --chrome-light: #e8e8e8;
  --chrome-lighter: #ffffff;
  --chrome-dark: #888888;
  --chrome-darker: #404040;
  --yellow: #ffcc00;
  --yellow-dark: #cc9900;
  --red-dot: #e04040;
  --teal: #006666;
  --ink: #000;
  --font: "Swiss 721", "Helvetica", "Swis721 BT", "Arial", sans-serif;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--ink);
  font: 13px/1.4 var(--font);
  padding: 28px 16px 40px;
}
.desktop { max-width: 1280px; margin: 0 auto; }

/* ---- BeOS 窗口 ---- */
.window {
  background: var(--chrome);
  border: 1px solid var(--chrome-darker);
  box-shadow: 2px 2px 0 rgba(0,0,0,.35);
  padding: 3px;
}
.window-frame {
  border-top: 1px solid var(--chrome-lighter);
  border-left: 1px solid var(--chrome-lighter);
  border-right: 1px solid var(--chrome-darker);
  border-bottom: 1px solid var(--chrome-darker);
}
.titlebar { display: flex; align-items: stretch; gap: 0; margin-bottom: 3px; }
.tab {
  display: inline-flex; align-items: center; gap: 8px;
  background: var(--yellow);
  border: 1px solid var(--chrome-darker);
  border-bottom: none;
  border-radius: 6px 6px 0 0;
  padding: 3px 18px 4px 10px;
  font-weight: bold; font-size: 13px;
  transform: translateY(1px);
}
.tab .dot {
  width: 10px; height: 10px; border-radius: 50%;
  background: var(--red-dot);
  border: 1px solid #a03030;
  display: inline-block;
}
.tab .dot.min { background: var(--chrome-lighter); border-color: var(--chrome-dark); }
.tab .dot.zoom { background: var(--chrome-light); border-color: var(--chrome-dark); }
.tab-rest {
  flex: 1;
  border: 1px solid var(--chrome-darker);
  border-bottom: none;
  border-radius: 6px 6px 0 0;
  background: linear-gradient(var(--chrome-light), var(--chrome));
  transform: translateY(1px);
}

/* ---- 菜单栏 ---- */
.menubar {
  display: flex; gap: 0;
  border: 1px solid var(--chrome-darker);
  background: var(--chrome);
  margin-bottom: 3px;
}
.menubar span { padding: 2px 10px; font-size: 12px; }
.menubar span + span { border-left: 1px solid var(--chrome-dark); }
.menubar .hint { margin-left: auto; color: var(--chrome-darker); border: none; font-style: italic; }

/* ---- 工具栏 ---- */
.toolbar {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  padding: 8px;
  border: 1px solid var(--chrome-darker);
  background: var(--chrome-light);
  margin-bottom: 3px;
}
.bevel-in {
  border-top: 1px solid var(--chrome-dark);
  border-left: 1px solid var(--chrome-dark);
  border-right: 1px solid var(--chrome-lighter);
  border-bottom: 1px solid var(--chrome-lighter);
  background: var(--chrome-lighter);
  padding: 3px 6px;
  font: 13px var(--font);
}
select.bevel-in { padding: 3px 2px; }
button {
  font: bold 12px var(--font);
  background: var(--chrome);
  border: 1px solid var(--chrome-darker);
  border-top-color: var(--chrome-lighter);
  border-left-color: var(--chrome-lighter);
  padding: 4px 14px;
  cursor: pointer;
}
button:active {
  border-top-color: var(--chrome-dark);
  border-left-color: var(--chrome-dark);
  border-right-color: var(--chrome-lighter);
  border-bottom-color: var(--chrome-lighter);
}
label.chk { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; cursor: pointer; }
.legend { margin-left: auto; display: flex; align-items: center; gap: 10px; font-size: 11px; }
.legend i { display: inline-block; width: 12px; height: 12px; border: 1px solid var(--chrome-darker); vertical-align: -2px; margin-right: 4px; }
.sw-ok { background: #bfe6d4; }
.sw-warn { background: #ffe566; }
.sw-bad { background: #ffb0b0; }
.sw-empty { background: var(--chrome); }

/* ---- 表格 ---- */
.tablewrap { border: 1px solid var(--chrome-darker); background: var(--chrome-lighter); overflow: auto; max-height: 62vh; }
table { border-collapse: collapse; width: 100%; font-size: 12px; }
th, td { border: 1px solid #d4d4d4; padding: 3px 4px; text-align: center; white-space: nowrap; }
thead th {
  position: sticky; top: 0; z-index: 2;
  background: var(--chrome);
  border-top: 1px solid var(--chrome-lighter);
  border-left: 1px solid var(--chrome-lighter);
  border-right: 1px solid var(--chrome-darker);
  border-bottom: 1px solid var(--chrome-darker);
  font-size: 11px; padding: 4px 3px;
}
th.cam-h, td.cam {
  position: sticky; left: 0; z-index: 3;
  background: var(--chrome-light);
  border-right: 1px solid var(--chrome-darker);
  font-weight: bold; text-align: left; padding: 3px 8px; min-width: 110px;
}
thead th.cam-h { z-index: 4; }
tbody tr:hover td.cam { background: var(--yellow); }
td.rate { font-weight: bold; font-size: 12px; }
td .cnt { display: block; font-size: 10px; color: var(--chrome-darker); font-weight: normal; }
td.full { background: #bfe6d4; color: #003d2b; }
td.warn { background: #ffe566; color: #5c4a00; }
td.bad { background: #ffb0b0; color: #7a1010; }
td.empty { background: var(--chrome); color: var(--chrome-dark); }
td.total-col { background: var(--chrome-light); font-weight: bold; }
tfoot td { background: var(--chrome); font-weight: bold; }

/* ---- 状态栏 ---- */
.statusbar {
  display: flex; gap: 6px; margin-top: 3px;
  border: 1px solid var(--chrome-darker);
  background: var(--chrome);
  padding: 3px 6px;
  font-size: 11px;
}
.statusbar .cell {
  border-top: 1px solid var(--chrome-dark);
  border-left: 1px solid var(--chrome-dark);
  border-right: 1px solid var(--chrome-lighter);
  border-bottom: 1px solid var(--chrome-lighter);
  padding: 1px 8px;
}
.statusbar .grow { flex: 1; }

.loading { padding: 30px; text-align: center; color: var(--chrome-darker); font-style: italic; }
.err { color: var(--red-dot); font-weight: bold; }
@media (prefers-reduced-motion: no-preference) {
  .refreshing .tab .dot.min { animation: blink 1s steps(2) infinite; }
  @keyframes blink { 50% { background: var(--red-dot); } }
}
</style>
</head>
<body>
<div class="desktop">
  <div class="window" id="win">
    <div class="titlebar">
      <div class="tab">
        <span class="dot"></span><span class="dot min"></span><span class="dot zoom"></span>
        小时OK/NG率
      </div>
      <div class="tab-rest"></div>
    </div>
    <div class="window-frame">
      <div class="menubar">
        <span>File</span><span>View</span><span>Help</span>
        <span class="hint" id="hint">CameraTinyMES · BeOS</span>
      </div>
      <div class="toolbar">
        <span>日期</span>
        <input type="date" id="date" class="bevel-in">
        <select id="daylist" class="bevel-in"><option value="">— 最近日期 —</option></select>
        <button id="refresh">刷新</button>
        <button id="today">今天</button>
        <label class="chk"><input type="checkbox" id="auto"> 自动刷新(60s)</label>
        <div class="legend">
          <span><i class="sw-ok"></i>全OK</span>
          <span><i class="sw-warn"></i>有NG</span>
          <span><i class="sw-bad"></i>NG≥20%</span>
          <span><i class="sw-empty"></i>无数据</span>
        </div>
      </div>
      <div class="tablewrap" id="content"><div class="loading">正在加载数据…</div></div>
      <div class="statusbar">
        <span class="cell" id="st-backend">后端: -</span>
        <span class="cell" id="st-total">样本: -</span>
        <span class="cell grow" id="st-msg"></span>
        <span class="cell" id="st-time">-</span>
      </div>
    </div>
  </div>
</div>
<script>
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var dateEl = $("date"), listEl = $("daylist"), contentEl = $("content");
  var autoEl = $("auto"), timer = null;

  function dayParam() { return dateEl.value || new Date().toISOString().slice(0, 10); }

  function localToday() {
    var d = new Date();
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0")
         + "-" + String(d.getDate()).padStart(2, "0");
  }

  function rateClass(rate, ng) {
    if (ng <= 0) return "full";
    if (rate >= 0.8) return "warn";
    return "bad";
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function render(data) {
    var hours = [];
    for (var h = 0; h < 24; h++) hours.push(("0" + h).slice(-2));
    var html = ['<table><thead><tr><th class="cam-h">相机</th>'];
    hours.forEach(function (h) { html.push("<th>" + h + "</th>"); });
    html.push('<th style="min-width:96px">合计</th></tr></thead><tbody>');

    if (!data.cameras.length) {
      html.push('<tr><td class="cam">-</td><td class="empty" colspan="25">该日期无采样记录</td></tr>');
    }
    data.cameras.forEach(function (cam) {
      html.push('<tr><td class="cam">' + esc(cam.camera) + "</td>");
      hours.forEach(function (h) {
        var c = cam.hours[h];
        var judged = c ? c.ok + c.ng : 0;
        if (!judged) { html.push('<td class="empty">·</td>'); return; }
        var rate = c.ok / judged;
        var cls = rateClass(rate, c.ng);
        html.push('<td class="' + cls + ' rate" title="OK ' + c.ok + " / NG " + c.ng + '">' +
          (rate * 100).toFixed(1) + "%<span class='cnt'>" + c.ok + "/" + c.ng + "</span></td>");
      });
      var tJudged = cam.ok + cam.ng;
      var tRate = tJudged ? cam.ok / tJudged : null;
      html.push('<td class="total-col">' +
        (tRate === null ? "-" : (tRate * 100).toFixed(1) + "%") +
        '<span class="cnt">' + cam.ok + "/" + cam.ng + "</span></td></tr>");
    });
    html.push("</tbody><tfoot><tr><td class='cam'>合计</td>");
    var gOk = 0, gNg = 0;
    hours.forEach(function (h) {
      var ok = 0, ng = 0;
      data.cameras.forEach(function (cam) {
        var c = cam.hours[h]; if (c) { ok += c.ok; ng += c.ng; }
      });
      gOk += ok; gNg += ng;
      var judged = ok + ng;
      if (!judged) { html.push('<td class="empty">·</td>'); return; }
      var rate = ok / judged;
      html.push('<td class="' + rateClass(rate, ng) + ' rate">' +
        (rate * 100).toFixed(1) + "%<span class='cnt'>" + ok + "/" + ng + "</span></td>");
    });
    var gJudged = gOk + gNg;
    html.push('<td class="total-col">' +
      (gJudged ? ((gOk / gJudged) * 100).toFixed(1) + "%" : "-") +
      '<span class="cnt">' + gOk + "/" + gNg + "</span></td></tr></tfoot></table>");
    contentEl.innerHTML = html.join("");

    $("st-backend").textContent = "后端: " + data.backend + " · " + data.endpoint;
    $("st-total").textContent = "样本: " + data.cameras.reduce(function (s, c) { return s + c.total; }, 0);
    $("st-msg").textContent = data.cameras.length + " 台相机";
    $("st-time").textContent = "更新于 " + data.generated_at;
  }

  function load() {
    document.body.classList.add("refreshing");
    fetch("/api/data?date=" + encodeURIComponent(dayParam()))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        document.body.classList.remove("refreshing");
        if (data.error) { contentEl.innerHTML = '<div class="loading err">' + esc(data.error) + "</div>"; return; }
        render(data);
      })
      .catch(function (e) {
        document.body.classList.remove("refreshing");
        contentEl.innerHTML = '<div class="loading err">加载失败: ' + esc(e) + "</div>";
      });
  }

  function loadDays() {
    fetch("/api/dates").then(function (r) { return r.json(); }).then(function (d) {
      var opts = '<option value="">— 最近日期 —</option>';
      (d.days || []).forEach(function (day) { opts += '<option value="' + day + '">' + day + "</option>"; });
      listEl.innerHTML = opts;
      if (d.days && d.days.length && !dateEl.value) dateEl.value = d.days[0];
    }).catch(function () {});
  }

  dateEl.addEventListener("change", load);
  listEl.addEventListener("change", function () {
    if (listEl.value) { dateEl.value = listEl.value; load(); }
  });
  $("refresh").addEventListener("click", load);
  $("today").addEventListener("click", function () { dateEl.value = localToday(); load(); });
  autoEl.addEventListener("change", function () {
    if (timer) { clearInterval(timer); timer = null; }
    if (autoEl.checked) timer = setInterval(load, 60000);
  });

  if (!dateEl.value) dateEl.value = localToday();
  loadDays();
  load();
})();
</script>
</body>
</html>
"""


# ---------- HTTP 服务 ----------

class Handler(BaseHTTPRequestHandler):
    storage = None  # 由 serve() 注入
    cameras = ()    # 相机配置（含按相机结果单元格与通过/不通过标记）

    def log_message(self, fmt, *args):  # 静默默认访问日志
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif parsed.path == "/api/data":
                qs = parse_qs(parsed.query)
                day = (qs.get("date") or [""])[0]
                try:
                    datetime.strptime(day, "%Y-%m-%d")
                except ValueError:
                    self._json({"error": f"非法日期：{day!r}，应为 YYYY-MM-DD"}, 400)
                    return
                self._json({
                    "date": day,
                    "backend": self.storage.backend,
                    "endpoint": getattr(self.storage, "endpoint",
                                        getattr(self.storage, "path", "")),
                    "cameras": hourly_okng(self.storage, day, self.cameras),
                    "generated_at": datetime.now().strftime("%H:%M:%S"),
                })
            elif parsed.path == "/api/dates":
                self._json({"days": recent_days(self.storage)})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # 后端断连等运行期错误
            log.exception("请求失败")
            self._json({"error": f"数据库查询失败：{exc}"}, 500)


def serve(args) -> int:
    data_dir = args.data_dir or default_data_dir()
    print(f"数据目录: {data_dir}")
    cfg: DatabaseConfig = load_database_config(data_dir)
    print(f"数据库配置: backend={cfg.backend}", end="")
    if cfg.backend != "sqlite":
        print(f"  MariaDB {cfg.mariadb.user}@{cfg.mariadb.host}:{cfg.mariadb.port}"
              f"/{cfg.mariadb.database}")
    else:
        print(f"  SQLite {cfg.sqlite_path}")

    storage = open_storage(data_dir, cfg)
    kind = type(storage).__name__
    print(f"已连接: {kind}"
          + (f" -> {storage.endpoint}" if isinstance(storage, MariaDBStorage)
             else f" -> {storage.path}" if isinstance(storage, SQLiteStorage) else ""))

    try:
        cameras = load_config(data_dir)
    except Exception as exc:
        print(f"读取相机配置失败（{exc}），将按全局 OK/NG 文本判定", file=sys.stderr)
        cameras = []
    print(f"相机配置: {sum(1 for c in cameras if c.enabled)} 台启用"
          f" / {len(cameras)} 台（来自 config/cameras.json）")

    Handler.storage = storage
    Handler.cameras = cameras
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{args.port}/"
    print(f"小时 OK/NG 率看板已启动: {url}  (Ctrl+C 停止)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        server.server_close()
        storage.close()
    return 0


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="小时 OK/NG 率看板（独立网页程序）")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="数据目录（默认读取系统默认目录）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"监听端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise SystemExit(serve(args))
