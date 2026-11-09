# CameraTinyMES — 迷你 MES 系统

<p align="center">
  <img src="assets/app_icon_preview.png" alt="CameraTinyMES" width="128" />
</p>

基于 Python 的**迷你 MES（制造执行系统）**：只依赖 `sqlite3` 等轻量库，即可在产线现场实现 MES 的核心功能——**数据采集、生产追溯、质量判定与统计报表**。

> 本工程从 [CameraCellMonitor](https://github.com/JustSmartAuto/CameraViewerTauri)（Cognex In-Sight 单元格数据监控器）派生而来，复用其相机数据采集底座，在其上扩展 MES 功能。
> 界面与交互参考 `显示软件=20260321`（CameraHelper，WinForms 多相机宫格显示软件）。

## 项目定位

| 传统 MES | CameraTinyMES |
|---|---|
| 重型架构（Oracle/SQL Server + B/S + 多服务） | 单机轻量（标准库 `sqlite3` 单文件数据库） |
| 部署复杂、需要服务器 | Python 脚本 / 单 exe 即开即用 |
| 通用行业方案 | 围绕视觉检测产线（智能相机 + GigE 工业相机）定制 |

适合**小型产线 / 试产线 / 实验线**：没有 IT 部门支持时，一台电脑 + 几台相机即可跑起来。

## 界面截图

| 界面截图 | 说明 |
|---|---|
| ![多相机宫格主界面](assets/annotated_ui_2026-11-02_111229.png) | **多相机宫格主界面**：单元格实时值、连接状态与运行日志 |
| ![单相机面板](assets/annotated_ui_2026-11-02_090504.png) | **单相机面板**：单元格结果与连接 / 趋势图 / 导出操作 |
| ![单元格订阅管理](assets/annotated_ui_2026-11-02_090945.png) | **单元格订阅管理**：类型、记录模式、采样间隔与上下限 |

## 功能

### 数据采集（源自 CameraCellMonitor）

- **两类相机接入**：
  - **智能相机**：Cognex In-Sight（CogSocket `ws://<相机IP>:<端口>/ws`，固件 22.2+ 默认端口 80，6.x+ 为 8087），相机在机端完成检测，软件采集其单元格结果
  - **GigE 工业相机**：支持 GigE Vision / GenICam 标准的工业面阵相机，软件取像后结合检测算法 / 判定逻辑产出结果，适合需要自主成像与检测的场景
- **多相机并发**：可同时连接 / 混用多台智能相机与 GigE 工业相机，单相机掉线自动重连，不影响其他相机
- **多单元格订阅**：智能相机每台可配置多个电子表格单元格（测量值、分数、判定结果等），支持按结果事件（`resultChanged`）触发或定时轮询（`poll_interval`）
- **单元格数据类型**：`auto / int / float / string / bool / other`，未知类型可用 `auto`（按值推断）
- **阈值判定**：advanced 记录模式 + 数值类型时按上下限判定 OK/NG；simple 模式仅记录数据、不判定

### MES 功能（本项目新增）

- **产品追溯**：以工件 / 批次号为主线，串联 检测时间、相机、单元格数据与 OK/NG 判定，可按件 / 按批查询完整检测记录
- **生产记录**：班次 / 工单 / 产品型号维度的产量统计（总数 / OK 数 / NG 数 / 直通率）
- **质量看板**：按时间、产品、 相机等多维度统计良率、超差率、CPK 等指标
- **NG 分析**：NG 明细查询与导出（xlsx + CSV），辅助根本原因分析
- **报表导出**：xlsx 报表按 相机/日期/单元格 生成工作表，所有导出文件名自动追加时间戳（`_yyyyMMdd_HHmmss`）防覆盖
- **可视化**：折线 / 散点 / 趋势图（虚线基线 = 参考值/阈值，红色段 = 超差），中文字体内嵌 `assets/`，不依赖系统字体
- **人工复核（误判标注）**：对生产数据中的误判图片进行人工复核——在图像上绘制方框 ROI 圈出误判之处，并填写误判原因 / 注意点说明；标注结果按 **XAnyLabeling 3.3.x** 格式存储（详见下文「误判标注」），可直接用于其他视觉算法训练学习误判区分

## 技术要点

| 项 | 方案 |
|---|---|
| 语言/环境 | Python 3.13，尽量只用轻量依赖 |
| 包管理 | [uv](https://github.com/astral-sh/uv)（`pyproject.toml` + `uv.lock`） |
| 智能相机通信 | CogSocket WebSocket（JSON-RPC：GET/POST/PUT/DELETE + 事件订阅），参考 `reference/CogSocket&WebApi.md` |
| GigE 工业相机 | GigE Vision / GenICam 取像（帧回调触发采样与判定） |
| 数据存储 | 优先连接 MariaDB（工控机可经 `reference/mariadb_installer.py` 安装）；连接不可用时自动回退到标准库 `sqlite3` 单文件数据库，保证最小可用性 |
| 报表 | `openpyxl`（xlsx） |
| 图表 | `matplotlib`（中文字体从 `assets/*.ttf` 加载） |
| 界面 | tkinter（多相机宫格布局，参照 CameraHelper） |
| 打包 | PyInstaller 单文件 exe，命名 `exe_{yyyyMMddHHmmss}`，logo 使用 `assets/app_icon.ico` |

## 数据模型（SQLite）

当前为采样、图像、AV1 分片、帧索引、事件、标注六张表；相机与单元格配置存于 `config/cameras.json`，不入库。

| 表 | 说明 | 关键字段 |
|---|---|---|
| `samples` | 采样记录（含判定依据快照） | `ts` 时间戳、`camera` 相机、`cell` 单元格、`job` 拍照时相机作业（jobx 配方名）、`value` 数值、`text` 原始文本、`ok` 判定（1=OK / 0=NG / NULL=无阈值）、`lower_limit` / `upper_limit` 判定时上下限快照、`spec_version` 规格版本（预留） |
| `images` | 单帧图像 BLOB（旧数据与编码回退） | `ts`、`camera`、`cell`、`job`（jobx 配方名）、`kind`（`image`=原图 / `render`=结果渲染图）、`mime`、`width`、`height`、`data` |
| `image_streams` | AV1 帧序列分片（IVF 码流） | `ts_start` / `ts_end` 分片首末帧时间、`camera`、`cell`、`job`（jobx 配方名）、`kind`、`codec`、`container`、`frame_count`、`width`、`height`、`size_bytes`、`data` |
| `image_frames` | 分片帧索引 | `stream_id`（关联 `image_streams.id`，级联删除）、`frame_index` 片内序号、`ts`、`camera`、`cell`、`kind`、`width`、`height` |
| `events` | 异常事件（轻量） | `ts` 首次发生、`last_ts` 最近发生、`recovered_ts` 恢复时间、`camera`、`cell`、`code` 事件码、`message`、`status`（`open` / `recovered` / `acked`） |
| `annotations` | 误判人工复核标注 | `image_id`（关联 `images.id`，随图像级联删除）、`camera`、`cell`、`ts`、`reviewer` 复核人、`label` 误判类别、`description` 误判原因/注意点、`json` 完整 XAnyLabeling 3.3.x JSON（`imageData` 内嵌图片） |

索引：`idx_samples_cam_cell(camera, cell, ts)`、`idx_samples_cam_job(camera, job, ts)`、`idx_images_cam_cell(camera, cell, ts)`、`idx_streams_cam_cell_ts(camera, cell, kind, ts_start)`、`idx_frames_cam_cell_ts(camera, cell, kind, ts)`、`idx_frames_stream(stream_id, frame_index)`、`idx_events_camera_code_ts(camera, code, ts)`、`idx_annotations_image(image_id)`、`idx_annotations_cam_cell_ts(camera, cell, ts)`。

`job` 为拍照时相机正在运行的作业名（`GET cam0/hmi/job/name`，如 `myjob.jobx`），即该次检测使用的配方：连上相机立即读取，之后每 10 秒刷新；换配方时自动封掉旧的 AV1 分片，保证一个分片只属于一种配方。按 `job` 分组即可统计各配方的 NG 率 / 误判率（标注经 `annotations.image_id` 关联到带 `job` 的 `images`）。

`lower_limit` / `upper_limit` 在写入采样时记录当时的判定阈值（仅 `advanced` + 数值类型），后续修改单元格配置不影响历史判定依据；`events` 对应对标系统 `ExceptionEvents` 的简化版，表结构已就绪，采集端断线 / NG 突发写入待接入；`spec_version` 对应 `ParameterSpecs.Version`，暂为预留列。

旧库升级：初始化时检测缺失列并自动 `ALTER TABLE` 补齐（SQLite 用 `PRAGMA table_info`，MariaDB 用 `SHOW COLUMNS`），已有采样数据不受影响。

> 具体表结构以 `src/cameratinymes/storage.py` 实际代码为准。

### AV1 帧序列压缩（A0 连续图像）

产线上同工位的 A0 图像连续且变化很小，逐张 JPEG 存储占用大。软件把同一「相机 + 单元格 + 类型」的连续帧按 GOP 打成 AV1（IVF）分片后入库：

- 每片最多 **30 帧**，或空闲 **5 秒**、或相机换配方（`job` 变化）即封片；片内首帧为关键帧，取单帧最多解码 30 帧
- 编码参数：SVT-AV1 `preset 10` + `CRF 32`；实测 OP20-58 真实序列压缩率约 **6.5×**（1.39 MB → 213 KB / 30 帧），编码约 0.53 s、解码约 0.17 s
- 原始图与 A0 渲染图都按序列编码；旧版逐张 `images` 数据保持原样，查询与导出时自动合并两种来源
- 删除按整片进行（片内帧互相依赖）：时间范围与分片重叠即整片删除，片内所有帧一并计数
- 无 ffmpeg（或编码 / 入库失败）时自动回退为逐张 JPEG 写入 `images`，不丢数据
- 依赖外部 `ffmpeg`（需含 `libsvtav1`）；启动时按 **环境变量 `CAMERATINYMES_FFMPEG` → `PATH` → 离线安装器默认目录（`%LOCALAPPDATA%\ffmpeg` / `%ProgramFiles%\ffmpeg`）→ 注册表 `PATH`** 的顺序自动定位，用 `silent安装器_2606231854/ffmpeg_installer` 装好后无需手工配置（进程环境是安装前的旧值也能找到，首次启动前请先关闭已运行的旧程序）
- 崩溃 / 断电最多丢失最后一个未封片（≤30 帧或 ≤5 秒）；断开相机与关闭程序都会强制封片

## 数据库连接（MariaDB 优先，SQLite 兜底）

工控机可能已通过 `reference/mariadb_installer.py` 安装 MariaDB（一键离线安装器，内嵌 MariaDB 13.0.2 MSI 静默安装）。安装器的固定参数即本软件的默认连接参数：

| 安装器参数 | 值 |
|---|---|
| 安装 / 数据目录 | `D:\JustSmart\MariaDB`（无 D 盘回退 `%APPDATA%\MariaDB`） |
| 端口 | **8306**（非默认 3306） |
| 服务名 | `MariaDB`（注册为 Windows 服务） |
| 管理员账号 | `justsmart / justsmart`（`'%'` + `'localhost'`，全部权限） |
| 网络 | `bind-address=0.0.0.0`，允许产线其他设备远程连接 |

软件启动时按以下顺序选择存储后端：

1. **探测 MariaDB**：按配置的主机 / 端口 / 账号尝试连接（默认 `127.0.0.1:8306`，账号 `justsmart`）
2. **可用**：所有采样记录、图像与标注数据写入 MariaDB，支持多客户端并发访问与集中汇总
3. **不可用**（服务未安装 / 未启动 / 连接超时 / 认证失败）：自动回退到 `sqlite3` 单文件数据库并记录日志，**保证单机最小可用性**，不因数据库缺失影响产线采集

两套后端共用同一套表结构与数据访问接口（`Storage` 基类 + `SQLiteStorage` / `MariaDBStorage`），对上层功能（追溯、看板、报表、标注）透明。运行中连接断开时驱动会自动重连并重试一次（错误码 2006 / 2013）；重连失败则记录日志，当前版本不做本地缓冲补写。

配置文件为 `<数据目录>/config/database.json`（缺失时使用默认值，即 MariaDB 优先）：

```json
{
  "db_backend": "auto",
  "mariadb": {
    "host": "127.0.0.1",
    "port": 8306,
    "user": "justsmart",
    "password": "justsmart",
    "database": "camera_tiny_mes",
    "connect_timeout": 3
  },
  "sqlite": { "path": "db/cell_monitor.db" }
}
```

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `db_backend` | `auto`（MariaDB 优先，回退 SQLite）/ `mariadb`（连不上直接报错）/ `sqlite`（强制本地） | `auto` |
| `mariadb.host` / `port` | MariaDB 地址与端口 | `127.0.0.1` / `8306` |
| `mariadb.user` / `password` | 账号与密码 | `justsmart` / `justsmart` |
| `mariadb.database` | 数据库名（仅字母/数字/下划线，不存在则自动建库建表） | `camera_tiny_mes` |
| `mariadb.connect_timeout` | 连接超时（秒） | `3` |
| `sqlite.path` | 回退时 SQLite 文件路径（相对数据目录） | `db/cell_monitor.db` |

MariaDB 驱动使用纯 Python 的 `pymysql`（已列入 `pyproject.toml` 依赖）；所有时间戳统一存 ISO8601 字符串（毫秒），保证两种后端排序与范围查询行为一致。

## SQLite → MariaDB 数据迁移（实录）

2026-11-09 将现场 SQLite 库 `cell_monitor.db` 全量迁入 `192.168.10.64:8306/camera_tiny_mes`，可作为后续换机 / 汇聚的模板。

| 项 | 结果 |
|---|---|
| 源库 | SQLite，约 1 GB（`samples` 512,717 行；`images` 14,099 张 / 968,772,755 字节） |
| 目标 | MariaDB 13.0.2，非默认端口 **8306**，账号 `justsmart` |
| 建表 | 复用运行时 `MariaDBStorage` 的 DDL，保证迁移后表结构与程序一致 |
| 耗时 | 644 秒（采样约 394s，图像约 250s） |
| 校验 | 行数、id 区间、时间范围、`SUM(LENGTH(data))`、`text` 字节总量全部一致 |

### 操作步骤

1. **连通性**：先 `Test-NetConnection <host> -Port 8306`，再用 `pymysql` 登录，确认 `SELECT VERSION()`、`max_allowed_packet`、目标库是否已存在。
2. **建库建表**：直接实例化 `MariaDBStorage(MariaDBConfig(host=..., port=8306, ...))`，自动 `CREATE DATABASE` + 四张表 + 补列，避免手写 DDL 与程序漂移。
3. **空表检查**：迁移前 `SELECT COUNT(*) FROM samples/images`，非 0 立即中止，防止重复导入。
4. **分批导入**：保留源库主键 `id`；`samples` 每批 2000 行，`images` 按「50 张或 8 MB 先到者」打包；每批 `executemany` 后 `commit()`。
5. **校验**：对比两边的行数、`MIN/MAX(id)`、时间范围、图像字节总和，并抽查首尾行逐字段比对。

### 关键经验

- **端口不是 3306**：安装器固定 8306，远程连接前先确认端口与 `bind-address=0.0.0.0`。
- **`max_allowed_packet` 是图像批量的上限**：默认 16 MB，单批控制在 8 MB 以内；单张超 16 MB 需先调大服务端参数。
- **保留原 `id`**：显式插入主键后，MariaDB 会自动把 `AUTO_INCREMENT` 推到 `MAX(id)+1`，无需手工重置；同时便于核对与断点续传。
- **每批提交而非单事务**：1 GB 数据单事务会撑大 undo log 且失败全丢；分批提交可中断重跑（配合空表检查或按 id 续传）。
- **不要用 `SUM(LENGTH(text))` 直接对拷校验**：SQLite 的 `length()` 统计**字符数**，MariaDB 的 `LENGTH()` 统计**字节数**。正确做法是两边都取字节：SQLite 用 `LENGTH(CAST(text AS BLOB))`。本次该差异约 3.3 MB，属正常现象。
- **旧库缺列**：本次源库没有 `lower_limit` / `upper_limit` / `spec_version`，迁移后置 `NULL`（无历史快照可补）；`ok` 全为 `NULL` 时保持原样，不要用默认值填充。
- **迁移后启用**：应用不会自动切换目标库，需在 `<数据目录>/config/database.json` 把 `mariadb.host` 改为目标 IP；否则仍连本机或回退 SQLite。
- 迁移脚本属于一次性工具，放在临时目录执行，不纳入仓库。

## 采样机制

**智能相机**（Cognex In-Sight）的单元格取值时机由 `sample_mode` 决定：

| 模式 | 触发方式 | 说明 |
|---|---|---|
| `event` | 相机 `resultChanged` 事件 | 订阅 `{sid}/resultChanged`，相机每次检测产出新 `HmiResult` 后回调提取单元格值并落库；处理完毕调用 `ready()` 应答，相机才继续下一帧（背压控制） |
| `poll` | 软件定时轮询 | 每隔 `poll_interval` 秒调用 `queryCellResults` 拉取单元格当前值 |

**GigE 工业相机**由软件端取像：相机帧回调 → 触发采样与判定逻辑 → 结果落库（OK/NG 判定在软件内完成，阈值配置与智能相机共用一套）。

## 误判标注（人工复核）

复核流程：从生产记录中筛选误判图片 → 人工在图像上绘制方框 ROI 标记误判位置 → 填写误判原因 / 注意点 → 保存标注记录，供其他视觉算法训练学习误判区分。

标注内容采用 **XAnyLabeling 3.3.x** JSON 格式，完整 JSON 存入 `annotations.json` 列（可按需导出为每张图片对应的同名 `.json` 文件），同时保存 `image_id`、`camera`、`cell`、`reviewer`、`label`、`description` 等元数据便于检索：

- **图片数据内嵌 `imageData`**（Base64 编码），**不依赖 `imagePath`**——标注文件自包含，脱离原始图片目录仍可被训练管线直接读取
- 方框 ROI 使用 `shape_type: "rectangle"`，`points` 为左上角 / 右下角两点的像素坐标
- `label` 为误判类别；`description` 记录误判原因或注意点

示例：

```json
{
  "version": "3.3.6",
  "flags": {},
  "shapes": [
    {
      "label": "misjudge_scratch",
      "score": null,
      "points": [[512.0, 340.0], [768.0, 512.0]],
      "group_id": null,
      "description": "误判原因：反光被误判为划痕；注意避免侧光角度",
      "difficult": false,
      "shape_type": "rectangle",
      "flags": {},
      "attributes": {},
      "kie_linking": []
    }
  ],
  "imagePath": "CAM01_20261108_101530.jpg",
  "imageData": "<Base64 编码的图片数据>",
  "imageHeight": 1944,
  "imageWidth": 2592
}
```

> `imagePath` 仅保留文件名作展示用途，训练读取以 `imageData` 为准。

## 目录

| 路径 | 说明 |
|---|---|
| `reference/CogSocket&WebApi.md` | CogSocket & HMI/Platform Web API 参考（连接、单元格读写、事件订阅） |
| `reference/Cognex In-Sight 3D 相机电子表格函数参考（整理版）.md` | 电子表格函数参考 |
| `reference/相机数据监控器_web_2610171547.py` | 派生来源参考实现（tkinter + 内嵌 Web） |
| `assets/annotated_ui_*.png` | 界面截图（多相机宫格 / 单相机面板 / 单元格订阅） |
| `assets/mes.jpg` | 应用 logo 原图 |

## 开发 / 运行

```bash
uv sync                              # 安装依赖
uv run mes                           # 启动应用
uv run mes --simulate                # 启动内置模拟相机离线体验完整流程
uv run python build_exe.py           # PyInstaller 打包 dist/exe_{yyyyMMddHHmmss}.exe
```

## License

[LICENSE](LICENSE) © JustSmartAuto
