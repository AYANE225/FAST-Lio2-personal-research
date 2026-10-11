# 本轮求职展示数据

`voxel_benchmark.json` 是 Outdoor01 全图的 18 次独立工作进程运行及九次配对比较；`voxel_input.json` 记录统一 float64 输入、源 PCD 和重力变换的 SHA256。图中峰值内存使用 exec 后 `VmHWM`，另外保留可能带入父进程内存的 `ru_maxrss`。完整输入 NPY、输出点云及初版诊断数据保留于本地，不上传。

`regression_self.json` 使用 `data/outdoor01/recording` 与自身对比；`regression_surf03.json` 使用 recording 作基线、surf03 作候选。二者均读入 `configs/regression_limits.json` 的后验示例容差，文件哈希在报告中；前者退出码为 0，后者为 1。失败报告是被检查的工程配置未通过示例容差，不是检查器执行崩溃。

`validation.json` 与 `validation/` 记录最终本地验证命令、结果及其输出文件；GitHub 上的验证结果以对应提交的 Actions 运行记录为准。数据不能证明目标机器人部署、跨序列精度提升或统计显著性，详见 [成果说明](../../docs/career_evidence.md)。
