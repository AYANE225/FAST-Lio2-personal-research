# ROS 2 地图分块记录

`scripts/map_recorder.py` 订阅 `/cloud_registered`，由独立后台线程解析并写入分块点云。它记录 FAST-LIO2 已发布的全局坐标点，不修改定位、去畸变或地图优化算法。默认要求消息 `frame_id` 为 `camera_init`；不执行 TF 变换，也不把该坐标系自动视为重力对齐。

## 运行与结束

在研究仓库根目录运行。先激活实际 ROS Python 环境；本项目的 FAST_LIO2 运行环境由其 `scripts/env.sh` 提供，不要求系统安装 `/opt/ros`。回放与记录器必须使用同一 ROS domain；与原实验并行时应选择另一未占用 domain。

```bash
source /path/to/FAST_LIO2/scripts/env.sh
export ROS_DOMAIN_ID=208
python scripts/map_recorder.py record \
  --output /tmp/outdoor01-map-recording \
  --topic /cloud_registered \
  --expected-frame-id camera_init \
  --queue-capacity 32 \
  --chunk-points 250000 \
  --max-frame-points 250000 \
  --max-frame-bytes 16777216 \
  --overflow fail \
  --qos-reliability reliable \
  --qos-depth 10 \
  --status-interval 1
```

输出目录必须不存在或为空，已有记录不会被覆盖。提前启动记录器并确认订阅匹配，再开始回放；回放结束后，给**本轮记录器进程**发送 SIGINT 或 SIGTERM。记录器停止接收，排空已接收队列，提交最后一块并输出最终 JSON。退出码 `0` 表示正常排空且没有应用层丢帧或错误；退出码 `2` 表示丢帧、输入格式错误、写入错误或不完整恢复。SIGKILL 无法排空内存。

关闭上游累计保存与展示缓存时，应在独立回放配置中关闭 PCD 累积保存、历史地图发布和路径发布，并启用完整已配准扫描发布：`publish.scan_publish_en=true`、`publish.dense_publish_en=true`、`publish.scan_bodyframe_pub_en=false`。原始 PCD 汇总的是预处理后的全部扫描点；如果上游仅发布降采样点，记录器无法补回没有收到的点。实际帧数、点数及资源结果见对应回放报告。

## 有界缓冲与过载

ROS 回调只检查时间戳、坐标系、点数和数据字节上限，并把消息引用放入有界队列。NumPy 转换、校验和、JSON、文件写入及 `fsync` 均由后台线程完成，回调不会等待磁盘或队列空位。

每帧必须同时满足 `max_frame_points`、`max_frame_bytes`，且 `max_frame_points <= chunk_points`。帧不跨块拆分，整帧保存在一个块内；达到点数限制前会先提交当前块。`chunk_frames` 默认 256，也约束零点帧的元数据数量。`flush_interval` 默认 2 秒；遇到磁盘阻塞时，刷新和状态更新会相应延后。

记录器持有的数据量受队列容量、单帧上限、一个分块缓冲及单帧转换临时数组约束，不随录制总时长增长。保守理解为队列最多保留 `queue_capacity × max_frame_bytes` 的消息数据，另有当前消息、`16 × chunk_points` 字节的 XYZI 缓冲、转换数组、固定大小哈希缓冲与有界帧元数据。Python/ROS 对象、解释器、DDS 历史和操作系统页缓存另计，不能把这个公式当作进程 RSS 上限；消息在回调检查之前已经被 DDS 接收和反序列化。

DDS 使用独立的 `KEEP_LAST` 历史，默认深度 10，可靠性默认 `reliable`。该深度与写入队列上限分别生效。应用层计数从回调接到消息开始，不能证明发布前或 DDS 中没有丢失；应与上游实际发布数量、时间戳或回放日志核对。`best-effort` 适用于明确接受 DDS 丢包的部署，但该损失不包含在本工具的 `dropped_frames` 中。

- `--overflow fail`：队列满时把当前帧计为失败，停止继续接收并排空已经入队的帧，返回非零。
- `--overflow drop-newest`：只丢弃当前新帧，增加丢帧/丢点计数，继续录制。最终 `completed=false`，返回非零。
- 无效格式、超限帧或错误坐标系：计为失败并停止。解码或磁盘错误会使尚未提交的缓冲和队列帧计为失败，已提交块保留。

`status.json` 由后台线程定期原子替换，包含 `queue_size`、`queue_high_water`、累计写入时间、错误与计数。运行期状态不执行 `fsync`；权威数据块、索引和最终清单仍按设置同步。磁盘卡住时该文件可能变旧；它不是独立的磁盘看门狗。`write_seconds` 只累计成功块的持久化与索引提交耗时，不代表端到端延迟，也不包含 ROS 传输或点云解析时间。

## 文件格式与帧重建

```text
recording/
  settings.json
  index.json
  status.json
  manifest.json                 # 结束时写入，完成标志以此文件为准
  .recorder.lock
  blocks/
    block-000000/
      points.npy
      frames.json
      block.json
    block-000001/
      ...
```

`schema_version=1`。`points.npy` 是 NumPy NPY v1.0 文件，形状 `(N, 4)`，C 顺序、小端 float32；四列依次是 `[x, y, z, intensity]`，XYZ 单位为米。强度保留发布值，不重新标定；工具不假设其物理单位。没有隐含点头，也不保存 PointCloud2 的法向、时间偏移等额外字段。

解析器遵守 PointCloud2 字段偏移、标量数据类型、字节序、`point_step` 和 `row_step`，支持点内和行末填充。XYZ 转为 float32 后的非有限点默认丢弃并计数；`--nonfinite error` 可改为拒绝该帧。缺失 intensity 默认填 0 并记录 `missing_intensity=true`；`--missing-intensity error` 可改为拒绝。保留点中的非有限 intensity 填 0，并单独计数。因 float64 到 float32 溢出而无效的 XYZ 也按同一策略处理。

每块 `frames.json` 的 `frames` 数组按接收顺序存放：

| 字段 | 含义 |
| --- | --- |
| `sequence` | 从 0 开始的接收序号，丢帧时可有缺口 |
| `stamp_sec`, `stamp_nanosec` | 原消息时间戳，整数存储，不转浮点、不重排 |
| `frame_id` | 原消息坐标系标识 |
| `input_points` | 原消息 `width × height` |
| `point_offset`, `point_count` | 在当前块中对应的连续行范围 |
| `nonfinite_points` | 本帧因 XYZ 非有限而省略的点数 |
| `missing_intensity` | 原消息是否缺少 intensity |
| `nonfinite_intensity_points` | 保留点中被替换为 0 的非有限 intensity 数 |
| `nonmonotonic` | 时间戳是否不大于上一接收帧 |

原时间戳可能重复或倒退；它们不会导致重新排序或插值。状态中的 `nonmonotonic_frames` 统计不严格递增的时间戳，`duplicate_stamp_frames` 单独统计相等的情况。

```python
import json
from pathlib import Path
import numpy as np

recording = Path("/tmp/outdoor01-map-recording")
index = json.loads((recording / "index.json").read_text())
for block_id in range(index["committed_blocks"]):
    block = recording / "blocks" / f"block-{block_id:06d}"
    points = np.load(block / "points.npy", mmap_mode="r", allow_pickle=False)
    frames = json.loads((block / "frames.json").read_text())["frames"]
    for frame in frames:
        start = frame["point_offset"]
        xyzi = points[start:start + frame["point_count"]]
        # 按需处理 xyzi，避免重新把全部历史点拼接到内存。
```

## 状态计数与完成语义

`manifest.json` 包含最终状态、设置哈希和最终索引哈希。`completed=true` 需要正常排空、无错误、无失败帧且无丢帧。允许策略内的非有限 XYZ 过滤，过滤数量仍须核对；空记录也可正常结束，因此完整回放还需要检查实际帧数。

| 字段 | 统计范围 |
| --- | --- |
| `received_frames`, `received_points` | 回调接受检查的帧及原始点数 |
| `enqueued_frames`, `enqueued_points` | 成功进入写入队列的帧及原始点数 |
| `written_frames`, `written_input_points` | 已提交索引中帧数及其原始点数 |
| `written_points` | 实际持久化的有效 XYZ 点数 |
| `nonfinite_points` | 已提交帧中省略的非有限 XYZ 点数 |
| `dropped_frames`, `dropped_points` | `drop-newest` 拒收的帧及原始点数 |
| `failed_frames`, `failed_points` | 失败输入与无法提交的帧及原始点数 |
| `pending_frames`, `pending_points` | 尚未提交或判定失败的帧及原始点数 |
| `committed_blocks` | 索引已确认提交的块数 |

正常状态下，`received_frames = written_frames + dropped_frames + failed_frames + pending_frames`，`received_points = written_points + nonfinite_points + dropped_points + failed_points + pending_points`。`enqueued_*` 是过程计数，与最终分类重叠，不能再次加进总数。

## 原子提交与恢复

每块先写入 `blocks/.block-NNNNNN.tmp/`。点文件和帧元数据完成并同步后，写入包含二者 SHA256、块汇总、设置哈希和前一块哈希的 `block.json`。整个目录原子重命名为正式块，再原子替换顶层 `index.json`。索引只保存累计计数和末块哈希，大小不随录制时长增长；不会反复重写历史点数据或全部帧索引。正式块的哈希链及帧元数据共同支持顺序重建。

默认对数据块、索引及最终清单的文件和目录执行 `fsync`；运行期监控状态仅原子替换。`--no-fsync` 会降低断电时的持久性保证。这里假设支持原子重命名及目录同步的本地 POSIX 文件系统，不能把测试结论直接推广到所有网络盘。

```bash
python scripts/map_recorder.py recover /tmp/outdoor01-map-recording
```

恢复不需要 ROS。它取得进程锁后逐块核对 NPY 格式、精确文件长度、点数、帧偏移、序号、坐标系、SHA256 与索引关系：

- 正常结束且所有校验通过：保持 `completed=true`，返回 `0`。
- 块已完整重命名但索引尚未提交：验证后补入索引，报告 `adopted_blocks`。
- 临时块：移动到 `incomplete/` 保留现场，报告 `quarantined_temporary_blocks`；不把半块计为成功。
- 正式块损坏、缺块、设置哈希不符或已完成清单与索引哈希不符：拒绝恢复，不覆盖原索引，返回 `2`。
- 索引重命名成功但其目录同步报错，可能留下与新索引不一致的**未完成**清单。恢复重新核对所有块，明确给出 `recovery_warnings`、`integrity_ok=false`，写出不完整清单，返回 `2`。

中断后即使所有留存块都有效，也不能证明排空或输入已经结束。恢复会保持 `completed=false`、`received_counts_verified=false`，将无法恢复的接收、入队、丢弃、失败和队列峰值等计数置为 `null`；已验证块的写入计数仍是准确值。恢复不会继续向原目录追加录制。

最终完成标志以最后写入的 `manifest.json` 为准，不能只看 `status.json`。两个顶层 JSON 的替换不是一个文件系统事务：状态可能暂时领先清单；最终写入出错时，程序会保持内存状态未完成，尝试删除不确定的成功清单并写回失败状态。如果文件系统持续拒绝写入或删除，旧状态仍可能残留，必须保留进程非零退出和 stderr，并在存储恢复后运行校验。校验和用于检测内容不一致，不提供身份认证，也不能证明一次报错的 `fsync` 已经持久化到介质。

## 验证

```bash
python -m pytest -q tests/test_map_writer.py tests/test_map_recorder.py
```

如果现有 ROS 环境的 `launch_testing` 插件与其 pytest 版本不兼容，可在这组不依赖 ROS 插件的核心测试中关闭插件自动加载：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q tests/test_map_writer.py tests/test_map_recorder.py
```

测试覆盖确定性队列饱和、磁盘错误、正常排空、真实子进程突然退出、临时块隔离、索引提交前后失败、最终元数据失败、元数据/数据哈希、帧偏移校验，以及 PointCloud2 大小端与填充布局。ROS 回放的实际发布数、记录数、RSS 和 CPU 由独立回放验证，不由单元测试代替。
