# 招聘能力展示实施计划

本文归档本轮实现范围与最终本地验证状态；线上发布结果以对应提交的 [GitHub Actions](https://github.com/AYANE225/FAST-Lio2-personal-research/actions) 为准。

> **For agentic workers:** 使用 `executing-plans` 实施主任务，按 `dispatching-parallel-agents` 分配互不改写的招聘资料与 C++ 组件任务；实现遵循 TDD。

**Goal:** 为移动机器人软件/SLAM 求职补齐可复现的 C++ 点云处理、性能对照和自动回归验收证据。

**Architecture:** 可选 C++17 共享库接入既有离线体素处理；独立 Python CLI 检查已导出的运行摘要；招聘资料和测量结果用于文档、图表与 CI。

**Tech Stack:** C++17、CMake/CTest、ctypes、NumPy、pytest、Matplotlib、GitHub Actions。

**Spec:** [设计](2026-10-11-career-design.md)

## Global Constraints

- 用户已授权自动实施和验证后发布，不重复请求确认。
- 工作区：`.worktrees/career-evidence`，分支 `feat/career-evidence`，起点 `8d32a06`。
- 不改原工程 `/media/4T/cst/PROJECT/FAST_LIO2`，不停止其原 mapping 进程，不删除先前大型实验。
- Python 3.11，C++17；体素原点为零、XYZ 单位米、输出 float32 XYZI + int64 占据点数。
- 使用传统、准确名称；离线体素性能不等于定位精度或在线速度。
- 不覆写 `data/checksums.json` 或已有原始实验，新增结果单独保存。

## Review Focus

- 非有限值、过大体素键、累加溢出和原生内存生命周期：明确报错且 sanitizer 无错误。
- 跨边界/负坐标/空点云和确定性排序：两个实现分箱和计数相同。
- 可比性条件不一致：返回 2，并停止相对指标判定。
- 摘要数值为 NaN/Inf、布尔值、缺字段或重复 JSON 键：返回 2，不能产生通过。
- 容差未通过和写入报告失败：CLI 不返回 0，不能被 CI 当作成功。

## Task 1：招聘来源

**Files:** `docs/job_requirements.md`、`data/recruitment_sources.json`。

**Interfaces:** 提供真实岗位 URL、短摘录、访问 UTC、响应 SHA256、学历/经验硬条件、对应技能；不修改代码或宣称新能力已完成。

- [x] 核验 5–8 个岗位，优先工业移动机器人和中国官方来源。
- [x] 写岗位与项目证据映射，区分样本、硬条件和待补项。
- [x] 检查链接和引用，不保存完整招聘页或联系人到公开仓库。

## Task 2：原生体素处理

**Files:** 根 `CMakeLists.txt`、`cpp/`、`scripts/native_voxel.py`、`tests/test_native_voxel.py`。

**Interfaces:** `voxel_centroids_native(points, voxel_size_m, library_path=None) -> (float32[N,4], int64[N])`，无库时默认查找 `output/native-build/libfastlio_voxel.so`；允许显式路径。编译产物名 `fastlio_voxel`。主任务只通过该函数接入。

- [x] 为负坐标、边界、随机点、空输入、非法输入、溢出和重复调用写测试，记录真实失败。
- [x] 实现 C++17 哈希累加、XYZ 排序、异常与内存释放；实现 ctypes 包装。
- [x] 跑 CTest、Python 对照和 ASan/UBSan，记录输出。

## Task 3：配置化回归检查

**Files:** `scripts/check_regression.py`、`tests/test_check_regression.py`、`configs/regression_limits.json`。

**Interfaces:** `evaluate_regression(baseline, candidate, limits) -> report`，run 为 `{'metrics': ..., 'run_summary': ...}`；CLI 接收 `--baseline-dir`、`--candidate-dir`、`--limits`、可选 `--output`。

- [x] 先写正常、速度退步、位置误差退步、计数错误、不可比、坏 JSON 和退出码测试，并观察失败。
- [x] 实现类型/数值/计数验证、协议可比性、逐项容差检查与输入哈希。
- [x] 用真实 recording / surf03 摘要运行，保留真实结果与后验示例容差说明。

## Task 4：真实性能与展示

**Files:** `scripts/benchmark_voxel.py`、`scripts/plot_career_evidence.py`、`scripts/process_map.py`、`tests/test_voxel_benchmark.py`、`docs/career_evidence.md`、`data/career/`、`figures/voxel_benchmark.*`。

- [x] 给既有处理 CLI 接入可选 C++ 实现，默认 NumPy；测试显式失败和选择。
- [x] 实现独立进程、交错顺序三轮计时与数值对照，先测试指标提取和坏结果拒绝。
- [x] 用完整真实地图测三档体素，保留每轮和环境证据，不预设加速结论。
- [x] 生成带单位、范围与重复说明的图表、求职讲解和复现入口。

## Task 5：集成与发布

**Files:** `README.md`、`.github/workflows/verify.yml`、新增结果清单和验证记录。

- [x] CI 配置加入原生库构建、适用 Python／前端测试、CTest、sanitizer 与发布校验；已执行本地等价检查。
- [x] 更新 README 及招聘映射中实际完成的证据，保留未实现项。
- [x] 运行全套测试、哈希与文档链接检查；独立审查与必要修正。

发布步骤：快进合并 main，SSH 推送已授权仓库，再确认该提交的 CI 与 Pages；线上状态不由本地测试替代。

## 实施记录

- 已核验六个官方岗位样本，保留学历、经验、实习资格和工作地点等原文限制。
- C++ 组件、回归检查器与复现实验均已实现。最终全图三档体素的 NumPy／C++ 中位耗时比为 8.00／4.38／2.22；九次配对的输出计数和 float32 质心一致。
- 独立审查发现的输入对齐、摘要一致性及编译器来源问题已修正；新增统计舍入反例也已修复并复核。性能验收阈值未放宽。
- 本地全套 199 项 Python 测试（含 ROS）、20 项前端测试、8 组 C++ 行为检查和 sanitizer 验证通过；命令、时间及哈希见 [验证记录](../../data/career/validation.json)。
- 真实 surf03 候选仍因 p95 超过示例 +20% 容差返回 1。原工程、旧实验和开发 worktree 均保留。
