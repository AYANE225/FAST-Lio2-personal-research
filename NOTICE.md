# 来源与许可说明

- FAST-LIO2 / FAST_LIO 来自 [hku-mars/FAST_LIO](https://github.com/hku-mars/FAST_LIO)。算法开发贡献归上游作者，本仓库展示集成、诊断和评估结果。
- [fast-lio2-local.patch](patches/fast-lio2-local.patch) 包含对 FAST_LIO 的小幅修改及上游代码上下文，按上游 GNU GPL version 2 条款提供；完整许可见 [FAST_LIO_GPL-2.0.txt](licenses/FAST_LIO_GPL-2.0.txt)。补丁作用于实验记录中的提交，修改 C++ 标准编译参数并启用既有地图保存代码；它在两轮实验前已存在。
- Outdoor01 来自 [M3DGR](https://github.com/sjtuyinjie/M3DGR)。本仓库不重新分发原始数据集；图表、位姿对比和地图网格为该序列的派生实验结果。原始数据使用条款以项目发布者说明为准。
- 分析与制图使用 NumPy、SciPy 和 Matplotlib。第三方名称不表示对本实验结果的认可。
