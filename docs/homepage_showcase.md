# 首页展示与实验数据

[回到首页](../README.md) · [交互地图](https://AYANE225.github.io/FAST-Lio2-personal-research/viewer/) · [校验清单](../data/showcase_checksums.json)

## 建图回放

[GIF](../figures/outdoor01_mapping.gif) · [MP4](../figures/outdoor01_mapping.mp4) · [静态图](../figures/outdoor01_mapping_poster.png)

Outdoor01 的完整分块录制包含 **4,109 帧、8,909,069 个 XYZI 点、410.801509857 秒**。显示先将本次运行的初始估计重力旋转到 −Z，再保留每个 0.10 m 体素的首次观测点、选取局部高度 −5～30 m，最后执行**全程离线 SOR**（24 个近邻，均值 + 2 倍样本标准差）。最终显示 **1,829,853 点**。

点的坐标与强度不求均值、不平滑；保留下来的点及轨迹按原采集时间逐步显现。SOR 的邻域使用整段录制，因此这是离线展示处理。固定相机也根据全程范围设置；不能将此动画理解为在线滤波效果或在线吞吐。EDL 只改善深度显示。14 秒动画包含 12 秒过程与 2 秒末帧停留。

体素以零点为原点，对旋转后保存为 float32 的 XYZ 使用 `floor(XYZ / 0.10)`。时间线保留整数纳秒时间戳与每帧累计点数。首页在开启“减少动态效果”时显示静态海报。

[准备记录](../data/outdoor01/mapping/preparation.json) · [时间线](../data/outdoor01/mapping/timeline.json) · [原始点索引](../data/outdoor01/mapping/selected_indices.npz) · [桌面渲染记录](../data/outdoor01/mapping/rendering_desktop.json) · [手机渲染记录](../data/outdoor01/mapping/rendering_mobile.json)

## 位置误差

[高清图](../figures/outdoor01_position_error.png) · [原始位置比较](../data/outdoor01/recording/position_comparison.npz) · [指标](../data/outdoor01/recording/metrics.json)

使用分块 0.5 m 配置的 **4,107 个匹配位置**，末尾 2 帧没有参考位置。彩色路线画在参考坐标系中，颜色与曲线均为**三维位置残差范数**；不与另一坐标系的点云混叠。曲线未平滑。

| RMSE | p95 | 最大值 |
| --- | --- | --- |
| 0.224517 m | 0.331285 m | 0.387621 m |

全程一次 SE(3) 对齐，尺度固定为 1，不拟合时间偏移。RTK 天线杆臂未补偿，参考姿态为占位值，因此只比较位置。每配置一轮、单一序列，不支持统计显著性或跨场景精度提升结论。

## 中断恢复

[高清图](../figures/outdoor01_recovery.png) · [实测证据](../data/outdoor01/recovery/evidence.json) · [实验脚本](../scripts/benchmark_recording_recovery.py)

从原始录制取前 **60 帧 / 112,497 点**，在独立目录使用 10 帧分块并开启 fsync。每个场景分别运行，两个中断进程实测退出码均为 −9 / SIGKILL；原始录制只读。

| 注入位置 | 实测结果 |
| --- | --- |
| 正常结束 | 6 块、60 帧、112,497 点；完成状态及接收总数已验证 |
| 第 3 块完整落盘，更新索引前 SIGKILL | 索引 20 → 30 帧，37,017 → 56,352 点；校验并补入 1 块 |
| 第 3 块 NPY 只写入一半时 SIGKILL | 保留 20 帧 / 37,017 点；隔离 1 个临时块，保留原字节 |
| 已提交块修改 1 个 payload 字节 | SHA256 校验拒绝恢复，索引与损坏文件均未覆盖 |

两种中断恢复后的 `completed` 与 `received_counts_verified` 均为 `false`，接收、排队、丢弃等未知计数保持 `null`。没有恢复内存队列，也没有继续向旧录制追加。**SIGKILL 是进程级中断测试，尚未进行整机掉电验证。**

公开证据保留源文件与块哈希、故障边界、前后状态、结果及原始报告哈希；移除了本机绝对路径、PID 和原始子进程命令。原始子录制与完整日志保存在本地。实验实现哈希对应实际运行时的版本，公开导出脚本哈希另行记录。

## 性能取舍

[高清图](../figures/outdoor01_performance.png) · [完整工程结果](engineering_results.md) · [C++ 体素计时](../data/career/voxel_benchmark.json)

沿用已发布的三配置回放及离线体素计时，本次只统一绘图样式。分块 0.5 m 的同时采样峰值 RSS 降低 59.8%，核心 p95 从 6.60 ms 增至 11.45 ms；图中保留这一代价。RSS 约 1 Hz，原 TCP 未覆盖退出保存，分块配置覆盖受控退出并合计 mapping + recorder。核心计时不含输入预处理、发布和记录写盘。

离线 C++17 体素处理的 NumPy/C++ 耗时中位数比为 8.00 / 4.38 / 2.22，各体素档每实现计时三次；9 组配对的计数与 float32 质心一致。不能据此推断在线 SLAM 提速。图中的 0.5 / 0.3 m 是扫描体素；动画中的 0.10 m 是显示采样体素。

[全部图表来源与数值](../data/showcase_figure_manifest.json)

<details>
<summary>从公开数据重绘图表与动画</summary>

Python 3.11，安装仓库 `requirements.txt`。动画环境沿用 [旋转动画说明](map_rotation.md)：Node 24、Playwright 1.58.2、Chromium、FFmpeg、Noto Sans CJK SC。所有输出使用新目录。

```bash
python scripts/plot_homepage_showcase.py --output-dir output/homepage-figures
```

公开的压缩点文件保存完整的 1,829,853 个显示点，解压后即可渲染，无需重新下载 Outdoor01。渲染器会核对解压数据与时间线的 SHA256：

```bash
python - <<'PY'
import gzip
from pathlib import Path
import shutil
source = Path('data/outdoor01/mapping')
target = Path('output/homepage-replay-data')
target.mkdir(parents=True)
for name in ('preparation.json', 'timeline.json'):
    shutil.copyfile(source / name, target / name)
with gzip.open(source / 'points.bin.gz', 'rb') as incoming:
    with (target / 'points.bin').open('wb') as outgoing:
        shutil.copyfileobj(incoming, outgoing)
PY

PLAYWRIGHT_MODULE="$PWD/output/rotation-tools/node_modules/playwright/index.mjs" \
node scripts/render_mapping_progress.mjs \
  --data-dir output/homepage-replay-data --output-dir output/homepage-replay

PLAYWRIGHT_MODULE="$PWD/output/rotation-tools/node_modules/playwright/index.mjs" \
node scripts/render_mapping_progress.mjs \
  --data-dir output/homepage-replay-data --output-dir output/homepage-replay-mobile \
  --width 720 --height 940
```

两个渲染目录输出同名文件；首页发布时为手机资产增加 `_mobile` 后缀。浏览器与字体版本会影响像素，不要求跨平台逐字节重现。

</details>

<details>
<summary>从完整录制重新准备数据、运行故障实验</summary>

需要最初的完整 `recording_v2` 运行目录，其中包含逐帧 `recording/`、`trajectory_tum.txt` 和算法日志；公开的显示点不是原始录制。准备脚本会核对 [原运行源文件哈希](../data/outdoor01/recording/export_manifest.json)，不会修改或恢复原录制。

```bash
python scripts/prepare_mapping_progress.py \
  --run-dir /path/to/recording_v2 \
  --output-dir output/new-mapping-data \
  --height-range-m -5 30 --sor --workers 4

python scripts/benchmark_recording_recovery.py \
  --source-recording /path/to/recording_v2/recording \
  --output-dir output/new-recovery-experiment --frames 60

python scripts/benchmark_recording_recovery.py \
  --export-public output/new-recovery-experiment/evidence.json \
  --output-dir output/new-recovery-public
```

先核验自己的源录制，不要修改原文件来匹配已发布哈希。新的实验保留独立证据，不覆盖这里的已发布测量。

</details>
