# FAST-Lio2-个人研究版

**MID-360 · FAST-LIO2 · ROS 2** — 移动机器人定位、建图与工程验证。

**[交互地图](https://AYANE225.github.io/FAST-Lio2-personal-research/viewer/)** · [实验数据](docs/homepage_showcase.md) · [工程成果](docs/career_evidence.md) · [复现](docs/reproduction.md)

[![自动化验证](https://github.com/AYANE225/FAST-Lio2-personal-research/actions/workflows/verify.yml/badge.svg)](https://github.com/AYANE225/FAST-Lio2-personal-research/actions/workflows/verify.yml)

## 建图过程

<a href="docs/homepage_showcase.md#建图回放">
  <picture>
    <source media="(prefers-reduced-motion: reduce) and (max-width: 640px)" srcset="figures/outdoor01_mapping_poster_mobile.png">
    <source media="(prefers-reduced-motion: reduce)" srcset="figures/outdoor01_mapping_poster.png">
    <source media="(max-width: 640px)" srcset="figures/outdoor01_mapping_mobile.gif">
    <img src="figures/outdoor01_mapping.gif" alt="Outdoor01 建图回放：4,109 帧、890.91 万记录点。首次体素观测点经全程离线 SOR 后，按采集时间依次显示；14 秒缩时回放。" width="100%">
  </picture>
</a>

**410.8 秒记录 → 14 秒回放** · 0.10 m 体素保留首次观测点，再做全程离线 SOR；仅用于显示。

[回放 MP4](figures/outdoor01_mapping.mp4) · [360° 高清旋转](figures/outdoor01_rotation.gif) · [静态全景](figures/outdoor01_map_overview.png)

## 实测结果

<a href="docs/homepage_showcase.md#位置误差">
  <picture>
    <source media="(max-width: 640px)" srcset="figures/outdoor01_position_error_mobile.png">
    <img src="figures/outdoor01_position_error.png" alt="三维位置参考误差：RMSE 0.225 m、p95 0.331 m；参考坐标轨迹、未平滑误差曲线与累计分布。全程 SE(3) 对齐，RTK 杆臂未补偿。" width="100%">
  </picture>
</a>

<a href="docs/homepage_showcase.md#中断恢复">
  <picture>
    <source media="(max-width: 640px)" srcset="figures/outdoor01_recovery_mobile.png">
    <img src="figures/outdoor01_recovery.png" alt="实际 SIGKILL 恢复实验：索引从 20 帧恢复到 30 帧，半写入临时块被隔离，损坏块被拒绝。中断记录仍未完成，接收总数未知；不代表掉电验证。" width="100%">
  </picture>
</a>

<a href="docs/engineering_results.md">
  <picture>
    <source media="(max-width: 640px)" srcset="figures/outdoor01_performance_mobile.png">
    <img src="figures/outdoor01_performance.png" alt="三配置实测：分块记录的采样峰值内存降低 59.8%，核心耗时增加。C++ 离线体素处理的耗时中位数比为 8.00、4.38、2.22；每档各三次，不代表在线 SLAM 提速。" width="100%">
  </picture>
</a>

## 高清图集

<a href="docs/map_gallery.md">
  <picture>
    <source media="(max-width: 640px)" srcset="figures/outdoor01_gallery_mobile.png">
    <img src="figures/outdoor01_gallery.png" alt="Outdoor01 地图图集：三维全景、带比例尺和起终点的轨迹俯视图、建筑立面的几何轮廓与回波强度。" width="100%">
  </picture>
</a>

[全景](figures/outdoor01_map_overview.png) · [轨迹俯视](figures/outdoor01_map_route.png) · [立面对照](figures/outdoor01_map_facade.png) · [旋转 MP4](figures/outdoor01_rotation.mp4)

<details>
<summary>工程流程与能力对应</summary>

```mermaid
flowchart TB
    A["MID-360 + IMU<br/>公开数据序列"] --> B["ROS 2 接入<br/>时间检查 · DDS TCP"]
    B --> C["FAST-LIO2<br/>上游估计器"]
    C --> D["地图分块记录<br/>后台写盘 · 中断恢复"]
    C --> E["轨迹与资源评估<br/>帧数 · RSS · 位置误差"]
    C -->|PCD 地图| F["离线点云处理<br/>C++ 体素 · SOR"]
    E --> H["自动回归检查<br/>协议 · 阈值 · 退出码"]
    F --> G["WebGL 地图展示<br/>LOD · EDL · GIF"]
    classDef upstream fill:#edf2f7,stroke:#a2b4c5,color:#243e52
    classDef tools fill:#eaf5fb,stroke:#2788b3,color:#164a65
    classDef display fill:#eaf7f3,stroke:#25877b,color:#205c54
    class A,C upstream
    class B,D,E,H tools
    class F,G display
```

[岗位能力对应](docs/job_requirements.md) · [代码与实测](docs/career_evidence.md) · [记录器设计](docs/map_recording.md)

</details>

<details>
<summary>处理对照、回归结果与复现资料</summary>

**同一区域、视角、点大小与色标。显示处理不参与定位估计。**

[![原始点云、体素、SOR 与受约束局部平面投影的对照](figures/map_processing.png)](docs/engineering_results.md)

**保留真实失败结果：0.3 m 配置超过示例耗时容差。**

[![0.3 m 配置自动回归检查：核心 p95 超过示例 +20% 容差](figures/regression_check.png)](docs/career_evidence.md)

[全量曲线](docs/engineering_results.md) · [首页图表与实验口径](docs/homepage_showcase.md) · [配置对比原图](figures/readme_results.svg) · [体素计时原图](figures/readme_voxel.svg) · [旋转动画生成](docs/map_rotation.md) · [复现步骤](docs/reproduction.md)

</details>

Outdoor01 单序列桌面回放；实际移动机器人部署仍需现场验证。算法：[FAST_LIO](https://github.com/hku-mars/FAST_LIO) · 数据：[M3DGR](https://github.com/sjtuyinjie/M3DGR) · [来源与许可](NOTICE.md)
