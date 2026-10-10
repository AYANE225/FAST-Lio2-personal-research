# 来源与许可说明

- 本仓库原创、独立的 Python 工具、测试、查看器代码、配置和文档采用 [MIT License](LICENSE)，Copyright (c) 2026 AYANE225。以下上游补丁、第三方资源和数据材料分别遵守对应来源条款，不因根目录的 MIT 文本而改变许可。
- FAST-LIO2 / FAST_LIO 来自 [hku-mars/FAST_LIO](https://github.com/hku-mars/FAST_LIO)。算法开发贡献归上游作者，本仓库贡献为集成、工程工具、实验诊断和展示，未提出新的 FAST-LIO2 算法。
- [fast-lio2-local.patch](patches/fast-lio2-local.patch) 包含 FAST_LIO 修改及上游代码上下文，按上游 GNU GPL version 2 条款提供；完整许可见 [FAST_LIO_GPL-2.0.txt](licenses/FAST_LIO_GPL-2.0.txt)。补丁作用于 [source_version.json](data/outdoor01/source_version.json) 中的提交，修改 C++ 标准编译参数并启用既有地图保存代码；它在本仓库原始 UDP / TCP 对照前已存在，后续工程回放保持同一套定位算法源文件。
- 查看器随附 Three.js **0.160.1 (r160)** 与 OrbitControls；Copyright © 2010–2023 three.js authors，采用 MIT，原文见 [THREE-LICENSE.txt](docs/viewer/vendor/THREE-LICENSE.txt)。依赖保存在仓库内，运行时不从 CDN 下载。
- Outdoor01 来自 [M3DGR](https://github.com/sjtuyinjie/M3DGR) / [官方主页](https://sjtuyinjie.github.io/M3DGR-website/)。`data/` 中的轨迹、统计、局部点云，`docs/viewer/data/` 中的轻量点云，以及 `figures/` 中的实验图和截图均基于该序列；保留数据集与原作者署名。本仓库不重新分发原始 bag、完整 PCD 或完整分块记录。
- M3DGR 仓库的 MIT 文本适用对象写作软件及相关文档，不能据此自行认定下载 bag 及其全部派生数据也采用 MIT。派生数据与图表不纳入本仓库原创代码的 MIT 授权范围；数据使用条款以发布者说明为准。实际核查范围见 [engineering_references.md](docs/engineering_references.md)。
- PCL、CloudCompare、Potree、ROS 2 和 Linux man-pages 用于方法与接口参考，采用范围及官方链接见上述资料说明。分析与制图使用 NumPy、SciPy、Matplotlib 等库；第三方名称不表示对本实验结果的认可。
