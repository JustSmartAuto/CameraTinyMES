# 后台 AV1 压缩可行性研究

> **日期**：2026-11-10
> **动机**：实时分片（`SEGMENT_IDLE_SECONDS=5s`）被产线节拍卡死，96.6% 的分片只有 2-5 帧，GOP 帧间预测几乎失效，综合压缩比仅 2.3×（理想 5-10×）。

---

## 一、方案对比

| | 当前方案（实时分片） | 新方案（先存图+后台压缩） |
|---|---|---|
| 写入时机 | 相机回调 → 内存缓冲 → 满30帧/空闲5s → 立即编码 | 相机回调 → 直接写 `images` 表（JPEG） |
| 压缩时机 | 实时（阻塞采集线程） | 后台守护线程，批量扫描稳定窗口 |
| 分片大小 | 受节拍限制，平均 2.5 帧/片 | 固定 30 帧/片（攒够再打包） |
| 帧间编码 | 极少（5s 空闲就封片） | **充分发挥**（30帧长 GOP） |
| 丢数据风险 | 编码失败回退 JPEG；崩溃丢内存缓冲帧 | 先存图再压缩，**不丢数据** |
| 数据库抖动 | 编码耗时 ≈ 15ms/帧，高频相机可能延迟 | 写入仅 INSERT 一行，稳定；后台压缩低频串行 |
| 历史数据 | 只能压缩实时采集的新数据 | **可压缩 images 表所有历史数据** |
| 读写接口 | 合并查 images + image_streams | 不变（已经是合并查） |
| annotations 关联 | 无影响 | 需特殊处理（见 §4） |

**结论**：后台压缩方案在所有维度上优于实时分片，且不需要改动任何读接口。

---

## 二、方案设计

### 2.1 核心流程

```
相机拍照 → worker._download_images()
  │
  ├─ 直接 storage.insert_image(ts, camera, cell, kind, jpeg_data, ...)
  │  （不再走 ImageStreamWriter.add_frame）
  │
  └─ 完成 ✓
       │
       ▼
[后台守护线程 BackgroundImageCompressor] 每 COMPRESS_INTERVAL（默认 10 分钟）跑一次
       │
       ├─ 1. 扫描 images 表，按 (camera, cell, kind, job) 分组
       │
       ├─ 2. 组内按时间排序，取"稳定窗口"内的帧
       │     （最近一张距今 ≥ SETTLE_SECONDS，默认 10 分钟，确保不会再来新帧）
       │
       ├─ 3. 每组凑够 BATCH_FRAMES（默认 30）帧 → 调 encode_frames()
       │
       ├─ 4. 编码成功 → 写 image_streams + image_frames
       │
       ├─ 5. 同一事务内删除这批 images 行（WHERE id IN (...)）
       │     ← 跳过有 annotations 引用的行（见 §4）
       │
       └─ 6. 记录日志：压缩前 MB → 压缩后 MB，节省 %
```

### 2.2 关键参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `COMPRESS_INTERVAL` | 600s（10min） | 后台守护线程跑一次的间隔 |
| `SETTLE_SECONDS` | 600s（10min） | "稳定窗口"：最近一张图距现在多久以上才动 |
| `BATCH_FRAMES` | 30 | 每批打包帧数（GOP 长度） |
| `MIN_BATCH_FRAMES` | 5 | 少于此数的批次跳过（不值得编码） |

### 2.3 事务安全

**SQLite**：当前未启用 WAL，用线程锁串行写入。后台压缩需要在同一把锁内完成：
```sql
BEGIN TRANSACTION;
INSERT INTO image_streams ...
INSERT INTO image_frames ...
DELETE FROM images WHERE id IN (?, ?, ...);
COMMIT;
```

**MariaDB**：InnoDB 自动事务，同样在单次 `_execute_many` + `_execute` 内完成即可。

两种后端的 `StorageBase` 基类当前 `_execute` 已经自动 commit，需要新增一个 `_execute_batch` 支持手动控制事务。

### 2.4 线程安全

- 后台压缩线程独立，与 UI 线程通过 `storage._lock` 协调（现有锁已覆盖）
- 读取 images 时 worker 可能同时在写——`SELECT` + `DELETE WHERE id IN (...)` 模式在 SQLite 和 MariaDB 上都安全（不会删到 worker 刚写的新行，因为新行 id 更大且时间戳更近）

---

## 三、annotations 外键关联问题

### 3.1 现状

```sql
-- annotations 表
FOREIGN KEY (image_id) REFERENCES images (id) ON DELETE CASCADE
```

删除 images 行时，关联的 annotations 会被级联删除——这**绝对不能发生**。

### 3.2 解法

后台压缩时，**跳过所有有 annotations 引用的 images 行**：

```sql
SELECT i.id, i.camera, i.cell, ...
FROM images i
WHERE i.camera = ? AND i.cell = ? AND i.kind = ? AND i.job = ?
  AND i.ts < ?
  AND i.id NOT IN (SELECT image_id FROM annotations WHERE image_id IS NOT NULL)
ORDER BY i.ts
LIMIT ?
```

被跳过的有标注图片永久留在 `images` 表，不会被压缩。后台压缩线程每次都会重新扫描，所以**如果后续有人把标注删掉了，下一轮就能被压缩**。

### 3.3 长期方案（可选，当前不实现）

未来可以把 annotations 的 `image_id` 改成 **可空**，新增 `stream_id` + `frame_index` 关联 AV1 分片内的帧，然后把标注迁移到分片模式。当前先不做这个重构，避免影响现有标注数据。

---

## 四、对现有代码的改动范围

### 4.1 需要改的文件（4 个）

| 文件 | 改动 |
|---|---|
| `storage.py` | ① 新增 `_execute_batch()` 支持手动事务；② 新增 `query_images_for_compress()` 带排除 annotations 的查询；③ 新增 `delete_images_batch()` 批量删除（带事务） |
| `videocodec.py` | 新增 `BackgroundImageCompressor` 类（守护线程，扫描+编码+写入+删除） |
| `worker.py` | `_download_images()` 中去掉 `writer.add_frame()` 调用，直接写 `images` 表 |
| `app.py` / `ui.py` | 启动时初始化 `BackgroundImageCompressor`；关闭时优雅停止 |

### 4.2 不需要改的文件

| 文件 | 说明 |
|---|---|
| `load_images()` | 已经是合并查 `images` + `image_streams`，自动兼容两种来源 |
| `worker.py` 的 Monitor | `writer` 参数保留但不再用于实时分片；或可以去掉整个 `ImageStreamWriter` 实时分片类 |
| `export_xlsx()` | 通过 `load_images()` 间接获取数据，自动兼容 |
| `charts.py` | 只查 `samples` 表，不涉及图像 |
| 所有 UI 层 | 不需要知道压缩发生在后台还是实时 |

---

## 五、POC 验证计划

1. **模拟**：用 images 表里已经有的 41,709 帧 AV1 新采集数据（实际平均 2.5 帧/片），按 30 帧重新分批打包，计算压缩比理论提升
2. **实测**：改完代码后跑起来，观察 1-2 天，看后台压缩线程实际压缩了多少、压缩比如何
3. **回退**：如果效果不好或引入问题，注释掉后台压缩线程即可（worker 直接写 images 本身就是安全的）

---

## 六、风险与缓解

| 风险 | 概率 | 缓解 |
|---|---|---|
| 后台压缩与 worker 写入竞争 | 低 | 现有 `storage._lock` 已覆盖；SELECT+DELETE 按 id 精确删除不会误删新行 |
| 编码耗时（15ms/帧 × 30帧 ≈ 0.45s） | 中 | 后台线程独立，不阻塞 UI 和采集；可加 `time.sleep` 控制节奏 |
| 有标注图片永远不被压缩 | 中 | 每次扫描都动态查询 `annotations.image_id`，标注删除后自动纳入 |
| MariaDB 大批量 DELETE 锁表 | 低 | 每批 30 行删除，影响极小；如果量大（>1000 行/小时）可以分批提交 |
| 后台压缩线程崩溃 | 低 | 守护线程 + try/except 包裹循环，崩溃自动重建；下一轮继续扫 |

---

## 七、预期效果

| 指标 | 当前（实时分片） | 后台压缩（预期） |
|---|---|---|
| 平均帧/片 | 2.5 | 30（满 GOP） |
| 压缩比 | 2.3× | 5-10× |
| 分片膨胀 | 12.2× | 1.0× |
| images 表增长 | 不增长（实时写入 streams） | 正常增长，后台异步清理 |
| 数据丢失风险 | 编码失败回退 JPEG；崩溃丢内存缓冲 | 几乎为零（先存图再压缩） |

**OP40 系列（占 AV1 总量 98.9%）** 从 2-3 帧/片 → 30 帧/片，压缩比预期从 1.6-3.5× → 5-8×，节省空间再翻 2-3 倍。

---

## 八、实施顺序

1. **Step 1**：storage.py 新增事务批量写入方法 + 带 annotations 排除的 images 查询
2. **Step 2**：videocodec.py 实现 BackgroundImageCompressor 守护线程
3. **Step 3**：worker.py 切换为直接写 images 表
4. **Step 4**：app.py 集成后台线程的启动/关闭
5. **Step 5**：跑起来验证效果，写一份实测报告
