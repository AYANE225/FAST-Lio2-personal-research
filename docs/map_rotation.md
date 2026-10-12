# 高密度地图旋转动画

[GIF](../figures/outdoor01_rotation.gif) · [高清 MP4](../figures/outdoor01_rotation.mp4) · [PNG 封面](../figures/outdoor01_rotation.png) · [可交互三维地图](https://AYANE225.github.io/FAST-Lio2-personal-research/viewer/)

动画展示 M3DGR Outdoor01 回放得到的真实点云，适合在项目首页、作品集和面试演示中展示地图结构。分辨率 **1400×1050**，一圈 **16 秒、320 帧、20 fps**，GIF 无限循环；MP4 保留更细的色彩变化。

保留较小的 [旋转预览 GIF](../figures/outdoor01_rotation_preview.gif)：从同一组帧缩放到 **840×630、200 帧、12.5 fps**，仍为 16 秒循环，未重新采样地图点。README 现以[建图过程回放](homepage_showcase.md#建图回放)为主图，并提供高清旋转 GIF 链接。

## 点云与渲染

从已公开地图的 324 个最细层级点块读取 **1,852,691 点、0.10 m 体素**，逐块核对长度与 SHA256。沿用仓库 SOR：24 邻点、均值加 2 倍样本标准差阈值；清理 12,824 个稀疏离群点后，固定渲染 **1,839,867 点**。轨迹仍为已公开的 4,109 个估计位置，未做轨迹平滑或真值拟合。

| 设置 | 本次导出 |
|---|---|
| 着色 | 高度 −2～20 m，超出范围使用端点颜色 |
| 深度增强 | 复用查看器的 Eye-Dome Lighting，强度 0.45 |
| 点大小 | 1.6 CSS px；以 1.5 倍像素比渲染后输出目标尺寸 |
| 相机 | 透视视场角 28°、俯角 48°、起始方位角 60° |
| 旋转 | 每帧增加 1.125°；末帧 358.875°，不重复 360° 终点 |
| 构图 | XY 包围盒中心、平均点高度；按全部点和轨迹计算固定相机距离 |
| GIF 编码 | 全片共用调色板，255 个不透明颜色及一个透明索引，无抖色 |
| MP4 编码 | H.264，CRF 17，yuv420p，faststart |

相机距离对整圈方位角计算视锥约束，预留画面边距，旋转时不切换 LOD 或点数预算。地图已经应用公开记录中的初始重力旋转。本次 SOR 和着色只用于展示，**不构成定位精度改进；20 fps 是动画播放速率**。

## 从公开点块重新生成

使用 Python 3.11、Node 24、Playwright 1.58.2 和 FFmpeg。以下命令从仓库根目录运行；需要安装依赖时联网，正式渲染只读取本地文件，不请求 CDN 或外部地图服务。

```bash
sudo apt-get install ffmpeg fonts-noto-cjk
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm install --prefix output/rotation-tools --no-save playwright@1.58.2
output/rotation-tools/node_modules/.bin/playwright install chromium

.venv/bin/python scripts/prepare_map_rotation.py \
  --output-dir output/rotation-data --workers 4

PLAYWRIGHT_MODULE="$PWD/output/rotation-tools/node_modules/playwright/index.mjs" \
node scripts/render_map_rotation.mjs \
  --data-dir output/rotation-data --output-dir output/rotation-final
```

输出目录必须是新的，避免覆盖此前的导出。预览时可使用另一个输出目录并加 `--frames 4`，先检查四个方位。完整运行会保留逐帧 PNG、调色板、GIF、MP4、封面和 `rendering.json`；中间文件位于被 Git 忽略的 `output/`。

无头 Chromium 显式使用 ANGLE / SwiftShader，因此无独立显卡的机器也可导出。字体、Chromium、FFmpeg 版本可能影响输出像素和压缩结果；重新生成不要求文件哈希与发布版逐位一致。

旋转预览从完整导出的 PNG 编码，使用较小尺寸、帧率和 128 色调色板：

```bash
mkdir output/rotation-preview-small
ffmpeg -hide_banner -loglevel error -n \
  -framerate 20 -i output/rotation-final/frame-%04d.png \
  -vf 'fps=25/2,scale=840:630:flags=lanczos,palettegen=max_colors=128:reserve_transparent=1' \
  -frames:v 1 output/rotation-preview-small/preview-palette.png
ffmpeg -hide_banner -loglevel error -n \
  -framerate 20 -i output/rotation-final/frame-%04d.png \
  -i output/rotation-preview-small/preview-palette.png \
  -lavfi '[0:v]fps=25/2,scale=840:630:flags=lanczos[scaled];[scaled][1:v]paletteuse=dither=none:diff_mode=rectangle' \
  -loop 0 output/rotation-preview-small/outdoor01_rotation_preview.gif
```

## 发布结果核对

[生成记录](../data/map_rotation.json) 保存源 PCD、公开点块清单、过滤后点集、轨迹、导出代码与依赖代码的 SHA256，另外记录相机、浏览器、FFmpeg 命令、逐帧 PNG 哈希和成品哈希。[验证记录](../data/map_rotation_validation.json) 保存实际解码的帧数、时长、循环设置、帧差异以及全部地图范围的投影检查。

```bash
.venv/bin/python scripts/verify_artifacts.py
ffprobe -v error -count_frames -select_streams v:0 \
  -show_entries stream=codec_name,width,height,r_frame_rate,nb_read_frames:format=duration \
  -of json figures/outdoor01_rotation.mp4
ffmpeg -v error -i figures/outdoor01_rotation.gif -f null -
```

GIF、MP4、PNG 与本次记录单独纳入 [animation_checksums.json](../data/animation_checksums.json)。原实验数据、旧截图与既有校验清单均保留，动画不会替换性能或位置误差的测量证据。
