# 数据与复现

## 仅使用公开结果：校验与重绘

Python 3.11，Ubuntu 22.04 或更新版本。以下操作无需 ROS、原始 bag 或完整 PCD。

```bash
sudo apt-get install fonts-noto-cjk
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/verify_artifacts.py
.venv/bin/python scripts/plot_evaluation.py --output-dir output/baseline-figures
.venv/bin/python scripts/plot_engineering.py --output-dir output/engineering-figures
```

第一份绘图脚本重绘原 UDP / TCP 基线的五张 PNG / SVG；第二份重绘内存、耗时、位置误差对照和固定视角点云处理图。原始五张图和 [原实验报告](report.md) 保留。

[data/checksums.json](../data/checksums.json) 的原 27 项不变；[engineering_checksums.json](../data/engineering_checksums.json) 单独记录新增数据、配置、网页点块及展示图的大小与 SHA256。校验清单不包含自身。绘图另输出实际读取文件的 SHA256，便于核对图与数据的对应关系。新运行的耗时、处理时间与渲染环境可能变化，不要求 PNG 或运行时间逐位相同。

| 目录 | 内容 |
|---|---|
| `data/outdoor01/udp/`、`tcp/` | 原传输对照、输入时间台账、轨迹、计时、资源、位置参考数组、地图二维网格 |
| `data/outdoor01/recording/`、`surf03/` | 新完整回放结果、逐帧记录台账、记录清单/索引、配置来源哈希 |
| `data/map_processing/local_comparison.npz` | 固定局部区域的真实 XYZI：原始、体素、SOR、局部投影 |
| `docs/viewer/data/` | 轻量派生三维点云、LOD 空间块、轨迹、处理参数与源地图哈希 |

新结果的 `recorded_frames.npz` 全部使用 int64，包括 `stamp_ns`、`input_points`、`point_count`、`block_id`、`sequence`、`point_offset`。完整块未发布，公开台账可以复核帧数/点数/时间戳，不能据此在下载仓库后重验未发布块的内容哈希。`export_manifest.json` 记录本地完整运行文件的来源哈希；公开 `run_summary.json` 已省去机器路径、命令与 PID，因此它的文件哈希与未裁剪来源不同。

## 本地三维页面与浏览器验收

```bash
python3 -m http.server 8767 --bind 127.0.0.1 --directory docs
```

访问 <http://127.0.0.1:8767/viewer/>。必须通过 HTTP 服务访问，不能直接双击 HTML。Three.js 0.160.1、OrbitControls 和许可已随仓库提供，运行时不请求 CDN；现代浏览器需要 WebGL 和 `DecompressionStream` 支持。

在另一个终端运行 Node 24 / Playwright 1.58.2 验收：

```bash
node --test docs/viewer/tests/*.test.mjs
npm install --prefix output/viewer-test --no-save playwright@1.58.2
output/viewer-test/node_modules/.bin/playwright install chromium
PLAYWRIGHT_MODULE="$PWD/output/viewer-test/node_modules/playwright/index.mjs" \
VIEWER_URL=http://127.0.0.1:8767/viewer/ \
VIEWER_SCREENSHOT_DIR=output/browser \
node docs/viewer/tests/browser-check.mjs
```

浏览器脚本检查版本/预算切换、色彩、EDL、相机、PNG 下载、手机面板与 WebGL 上下文恢复。无头 Chromium 使用显式 ANGLE / SwiftShader；这项功能验收不是目标机器 GPU 帧率测试。公开截图与验收结果见 [viewer_validation.json](../data/viewer_validation.json)。

## 从完整 PCD 重新处理地图

需要原 TCP 基线的 `map.pcd`，源 SHA256 为 `a1647137abfbc2e47f209a88c42900b1d856013373dbb5550da5cfab410ae557`。使用新的输出目录，避免覆盖发布数据：

```bash
.venv/bin/python scripts/process_map.py \
  --map-pcd /path/to/map.pcd \
  --output-dir output/map-rebuild \
  --viewer-data output/viewer-data-rebuild \
  --workers 2
```

仅支持 `DATA binary` PCD；检查字段、长度及非有限坐标。默认复用已保存的初始重力旋转与未平滑估计轨迹，导出固定网格体素、SOR 和局部平面投影。默认局部区域为 `X [10,30), Y [-12,8), Z [-2,10) m`，所有局部点参与对照绘制。完整参数与点数在 `processing_report.json`。

原二维地图网格可以独立重算：

```bash
.venv/bin/python scripts/plot_evaluation.py \
  --map-pcd /path/to/map.pcd --output-dir output/from-pcd
```

该网格使用全部 8,909,069 点、0.1 m 网格边长与所有高度，颜色为对数点数；它不是定位精度图。主投影显示轨迹外扩 35 m 区域，完整地图附图显示全部范围。

## 位置参考协议与独立核对

三组各使用全部 4,107 个匹配位置。参考 XYZ 在估计时间戳上线性插值、不外推，最近 GT 时间差不超过 0.11 s、插值区间不超过 0.25 s；最后两帧超出参考时间范围。全程仅拟合一次 SE(3)，尺度固定为 1，不拟合时间偏移，不补偿 RTK 天线杆臂；GT 姿态为占位值。

新评估严格匹配原基线已发布参考数组的所有时间戳，不允许删除中间坏样本。另从官方 `GTOutdoor01.txt` 直接核对：按时间排序并对相同时间保留第一条，用 `numpy.interp` 对 XYZ 插值，两组与保存参考的最大坐标差均为 0.0 m。源 GT 哈希、样本数和区间检查见 [reference_validation.json](../data/outdoor01/reference_validation.json)。

## 从已转换的 ROS 2 bag 完整回放

原始数据从 [M3DGR 官方仓库](https://github.com/sjtuyinjie/M3DGR) 获取 Outdoor01；见 [下载与校验记录](../data/outdoor01/download_verification.json)。原 ROS1 bag 为 6,553,326,739 bytes，本地 SHA256 与当时 OneDrive 元数据匹配，但与数据仓库给出的校验值不同；保留这一差异。本仓库不重新分发原 bag、完整 PCD 或完整记录块。

完整回放前需准备 ROS 2 Humble、Cyclone DDS 0.10.5、livox_ros_driver2 消息包，以及只包含 `/livox/mid360/lidar` 和 `/livox/mid360/imu` 的已转换 ROS 2 bag。转换必须保留 CustomMsg 点时间字段；原生反序列化抽查见 [conversion_validation.json](../data/outdoor01/tcp/conversion_validation.json)。此仓库的回放入口从已转换 bag 开始，不自动安装 ROS 或转换原数据。

本轮使用项目已有的 Python 3.11 / ROS 环境。以下 `env.sh` 是本地环境入口占位，需替换为实际路径；原项目入口同时提供 Cyclone DDS 和 Livox 消息依赖。系统 ROS 的默认 Python 版本可能不同，不应把 `.venv` 中的解释器直接当作可导入 `rclpy` 的解释器。

从研究仓库根目录准备独立源码与构建，不能复用正在运行节点的编译目录，因为 FAST_LIO 的 `Log` 目录编译进二进制：

```bash
source /path/to/FAST_LIO2/scripts/env.sh
python -m pip install -r requirements-runtime.txt
RESEARCH_ROOT="$PWD"
REPLAY_WS="$RESEARCH_ROOT/output/replay_workspace"
REPLAY_PYTHON="$(command -v python)"
mkdir -p "$REPLAY_WS/src"
git clone https://github.com/hku-mars/FAST_LIO.git "$REPLAY_WS/src/FAST_LIO"
git -C "$REPLAY_WS/src/FAST_LIO" checkout a4743b095409588842a5b30ddfa27e29d2f99164
git -C "$REPLAY_WS/src/FAST_LIO" submodule update --init --recursive
git -C "$REPLAY_WS/src/FAST_LIO" apply "$RESEARCH_ROOT/patches/fast-lio2-local.patch"
mkdir -p "$REPLAY_WS/src/FAST_LIO/Log" "$REPLAY_WS/src/FAST_LIO/PCD"
export OMP_NUM_THREADS=3
colcon --log-base "$REPLAY_WS/log" build \
  --base-paths "$REPLAY_WS/src" --packages-select fast_lio \
  --build-base "$REPLAY_WS/build" --install-base "$REPLAY_WS/install" \
  --executor sequential --symlink-install \
  --cmake-args -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF \
  -DCMAKE_C_COMPILER=/usr/bin/gcc -DCMAKE_CXX_COMPILER=/usr/bin/g++ \
  -DPython3_EXECUTABLE="$REPLAY_PYTHON" -DPYTHON_EXECUTABLE="$REPLAY_PYTHON"
source "$REPLAY_WS/install/local_setup.bash"
```

应用的是 [已记录的补丁](../patches/fast-lio2-local.patch)，没有在本轮修改定位算法。对照 [source_version.json](../data/outdoor01/source_version.json) 与 [engineering_environment.json](../data/outdoor01/engineering_environment.json) 核对源码和编译条件。本轮 CMake `ProcessorCount` 识别 3，最终 `MP_PROC_NUM=1`、无 `MP_EN`；上游会按构建环境自动选择并行宏，因此在其他机器应实际检查 `build/fast_lio/CMakeFiles/fastlio_mapping.dir/flags.make`，不能只凭相同命令假设编译条件一致。

选择未占用 domain 和四个连续 TCP 端口，输出目录必须是新的。示例：

```bash
python scripts/replay_outdoor01.py \
  --workspace "$REPLAY_WS" --bag /path/to/outdoor01_ros2 \
  --config configs/mid360_recording.yaml \
  --output output/experiments/recording-repeat \
  --domain 190 --port-base 56020
python scripts/evaluate_engineering.py \
  --run-dir output/experiments/recording-repeat \
  --output-dir output/evaluation/recording-repeat
```

0.3 m 对照只将配置替换为 `configs/mid360_surf03.yaml`，使用另一输出目录及空闲端口。每轮约 7 分钟，独立构建的日志目录加锁，不要并发使用同一个构建。runner 自动生成 mapping / playback / observer / recorder 四份本机 TCP 配置，仅管理自己创建的进程组。回放结束等待完整输出、排空记录队列、受控退出，再核对输入覆盖和保存块。

阈值触发或中断会进入清理；清理采样不重复触发内存阈值，采样或单个进程停止失败记入 `cleanup_errors`，继续处理其余进程和输出摘要。磁盘无法写入时仍可能无法保存最终 JSON，应同时保留非零退出和日志。

## 自动化测试

普通 Python 环境可运行数值、记录器文件格式、故障恢复、回放核对测试；默认跳过真实 ROS smoke：

```bash
.venv/bin/pip install -r requirements-dev.txt
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
node --test docs/viewer/tests/*.test.mjs
.venv/bin/python scripts/verify_artifacts.py
```

在实际 ROS Python 环境中增加真实消息发布/记录测试：

```bash
source /path/to/FAST_LIO2/scripts/env.sh
RUN_ROS_TESTS=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
```

关闭自动插件加载是为了避免该 ROS 环境 `launch_testing` 与 pytest 的插件接口冲突，这些测试不依赖其插件。本地 59 项 Python（含 ROS）与 13 项 Node 测试的输出、源文件哈希见 [test_validation.json](../data/test_validation.json)。[GitHub Actions](../.github/workflows/verify.yml) 运行无 ROS 的 Python、Node 和发布数据校验；真实 ROS 与完整浏览器验收在已记录的本地环境执行。目标机器人长期运行、掉电和网络测试仍需实机验证。
