# FAST-Lio2-个人研究版

**MID-360 · FAST-LIO2 · ROS 2** — 移动机器人定位与建图工程研究，使用 Outdoor01 公开序列做桌面回放验证。

**[打开交互地图](https://AYANE225.github.io/FAST-Lio2-personal-research/viewer/)** · [复现](docs/reproduction.md) · [代码与实测](docs/career_evidence.md) · [![自动化验证](https://github.com/AYANE225/FAST-Lio2-personal-research/actions/workflows/verify.yml/badge.svg)](https://github.com/AYANE225/FAST-Lio2-personal-research/actions/workflows/verify.yml)

[![Outdoor01 真实点云地图：183.99 万点，0.10 m 体素，360° 旋转](figures/outdoor01_rotation_preview.gif)](figures/outdoor01_rotation.gif)

**183.99 万点 · 0.10 m 体素 · 16 秒循环**　[高清 GIF](figures/outdoor01_rotation.gif) · [MP4](figures/outdoor01_rotation.mp4)

## 实测结果

<a href="docs/engineering_results.md">
  <picture>
    <source media="(max-width: 640px)" srcset="figures/readme_results_mobile.svg">
    <img src="figures/readme_results.svg" alt="Outdoor01 回放：缺帧 329→0；三组配置的内存、核心耗时和位置参考误差。分块记录降低内存，但核心耗时增加。" width="100%">
  </picture>
</a>

<a href="docs/career_evidence.md">
  <picture>
    <source media="(max-width: 640px)" srcset="figures/readme_voxel_mobile.svg">
    <img src="figures/readme_voxel.svg" alt="890 万点离线体素处理：0.50、0.20、0.10 m 下，NumPy/C++ 耗时中位数比值为 8.00、4.38、2.22；每档各测三次。" width="100%">
  </picture>
</a>

## 工程流程

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

<details>
<summary>展开：点云处理对照、回归检查与详细资料</summary>

**同一区域、视角、点大小与色标。显示处理不参与定位估计。**

[![原始点云、体素、SOR 与受约束局部平面投影的对照](figures/map_processing.png)](docs/engineering_results.md)

**保留真实失败结果：0.3 m 配置超过示例耗时容差。**

[![0.3 m 配置自动回归检查：核心 p95 超过示例 +20% 容差](figures/regression_check.png)](docs/career_evidence.md)

[全量曲线与实验口径](docs/engineering_results.md) · [岗位能力对应](docs/job_requirements.md) · [记录器](docs/map_recording.md) · [动画生成](docs/map_rotation.md) · [复现步骤](docs/reproduction.md)

</details>

算法：[FAST_LIO](https://github.com/hku-mars/FAST_LIO) · 数据：[M3DGR](https://github.com/sjtuyinjie/M3DGR) · 渲染：[Three.js](https://threejs.org/) · [来源与许可](NOTICE.md)
