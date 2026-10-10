# 地图处理、展示与记录的工程依据

本轮采用体素降采样、统计离群点过滤、局部平面拟合、深度后处理、多分辨率加载和后台分块写盘等成熟方法，目标是让真实地图便于检查、展示与长期记录。这些工程工作不构成新的 FAST-LIO2 算法；地图更平滑、显示帧率更高或保存更稳定，都不能直接证明定位精度提高。具体实现与测试结果以仓库代码、[工程实验结果](engineering_results.md)、[原始实验报告](report.md)和[复现说明](reproduction.md)为准。

以下 12 组资料均于 **2026-10-10** 实际联网访问并核对。链接指向当日读取的官方文档、官方仓库或 Linux man-pages 维护者仓库；表内“采用范围”是本轮工程取舍，不是来源对本项目效果的背书。

| 编号 | 官方来源与原文术语 | 本轮采用范围 |
| --- | --- | --- |
| R1 | [PCL — Downsampling a PointCloud using a VoxelGrid filter](https://pcl.readthedocs.io/projects/tutorials/en/latest/voxel_grid.html)：`3D voxel grid`、`centroid`。 | 每个体素输出点的质心，避免用体素中心替代真实点的分布。NumPy 实现保留固定体素原点、尺寸、聚合与输出顺序；这是本项目的可复现要求，不声称与 PCL 输出逐位一致。 |
| R2 | [PCL — StatisticalOutlierRemoval](https://pcl.readthedocs.io/projects/tutorials/en/latest/statistical_outlier.html)：`mean k-nearest neighbor distances`、`setMeanK`、`setStddevMulThresh`。 | 统计各点到邻居的平均距离，以全局均值和标准差阈值剔除稀疏离群点。可选开启，记录邻居数、阈值与删除点数；不能把被删除点一律称为动态物体或错误测量。 |
| R3 | [PCL — Smoothing and normal estimation based on polynomial reconstruction](https://pcl.readthedocs.io/projects/tutorials/en/latest/resampling.html)：`Moving Least Squares (MLS)`、`bivariate polynomial height function`、`reference plane`。 | 借鉴局部表面拟合思路，只做带邻域、平面性和位移限制的局部平面投影。没有实现该教程的完整多项式重建、重采样与法线输出，名称不写成“PCL MLS”。 |
| R4 | [CloudCompare 官方 qEDL shader](https://raw.githubusercontent.com/CloudCompare/CloudCompare/master/plugins/core/GL/qEDL/shaders/EDL/edl_shade.frag)：`EyeDome Lighting`、`Depth buffer of the scene`；源码读取 `s2_depth` 并比较屏幕邻域。 | 将点云颜色和深度渲染到离屏目标，再用邻近像素深度差调制颜色。处理透视深度与背景；EDL 开关仅改变图像，不改变地图坐标。 |
| R5 | [Potree 官方 README](https://raw.githubusercontent.com/potree/potree/develop/README.md) 的 `multi-res-octree`、`level of detail`；[可见性选择源码](https://raw.githubusercontent.com/potree/potree/develop/src/Potree_update_visibility.js) 的 `pointBudget`、`insideFrustum`、`numVisiblePoints`。 | 采用空间分块、视锥筛选、多分辨率数据和点数预算。本轮文件结构是独立分辨率的空间块，不宣称实现 Potree octree、其文件格式或完整加载算法。 |
| R6 | [ROS 2 Humble — Executors](https://docs.ros.org/en/humble/Concepts/Intermediate/About-Executors.html)：`callback groups`、`round-robin`、`priority inversion`。 | 在独立 ROS 2 节点/进程中记录地图；订阅回调只做必要检查和有界入队，后台线程解码与写盘。普通 Executor 在过载时不保证 FIFO 或硬实时，多线程本身也不保证延迟上限。 |
| R7 | [ROS 2 Humble — Quality of Service settings](https://docs.ros.org/en/humble/Concepts/Intermediate/About-Quality-of-Service-Settings.html)：`History`、`Keep last`、`Depth`、`Reliability`、`QoS compatibility`。 | 明确设置并记录 QoS，与发布端核对兼容性。DDS 历史深度和应用写盘队列分别配置；`reliable` 是交付策略，不能写成“使用 TCP”。 |
| R8 | [ROS 2 Humble — diagnostic_updater](https://docs.ros.org/en/ros2_packages/humble/api/diagnostic_updater/)：`DiagnosticStatusWrapper`、`Updater`、`DiagnosedPublisher`。 | 采用可观测运行状态的做法，报告接收、拒绝、持久化、队列占用与写盘错误。日志/JSON 是本轮可核对记录；是否接入 `/diagnostics` 以实际实现为准，不能仅引用该包就宣称已经集成。 |
| R9 | [Python 官方 os 文档](https://docs.python.org/3/library/os.html)：`os.replace`、`os.fsync`；[Linux man-pages — fsync(2)](https://raw.githubusercontent.com/mkerrisk/man-pages/master/man2/fsync.2) 对目录同步的说明。 | 块文件与索引分别先写同目录临时文件，检查写入后再原子替换；持久化还需文件和目录同步。原子替换保证名称切换，不等于多个文件组成一个事务。 |
| R10 | [ROS REP-105 — Coordinate Frames for Mobile Platforms](https://www.ros.org/reps/rep-0105.html)：`odom` 连续但可漂移，`map` 可因全局定位更新发生跳变。 | 保存实际 `frame_id` 和时间戳。未来回环/位姿图优化按 `map → odom → base_link` 分工，保持局部里程计连续；当前重力对齐与展示不能称为全局漂移校正。 |
| R11 | [M3DGR 官方 README](https://raw.githubusercontent.com/sjtuyinjie/M3DGR/main/README.md)、[项目主页](https://sjtuyinjie.github.io/M3DGR-website/)、[仓库 LICENSE](https://raw.githubusercontent.com/sjtuyinjie/M3DGR/main/LICENSE)。 | 保留数据集出处和实验来源。原始 bag 从官方渠道取得；本仓库展示本次实验的轻量派生结果，不把整个数据集附带发布。许可核查范围见下文。 |
| R12 | [Three.js 0.160.1 发布包 LICENSE](https://cdn.jsdelivr.net/npm/three@0.160.1/LICENSE)：`The MIT License`、`copyright notice`、`permission notice`。 | 实际 vendoring 固定为 0.160.1（r160），2026-10-10 访问返回 HTTP 200，已核对与随附 LICENSE 内容相同。运行时依赖全部在本仓库内。 |

地图处理应保存输入 SHA256、点数、坐标单位、重力旋转、参数和各步骤输出数量。体素原点与尺寸固定后，按体素索引和确定的聚合次序生成质心；相同输入和环境应可复现。它会合并近邻结构，不能用于宣称恢复了更细的几何。SOR 的距离统计受扫描距离和局部采样密度影响，远处墙面、细杆、植被和边缘可能被误删，因此保留未过滤版本并报告删除比例。[R1–R2]

局部平面投影只对支持点足够、邻域尺度受限且平面拟合条件满足的点生效，并限制每点最大位移。线状/退化邻域和跨表面邻域需要跳过或保守处理。平面性阈值仍可能漏掉两层近平行表面；过大的半径或位移会拉平棱角、合并双墙。它不能代替时间同步、外参标定或位姿优化，也不能证明“修好了”造成重影的根因。保留前后位移统计，并在同一区域、同一相机和尺度下比较。[R3]

本次源 PCD 的法线字段为零值占位，没有可用于表面光照的有效法线，也没有实测 RGB。高度着色、强度着色和单色均为显示方式：高度注明米和坐标基准，强度不冒充标定反射率，伪彩色不称为真实纹理。EDL 依赖屏幕深度关系，不依赖源法线；圆形点精灵、柔边或普通明暗渐变本身不构成 EDL。过强的深度暗化会产生黑边，结果也受点大小、视点、分辨率及深度精度影响。用同一几何和相机检查开关差异，并检查空白背景和近远裁剪。[R4]

浏览器先加载低分辨率覆盖，再按可见空间块与点数预算选择数据。同一空间区域的粗细版本是替换关系，不叠加计数；如果每级是独立覆盖，更不能沿用 Potree 增量层次节点的累加假设。限制加载并发、在途数据和缓存，释放不用的 GPU 缓冲；切换参数后防止旧请求覆盖新状态。显示点数预算不直接约束压缩文件、解压临时数组和 GPU/CPU 双份缓存，不能单凭点数上限宣称总内存有界。[R5]

记录器的应用队列、单条消息和聚合块都需要有界策略。队列满时明确拒绝哪一帧并累加计数；写入失败保留可观察错误，不能继续增加成功数量。记录进程独立后仍与 mapping 争用 CPU、内存带宽和磁盘，验收应同时观察两进程的 RSS、CPU、处理覆盖率及写盘耗时。关闭上游历史地图/轨迹缓存后，另核对输出行为和记录完整性，不能只观察新进程自身内存。[R6–R8]

应用收到的帧数只覆盖回调实际收到的数据。`PointCloud2` 头部没有序列号，时间戳间隔只能提示疑似缺口，不能精确区分上游未发布、DDS 丢失和队列拒绝。已知数据回放应把发布/处理帧数、回调接收数、拒绝数、尚未写入数和持久化数逐项核对。源时间、接收时间、写盘耗时分开记录；仿真时间与单调计时不能直接相减。QoS 兼容和“可靠”配置都不能替代这项端到端核查。[R6–R8]

每块先写临时文件，执行 `flush` 和 `fsync`，再在同文件系统内 `os.replace`；需要掉电持久性时，还应同步目录。索引在块完成后用同样次序更新。恢复需处理“块已完成、索引尚未更新”的窗口，核对格式、长度、计数和可用校验值后再接纳完整块；临时文件与损坏块不能计入成功。恢复测试至少覆盖写入失败和异常中断。文件系统与设备缓存会影响掉电行为，进程终止测试不能代替真实掉电验证。[R9]

后续定位研究先校验时间同步、传感器外参、RTK 杆臂和参考轨迹有效范围，再在固定协议下报告位置 APE/RPE、失败率及运行代价。位姿图优化需要回环候选、几何验证、约束及优化后的地图更新，而不是给历史轨迹做平滑。本轮保存真实坐标系和时间戳，为这类工作提供可追溯输入；未实现的回环、在线标定或精度提升不列为既有能力。[R10]

来源与许可核查：M3DGR 仓库的 `LICENSE` 为 MIT，版权行为 `Copyright (c) 2025 Yin Jie`，正文适用对象写作 “software and associated documentation files”。本次访问的 README、官网和该许可文件没有看到针对下载 bag 的独立许可或明确的 non-commercial 条款；这项观察不等于已核实原始数据及全部派生数据的商业再分发授权。保留官方获取链接和实验派生说明，不擅自给数据补写 MIT 标签。Three.js 随附资源应保留完整 MIT 文本；PCL、CloudCompare 和 Potree 在本文作为方法参考，实际复制第三方源码时仍需保留该源码对应许可。[R11–R12]

核查时 CloudCompare wiki 返回 HTTP 403，因此 EDL 依据采用可读取的官方 shader；没有把访问受阻页面当作已核对内容。临时检索材料存放于被忽略的 `output/research/`，不随仓库提交。
