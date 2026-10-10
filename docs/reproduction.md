# 数据与复现

## 仓库内可直接复现的内容

仓库包含两轮回放的逐帧计时、资源采样、轨迹，以及输入时间戳、位置对比数组、完整地图的二维点数网格和测试配置。运行 README 中的绘图命令即可重新生成五张图，不会启动 ROS 回放。

[data/checksums.json](../data/checksums.json) 记录发布数据与配置的大小和 SHA256。脚本先校验这些输入，再核对帧数、初始化后缺帧、p95、位置残差和地图网格点数总和。[import_manifest.json](../data/import_manifest.json) 记录首次整理文件时的来源与哈希。

## 地图与误差协议

地图网格由完整 8,909,069 点构建，网格边长 0.1 m，所有高度的点均参与计数。仅将初始估计重力旋转到 −Z；未用参考轨迹拟合地图。主图显示轨迹外扩 35 m 的区域，完整地图附图显示全部范围。点数采用对数颜色刻度；地图是预滤波扫描累积结果，不是去重地图或精度图。

如果持有本次原始 map.pcd，可用以下命令重新计算网格：

    .venv/bin/python scripts/plot_evaluation.py --map-pcd /path/to/map.pcd --output-dir output/from-pcd

位置误差使用 4,107 个匹配位姿。参考 XYZ 在估计时间戳上线性插值，不外推；最近时间差不超过 0.11 s，插值区间不超过 0.25 s。全程拟合一次 SE(3)，尺度固定为 1，不拟合时间偏移。未补偿 RTK 天线杆臂，GT 姿态为占位值。完整范围说明见 [实验报告](report.md)。

## 原始数据与完整回放

原始数据请从 [M3DGR 官方仓库](https://github.com/sjtuyinjie/M3DGR) 获取 Outdoor01，或查看 [下载来源与校验记录](../data/outdoor01/download_verification.json)。原 bag 为 6,553,326,739 bytes，本地 SHA256 与当时 OneDrive 文件元数据匹配，但与数据仓库给出的校验值不同；这一差异在记录中保留。

本仓库不包含原始 bag 和约 285 MB 的完整 PCD。发布的网格是全图计数，足以重绘地图投影，不能还原原始三维点云。

完整回放需要 ROS 2 Humble、Cyclone DDS 0.10.5、livox_ros_driver2 消息包，以及 [记录的 FAST_LIO 提交](../data/outdoor01/source_version.json)。在该提交上应用 [已有补丁](../patches/fast-lio2-local.patch)，以 Release 构建，使用 [MID-360 配置](../configs/mid360.yaml)。补丁在本次两轮实验前已存在，用于 C++17 构建和启用地图保存。

ROS1 转 ROS2 时仅保留 /livox/mid360/lidar 和 /livox/mid360/imu，并保留 CustomMsg 点时间字段；原生反序列化抽查结果见 [转换校验](../data/outdoor01/tcp/conversion_validation.json)。完成转换后以 1× 速率播放，记录 /Odometry、算法计时和 mapping 进程 CPU / RSS。

TCP 配置分别用于 mapping、播放器、观测节点，见 [configs](../configs)。它们使用本机端口 55781–55783，需要分别设置各进程的 CYCLONEDDS_URI，使用相同且未占用的 ROS domain。它们是此次本机回放配置，机器人实机网络需要独立验证。

本仓库自动化复现覆盖保存结果的校验与制图；完整数据转换、ROS 构建和实机接入仍需上述环境。
