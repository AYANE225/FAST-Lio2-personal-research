"""Create traceable figures from the saved Outdoor01 measurements."""
import argparse
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
from matplotlib import colors, font_manager
import matplotlib.pyplot as plt
import numpy as np
from scipy.spatial.transform import Rotation

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'data/outdoor01/tcp'
BASELINE = ROOT / 'data/outdoor01/udp'
OUTPUT = ROOT / 'figures'
MAP_PCD = RUN / 'map.pcd'
FONT = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
font_manager.fontManager.addfont(FONT)
plt.rcParams.update({'font.family': font_manager.FontProperties(fname=FONT).get_name(),
                     'font.size': 10, 'axes.titlesize': 12, 'axes.labelsize': 10,
                     'axes.unicode_minus': False, 'svg.fonttype': 'path',
                     'savefig.facecolor': 'white'})
BLUE, ORANGE, RED, GRAY = '#0072B2', '#D55E00', '#C23B3A', '#737373'
MANIFEST = {'sequence': 'M3DGR Outdoor01', 'primary_run': 'data/outdoor01/tcp',
            'baseline_run': 'data/outdoor01/udp', 'figures': {}}


def save_figure(fig, name, description):
    for suffix in ('png', 'svg'):
        fig.savefig(OUTPUT / f'{name}.{suffix}', dpi=200, facecolor=fig.get_facecolor())
    plt.close(fig)
    MANIFEST['figures'][name] = description
    print(f'Saved {name}', flush=True)


def grid(axis):
    axis.grid(alpha=0.22, linewidth=0.6)
    axis.set_axisbelow(True)


def accuracy_figure():
    data = np.load(RUN / 'position_comparison.npz')
    ref, est, errors = data['reference_xyz'], data['aligned_xyz'], data['error_m']
    t = data['timestamps'] - data['timestamps'][0]
    residual = est - ref
    assert np.allclose(residual, data['residual_xyz_m'], atol=1e-12)
    assert np.allclose(np.linalg.norm(residual, axis=1), errors, atol=1e-12)
    rmse, p95, maximum = np.sqrt(np.mean(errors ** 2)), np.percentile(errors, 95), errors.max()
    fig, axes = plt.subplots(2, 2, figsize=(13, 9), constrained_layout=True)
    ax = axes[0, 0]
    ax.plot(ref[:, 0], ref[:, 1], color='#222222', lw=2.2, label='RTK 参考轨迹', zorder=1)
    points = ax.scatter(est[:, 0], est[:, 1], c=errors, cmap='cividis', vmin=0, vmax=0.5,
                        s=5, linewidths=0, label='FAST-LIO2（颜色表示误差）', zorder=2)
    ax.scatter(est[0, 0], est[0, 1], marker='o', facecolors='none', edgecolors=RED, s=70, zorder=3)
    ax.set(title='(a) 轨迹与空间误差：全部匹配位置', xlabel='参考坐标 X (m)', ylabel='参考坐标 Y (m)')
    ax.set_aspect('equal', adjustable='datalim')
    ax.legend(loc='lower left', fontsize=8)
    fig.colorbar(points, ax=ax, shrink=0.85, label='平移误差 (m)')
    ax = axes[0, 1]
    ax.plot(t, errors, color=BLUE, lw=0.65)
    ax.axhline(rmse, color=ORANGE, ls='--', lw=1, label=f'RMSE {rmse:.3f} m')
    ax.axhline(p95, color=GRAY, ls=':', lw=1, label=f'p95 {p95:.3f} m')
    ax.set(title='(b) 全程误差：纵轴从零开始，不平滑', xlabel='距首个匹配位姿的时间 (s)',
           ylabel='平移误差 (m)', xlim=(0, 412), ylim=(0, 0.5))
    ax.legend(fontsize=9)
    ax = axes[1, 0]
    sorted_errors = np.sort(errors)
    cdf = np.arange(1, len(errors) + 1) / len(errors) * 100
    ax.step(np.r_[0, sorted_errors], np.r_[0, cdf], where='post', color=BLUE, lw=1.5)
    ax.axhline(95, color=GRAY, ls=':', lw=0.8)
    ax.axvline(p95, color=GRAY, ls=':', lw=0.8)
    thresholds = (0.1, 0.2, 0.3, 0.5)
    percentages = {str(threshold): float(np.mean(errors <= threshold) * 100) for threshold in thresholds}
    annotation = '\n'.join(f'误差 ≤ {threshold:.1f} m：{percentages[str(threshold)]:.2f}%' for threshold in thresholds)
    ax.text(0.98, 0.04, annotation, transform=ax.transAxes, ha='right', va='bottom', fontsize=9,
            bbox={'facecolor': 'white', 'edgecolor': '#cccccc', 'alpha': 0.95})
    ax.set(title='(c) 经验累计分布：直接统计全部 4,107 个样本', xlabel='误差阈值 (m)',
           ylabel='不超过该误差的样本比例 (%)', xlim=(0, 0.5), ylim=(0, 100))
    ax = axes[1, 1]
    for i, (label, color) in enumerate(zip(('X', 'Y', 'Z'), (BLUE, ORANGE, '#009E73'))):
        ax.plot(t, residual[:, i], color=color, lw=0.7, label=f'{label} 分量')
    ax.axhline(0, color='#333333', lw=0.7)
    ax.set(title='(d) 有符号位置残差：同一纵轴尺度', xlabel='距首个匹配位姿的时间 (s)',
           ylabel='估计位置 − 参考位置 (m)', xlim=(0, 412), ylim=(-0.5, 0.5))
    ax.legend(ncol=3, fontsize=9)
    for ax in axes.flat:
        grid(ax)
    fig.suptitle('Outdoor01 / MID-360：位置误差参考评估\n'
                 '全程一次 SE(3) 对齐；尺度固定为 1；未拟合时间偏移；未补偿 RTK 天线杆臂', fontsize=13)
    save_figure(fig, 'accuracy', {'samples': len(errors), 'rmse_m': float(rmse), 'p95_m': float(p95),
                                'maximum_m': float(maximum), 'within_threshold_percent': percentages,
                                'smoothing': False, 'error_axis_m': [0, 0.5],
                                'gt_orientation_used': False, 'excluded_poses': 2,
                                'exclusion_reason': 'Last two poses exceed GT time range.'})


def read_run(path, source_t):
    timing = np.loadtxt(path / 'algorithm_logs/fast_lio_time_log.csv', delimiter=',', skiprows=1)
    indices = np.searchsorted(source_t, timing[:, 0])
    indices = np.minimum(indices, len(source_t) - 1)
    left = np.maximum(indices - 1, 0)
    indices = np.where(abs(source_t[left] - timing[:, 0]) < abs(source_t[indices] - timing[:, 0]), left, indices)
    assert abs(source_t[indices] - timing[:, 0]).max() < 1e-5
    assert np.all(np.diff(indices) > 0)
    processed = np.zeros(len(source_t), dtype=bool)
    processed[indices] = True
    resources = [json.loads(line) for line in (path / 'resources.jsonl').read_text().splitlines()]
    return {'indices': indices, 'processed': processed, 'timing': timing,
            'resources': resources, 'first_processed': int(indices[0])}


def coverage_figure(source_t, runs):
    elapsed = source_t - source_t[0]
    edges = np.r_[elapsed, elapsed[-1] + np.median(np.diff(elapsed))]
    fig, axes = plt.subplots(2, 1, figsize=(13, 6.5), constrained_layout=True)
    state = np.zeros((2, len(source_t)), dtype=int)
    deficits = {}
    for row, (label, run) in enumerate(runs.items()):
        state[row, ~run['processed']] = 2
        state[row, :run['first_processed']] = 1
        missing = ~run['processed']
        missing[:run['first_processed']] = False
        cumulative = np.cumsum(missing)
        deficits[label] = int(cumulative[-1])
        axes[1].step(elapsed, cumulative, where='post', lw=1.3,
                     color=ORANGE if row == 0 else BLUE, label=f'{label}：累计 {cumulative[-1]} 帧')
    cmap = colors.ListedColormap([BLUE, '#c4c4c4', RED])
    axes[0].pcolormesh(edges, np.arange(3), state, cmap=cmap, vmin=0, vmax=2, shading='flat', rasterized=True)
    axes[0].set(yticks=[0.5, 1.5], yticklabels=list(runs), ylim=(2, 0), xlim=(0, edges[-1]),
                title='(a) 逐帧处理记录：每个输入扫描都有对应状态', xlabel='距首帧点云起始时间 (s)')
    from matplotlib.patches import Patch
    axes[0].legend(handles=[Patch(color=BLUE, label='有算法处理记录'),
                            Patch(color='#c4c4c4', label='初始化前 7 帧'),
                            Patch(color=RED, label='初始化后缺少处理记录')],
                   loc='upper center', bbox_to_anchor=(0.5, -0.22), ncol=3, fontsize=9)
    axes[1].set(title='(b) 初始化后的累计缺帧数：纵轴从零开始',
                xlabel='距首帧点云起始时间 (s)', ylabel='累计未处理帧数',
                xlim=(0, edges[-1]), ylim=(0, 350))
    axes[1].legend(loc='upper left')
    grid(axes[1])
    fig.suptitle('Outdoor01：同一算法、同一数据、相同参数，两次完整 1× 回放\n'
                 '输入 4,116 帧；以算法计时日志与输入时间戳匹配判定处理覆盖率', fontsize=13)
    save_figure(fig, 'processing_coverage', {'input_frames': len(source_t),
                                           'missing_after_initialization': deficits,
                                           'one_cell_per_input_frame': True,
                                           'trials_per_transport': 1})


def runtime_figure(source_t, runs):
    fig, axes = plt.subplots(3, 1, figsize=(13, 9), constrained_layout=True)
    for row, (label, run) in enumerate(runs.items()):
        color = ORANGE if row == 0 else BLUE
        data = run['timing']
        elapsed = data[:, 0] - source_t[0]
        axes[0].scatter(elapsed, data[:, 1] * 1000, s=3, color=color, alpha=0.7,
                        linewidths=0, label=f'{label}：{len(data):,} 帧', rasterized=True)
        resources = run['resources']
        t = [item['elapsed_s'] for item in resources]
        axes[1].plot(t, [item['cpu_percent'] for item in resources], color=color, lw=0.9, label=label)
        axes[2].plot(t, [item['rss_bytes'] / 1048576 for item in resources], color=color, lw=1.2, label=label)
    axes[0].axhline(100, color=GRAY, ls='--', lw=1, label='10 Hz 输入帧周期：100 ms')
    axes[0].set(title='(a) 各帧核心处理耗时：每个记录绘为一个点', ylabel='耗时 (ms)', ylim=(0, 110))
    axes[1].set(title='(b) mapping 进程 CPU：100% 表示占满一个逻辑核', ylabel='CPU (%)', ylim=(0, 100))
    axes[2].set(title='(c) mapping 进程 RSS：约每秒采样一次', ylabel='RSS (MiB)', ylim=(0, 800))
    for i, axis in enumerate(axes):
        axis.set_xlim(0, 425)
        axis.set_xlabel('点云时间 (s)' if i == 0 else '从播放器启动计时 (s)')
        axis.legend(loc='upper left', fontsize=8, ncol=3 if i == 0 else 2)
        grid(axis)
    fig.suptitle('Outdoor01：完整耗时与资源记录，纵轴均从零开始\n'
                 '核心耗时不等于端到端延迟；资源采样未覆盖退出保存地图；每种传输仅一轮', fontsize=13)
    save_figure(fig, 'runtime', {'cpu_axis_percent': [0, 100], 'rss_axis_mib': [0, 800],
                               'core_time_axis_ms': [0, 110], 'smoothing': False,
                               'cpu_percent_convention': '100% equals one logical CPU',
                               'end_to_end_latency_measured': False})


def map_density(cell_size_m=0.1):
    """Count every map point in a gravity-aligned horizontal grid."""
    path = MAP_PCD
    if not path.exists():
        cached = np.load(RUN / 'map_density.npz', allow_pickle=False)
        details = json.loads((RUN / 'map_projection.json').read_text())
        counts = cached['counts']
        assert int(counts.sum(dtype=np.uint64)) == details['points']
        assert np.isclose(cached['cell_size_m'], cell_size_m)
        assert np.allclose(cached['gravity_rotation'], details['gravity_rotation'])
        MANIFEST['map_projection'] = details
        MANIFEST['map_input'] = 'Saved full-map grid counts; raw PCD is not included in this repository.'
        return counts, cached['extent_m'], cached['trajectory_xyz_m'], details
    header = {}
    with path.open('rb') as stream:
        while True:
            line = stream.readline()
            if not line:
                raise ValueError('PCD header has no DATA entry')
            fields = line.decode('ascii').strip().split()
            if fields and not fields[0].startswith('#'):
                header[fields[0]] = fields[1:]
            if fields and fields[0] == 'DATA':
                offset = stream.tell()
                break
    assert header['DATA'] == ['binary']
    assert header['FIELDS'][:3] == ['x', 'y', 'z']
    assert set(header['SIZE']) == {'4'} and set(header['TYPE']) == {'F'}
    assert set(header['COUNT']) == {'1'}
    count, columns = int(header['POINTS'][0]), len(header['FIELDS'])
    assert path.stat().st_size == offset + count * columns * 4
    points = np.memmap(path, dtype='<f4', mode='r', offset=offset, shape=(count, columns))
    gravity = np.loadtxt(RUN / 'algorithm_logs/mat_out.txt')[0, -4:-1]
    rotation, _ = Rotation.align_vectors([[0, 0, -1]], [gravity / np.linalg.norm(gravity)])
    rotation = rotation.as_matrix()
    assert np.allclose(rotation @ (gravity / np.linalg.norm(gravity)), [0, 0, -1])
    minimum, maximum = np.full(3, np.inf), np.full(3, -np.inf)
    block_size = 262144
    for start in range(0, count, block_size):
        xyz = np.asarray(points[start:start + block_size, :3], dtype=np.float64) @ rotation.T
        assert np.isfinite(xyz).all()
        minimum = np.minimum(minimum, xyz.min(axis=0))
        maximum = np.maximum(maximum, xyz.max(axis=0))
    lower = np.floor(minimum[:2] / cell_size_m).astype(np.int64)
    upper = np.floor(maximum[:2] / cell_size_m).astype(np.int64) + 1
    shape_xy = upper - lower
    counts = np.zeros((shape_xy[1], shape_xy[0]), dtype=np.uint32)
    for start in range(0, count, block_size):
        xyz = np.asarray(points[start:start + block_size, :3], dtype=np.float64) @ rotation.T
        indices = np.floor(xyz[:, :2] / cell_size_m).astype(np.int64) - lower
        np.add.at(counts, (indices[:, 1], indices[:, 0]), 1)
    assert int(counts.sum(dtype=np.uint64)) == count
    extent = np.array([lower[0], upper[0], lower[1], upper[1]]) * cell_size_m
    trajectory = np.loadtxt(RUN / 'trajectory_tum.txt')[:, 1:4] @ rotation.T
    bounds = [trajectory[:, 0].min() - 35, trajectory[:, 0].max() + 35,
              trajectory[:, 1].min() - 35, trajectory[:, 1].max() + 35]
    x_centers = (np.arange(shape_xy[0]) + lower[0] + 0.5) * cell_size_m
    y_centers = (np.arange(shape_xy[1]) + lower[1] + 0.5) * cell_size_m
    x_mask = (x_centers >= bounds[0]) & (x_centers <= bounds[1])
    y_mask = (y_centers >= bounds[2]) & (y_centers <= bounds[3])
    view_count = int(counts[np.ix_(y_mask, x_mask)].sum(dtype=np.uint64))
    np.savez_compressed(OUTPUT / 'map_density.npz', counts=counts, extent_m=extent,
                        cell_size_m=cell_size_m, gravity_rotation=rotation, trajectory_xyz_m=trajectory)
    details = {'points': count, 'cell_size_m': cell_size_m, 'sum_of_grid_counts': int(counts.sum()),
               'gravity_m_s2': gravity.tolist(), 'gravity_rotation': rotation.tolist(),
               'full_extent_xy_m': extent.tolist(), 'minimum_xyz_m': minimum.tolist(),
               'maximum_xyz_m': maximum.tolist(), 'portfolio_view_xy_m': [float(x) for x in bounds],
               'portfolio_view_grid_count': view_count,
               'portfolio_view_grid_count_percent': view_count / count * 100,
               'map_type': 'Accumulated transformed prefiltered scans, not a deduplicated map.',
               'display_transform': 'Rotate initial estimated gravity to -Z; no GT fit; no translation.',
               'height_filter': None, 'point_subsampling': None,
               'color_value': 'Number of points per 0.1 m x 0.1 m XY cell, logarithmic color scale.'}
    MANIFEST['map_projection'] = details
    print(json.dumps(details, ensure_ascii=False), flush=True)
    return counts, extent, trajectory, details


def draw_map(axis, map_data, full_extent=False):
    counts, extent, trajectory, details = map_data
    cmap = colors.LinearSegmentedColormap.from_list(
        'point_count', ['#dce6ed', '#9bb8ca', '#487b99', '#173f5b', '#081e30'])
    cmap.set_bad('#ffffff')
    projection = axis.imshow(np.ma.masked_equal(counts, 0), origin='lower', extent=extent,
                             cmap=cmap, norm=colors.LogNorm(vmin=1, vmax=int(counts.max())),
                             interpolation='nearest', rasterized=True)
    axis.plot(trajectory[:, 0], trajectory[:, 1], color='#e27619', lw=1.5, label='估计轨迹')
    axis.scatter(*trajectory[0, :2], s=38, c='#008c78', marker='o', edgecolors='white',
                 linewidths=0.8, label='起点', zorder=4)
    bounds = extent if full_extent else details['portfolio_view_xy_m']
    axis.set(xlim=bounds[:2], ylim=bounds[2:], xlabel='X (m)', ylabel='Y (m)')
    axis.set_aspect('equal', adjustable='box')
    axis.legend(loc='upper right', fontsize=8, framealpha=0.95)
    axis.tick_params(labelsize=8)
    return projection


def map_figure(map_data):
    fig, ax = plt.subplots(figsize=(12, 9), constrained_layout=True)
    projection = draw_map(ax, map_data, full_extent=True)
    fig.colorbar(projection, ax=ax, shrink=0.8, label='每 0.1 m × 0.1 m 网格的点数（对数刻度）')
    ax.set_title('Outdoor01：完整累积地图的水平投影与估计轨迹\n'
                 '全部 8,909,069 点；无点抽样、无高度裁剪；XY 等比例', fontsize=13)
    ax.set_xlabel('重力对齐坐标 X (m)')
    ax.set_ylabel('重力对齐坐标 Y (m)')
    fig.supxlabel('仅将初始估计重力旋转至 −Z；原 PCD 不变。颜色表示点数，不表示精度。', fontsize=10)
    save_figure(fig, 'map_projection', {'full_extent': True, 'all_points_counted': True,
                                       'map_accuracy_evaluated': False})


def portfolio_figure(source_t, runs, map_data):
    from matplotlib.patches import FancyBboxPatch
    metrics = json.loads((RUN / 'metrics.json').read_text())
    summary = json.loads((RUN / 'runtime_summary.json').read_text())
    fig = plt.figure(figsize=(16, 9), facecolor='#f3f6f9')
    dark, muted = '#16354d', '#50677a'

    def text(x, y, value, size=10, color=dark, **kwargs):
        return fig.text(x, y, value, fontsize=size, color=color, **kwargs)

    def panel(x, y, width, height, facecolor='white'):
        fig.add_artist(FancyBboxPatch((x, y), width, height,
                       boxstyle='round,pad=0.006,rounding_size=0.009',
                       transform=fig.transFigure, facecolor=facecolor,
                       edgecolor='#dce4eb', linewidth=0.7, zorder=-1))

    text(0.035, 0.944, 'MID-360 激光惯性定位｜工程集成与验证', size=24, weight='bold')
    text(0.035, 0.899, '基于 FAST-LIO2  ·  M3DGR Outdoor01  ·  ROS 2 Humble  ·  两轮完整 1× 回放',
         size=12, color=muted)
    cards = [
        (f"{summary['bag_duration_s']:.1f} s", '完整序列 · 4,116 帧点云'),
        ('4,109 / 4,109', 'TCP 回放 · 初始化后全部处理'),
        (f"{metrics['core_processing_time_ms']['p95']:.2f} ms", '算法核心处理耗时 p95'),
        (f"{metrics['resources']['observed_peak_rss_mib']:.0f} MiB", 'mapping 采样峰值 RSS'),
    ]
    for i, (value, label) in enumerate(cards):
        x = 0.035 + i * 0.24
        panel(x, 0.784, 0.218, 0.077)
        text(x + 0.012, 0.821, value, size=21, weight='bold')
        text(x + 0.012, 0.796, label, size=9, color=muted)
    panel(0.035, 0.265, 0.517, 0.482)
    panel(0.580, 0.265, 0.390, 0.482)
    text(0.052, 0.715, '真实点云地图与估计轨迹', size=14, weight='bold')
    text(0.052, 0.687, '8,909,069 点累积地图 · 重力对齐俯视 · XY 等比例', size=9, color=muted)
    ax = fig.add_axes([0.072, 0.337, 0.422, 0.317])
    projection = draw_map(ax, map_data)
    cax = fig.add_axes([0.517, 0.352, 0.009, 0.240])
    colorbar = fig.colorbar(projection, cax=cax)
    colorbar.ax.tick_params(labelsize=7)
    colorbar.ax.set_title('点数', fontsize=8, pad=8)
    percent = map_data[3]['portfolio_view_grid_count_percent']
    text(0.052, 0.281, f'视区：轨迹外扩 35 m（含 {percent:.1f}% 点数）；颜色：0.1 m 网格点数，对数刻度。',
         size=8, color=muted)
    text(0.597, 0.715, 'ROS 2 传输问题定位与验证', size=14, weight='bold')
    text(0.597, 0.681, '发现 UDP socket 丢包 → 保持算法与 QoS 不变 → TCP 回放',
         size=9, color=muted)
    deficits = [int((~run['processed'][run['first_processed']:]).sum()) for run in runs.values()]
    text(0.597, 0.626, f'{deficits[0]} → {deficits[1]}', size=28, color=BLUE, weight='bold')
    text(0.788, 0.640, '初始化后缺帧数', size=12)
    text(0.788, 0.613, '两轮均扣除前 7 帧', size=9, color=muted)
    ax = fig.add_axes([0.628, 0.361, 0.322, 0.210])
    for i, (label, run) in enumerate(runs.items()):
        missing = ~run['processed']
        missing[:run['first_processed']] = False
        ax.step(source_t - source_t[0], np.cumsum(missing), where='post',
                label=label, color=ORANGE if i == 0 else BLUE, lw=1.6, zorder=3,
                clip_on=False)
    ax.set(xlim=(0, 415), ylim=(0, 350), xticks=[0, 100, 200, 300, 400],
           yticks=[0, 100, 200, 300], xlabel='回放时间 (s)', ylabel='累计缺帧数')
    ax.tick_params(labelsize=8)
    ax.xaxis.label.set_size(9)
    ax.yaxis.label.set_size(9)
    ax.legend(loc='upper left', fontsize=8, frameon=False)
    for spine in ax.spines.values():
        spine.set_color('#b9c8d2')
    grid(ax)
    text(0.597, 0.281, '逐帧时间戳核对；验证范围为本机 DDS 进程间传输。', size=8, color=muted)
    steps = [
        ('01  传感器数据接入', 'ROS1 → ROS2 消息转换\n核对 CustomMsg 字段与点时间'),
        ('02  ROS 2 系统联调', '节点、topic 与 DDS 配置\n基于日志和套接字排查缺帧'),
        ('03  运行性能分析', '逐帧计时、CPU 与 RSS 采样\n识别地图累积时的内存增长'),
        ('04  可复现实验', '保留配置、源码版本与原始日志\n导出轨迹、地图及 SHA256 清单'),
    ]
    for i, (title, body) in enumerate(steps):
        x = 0.035 + i * 0.24
        panel(x, 0.121, 0.218, 0.111)
        text(x + 0.012, 0.205, title, size=11, weight='bold')
        text(x + 0.012, 0.151, body, size=9, color=muted, linespacing=1.65)
    text(0.035, 0.073, '测试范围：桌面离线回放，每种传输仅一轮。核心耗时不等于端到端延迟；RSS 采样未覆盖退出保存地图。',
         size=9, color=muted)
    text(0.035, 0.039, 'Intel Core Ultra 9 285K · Release · 2026-10-10     原始证据：report.md / metrics.json / algorithm_logs / network_diagnostics.json',
         size=8, color=muted)
    save_figure(fig, 'portfolio_overview', {
        'purpose': 'Robotics localization / ROS 2 engineering portfolio',
        'basis': 'FAST-LIO2 engineering integration and validation; not original SLAM algorithm development',
        'missing_after_initialization': deficits, 'main_view': 'Trajectory bounding box plus 35 m',
        'position_accuracy_claim': None, 'hardware_deployment_validated': False})


def file_record(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    return {'size_bytes': path.stat().st_size, 'sha256': digest.hexdigest()}


def main():
    global OUTPUT, MAP_PCD
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=OUTPUT)
    parser.add_argument('--map-pcd', type=Path, help='Optional original PCD for rebuilding the map grid.')
    args = parser.parse_args()
    OUTPUT = args.output_dir.resolve()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if args.map_pcd:
        MAP_PCD = args.map_pcd.resolve()
        if not MAP_PCD.is_file():
            parser.error(f'PCD file not found: {MAP_PCD}')
    expected = json.loads((ROOT / 'data/checksums.json').read_text())
    for name, record in expected.items():
        assert file_record(ROOT / name) == record, f'Input artifact changed: {name}'
    source_t = np.load(RUN / 'input_timestamps.npz')['lidar'][:, 1] * 1e-9
    runs = {'默认 DDS UDP': read_run(BASELINE, source_t), '本机 DDS TCP': read_run(RUN, source_t)}
    for path, run in zip((BASELINE, RUN), runs.values()):
        metrics = json.loads((path / 'metrics.json').read_text())
        assert len(run['indices']) == metrics['coverage']['processed_frames']
        assert int((~run['processed'][run['first_processed']:]).sum()) == metrics['coverage']['missing_frames_after_initialization']
        assert np.isclose(np.percentile(run['timing'][:, 1] * 1000, 95),
                          metrics['core_processing_time_ms']['p95'])
    accuracy_figure()
    coverage_figure(source_t, runs)
    runtime_figure(source_t, runs)
    map_data = map_density()
    map_figure(map_data)
    portfolio_figure(source_t, runs, map_data)
    MANIFEST['inputs'] = expected
    MANIFEST['script'] = file_record(Path(__file__))
    MANIFEST['software'] = {'numpy': np.__version__, 'matplotlib': matplotlib.__version__}
    MANIFEST['outputs'] = {path.name: file_record(path) for path in sorted(OUTPUT.iterdir())
                           if path.suffix in {'.png', '.svg', '.npz', '.md'}}
    (OUTPUT / 'manifest.json').write_text(json.dumps(MANIFEST, ensure_ascii=False, indent=2) + '\n')
    print('Verified original artifacts and all figure metrics; wrote figures/manifest.json.', flush=True)


if __name__ == '__main__':
    main()
