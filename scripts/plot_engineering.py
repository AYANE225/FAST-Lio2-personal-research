"""Replot engineering comparisons and fixed-view map processing from public data."""
import argparse
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
from matplotlib import font_manager
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
COLORS = ['#667085', '#0072B2', '#D55E00']


def setup_style():
    font = Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
    if font.exists():
        font_manager.fontManager.addfont(str(font))
        plt.rcParams['font.family'] = font_manager.FontProperties(fname=str(font)).get_name()
    plt.rcParams.update({'font.size': 10, 'axes.titlesize': 12, 'axes.labelsize': 10,
                         'axes.unicode_minus': False, 'svg.fonttype': 'path',
                         'axes.spines.top': False, 'axes.spines.right': False})


def save(fig, output, name):
    for extension in ('png', 'svg'):
        fig.savefig(output / f'{name}.{extension}', dpi=180, facecolor='white')
    plt.close(fig)


def engineering_comparison(output):
    runs = [('原 TCP 基线', 'tcp'), ('分块记录 · 0.5 m', 'recording'), ('分块记录 · 0.3 m', 'surf03')]
    fig, axes = plt.subplots(2, 2, figsize=(14, 9), constrained_layout=True)
    table_rows = []
    for (label, directory), color in zip(runs, COLORS):
        path = ROOT / 'data/outdoor01' / directory
        metrics = json.loads((path / 'metrics.json').read_text())
        timing = np.loadtxt(path / 'algorithm_logs/fast_lio_time_log.csv', delimiter=',', skiprows=1)
        position = np.load(path / 'position_comparison.npz', allow_pickle=False)
        resource = [json.loads(line) for line in (path / 'resources.jsonl').read_text().splitlines()]
        if directory == 'tcp':
            rss = np.array([row['rss_bytes'] for row in resource]) / 1048576
            cpu_mean = metrics['resources']['cpu_percent_mean']
        else:
            rss = np.array([sum(row.get(role, {}).get('rss_bytes', 0)
                                for role in ('mapping', 'recorder')) for row in resource]) / 1048576
            cpu_mean = metrics['resources']['combined']['cpu_percent']['mean']
        axes[0, 0].plot([r['elapsed_s'] for r in resource], rss, lw=1.3, color=color, label=label)
        core = np.sort(timing[:, 1] * 1000)
        axes[0, 1].step(np.r_[0, core], np.r_[0, np.arange(1, len(core)+1) / len(core) * 100],
                        where='post', color=color, lw=1.25, label=f'{label} · p95 {np.percentile(core,95):.2f} ms')
        error = np.sort(position['error_m'])
        rmse = float(np.sqrt(np.mean(error**2)))
        axes[1, 0].step(np.r_[0, error], np.r_[0, np.arange(1, len(error)+1) / len(error) * 100],
                        where='post', color=color, lw=1.3, label=f'{label} · RMSE {rmse:.3f} m')
        table_rows.append([label, f"{metrics['coverage']['processed_frames']:,}",
                           f'{rss.max():.1f}', f'{cpu_mean:.2f}', f'{rmse:.3f}'])
    axes[0, 0].set(title='(a) 全程采样内存：工程版计入两个进程', xlabel='播放器启动后的时间 (s)', ylabel='RSS (MiB)', ylim=(0, None))
    axes[0, 1].set(title='(b) 核心处理耗时：全部帧的经验分布', xlabel='核心处理耗时 (ms)', ylabel='累计比例 (%)', xlim=(0, None), ylim=(0, 100))
    axes[1, 0].set(title='(c) 同一位置参考：全部 4,107 个样本', xlabel='位置参考误差 (m)', ylabel='累计比例 (%)', xlim=(0, None), ylim=(0, 100))
    for ax in (axes[0, 0], axes[0, 1], axes[1, 0]):
        ax.grid(alpha=.2, lw=.6)
        ax.set_axisbelow(True)
        ax.legend(fontsize=8, loc='lower right' if ax is axes[1, 0] else 'best')
    axes[1, 1].axis('off')
    axes[1, 1].set_title('(d) 单轮测量汇总与评估范围', loc='left')
    table = axes[1, 1].table(cellText=table_rows,
        colLabels=['配置', '输出帧', '峰值 MiB', 'CPU %', 'RMSE m'],
        colWidths=[.34, .16, .17, .15, .18], loc='upper center', cellLoc='center', bbox=[0,.56,1,.37])
    table.auto_set_font_size(False); table.set_fontsize(9)
    for (row, _), cell in table.get_celld().items():
        cell.set_edgecolor('#e4e7ec')
        cell.set_facecolor('#eef3f7' if row == 0 else 'white')
    axes[1, 1].text(0, .46, 'CPU 100% = 一个逻辑核；工程版为 mapping + recorder。\n'
        'RSS 约每秒采样，不能捕捉所有瞬时峰值。\n'
        '原基线未覆盖退出保存；工程版覆盖受控退出。\n\n'
        '每配置一轮；不据此声称稳定提速或跨场景精度提升。\n'
        '位置评估为一次 SE(3) 对齐，尺度固定为 1；\n'
        '未拟合时间偏移、未补偿 RTK 杆臂，不评估姿态。',
        transform=axes[1, 1].transAxes, va='top', linespacing=1.7, fontsize=10, color='#475467')
    fig.suptitle('Outdoor01 / MID-360：工程化与扫描体素参数对照\n'
                 '完整 1× 回放 · 相同源代码与 Release 构建 · 核心计时不含发布和写盘', fontsize=15)
    save(fig, output, 'engineering_comparison')


def map_processing(output):
    data = np.load(ROOT / 'data/map_processing/local_comparison.npz', allow_pickle=False)
    report = json.loads((ROOT / 'docs/viewer/data/processing_report.json').read_text())
    local = report['local_comparison']; projection = local['plane_projection']
    roi = data['roi']; center = roi.mean(axis=1)
    azimuth, elevation = np.deg2rad([-50, 35])
    right = np.array([-np.sin(azimuth), np.cos(azimuth), 0])
    up = np.array([-np.sin(elevation)*np.cos(azimuth), -np.sin(elevation)*np.sin(azimuth), np.cos(elevation)])
    depth = np.cross(right, up)
    rotation = np.stack([right, up, depth])
    corners = np.array([[x,y,z] for x in roi[0] for y in roi[1] for z in roi[2]])
    corners = (corners - center) @ rotation.T
    fig, axes = plt.subplots(3, 2, figsize=(14, 13), constrained_layout=True,
                             gridspec_kw={'height_ratios':[1,1,.6]})
    stages = [('raw', '(a) 原始累积点', '全部点参与绘制'),
              ('voxel', '(b) 0.05 m 体素质心', '每个固定体素内取均值'),
              ('sor', '(c) 体素 + SOR', '24 邻点 · 2 倍样本标准差'),
              ('plane', '(d) SOR + 局部平面投影', '单次投影 · 最终位移不超过 30 mm')]
    for axis, (key, title, subtitle) in zip(axes[:2].flat, stages):
        points = data[key]
        screen = (points[:, :3] - center) @ rotation.T
        order = np.argsort(screen[:, 2])
        scatter = axis.scatter(screen[order, 0], screen[order, 1], c=points[order, 2],
            vmin=-2, vmax=10, cmap='cividis', s=.65, linewidths=0, rasterized=True)
        axis.set(title=f'{title} · {len(points):,} 点\n{subtitle}',
            xlim=(corners[:, 0].min(), corners[:, 0].max()), ylim=(corners[:, 1].min(), corners[:, 1].max()),
            xlabel='投影横向 (m)', ylabel='投影纵向 (m)')
        axis.set_aspect('equal'); axis.set_facecolor('#f5f7fa')
        axis.grid(alpha=.16, lw=.5); axis.set_axisbelow(True)
    fig.colorbar(scatter, ax=list(axes[:2].flat), shrink=.75, pad=.015, label='重力对齐高度 Z (m)')
    counts = [len(data[key]) for key, _, _ in stages]
    axes[2, 0].barh(['原始', '体素', 'SOR', '局部投影'], counts, color=['#98a2b3', '#0072B2', '#009E73', '#D55E00'], height=.6)
    for row, count in enumerate(counts):
        axes[2, 0].text(count + max(counts)*.02, row, f'{count:,}', va='center', fontsize=9)
    axes[2, 0].invert_yaxis()
    axes[2, 0].set(title='(e) 同一区域的点数变化', xlabel='保留点数', xlim=(0, max(counts)*1.25))
    movement = np.linalg.norm(data['plane'][:, :3].astype(float) - data['sor'][:, :3].astype(float), axis=1) * 1000
    if movement.max() > 30 or len(data['plane']) != len(data['sor']):
        raise ValueError('Published local projection violates its displacement/count contract')
    axes[2, 1].hist(movement, bins=np.linspace(0,30,61), color='#0072B2', edgecolor='none')
    axes[2, 1].axvline(np.percentile(movement,95), color='#D55E00', ls='--',
                      label=f'p95 {np.percentile(movement,95):.2f} mm')
    axes[2, 1].set(title='(f) 局部投影改变了多少坐标', xlabel='点位移 (mm)', ylabel='点数', xlim=(0,30))
    axes[2, 1].legend(fontsize=9)
    fig.suptitle('真实点云处理对照：固定区域、相机、点大小与色标\n'
        'X [10, 30)、Y [−12, 8)、Z [−2, 10) m · 显示处理不参与定位估计', fontsize=15)
    save(fig, output, 'map_processing')
    return {'roi_xyz_m': roi.tolist(), 'azimuth_degrees': -50, 'elevation_degrees': 35,
            'height_color_range_m': [-2,10], 'point_size_pt2': .65, 'all_local_points_drawn': True,
            'projection_displacement_p95_mm': float(np.percentile(movement,95)),
            'projection_displacement_max_mm': float(movement.max()), 'stages': dict(zip([s[0] for s in stages], counts))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'figures')
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    setup_style()
    engineering_comparison(args.output_dir)
    local = map_processing(args.output_dir)
    inputs = [ROOT / 'data/map_processing/local_comparison.npz', ROOT / 'docs/viewer/data/processing_report.json']
    for directory in ('tcp','recording','surf03'):
        inputs.extend(ROOT / 'data/outdoor01' / directory / name for name in
                      ('metrics.json', 'resources.jsonl', 'position_comparison.npz', 'algorithm_logs/fast_lio_time_log.csv'))
    manifest = {'map_processing': local,
        'inputs_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs},
        'scope': 'Saved single-run measurements and real point coordinates; no generated geometry or trajectory smoothing.'}
    (args.output_dir / 'engineering_figure_manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print('Saved engineering_comparison and map_processing (PNG/SVG).')


if __name__ == '__main__':
    main()
