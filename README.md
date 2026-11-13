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
- **单元格数据类型**：`auto / int / float / string / bool / other / image / pointcloud`，未知类型可用 `auto`（按值推断）
- **点云单元格（`pointcloud`）**：3D 相机（如 OP20-55 / `192.168.10.55` 的 A0）点云单元格值为占位标记 `❒PointCloud`，
  软件从 `acqPointCloudView` 取 `PointCloud3D.url`（`/cam0/pc/<id>`）下载 CPCD 点云（`image/x-cpcd`）原样入库
  （`images.kind='pointcloud'`，头部 20 字节 + N×3 个 float32 的 xyz 毫米坐标），同时记一条 `[点云 N 点 X MB]` 样本；
  `auto` 类型同样可自动识别点云单元格（与图像 `❒Image` 区分）。点云不参与 AV1 打包与阈值判定；
  单帧约 12 MB（百万点）量级，MariaDB 的 `max_allowed_packet` 建议不低于 32M（格式与取用详见下文「点云（CPCD）存储」）
- **阈值判定**：advanced 记录模式 + 数值类型时按上下限判定 OK/NG；simple 模式仅记录数据、不判定
- **报表单元格用途**（每台相机独立配置，不要求都叫 C11 / A0）：单元格「用途」可设为
  `自动（按名称推断）/ 普通数据 / 判定结果 / 报告图片`；判定结果单元格可自定义通过值与不通过值
  （默认 `OK` / `NG`，多个值用 `/` 分隔），报告图片单元格决定 NG 证据图与 OK 参考图取自哪一列

### MES 功能（本项目新增）

- **报表导出**：xlsx 报表按 相机/日期/单元格 生成工作表，所有导出文件名自动追加时间戳（`_yyyyMMdd_HHmmss`）防覆盖；图像单元格以 **WPS 嵌入单元格（`DISPIMG`）** 写入，图片随单元格缩放/排序，不浮动错位
- **可视化**：折线 / 散点 / 趋势图（虚线基线 = 参考值/阈值，红色段 = 超差）并附 均值 / 标准差 / CPK / 超差率，中文字体内嵌 `assets/`，不依赖系统字体
- **人工复核（误判标注）**：对生产数据中的误判图片进行人工复核——在图像上绘制方框 ROI 圈出误判之处，并填写误判原因 / 注意点说明；标注结果按 **XAnyLabeling 3.3.x** 格式存储（详见下文「误判标注」），可直接用于其他视觉算法训练学习误判区分
- **当日改善分析报告**（已实现，详见下文同名章节）：选日期 + 勾小时 → 统计各工站 NG/OK 与不良率 → 取证据图 → 生成 **xlsx（WPS `DISPIMG` 嵌入单元格）+ PDF（A3 横版单页米黄底）**，先入库再落盘；配套局域网 Web 工作台（快速 tag、草稿、标注、工单闭环、后台任务中心）

> 产品追溯、生产记录、质量看板、NG 明细导出等 MES 扩展功能**尚未实现**，与报告侧待验证项一并列在文末「路线图」。

## 技术要点

| 项 | 方案 |
|---|---|
| 语言/环境 | Python 3.13，尽量只用轻量依赖 |
| 包管理 | [uv](https://github.com/astral-sh/uv)（`pyproject.toml` + `uv.lock`） |
| 智能相机通信 | CogSocket WebSocket（JSON-RPC：GET/POST/PUT/DELETE + 事件订阅），参考 `reference/CogSocket&WebApi.md` |
| 点云采集 | 3D 相机点云单元格（`❒PointCloud`）→ `acqPointCloudView` 的 `PointCloud3D.url` → HTTP 下载 CPCD（`image/x-cpcd`），原样入库 `images`（`kind=pointcloud`），不参与 AV1 打包 |
| GigE 工业相机 | GigE Vision / GenICam 取像（帧回调触发采样与判定） |
| 数据存储 | 优先连接 MariaDB（工控机可经 `reference/mariadb_installer.py` 安装）；连接不可用时自动回退到标准库 `sqlite3` 单文件数据库，保证最小可用性 |
| 报表 | `openpyxl`（xlsx） |
| xlsx 图片 | **所有 xlsx 产物（改善报告 + 数据导出）统一把图片嵌入单元格**：openpyxl 生成版面后在 zip 层注入 WPS `DISPIMG` 公式与 `xl/cellimages.xml`（自实现，不依赖 WPS 手工操作） |
| 改善报告（PDF） | `reportlab`（`pdfrender.py`）：A3 横版单页、米黄纸底 `#FBF3DC`、中文字体内嵌 |
| Web 工作台 | 标准库 `ThreadingHTTPServer` + 原生 HTML/CSS/JS 单页（无框架、无 CDN），默认 `0.0.0.0:18110`，端口占用自动顺延 |
| 后台任务 | `tasks.py` 通用任务管理器：单线程队列、进度回调、取消、`dedupe_key` 去重、部分完成（`partial`）状态 |
| 图表 | `matplotlib`（中文字体从 `assets/*.ttf` 加载） |
| 界面 | tkinter（多相机宫格布局，参照 CameraHelper） |
| 打包 | PyInstaller 单文件 exe，命名 `CameraTinyMES_{yyyyMMddHHmmss}`，logo 使用 `assets/app_icon.ico`；渲染子进程用到的 `pywebview` / `pythonnet` / `clr_loader` 与 WebView2 原生 DLL（`webview/lib`）显式收集，运行机需有 WebView2 运行时（Win10/11 通常自带） |

## 数据模型（SQLite）

当前为六张采集表（采样、图像、AV1 分片、帧索引、事件、标注）加八张业务表（报告存档 4 张 + schema 版本 1 张 + 工单 3 张）；相机与单元格配置存于 `config/cameras.json`，报告生成选项存于 `config/report.json`，均不入库。

| 表 | 说明 | 关键字段 |
|---|---|---|
| `samples` | 采样记录（含判定依据快照） | `ts` 时间戳、`camera` 相机、`cell` 单元格、`job` 拍照时相机作业（jobx 配方名）、`value` 数值、`text` 原始文本、`ok` 判定（1=OK / 0=NG / NULL=无阈值）、`lower_limit` / `upper_limit` 判定时上下限快照、`spec_version` 规格版本（预留） |
| `images` | 单帧图像 BLOB（旧数据与编码回退） | `ts`、`camera`、`cell`、`job`（jobx 配方名）、`kind`（`image`=原图 / `render`=结果渲染图 / `pointcloud`=CPCD 点云）、`mime`、`width`、`height`、`data` |
| `image_streams` | AV1 帧序列分片（IVF 码流） | `ts_start` / `ts_end` 分片首末帧时间、`camera`、`cell`、`job`（jobx 配方名）、`kind`、`codec`、`container`、`frame_count`、`width`、`height`、`size_bytes`、`data` |
| `image_frames` | 分片帧索引 | `stream_id`（关联 `image_streams.id`，级联删除）、`frame_index` 片内序号、`ts`、`camera`、`cell`、`kind`、`width`、`height` |
| `events` | 异常事件（轻量） | `ts` 首次发生、`last_ts` 最近发生、`recovered_ts` 恢复时间、`camera`、`cell`、`code` 事件码、`message`、`status`（`open` / `recovered` / `acked`） |
| `annotations` | 误判人工复核标注 | `image_id`（关联 `images.id`，随图像级联删除）、`camera`、`cell`、`ts`、`reviewer` 复核人、`label` 误判类别、`description` 误判原因/注意点、`json` 完整 XAnyLabeling 3.3.x JSON（`imageData` 内嵌图片） |
| `schema_meta` | schema 版本号（规则同软件版本 `YY.MM.DD.N`） | `key`、`value`、`updated_at`；启动时比对并自动补列 / 升级 |
| `reports` | 改善报告存档头 | `day` 统计日期、`hours` 勾选小时 JSON、`filename`、`xlsx_size` / `pdf_size`、`xlsx_sha256` / `pdf_sha256`、`stations` / `products` / `ng` 汇总、`payload_json` 生成参数、`created_at` / `created_by`、`saved_xlsx_path` / `saved_pdf_path` / `saved_at` 落盘信息 |
| `report_files` | 报告文件二进制分块 | `report_id`（级联删除）、`kind`（`xlsx` / `pdf`）、`part_no` 分块序号、`data`（1 MB 分块，主键 `report_id+kind+part_no`） |
| `report_tags` | 快速 tag（人工复核口径） | `day`、`camera`、`cell`（该相机配置的报告图片单元格，默认 `A0`）、`ts`、`tag`（`misjudge` 误判 / `product` 产品自身NG / `external` 外部原因）、`reviewer`、`note`、`updated_at`；主键 `day+camera+ts` |
| `report_drafts` | 报告工作台草稿 | `day` 主键、`hours`、`payload_json`（原因、选图、其他NG、关联工单、项目信息）、`updated_at`、`updated_by`；乐观锁基于 `updated_at` |
| `tickets` | 改善工单（类 GitHub Issue） | `title`、`body`、`measure` 改善措施、`status`（`open`/`in_progress`/`done`/`closed`）、`close_reason`、`priority`、`category`（`misjudge`/`product`/`external`/`other`）、`station`、`camera`、`assignee`（默认「视觉相关人员」）、`due_date`、`created_at` / `updated_at` / `closed_at` |
| `ticket_reports` | 工单 ↔ 报告双向关联 | `ticket_id`、`report_id`（均级联删除）、`station`、`created_at` |
| `ticket_events` | 工单时间线 | `ticket_id`（级联删除）、`ts`、`actor`、`kind`（`created`/`comment`/`status`/`assign`/`link_report`/`measure`）、`payload` JSON |

索引：`idx_samples_cam_cell(camera, cell, ts)`、`idx_samples_cam_job(camera, job, ts)`、`idx_images_cam_cell(camera, cell, ts)`、`idx_streams_cam_cell_ts(camera, cell, kind, ts_start)`、`idx_frames_cam_cell_ts(camera, cell, kind, ts)`、`idx_frames_stream(stream_id, frame_index)`、`idx_events_camera_code_ts(camera, code, ts)`、`idx_annotations_image(image_id)`、`idx_annotations_cam_cell_ts(camera, cell, ts)`；报告与工单侧另有 `idx_reports_day(day, created_at)`、`idx_reports_created(created_at)`、`idx_report_tags_day_tag(day, tag)`、`idx_report_tags_cam_ts(camera, ts)`、`idx_tickets_status(status, updated_at)`、`idx_tickets_station(station, category)`、`idx_ticket_reports_report(report_id)`、`idx_ticket_events_ticket(ticket_id, ts)`。

`job` 为拍照时相机正在运行的作业名（`GET cam0/hmi/job/name`，如 `myjob.jobx`），即该次检测使用的配方：连上相机立即读取，之后每 10 秒刷新；换配方时自动封掉旧的 AV1 分片，保证一个分片只属于一种配方。按 `job` 分组即可统计各配方的 NG 率 / 误判率（标注经 `annotations.image_id` 关联到带 `job` 的 `images`）。

`lower_limit` / `upper_limit` 在写入采样时记录当时的判定阈值（仅 `advanced` + 数值类型），后续修改单元格配置不影响历史判定依据；`events` 对应对标系统 `ExceptionEvents` 的简化版，表结构已就绪，采集端断线 / NG 突发写入待接入；`spec_version` 对应 `ParameterSpecs.Version`，暂为预留列。

旧库升级：初始化时检测缺失列并自动 `ALTER TABLE` 补齐（SQLite 用 `PRAGMA table_info`，MariaDB 用 `SHOW COLUMNS`），已有采样数据不受影响。

> 具体表结构以 `src/cameratinymes/storage.py` 实际代码为准。

### AV1 帧序列压缩（图片单元格连续图像）

产线上同工位的 A0 图像连续且变化很小，逐张 JPEG 存储占用大。软件把同一「相机 + 单元格 + 类型」的连续帧按 GOP 打成 AV1（IVF）分片后入库：

- 每片最多 **30 帧**，或空闲 **5 秒**、或相机换配方（`job` 变化）即封片；片内首帧为关键帧，取单帧最多解码 30 帧
- 编码参数：SVT-AV1 `preset 10` + `CRF 32`（默认 YUV 4:2:0）
- 原始图与 A0 渲染图都按序列编码；旧版逐张 `images` 数据保持原样，查询与导出时自动合并两种来源
- 删除按整片进行（片内帧互相依赖）：时间范围与分片重叠即整片删除，片内所有帧一并计数
- 无 ffmpeg（或编码 / 入库失败）时自动回退为逐张 JPEG 写入 `images`，不丢数据
- 依赖外部 `ffmpeg`（需含 `libsvtav1`）；启动时按 **环境变量 `CAMERATINYMES_FFMPEG` → `PATH` → 离线安装器默认目录（`%LOCALAPPDATA%\ffmpeg` / `%ProgramFiles%\ffmpeg`）→ 注册表 `PATH`** 的顺序自动定位，用 `silent安装器_2606231854/ffmpeg_installer` 装好后无需手工配置（进程环境是安装前的旧值也能找到，首次启动前请先关闭已运行的旧程序）
- 崩溃 / 断电最多丢失最后一个未封片（≤30 帧或 ≤5 秒）；断开相机与关闭程序都会强制封片

#### 实测数据（SAB 现场数据库 `10.126.126.3`，原 `192.168.10.64`，2026-11-09）

**压缩比**（两个口径：JPEG→AV1 反映实际存储节省，PNG→AV1 反映 AV1 编码器本身效率）：

| 相机 / 单元格 | 类型 | 分辨率 | 原始 JPEG KB/帧 | PNG KB/帧 | AV1 KB/帧（30 帧 GOP） | JPEG→AV1 | PNG→AV1 |
|---|---|---|---|---|---|---|---|
| OP10-28 / A0 | image | 640×480 | 17.1 | 49.5 | 1.6 | **10.5×** | 30.6× |
| OP10-28 / A0 | render | 640×480 | 26.0 | 103.8 | 3.5 | **7.4×** | 29.6× |
| OP20-58 / A0 | image | 640×535 | 82.9 | 230.7 | 7.0 | **11.9×** | 33.1× |
| OP20-58 / A0 | render | 640×535 | 90.7 | 261.9 | 8.5 | **10.7×** | 30.9× |
| OP30-122 / A0 | image | 640×535 | 55.3 | 239.1 | 20.4 | **2.7×** | 11.7× |
| OP30-122 / A0 | render | 640×535 | 62.4 | 303.2 | 22.5 | **2.8×** | 13.5× |

- OP10 / OP20 两个工位（焊缝连续、帧间变化极小）实际存储节省 **7～12×**
- OP30 工位帧间变化较大（不同焊缝状态交替出现），压缩比降至 **~3×**，但仍然显著
- 原始 JPEG → AV1 分片 vs 原始 PNG → AV1 分片的巨大差距是因为 JPEG 本身已经做了帧内有损压缩；AV1 的帧间参考编码对"帧间变化率"高度敏感

> 历史实测（OP20-58 另一段序列，1.39 MB / 30 帧 → 213 KB）压缩率约 **6.5×**，与本次 10-12× 同量级。

**视觉质量**（用 [odiff](https://github.com/dmtrKovalenko/odiff) — Zig SIMD 加速的像素级图像差分工具 + PSNR + SSIM 三重佐证）：

| 相机 / 单元格 | 类型 | odiff 差异像素%（阈值 0.1） | PSNR (dB) | SSIM | 编码 30 帧 | 解码 30 帧 |
|---|---|---|---|---|---|---|
| OP10-28 / A0 | image | 0.024% | 44.57 | 0.9999 | 0.34 s | 0.23 s |
| OP10-28 / A0 | render | 0.043% | 38.78 | 0.9997 | 0.43 s | 0.23 s |
| OP20-58 / A0 | image | 0.028% | 39.59 | 0.9988 | 0.34 s | 0.23 s |
| OP20-58 / A0 | render | 0.062% | 37.44 | 0.9977 | 0.37 s | 0.29 s |
| OP30-122 / A0 | image | 0.050% | 36.80 | 0.9991 | 0.49 s | 0.54 s |
| OP30-122 / A0 | render | 0.103% | 34.69 | 0.9984 | 0.50 s | 0.63 s |

- **odiff 差异像素比例 0.02%～0.10%**：绝大多数帧在阈值 0.1 下完全匹配，偶有差异帧也只有几十个像素级别变化
- **PSNR 34-45 dB**：> 30 dB 通常被认为视觉质量优秀，> 40 dB 几乎无法察觉差异
- **SSIM 0.9977-0.9999**：结构相似性接近 1.0，图像结构几乎无损

**速度**：每 GOP（30 帧）编码 0.34-0.50 s（平均 ~15 ms/帧），解码 0.23-0.63 s（平均 ~15 ms/帧）——产线在线采集（通常 1-5 Hz）完全够用，解码后逐帧渲染也无感知延迟。

**测量方法**：odiff 项目（`D:\JustStupid\diffimg图像差分检测_2603170732`）提供 Zig SIMD 加速的像素级图像差分引擎 + PatInspect 模板训练，用于验证 AV1 压缩对产线图像检测精度的影响。测试脚本从 MariaDB 导出 30 张连续帧 → SVT-AV1 编码 → 解码 → odiff 逐帧对比原始 vs 压缩恢复。

#### AV1 vs JPEG 逐张存储的决策边界

| 因素 | 适合 AV1 分片 | 不适合 AV1（逐张 JPEG 即可） |
|---|---|---|
| 连续帧数量 | ≥ 5 张且帧间变化小 | 单帧或帧间差异大 |
| 帧间隔 | ≤ 5 秒连续采集 | 偶尔触发、间隔数分钟以上 |
| 内容变化 | 同工位稳定画面（如持续焊缝） | 频繁换料、画面剧烈变化 |
| 检索需求 | 通常批量访问 | 常需要单帧随机访问 |

软件已自动处理：满足 AV1 条件的走分片存储，不满足的（或 ffmpeg 不可用）回退逐张 `images` 表存储，两种来源在查询 / 导出时合并呈现。

### 点云（CPCD）存储

适用于 In-Sight 3D 相机（3D 视觉工站，如 OP20-55 / `192.168.10.55` 的 A0）。点云单元格在 `HmiResult.cells` 中只是一个占位标记 `❒PointCloud`，点云本体需另行下载：

| 环节 | 说明 |
|---|---|
| 地址解析 | `HmiResult.acqPointCloudView` → `layers[*].graphics` → `PointCloud3D.url`（形如 `/cam0/pc/000000000000528`） |
| 下载 | `GET http://<相机IP>:<端口>/cam0/pc/<id>`，返回 `image/x-cpcd`（Cognex Point Cloud Data） |
| 格式 | 头部 20 字节（小端：魔数 `0xB0B15BAD`、保留、头部长度 20、点数 N、保留），其后为 N × 3 个 `float32`（x / y / z，单位毫米）。`文件长度 = 20 + N × 12` |
| 入库 | 原样写入 `images`（`kind=pointcloud`、`mime=image/x-cpcd`、`width`/`height` 为 NULL），同时在 `samples` 记一条 `[点云 N 点 X MB]` 文本样本，便于合表查看与检索 |
| 采样 | 与图像单元格一致，强制按 `resultChanged` 事件采样；`auto` 类型可自动识别为点云（不会被误判成图像） |
| 不参与 | AV1 打包（分组查询限定 `kind IN ('image','render')`）、阈值判定、报告取图 |

单帧规模参考：OP20-55 / A0 约 **101 万点 / 12.1 MB**（实测 z 量程 −5.04 ~ −1.16 mm）。因此：

- MariaDB 的 `max_allowed_packet` 建议不低于 **32M**（默认 16M 时单帧 12 MB 已接近上限）；
- 点云不做压缩，入库占用随拍照次数线性增长，长期留存请配合时间范围清理策略。

取用示例（SQLite / MariaDB 通用）：

```sql
SELECT ts, cell, length(data) AS bytes FROM images
 WHERE camera='OP20-55' AND kind='pointcloud' ORDER BY ts DESC LIMIT 10;
```

读出 `data` 后按上表格式解析即可，`src/cameratinymes/pointcloud.py` 的 `parse_cpcd()` 会校验魔数与长度并返回点数。当前**未提供**点云可视化与 PLY / PCD 转换导出（见路线图）。

## 数据库连接（MariaDB 优先，SQLite 兜底）

### 产线数据库部署（虚拟局域网）

产线两台数据库工控机位于虚拟局域网，均为 MariaDB（端口 **8306**，账号 `justsmart`），可产线内远程连接：

| 产线 | 数据库主机 | 备注 |
|---|---|---|
| SAB | `10.126.126.3` | 原 `192.168.10.64`，迁入虚拟局域网后变更 |
| CAB | `10.126.126.4` | |

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

2026-11-09 将现场 SQLite 库 `cell_monitor.db` 全量迁入 `192.168.10.64:8306/camera_tiny_mes`（该库即现 SAB 产线数据库，迁入虚拟局域网后地址变更为 `10.126.126.3:8306`），可作为后续换机 / 汇聚的模板。

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

**图像 / 点云等媒体单元格**无论 `sample_mode` 怎么配，都强制按 `resultChanged` 事件采样（轮询会重复下载同一帧），并各自走独立的下载任务：图像解码为 JPEG（或进 AV1 分片），点云原样落库。

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

## 当日改善分析报告

面向客户的「视觉改善分析报告」：按日期 + 小时统计不良，自动取证据图，输出 xlsx（WPS 单元格内嵌图）与 A3 PDF，并把原因与改善措施挂到工单上闭环。

### 入口与载体

- **桌面端**：工具栏「改善报告」、数据菜单「生成改善报告…」、托盘菜单「改善报告工作台」
- **入口对话框**：选日期（只列有产线记录的日期）+ 勾小时（快捷：全选 / 0点-当前小时 / 清空 / 仅NG时段），并显示本机与局域网访问地址
- **载体**：内嵌 Web 工作台（单页，无框架），默认 `http://<本机IP>:18110/report`；地址 / 端口 / 口令在「系统 → 设置 → 改善报告工作台」
- **端口顺延**：默认端口被占用时自动 +1（最多 20 次），实际端口写回 `settings.json` 并在 `reports/.web_port` 留痕
- **安全边界**：监听默认 `0.0.0.0`（其他工控机可连）；打开桌面入口、打开/删除存档等敏感操作只允许**回环地址**；可设访问口令（cookie `ctm_token` / `?k=` / `X-CTM-Token`），请求体上限 1 MB

### 统计口径

- `H` NG 总数（NG 事件数）、`I` 视觉NG = `H - J`、`J` 其他NG = 人工 tag 之和（`M` 误判 + `X` 产品自身NG + 外部原因…）
- tag 可人工覆盖；tag 之和超过 `H` 时截断到 `H`，不出现负数
- 工站识别：相机配置 `station` 优先，缺失按相机名前缀 `OPxx` 推断；`station_name` 与 `model` 分别填报告 C、B 列
- 时段默认：当日 0 点→当前小时，历史日期全天

### 取图规则

- **NG 证据帧**：NG 事件时间与该相机图片单元格（默认 `A0`）帧时间最近且相差 ≤ 5 秒；渲染图（`kind=render`）优先，缺失回退原图并在工作台标「原图」角标
- **OK 参考图**：时段内 OK 记录时间中位数附近取一帧，匹配窗口 5 秒 → 60 秒 → 300 秒逐级放宽；也可在图库里多选（首张作 F 列参考图，其余作为误判 OK 证据图追加到 NG 图之后）
- 每台相机最多 100 张图片（NG 图占报告 M..X 列，超出模板 12 列时向右自动扩展；未手选时自动抽 12 张），单图长边压到 800 px 以内

### 输出规范

| 产物 | 说明 |
|---|---|
| 工作表 | 主表名 = 统计日期（如 `11月10日`），另附「数据核对」表（逐相机 OK/NG 与 tag 分布） |
| 列 | A 序号、B No.、C 工站&机台、D 相机型号&站名、E 实物图片、F OK相机图片、G 生产总数、H NG总数、I 视觉NG、J 其他NG、K、L 原因分析及改造解决方案、M..X NG 图 |
| 隐藏列 | E（实物图片）与 F（OK 相机图片）默认隐藏，按需用工作台「显示实物图片」开关打开 |
| DISPIMG | 图片以 `=_xlfn.DISPIMG("ID_xxx",1)` 公式嵌入单元格，并注入 `xl/cellimages.xml` + 关系文件 + `Content_Types`（WPS 私有部件类型 `application/vnd.wps-officedocument.cellimage+xml` 与关系类型 `http://www.wps.cn/officeDocument/2020/cellImage`）。`a:ext` 必须写图片真实像素换算的 EMU（1px=9525），写成 0 时 WPS 不绘制、单元格空白；数据导出的「原始图 / 渲染图」工作表同样走这条注入链路。报告注入失败时降级为浮动图片并记录警告 |
| PDF | A3 横版单页（1190.55 × 841.89 pt）、米黄纸底 `#FBF3DC`、中文字体内嵌，跳过隐藏列、一页自适应 |
| 文件名 | `视觉改善分析报告_{yyyyMMddHHmmss}.xlsx` / `.pdf`，写入数据目录的 `reports/` |

### 存档与落盘

- **先入库再落盘**：`reports` 存档头 + `report_files` 每 1 MB 分块（含 sha256 校验）→ 再按 1 MB 分块原子写盘
- 入库成功但落盘失败或被取消时，任务标记为**部分完成**，存档仍可在工作台下载
- 存档视图：列出历史报告（日期、小时、工站数、产品数、NG、文件大小），支持下载 / 重新导出 / 删除；显示存储占用统计
- 保留策略：`report_keep_days`（默认 90 天）、`report_keep_count`（默认 200 条），启动时清理 `*.tmp` 残留

### 工单闭环

- 右栏可按工站勾选或新建工单；分类 `misjudge` / `product` / `external` / `other`，责任人默认「视觉相关人员」
- 未闭环工单的「改善措施」自动追加到报告「原因分析及改造解决方案」正文
- 工单详情含状态机（`open` → `in_progress` → `done` → `closed`）、时间线（创建 / 评论 / 状态 / 指派 / 关联报告 / 改善措施）
- 编辑采用乐观锁（`base_updated_at`），冲突返回 409 并提示覆盖

### 后台任务

- 生成与导出统一提交到 `TaskManager`，页面右下角任务面板轮询进度，可取消
- 同一「日期 + 小时」重复提交自动去重（`dedupe_key`）；程序重启时未完成任务标记为「已中断」

### 相关配置

`config/settings.json`：

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `web_host` | 工作台监听地址（`0.0.0.0` = 局域网可访问，`127.0.0.1` = 仅本机） | `0.0.0.0` |
| `web_port` | 监听端口（被占用时自动顺延并写回） | `18110` |
| `web_token` | 访问口令，空 = 关闭 | 空 |
| `project_name` / `destination` / `line_name` | 报告标题与抬头信息 | 空 |

`config/report.json`：

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `pdf_engine` | `reportlab` / `com` | `reportlab` |
| `ng_images_per_station` | 每台相机图片上限（1~100，含 NG 与误判 OK） | `100` |
| `image_max_edge` | 嵌入图片长边上限（px，200~2000） | `800` |
| `reviewer` | 默认编制人 / 审核人 | 空 |
| `op_aggregate` | 数量聚合方式 `median` / `max` / `first` | `median` |
| `report_keep_days` / `report_keep_count` | 存档保留天数 / 条数（0 = 不限） | `90` / `200` |

## 目录

| 路径 | 说明 |
|---|---|
| `src/cameratinymes/report.py` | 改善报告：小时统计、原因摘要、`LayoutModel` 版面、xlsx 生成 |
| `src/cameratinymes/pointcloud.py` | 点云单元格支持：`❒PointCloud` 识别、`acqPointCloudView` 地址解析、CPCD 下载与头部解析 |
| `src/cameratinymes/wpsdispimg.py` | WPS `DISPIMG` 单元格图片注入（`cellimages.xml` + 关系 + Content_Types） |
| `src/cameratinymes/pdfrender.py` | 由 `LayoutModel` 渲染 A3 横版单页米黄底 PDF |
| `src/cameratinymes/reportstore.py` | 报告入库 / 分块落盘 / 重新导出 / 保留清理 |
| `src/cameratinymes/tasks.py` | 通用后台任务管理器（进度、取消、去重、部分完成） |
| `src/cameratinymes/web.py`、`src/cameratinymes/webui/` | 改善报告 Web 工作台后端与前端（`index.html` / `app.js` / `style.css`） |
| `docs/当日改善报告功能设计.md` | 改善报告完整设计（口径、接口、版面、存档、工单、验收） |
| `docs/数据库分析报告.md`、`docs/界面分析报告.md` | 对标 MES 数据库分析与界面分析 |
| `mocks/` | 改善报告与工单交互原型（纯静态 HTML，假数据） |
| `reference/CogSocket&WebApi.md` | CogSocket & HMI/Platform Web API 参考（连接、单元格读写、事件订阅） |
| `reference/Cognex In-Sight 3D 相机电子表格函数参考（整理版）.md` | 电子表格函数参考 |
| `reference/相机数据监控器_web_2610171547.py` | 派生来源参考实现（tkinter + 内嵌 Web） |
| `assets/annotated_ui_*.png` | 界面截图（多相机宫格 / 单相机面板 / 单元格订阅） |
| `assets/mes.jpg` | 应用 logo 原图 |

## 开发 / 运行

```bash
uv sync                              # 安装依赖
uv run camera-tiny-mes               # 启动应用
uv run camera-tiny-mes --simulate    # 启动内置模拟相机离线体验完整流程
uv run python build_exe.py           # PyInstaller 打包 dist/CameraTinyMES_{yyyyMMddHHmmss}.exe
```

应用启动时会自动拉起改善报告 Web 工作台：本机访问 `http://127.0.0.1:18110/report`，其他工控机访问 `http://<本机IP>:18110/report`。端口被占用时实际端口写入 `settings.json` 的 `web_port`，也可在「系统 → 设置 → 改善报告工作台」修改监听地址 / 端口 / 口令。

## 数据监控 Web（已实现 v1.0）

2026-11-13 交付。详情见 `docs/数据监控Web实施方案.md`。

### 功能总览

Web 工作台顶部新增"数据监控"页签，完整覆盖 Tkinter 主界面的实时采集能力：

| 能力 | 说明 |
|---|---|
| 多相机宫格面板 | 每卡显示相机连接状态、配方名、各单元格实时值（WebSocket 推送 ≤100ms） |
| 迷你折线图 | 每数值单元格内嵌 Chart.js 迷你折线（红段=超差），数据来自 LiveCache 最近 200 点 |
| 历史趋势对话框 | 单元格名旁 📈 按钮 → 时间范围选择 → Chart.js 折线（红段超差 + 虚线阈值基线），复用 `storage.range_cell_values` |
| DB / AV1 状态 | 顶部状态栏实时显示数据库健康、AV1 压缩进度（HTTP 轮询 5s） |
| WS 连接状态 | 顶部指示灯：绿=已连接，红=重连中（指数退避 500ms→30s），灰=独立进程降级 |
| 实时日志 | 底部暗底滚动尾日志，最多 200 条 |
| 相机 CRUD | Web 端添加/编辑/删除相机 → 乐观锁（`CameraConfig.updated_at`） → `save_config` → `MonitorApp.reload_config` 热重载 → Worker diff stop/start |
| 远程控制 | 连接全部 / 断开全部 / 单相机连接断开（仅本机 `require_local` 可调用） |
| 相机级 xlsx 导出 | 复用后台任务队列，与 Tkinter 面板导出同源 |

### 实时数据通路

```
Worker (asyncio) → Monitor.push() 扇出多订阅者 queue (maxsize=1000)
                    ├→ Tkinter 主界面 poll
                    └→ Web 订阅者 poll → LiveCache 更新 → WebSocket 帧推所有浏览器
浏览器 WS 握手 → 立即推 LiveCache 快照（history 消息）→ 上线即有数据可看
```

### 安全矩阵

- **GET /api/monitor/* 全部开放**（数据库状态、相机配置、历史采样、实时 WS）
- **POST/PUT/PATCH/DELETE** 需 Origin（本机/局域网 IP）或空 Origin + `X-CTM-Token`
- **连接/断开/导出/删除数据** 叠加 `_require_local`（仅本机回环地址可调用）
- WebSocket `/ws` 握手复用 token 校验，空 Origin + 合法 Cookie 放行

### 独立进程降级

`web_only.py` 独立进程启动（如远端访问中心库）时没有 `Monitor` 引用，WS 端点自动降级为 `standalone` 模式：实时状态显示"📂 独立进程模式（仅历史数据）"，`POST /api/monitor/cameras/*` 返回 422。历史采样查询不受影响。

### 已新增文件

- `src/cameratinymes/webui/chart.umd.min.js` — Chart.js v4 UMD 压缩版（206KB，本地化不走 CDN）
- `src/cameratinymes/config.py` — `CameraConfig.updated_at` 字段 + `save_config` 每次写入自动 bump
- `src/cameratinymes/storage.py` — `StorageBase.range_cell_values` 基类方法（SQLite/MariaDB 双实现）
- `src/cameratinymes/worker.py` — `Monitor.events` 多订阅者（原 `queue.Queue` → `list[queue.Queue]`）+ `subscribe()/push()`
- `src/cameratinymes/ui.py` — `db_status_q` 多订阅者 + `MonitorApp.reload_config`（Worker diff stop/start）+ `connect_camera_by_name` 等公开桥接
- `src/cameratinymes/web.py` — 内联 RFC 6455 WebSocket `/ws` + LiveCache 200/键 + WS push loop + DB push loop + 13 个新端点（5 GET + 8 POST/PUT/PATCH/DELETE）

## 路线图（未完成规划）

### 报告功能现场验证

- [ ] 用真实一天数据与客户模板逐格对比（列宽、行高、合并、图片位置、字体）
- [ ] WPS（`DISPIMG` 单元格内嵌，已现场确认）、Excel（不支持 `DISPIMG`，图片位置显示 `#NAME?`）、PDF 三种打开方式的现场确认
- [ ] 单页容量评估：NG 图超过 12 张 / 工站较多时的多页排版方案

### MES 功能

- [ ] 产品追溯：以工件 / 批次号串联检测记录（依赖扫码数据，跨相机产品关联后置）
- [ ] 生产记录：班次 / 工单 / 产品型号维度的产量与直通率统计
- [ ] 质量看板：跨日期 / 产品 / 相机的良率、超差率、CPK 汇总
- [ ] NG 明细查询与 xlsx + CSV 导出

### 采集与事件

- [ ] `events` 表接入采集端写入（相机断线、NG 突发），并在工作台提示
- [ ] 规格版本管理：把 `ParameterSpecs.Version` 一类规格版本落到判定与报告快照
- [ ] 点云后续处理：CPCD → PLY / PCD 转换导出、点云可视化（当前仅原样入库 + 点数校验）

### 明确不做（与非目标保持一致）

自动缺陷分类 / 自动归因、PPT 输出、客户 MES 序列号对齐、报告多用户权限、工单通知（邮件 / IM）、工时与成本核算、附件上传、甘特图、与客户系统同步。

## License

[LICENSE](LICENSE) © JustSmartAuto
