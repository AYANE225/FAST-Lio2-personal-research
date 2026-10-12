"""Compose Outdoor01 map posters and a responsive gallery from verified inputs."""
import argparse
from functools import lru_cache
import hashlib
from io import BytesIO
import json
from pathlib import Path
import platform
import subprocess

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps, __version__ as pillow_version

from plot_engineering import setup_style

ROOT = Path(__file__).resolve().parents[1]
INK = '#e7eff5'
MUTED = '#9ab0c1'
ACCENT = '#7fd8cd'
GOLD = '#ffd38b'
BORDER = '#263748'
FONTS = {}
BACKGROUND = (13, 13, 28)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@lru_cache(maxsize=24)
def font(size, bold=False):
    path, index = FONTS[bold]
    return ImageFont.truetype(path, size, index=index)


def label(canvas, xy, text, size=28, color=INK, bold=False, anchor='lt'):
    draw = ImageDraw.Draw(canvas)
    bounds = draw.textbbox(xy, text, font=font(size, bold), anchor=anchor)
    if bounds[0] < 0 or bounds[1] < 0 or bounds[2] > canvas.width or bounds[3] > canvas.height:
        raise ValueError(f'Text extends beyond the poster: {text}')
    draw.text(xy, text, font=font(size, bold), fill=color, anchor=anchor)


def fit_image(canvas, source, box):
    left, top, width, height = box
    fitted = ImageOps.contain(source, (width, height), Image.Resampling.LANCZOS)
    canvas.paste(fitted, (left + (width - fitted.width) // 2, top + (height - fitted.height) // 2))


def header(canvas, title, value, caption):
    label(canvas, (72, 46), 'FAST-LIO2 / MID-360 · OUTDOOR01', 26, ACCENT)
    label(canvas, (72, 100), title, 52, bold=True)
    label(canvas, (1848, 66), value, 46, bold=True, anchor='rt')
    label(canvas, (1848, 125), caption, 25, MUTED, anchor='rt')


def gradient(canvas, xy, width, stops, low, high):
    offsets = np.array([stop[0] for stop in stops])
    colors = np.array([[int(stop[1][i:i + 2], 16) for i in (1, 3, 5)] for stop in stops])
    values = np.linspace(0, 1, width)
    rgb = np.column_stack([np.interp(values, offsets, colors[:, i]) for i in range(3)])
    strip = np.repeat(np.rint(rgb).astype(np.uint8)[None, :, :], 12, axis=0)
    canvas.paste(Image.fromarray(strip), xy)
    label(canvas, (xy[0], xy[1] + 22), low, 23, MUTED)
    label(canvas, (xy[0] + width, xy[1] + 22), high, 23, MUTED, anchor='rt')


def draw_plan(points, positions, crop):
    """Draw the unmodified XY projection with an equal metric scale on both axes."""
    figure, axis = plt.subplots(figsize=(18, 12), dpi=100, facecolor=np.array(BACKGROUND) / 255)
    figure.subplots_adjust(left=.065, right=.985, top=.99, bottom=.075)
    axis.set_facecolor(np.array(BACKGROUND) / 255)
    axis.scatter(points[:, 0], points[:, 1], s=.14, color='#86aebe', alpha=.64,
                 edgecolors='none', rasterized=True, zorder=2)
    axis.plot(positions[:, 0], positions[:, 1], color='#101923', lw=5, zorder=3)
    axis.plot(positions[:, 0], positions[:, 1], color=GOLD, lw=2.1, zorder=4)
    axis.scatter(*positions[0, :2], s=48, color=ACCENT, edgecolors=INK, linewidths=.7, zorder=6)
    axis.scatter(*positions[-1, :2], s=42, color='#ff937e', marker='x', linewidths=1.7, zorder=7)
    for text, position, label_position, color in (
            ('起点', positions[0, :2], (-31, 12), ACCENT),
            ('终点', positions[-1, :2], (-31, -12), '#ff937e')):
        axis.annotate(text, xy=position, xytext=label_position, color=color, fontsize=15,
                      arrowprops={'arrowstyle': '-', 'color': color, 'lw': .9}, zorder=8,
                      bbox={'facecolor': np.array(BACKGROUND) / 255, 'edgecolor': 'none', 'pad': 3})
    axis.add_patch(Rectangle(crop['min'][:2], crop['max'][0] - crop['min'][0],
                             crop['max'][1] - crop['min'][1], fill=False,
                             edgecolor=ACCENT, linewidth=1.1, linestyle=(0, (5, 4)), zorder=5))
    axis.text(crop['min'][0] + 2, crop['min'][1] + 2, 'A · 立面特写区域', fontsize=13, color=ACCENT,
              bbox={'facecolor': np.array(BACKGROUND) / 255, 'edgecolor': 'none', 'pad': 3}, zorder=6)
    minimum = points[:, :2].min(axis=0) - 5
    maximum = points[:, :2].max(axis=0) + 5
    axis.set_xlim(minimum[0], maximum[0])
    axis.set_ylim(minimum[1], maximum[1])
    axis.set_aspect('equal', adjustable='box')
    axis.set_xticks(np.arange(-40, 181, 20))
    axis.set_yticks(np.arange(-60, 81, 20))
    axis.tick_params(colors=MUTED, labelsize=12, length=0, pad=8)
    axis.grid(color='#668699', alpha=.18, linewidth=.6, zorder=0)
    for spine in axis.spines.values():
        spine.set_color(BORDER)
    axis.set_xlabel('局部 X / m', color=MUTED, fontsize=14, loc='right', labelpad=7)
    axis.set_ylabel('局部 Y / m', color=MUTED, fontsize=14, loc='top', labelpad=8)
    # Coordinates are metres in the gravity-aligned local frame, not latitude/longitude.
    scale_x, scale_y, scale_m = -34., 73., 20.
    axis.plot([scale_x, scale_x + scale_m], [scale_y, scale_y], color=INK, lw=2.5, zorder=7)
    axis.plot([scale_x, scale_x], [scale_y - 1, scale_y + 1], color=INK, lw=1.2, zorder=7)
    axis.plot([scale_x + scale_m, scale_x + scale_m], [scale_y - 1, scale_y + 1], color=INK, lw=1.2, zorder=7)
    axis.text(scale_x + scale_m / 2, scale_y + 2, '20 m', color=INK, ha='center', va='bottom', fontsize=14)
    figure.canvas.draw()
    scale_pixels = float(axis.transData.transform((scale_x + scale_m, scale_y))[0]
                         - axis.transData.transform((scale_x, scale_y))[0])
    buffer = BytesIO()
    figure.savefig(buffer, dpi=100, facecolor=figure.get_facecolor())
    plt.close(figure)
    buffer.seek(0)
    return Image.open(buffer).convert('RGB'), {
        'limits_m': {'x': [float(minimum[0]), float(maximum[0])], 'y': [float(minimum[1]), float(maximum[1])]},
        'scale_bar_m': scale_m, 'scale_bar_pixels_at_export': scale_pixels,
        'aspect': 'equal', 'projected_points': len(points), 'trajectory_positions': len(positions),
        'point_color': '#86aebe', 'point_alpha': .64, 'trajectory_color': GOLD,
    }


def gallery(images, mobile=False):
    columns = 1 if mobile else 2
    width = 960 if mobile else 1920
    margin, gap, top, cell_height = 48, 32, 154, 594
    cell_width = (width - 2 * margin - gap * (columns - 1)) // columns
    rows = len(images) // columns
    canvas = Image.new('RGB', (width, top + rows * cell_height + (rows - 1) * gap + 82), BACKGROUND)
    label(canvas, (margin, 36), 'OUTDOOR01 / 地图图集', 44, bold=True)
    label(canvas, (margin, 100), '同一真实点云 · 四种观察方式', 26, MUTED)
    for i, (title, subtitle, source) in enumerate(images):
        x = margin + (i % columns) * (cell_width + gap)
        y = top + (i // columns) * (cell_height + gap)
        ImageDraw.Draw(canvas).rounded_rectangle((x, y, x + cell_width, y + cell_height),
                                                 radius=14, outline=BORDER, width=2)
        label(canvas, (x + 24, y + 22), f'{i + 1:02d}  {title}', 32, bold=True)
        label(canvas, (x + 24, y + 67), subtitle, 23, MUTED)
        fit_image(canvas, source, (x + 14, y + 110, cell_width - 28, cell_height - 126))
    label(canvas, (margin, canvas.height - 47), '点云显示处理 · 保留点集坐标与估计轨迹', 24, MUTED)
    return canvas


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--render-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error('Use a new output directory to preserve previous exports')
    inputs = {}
    for name in ('preparation.json', 'points.bin', 'trajectory.json'):
        inputs[name] = sha256(args.data_dir / name)
    preparation = json.loads((args.data_dir / 'preparation.json').read_text())
    rendering = json.loads((args.render_dir / 'rendering.json').read_text())
    if (inputs['preparation.json'] != rendering['preparation_sha256']
            or inputs['points.bin'] != rendering['point_file_sha256']
            or inputs['trajectory.json'] != rendering['trajectory_sha256']):
        raise ValueError('Plot inputs differ from the rendering inputs')
    scene_images = {}
    for name, expected in rendering['outputs'].items():
        path = args.render_dir / name
        if path.stat().st_size != expected['size_bytes'] or sha256(path) != expected['sha256']:
            raise ValueError(f'Rendered image checksum mismatch: {name}')
        scene_images[path.stem] = Image.open(path).convert('RGB')
    global BACKGROUND
    BACKGROUND = scene_images['overview'].getpixel((0, 0))
    points = np.fromfile(args.data_dir / 'points.bin', dtype='<f4').reshape(-1, 4)
    trajectory = json.loads((args.data_dir / 'trajectory.json').read_text())
    positions = np.asarray(trajectory['positions'], dtype=float)
    timestamps = np.asarray(trajectory['timestamps'], dtype=float)
    if (len(points) != preparation['point_count'] or not np.isfinite(points).all()
            or positions.shape != (preparation['trajectory_count'], 3) or not np.isfinite(positions).all()
            or timestamps.shape != (len(positions),) or not np.isfinite(timestamps).all()
            or not np.all(np.diff(timestamps) > 0)):
        raise ValueError('Invalid point or trajectory arrays')
    configurations = {view['id']: view for view in rendering['config']['views']}
    views = {view['id']: view for view in rendering['views']}
    for name, view in views.items():
        crop = configurations[name].get('crop_bounds_m')
        selected = points if crop is None else points[np.all((points[:, :3] >= crop['min']) & (points[:, :3] <= crop['max']), axis=1)]
        if (len(selected) != view['selected_points'] or hashlib.sha256(selected.tobytes()).hexdigest() != view['selected_xyzi_sha256']
                or max(view['max_abs_ndc_xy']) > rendering['config']['frame_margin']):
            raise ValueError(f'Rendered selection or framing mismatch: {name}')
    pair = ('facade_structure', 'facade_intensity')
    for key in ('selected_xyzi_sha256', 'camera_position_m', 'center_m', 'viewport_css_pixels'):
        if views[pair[0]][key] != views[pair[1]][key]:
            raise ValueError(f'Facade comparison does not share {key}')
    for key in ('point_size_css_px', 'edl_strength'):
        if configurations[pair[0]][key] != configurations[pair[1]][key]:
            raise ValueError(f'Facade comparison does not share {key}')
    for bold in (False, True):
        family = 'Noto Sans CJK SC' + (':style=Bold' if bold else '')
        matched = subprocess.check_output(['fc-match', '-f', '%{file}\t%{index}', family], text=True).strip().split('\t')
        if len(matched) != 2 or 'NotoSansCJK' not in Path(matched[0]).name:
            raise RuntimeError('Install fonts-noto-cjk to render the Chinese labels')
        FONTS[bold] = matched[0], int(matched[1])
    setup_style()
    args.output_dir.mkdir(parents=True)
    overview = Image.new('RGB', (1920, 1440), BACKGROUND)
    header(overview, '三维点云 · 全景', f'{len(points):,}', '显示点 · 0.10 m 体素 + SOR')
    overview.paste(scene_images['overview'], (60, 195))
    label(overview, (72, 1300), '高度 / m', 27)
    height_range = rendering['color_ranges']['height']
    gradient(overview, (230, 1311), 430, rendering['graphics']['color_maps']['height'],
             f'≤ {height_range[0]:g}', f'≥ {height_range[1]:g}')
    ImageDraw.Draw(overview).line((1350, 1320, 1400, 1320), fill='#d9fff2', width=3)
    label(overview, (1420, 1303), f'估计轨迹 · {len(positions):,} 个位姿', 26, MUTED)
    label(overview, (72, 1380), '真实点云 · EDL 深度增强 · 已按初始重力旋转', 25, MUTED)

    crop = configurations['facade_structure']['crop_bounds_m']
    plan_image, plan_report = draw_plan(points, positions, crop)
    length_m = float(np.linalg.norm(np.diff(positions, axis=0), axis=1).sum())
    plan = Image.new('RGB', (1920, 1520), BACKGROUND)
    header(plan, '地图与轨迹 · 俯视', f'{length_m:.1f} m', '估计路径长度')
    plan.paste(plan_image, (60, 195))
    label(plan, (72, 1458), '等比例 XY 投影 · 局部坐标，非地理北向 · A 框对应立面特写', 25, MUTED)

    facade = Image.new('RGB', (1920, 1080), BACKGROUND)
    header(facade, '建筑立面 · 同视角对照', f'{views[pair[0]]["selected_points"]:,}', '局部显示点 · 同一点集')
    for x, name, title, subtitle in ((60, pair[0], '几何轮廓', '单色 + EDL'), (984, pair[1], '回波强度', '强度着色 + EDL')):
        label(facade, (x + 18, 222), title, 33, bold=True)
        label(facade, (x + 18, 271), subtitle, 25, MUTED)
        fit_image(facade, scene_images[name], (x, 312, 876, 514))
    crop_label = f"A 区域：X {crop['min'][0]:g}–{crop['max'][0]:g} m / Y {crop['min'][1]:g}–{crop['max'][1]:g} m"
    label(facade, (78, 893), crop_label.replace('-', '−'), 27, ACCENT)
    label(facade, (78, 944), '同一相机 · 同一点大小 · 同一 EDL 强度', 26, MUTED)
    label(facade, (1026, 891), '强度（体素均值）', 25)
    intensity_range = rendering['color_ranges']['intensity']
    gradient(facade, (1340, 901), 475, configurations[pair[1]]['palette'],
             f'≤ {intensity_range[0]:g}', f'≥ {intensity_range[1]:g}')
    label(facade, (78, 1020), '同一份离线点云，仅切换着色。', 25, MUTED)

    images = [('三维全景', f'高度着色 · {len(points) / 10000:.2f} 万点', scene_images['overview']),
              ('地图与轨迹', '等比例俯视 · 起终点与比例尺', plan_image),
              ('建筑立面', f'几何轮廓 · {views[pair[0]]["selected_points"] / 10000:.2f} 万点', scene_images['facade_structure']),
              ('回波强度', '同一点集 · 同一视角', scene_images['facade_intensity'])]
    posters = {'outdoor01_map_overview.png': overview, 'outdoor01_map_route.png': plan,
               'outdoor01_map_facade.png': facade, 'outdoor01_gallery.png': gallery(images),
               'outdoor01_gallery_mobile.png': gallery(images, mobile=True)}
    outputs = {}
    for name, poster in posters.items():
        path = args.output_dir / name
        poster.save(path, optimize=True)
        outputs[name] = {'size_bytes': path.stat().st_size, 'sha256': sha256(path), 'pixels': list(poster.size)}
    report = {'schema_version': 1, 'scope': 'Presentation of existing Outdoor01 point cloud and poses; no new SLAM run or accuracy claim.',
              'preparation': preparation, 'rendering': rendering, 'input_sha256': inputs,
              'plot_sources_sha256': {name: sha256(ROOT / name) for name in ('scripts/plot_map_gallery.py', 'scripts/plot_engineering.py', 'requirements.txt')},
              'plot_environment': {'python': platform.python_version(), 'numpy': np.__version__, 'matplotlib': matplotlib.__version__, 'pillow': pillow_version},
              'point_count': len(points), 'trajectory_count': len(positions), 'estimated_path_length_m': length_m,
              'intensity_description': 'Per-voxel mean of recorded intensity; device scale, not calibrated reflectance.',
              'trajectory_duration_s': float(timestamps[-1] - timestamps[0]), 'plan_view': plan_report,
              'fonts': [{'file': Path(path).name, 'index': index, 'sha256': sha256(Path(path))} for path, index in FONTS.values()],
              'outputs': outputs}
    (args.output_dir / 'map_gallery.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(f'Saved three posters and desktop/mobile gallery previews; verified {len(points):,} source points.')


if __name__ == '__main__':
    main()
