"""Render responsive homepage figures from verified Outdoor01 measurements.

Position errors are unsmoothed 3-D residual norms in the reference frame.
Recovery panels describe the recorded process-interruption experiments, not
power-loss durability. Performance panels reuse published measurements.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.colors import Normalize
from matplotlib.patches import FancyBboxPatch
import numpy as np

from benchmark_voxel import summarize_trials
from plot_engineering import setup_style
from plot_readme import load_system_results

ROOT = Path(__file__).resolve().parents[1]
BACKGROUND = '#0d0d1c'
PANEL = '#131b2b'
TEXT = '#e7eff5'
MUTED = '#9ab0c1'
GRID = '#263748'
TEAL = '#7fd8cd'
GOLD = '#ffd38b'
CORAL = '#ef9a91'
RUN_COLORS = ('#7893ae', TEAL, GOLD)


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load_position_errors(position_path, metrics_path):
    """Recompute plot statistics and reject contradictory saved evidence."""
    with np.load(position_path, allow_pickle=False) as saved:
        data = {name: np.asarray(saved[name], dtype=float) for name in
                ('timestamps', 'reference_xyz', 'aligned_xyz', 'residual_xyz_m', 'error_m')}
    stamps, errors = data['timestamps'], data['error_m']
    count = len(stamps)
    if (stamps.shape != (count,) or count < 2 or errors.shape != (count,)
            or any(data[name].shape != (count, 3) for name in
                   ('reference_xyz', 'aligned_xyz', 'residual_xyz_m'))
            or not all(np.isfinite(value).all() for value in data.values())
            or np.any(np.diff(stamps) <= 0)):
        raise ValueError('Invalid position samples or timestamps')
    residuals = data['aligned_xyz'] - data['reference_xyz']
    if (not np.allclose(residuals, data['residual_xyz_m'], rtol=1.e-10, atol=1.e-10)
            or not np.allclose(np.linalg.norm(residuals, axis=1), errors,
                               rtol=1.e-10, atol=1.e-10)):
        raise ValueError('Saved position error disagrees with the unsmoothed 3-D residual')
    summary = json.loads(Path(metrics_path).read_text())['position_reference_comparison']
    rmse, p95, maximum = float(np.sqrt(np.mean(errors ** 2))), float(np.percentile(errors, 95)), float(errors.max())
    if (summary['matched_poses'] != count or summary['scale'] != 1.
            or summary['fitted_time_offset_s'] != 0.
            or not np.allclose([rmse, p95, maximum],
                               [summary['rmse_m'], summary['error_m']['p95'], summary['error_m']['maximum']],
                               rtol=1.e-10, atol=1.e-12)):
        raise ValueError('Position statistics or alignment scope disagree with the saved metrics')
    return {**data, 'elapsed_s': stamps - stamps[0], 'matched_poses': count,
            'unmatched_poses': summary['unmatched_poses'], 'rmse_m': rmse,
            'p95_m': p95, 'maximum_m': maximum}


def load_recovery_results(path):
    """Validate experiment states before assigning any success labels."""
    evidence = json.loads(Path(path).read_text())
    if (evidence['schema_version'] != 1 or evidence['expectations_met'] is not True
            or evidence['source']['files_unchanged'] is not True
            or evidence['input']['settings']['fsync'] is not True):
        raise ValueError('Recovery evidence is unverified')
    cases = evidence['cases']
    for name, case in cases.items():
        checks = case['validation']
        if not all(checks[key] is True for key in
                   ('expectations_met', 'block_files_unchanged', 'quarantined_files_unchanged')):
            raise ValueError(f'{name}: persisted data did not pass validation')
        for phase in ('before', 'after'):
            snapshot = case[phase]
            index, blocks = snapshot['index'], snapshot['blocks']
            committed = index['committed_blocks']
            if (committed > len(blocks)
                    or sum(block['written_frames'] for block in blocks[:committed]) != index['written_frames']
                    or sum(block['written_points'] for block in blocks[:committed]) != index['written_points']):
                raise ValueError(f'{name}: index counts disagree with persisted blocks')
        if name == 'corrupted_committed_block':
            if (case['recovery']['outcome'] != 'rejected' or case['recovery']['report'] is not None
                    or case['recovery']['exception_type'] != 'RecordingIntegrityError'
                    or checks['index_file_unchanged'] is not True
                    or case['before']['index'] != case['after']['index']
                    or case['corruption']['original_byte'] == case['corruption']['corrupted_byte']):
                raise ValueError('Corrupted payload must be rejected without index changes')
            continue
        report = case['recovery']['report']
        if (case['recovery']['outcome'] != 'accepted' or report['integrity_ok'] is not True
                or checks['frame_payloads_match_source_prefix'] is not True
                or any(report[key] != case['after']['index'][key] for key in
                       ('written_frames', 'written_points', 'committed_blocks'))):
            raise ValueError(f'{name}: recovery report disagrees with verified data')
        if name.startswith('sigkill_'):
            if (report['completed'] is not False or report['received_counts_verified'] is not False
                    or any(report[key] is not None for key in
                           ('received_frames', 'received_points', 'enqueued_frames', 'enqueued_points',
                            'dropped_frames', 'dropped_points', 'failed_frames', 'failed_points',
                            'pending_frames', 'pending_points'))):
                raise ValueError('Interrupted recordings must remain incomplete with unknown receipt counts')
            if (case['child']['returncode'] != -9 or case['child']['signal'] != 'SIGKILL'
                    or case['child']['boundary_event']['fsync'] is not True):
                raise ValueError('The recorded child must have been interrupted by SIGKILL')
        elif (report['completed'] is not True or report['received_counts_verified'] is not True
              or report['written_frames'] != evidence['source']['selected_frame_count']
              or report['written_points'] != evidence['source']['selected_point_count']):
            raise ValueError('Normal completion must match the selected input')
    normal = cases['normal_completion']['recovery']['report']
    interrupted = cases['sigkill_before_index']
    temporary = cases['sigkill_partial_temporary_block']
    before, after = interrupted['before']['index'], interrupted['after']['index']
    report = interrupted['recovery']['report']
    partial = temporary['recovery']['report']
    if (report['adopted_blocks'] != after['committed_blocks'] - before['committed_blocks']
            or partial['adopted_blocks'] != 0
            or partial['quarantined_temporary_blocks'] != len(temporary['after']['quarantined_blocks'])
            or temporary['before']['index'] != temporary['after']['index']
            or interrupted['child']['boundary_event']['condition'] != 'complete_block_before_index'
            or temporary['child']['boundary_event']['condition'] != 'partial_temporary_npy'):
        raise ValueError('Recovery outcomes disagree with the injection boundaries')
    return {
        'normal_frames': normal['written_frames'], 'normal_points': normal['written_points'],
        'normal_blocks': normal['committed_blocks'],
        'frames_per_block': evidence['parameters']['frames_per_block'],
        'indexed_frames_before': before['written_frames'], 'verified_frames_after': after['written_frames'],
        'indexed_points_before': before['written_points'], 'verified_points_after': after['written_points'],
        'indexed_blocks_before': before['committed_blocks'], 'verified_blocks_after': after['committed_blocks'],
        'adopted_blocks': report['adopted_blocks'],
        'quarantined_blocks': partial['quarantined_temporary_blocks'],
        'temporary_case_frames': partial['written_frames'], 'temporary_case_points': partial['written_points'],
        'corruption_rejected': True, 'interrupted_received_counts_verified': False,
    }


def text(figure, x, y, value, size=24, color=TEXT, weight='normal', **kwargs):
    width, height = figure.get_size_inches() * figure.dpi
    return figure.text(x / width, 1 - y / height, value, fontsize=size * .72,
                       color=color, weight=weight, va='top', **kwargs)


def panel(figure, x, y, width, height, color=PANEL):
    fw, fh = figure.get_size_inches() * figure.dpi
    patch = FancyBboxPatch((x / fw, 1 - (y + height) / fh), width / fw, height / fh,
                          boxstyle='round,pad=0.0,rounding_size=0.015',
                          transform=figure.transFigure, facecolor=color, edgecolor=GRID,
                          linewidth=.8, zorder=-1)
    figure.add_artist(patch)


def axes(figure, x, y, width, height, tick_size=20):
    fw, fh = figure.get_size_inches() * figure.dpi
    axis = figure.add_axes([x / fw, 1 - (y + height) / fh, width / fw, height / fh], facecolor=BACKGROUND)
    axis.tick_params(colors=MUTED, labelsize=tick_size * .72, length=0, pad=8)
    axis.grid(color=GRID, lw=.7, alpha=.85)
    axis.set_axisbelow(True)
    for side in ('left', 'bottom'):
        axis.spines[side].set_color(GRID)
    return axis


def figure_header(number, title, subtitle, mobile, height):
    width = 800 if mobile else 1600
    figure = plt.figure(figsize=(width / 100, height / 100), dpi=100, facecolor=BACKGROUND)
    left = 36 if mobile else 56
    text(figure, left, 28, number, 21 if mobile else 20, TEAL)
    text(figure, left, 70, title, 42 if mobile else 46, weight='bold')
    text(figure, left, 133, subtitle, 22, MUTED)
    return figure


def save_figure(figure, directory, name):
    path = directory / f'{name}.png'
    figure.savefig(path, dpi=100, facecolor=BACKGROUND)
    plt.close(figure)
    return path


def plot_position(position, directory, mobile):
    figure = figure_header('02 / POSITION ERROR', '误差沿路线如何变化',
                           f"Outdoor01 · 分块 0.5 m · {position['matched_poses']:,} 个匹配位置",
                           mobile, 1450 if mobile else 1040)
    left, gap, card_width = (36, 16, 232) if mobile else (56, 28, 477)
    for index, (label, value, color) in enumerate([
            ('RMSE / m', position['rmse_m'], TEAL), ('p95 / m', position['p95_m'], GOLD),
            ('最大值 / m', position['maximum_m'], CORAL)]):
        x = left + index * (card_width + gap)
        panel(figure, x, 187, card_width, 115)
        text(figure, x + 22, 202, label, 22, MUTED)
        text(figure, x + 22, 240, f'{value:.3f}', 42, color, 'bold')
    if mobile:
        route = axes(figure, 100, 380, 645, 280)
        time_axis = axes(figure, 100, 835, 645, 180)
        distribution = axes(figure, 100, 1125, 645, 175)
        color_axis = figure.add_axes([100 / 800, 1 - 710 / 1450, 645 / 800, 12 / 1450])
        text(figure, 36, 333, '参考坐标系中的轨迹 · 颜色表示三维误差', 22)
        text(figure, 36, 787, '全部匹配样本 · 未平滑', 24)
        text(figure, 36, 1077, '误差累计分布', 24)
    else:
        route = axes(figure, 120, 394, 925, 286)
        distribution = axes(figure, 1180, 394, 345, 286)
        time_axis = axes(figure, 120, 824, 1405, 120)
        color_axis = figure.add_axes([120 / 1600, 1 - 733 / 1040, 925 / 1600, 12 / 1040])
        text(figure, 56, 343, '参考坐标系中的轨迹 · 颜色表示三维误差', 25)
        text(figure, 1150, 343, '误差累计分布', 25)
        text(figure, 56, 776, '全部匹配样本 · 未平滑', 25)
    xy = position['reference_xyz'][:, :2]
    error_limit = max(.4, np.ceil(position['maximum_m'] / .1) * .1)
    route.plot(xy[:, 0], xy[:, 1], color=GRID, lw=1, zorder=1)
    cloud = route.scatter(xy[:, 0], xy[:, 1], c=position['error_m'], s=8 if mobile else 10,
                          cmap='viridis', norm=Normalize(0, error_limit), edgecolors='none', zorder=2)
    route.set_aspect('equal', adjustable='box')
    # Put the X label above the panel; the space below is reserved for the
    # quantitative color scale, including on narrow layouts.
    text(figure, 745 if mobile else 1045, 361 if mobile else 367,
         '参考 X / m', 18, MUTED, ha='right')
    route.set_ylabel('参考 Y / m', labelpad=8)
    route.margins(.07)
    colorbar = figure.colorbar(cloud, cax=color_axis, orientation='horizontal', ticks=np.linspace(0, error_limit, 5))
    colorbar.outline.set_visible(False)
    colorbar.ax.tick_params(labelsize=18 * .72, colors=MUTED, length=0, pad=6)
    colorbar.set_label('三维位置残差范数 / m', fontsize=18 * .72, color=MUTED, labelpad=3)
    sorted_errors = np.sort(position['error_m'])
    distribution.step(np.r_[0, sorted_errors], np.r_[0, np.arange(1, len(sorted_errors) + 1) / len(sorted_errors) * 100],
                      where='post', color=TEAL, lw=2.)
    distribution.axhline(95, color=GOLD, ls='--', lw=.9)
    distribution.scatter([position['p95_m']], [95], color=GOLD, s=30, zorder=3)
    distribution.set(xlim=(0, error_limit), ylim=(0, 102), yticks=[0, 50, 95], xticks=np.linspace(0, error_limit, 5))
    distribution.set_xlabel('误差 / m', loc='right', labelpad=7)
    distribution.set_ylabel('累计比例 / %', labelpad=7)
    time_axis.plot(position['elapsed_s'], position['error_m'], color=TEAL, lw=1.1)
    time_axis.fill_between(position['elapsed_s'], 0, position['error_m'], color=TEAL, alpha=.11)
    time_axis.axhline(position['p95_m'], color=GOLD, lw=.9, ls='--')
    time_axis.set(xlim=(0, position['elapsed_s'][-1]), ylim=(0, error_limit * 1.05), yticks=[0, .2, .4])
    time_axis.set_xlabel('首个匹配位置之后 / s', loc='right', labelpad=8)
    time_axis.set_ylabel('三维误差 / m', labelpad=8)
    text(figure, 745 if mobile else 1525, 789 if mobile else 778,
         f"虚线：p95 {position['p95_m']:.3f} m", 20, GOLD, ha='right')
    footers = (['全程一次 SE(3) 对齐 · 固定尺度 1 · 不拟合时间偏移',
                f"RTK 杆臂未补偿；不评价姿态。末尾 {position['unmatched_poses']} 帧无参考。"]
               if mobile else ['全程一次 SE(3) 对齐 · 固定尺度 1 · 不拟合时间偏移 · RTK 杆臂未补偿 · 仅位置比较'])
    for index, line in enumerate(footers):
        text(figure, 36 if mobile else 56, (1355 if mobile else 996) + 33 * index, line, 20, MUTED)
    return save_figure(figure, directory, 'outdoor01_position_error' + ('_mobile' if mobile else ''))


def plot_recovery(result, directory, mobile):
    subtitle = f"Outdoor01 前 {result['normal_frames']} 帧 · {result['normal_points']:,} 点 · 每块 {result['frames_per_block']} 帧"
    figure = figure_header('03 / RECORDING RECOVERY', '中断后的数据恢复', subtitle,
                           mobile, 1470 if mobile else 940)
    left, width = (36, 728) if mobile else (56, 1488)
    panel(figure, left, 195, width, 393 if mobile else 320)
    text(figure, left + 26, 221, '第三块已落盘、索引更新前：SIGKILL', 26 if mobile else 28)
    text(figure, left + 26, 276, f"{result['indexed_frames_before']} → {result['verified_frames_after']} 帧", 68, TEAL, 'bold')
    text(figure, left + 28, 369, '恢复前索引 → 恢复后校验', 22, MUTED)
    block_start = left + 28 if mobile else 700
    block_top = 427 if mobile else 283
    block_width = 204 if mobile else 243
    for index in range(result['verified_blocks_after']):
        x = block_start + index * (block_width + 16)
        color = TEAL if index < result['indexed_blocks_before'] else GOLD
        panel(figure, x, block_top, block_width, 81, BACKGROUND)
        text(figure, x + 16, block_top + 12, f"块 {index + 1:02d} · {result['frames_per_block']} 帧", 24, color, 'bold')
        text(figure, x + 16, block_top + 46, '已有索引' if index < result['indexed_blocks_before'] else '校验后补入索引', 18, MUTED)
    text(figure, left + 28 if mobile else 700, 542 if mobile else 406,
         f"{result['verified_points_after']:,} 点已校验 · 补入 {result['adopted_blocks']} 个完整块 · fsync 开启", 22, MUTED)
    cases = [
        ('正常收尾', f"{result['normal_frames']} 帧完整", f"{result['normal_blocks']} 个块 · {result['normal_points']:,} 点", '接收总数与写入总数一致', TEAL),
        ('临时块半写入后 SIGKILL', f"隔离 {result['quarantined_blocks']} 块", f"保留 {result['temporary_case_frames']} 帧 · {result['temporary_case_points']:,} 点", '临时文件原字节保留以便检查', GOLD),
        ('已提交块修改 1 字节', '拒绝恢复', 'SHA256 校验检出损坏', '索引和损坏文件均未覆盖', CORAL),
    ]
    for index, (label, outcome, count, note, color) in enumerate(cases):
        x, y, card_width, card_height = ((36, 618 + 234 * index, 728, 212) if mobile
                                         else (56 + index * 504, 555, 480, 238))
        panel(figure, x, y, card_width, card_height)
        text(figure, x + 26, y + 22, label, 25, MUTED)
        text(figure, x + 26, y + 66, outcome, 43, color, 'bold')
        text(figure, x + 26, y + 126, count, 25)
        text(figure, x + 26, y + 166, note, 21, MUTED)
    if mobile:
        text(figure, 36, 1341, '中断记录仍标记未完成；接收总量未知。', 24, GOLD)
        text(figure, 36, 1386, '进程级中断实验 · 未测试整机掉电', 22, MUTED)
        text(figure, 36, 1420, '每场景独立运行；没有恢复内存队列或续写旧记录。', 20, MUTED)
    else:
        text(figure, 56, 830, '中断记录仍标记未完成；接收总量未知。', 27, GOLD)
        text(figure, 56, 882, '进程级中断实验 · 未测试整机掉电 · 每场景独立运行 · 没有恢复内存队列或续写旧记录', 22, MUTED)
    return save_figure(figure, directory, 'outdoor01_recovery' + ('_mobile' if mobile else ''))


def bars(axis, labels, values, colors, limit, digits, ticks, trials=None):
    positions = np.arange(len(values))
    axis.barh(positions, values, height=.48, color=colors, zorder=2)
    axis.set(xlim=(0, limit), ylim=(len(values) - .45, -.6), yticks=positions, yticklabels=labels, xticks=ticks)
    axis.grid(axis='y', visible=False)
    for index, value in enumerate(values):
        axis.text(value + limit * .025, index, f'{value:.{digits}f}', color=TEXT,
                  fontsize=21 * .72, va='center', weight='bold')
    if trials:
        for index, samples in enumerate(trials):
            axis.scatter(samples, index + np.linspace(-.11, .11, len(samples)),
                         s=30, facecolor=BACKGROUND, edgecolor=TEXT, lw=.9, zorder=3)


def plot_performance(results, voxel, benchmark, directory, mobile):
    figure = figure_header('04 / PERFORMANCE', '效率与精度的取舍',
                           'Outdoor01 · 每配置一次完整 1× 回放' if mobile else
                           'Outdoor01 · 每配置一次完整 1× 回放 · Core Ultra 9 285K · ROS 2 Humble · Release',
                           mobile, 1990 if mobile else 1150)
    reduction = (1 - results['recording']['combined_peak_rss_mib'] / results['tcp']['combined_peak_rss_mib']) * 100
    text(figure, 36 if mobile else 56, 193, f'分块 0.5 m：采样峰值内存降低 {reduction:.1f}%，核心耗时增加',
         25 if mobile else 29, TEAL)
    text(figure, 36 if mobile else 56, 241,
         f"初始化后缺帧 {results['udp']['missing_frames_after_initialization']} → {results['tcp']['missing_frames_after_initialization']}（UDP → TCP）",
         23 if mobile else 26, MUTED)
    specs = [
        ('采样峰值 RSS / MiB', 'combined_peak_rss_mib', 850, 1, [0, 200, 400, 600]),
        ('核心处理 p95 / ms', 'core_p95_ms', 18, 2, [0, 5, 10, 15]),
        ('位置参考 RMSE / m', 'position_reference_rmse_m', .34, 3, [0, .1, .2, .3]),
    ]
    for index, (title, key, limit, digits, ticks) in enumerate(specs):
        if mobile:
            top = 302 + index * 213
            text(figure, 36, top, title, 27)
            axis = axes(figure, 188, top + 57, 535, 124, 22)
        else:
            x = 56 + index * 508
            text(figure, x, 318, title, 28)
            axis = axes(figure, x + 119, 387, 337, 170)
        bars(axis, ('原 TCP', '分块 0.5 m', '分块 0.3 m'),
             [results[run][key] for run in ('tcp', 'recording', 'surf03')], RUN_COLORS, limit, digits, ticks)
    if mobile:
        notes = ['0.5 / 0.3 m 为扫描体素；RSS 约 1 Hz，分块配置含两进程。',
                 '原 TCP 未覆盖退出保存；分块配置覆盖受控退出。',
                 '核心计时不含输入预处理、发布、写盘。',
                 '位置：全程 SE(3)，尺度固定；RTK 杆臂未补偿。']
        for index, note in enumerate(notes):
            text(figure, 36, 955 + index * 32, note, 20, MUTED)
        text(figure, 36, 1086, 'Core Ultra 9 285K · ROS 2 Humble · Release', 20, MUTED)
        cpp_y = 1128
    else:
        text(figure, 56, 611, '0.5 / 0.3 m 为扫描体素。RSS 约 1 Hz，分块配置含 mapping + recorder；原 TCP 未覆盖退出保存。', 21, MUTED)
        text(figure, 56, 647, '核心计时不含输入预处理、发布、写盘。位置：全程 SE(3)，尺度固定，RTK 杆臂未补偿。', 21, MUTED)
        cpp_y = 729
    text(figure, 36 if mobile else 56, cpp_y, 'C++17 离线体素处理', 35, weight='bold')
    text(figure, 36 if mobile else 56, cpp_y + 54,
         f"{benchmark['input']['points']:,} 点 · 每档每实现 {benchmark['repeats']} 次 · 圆点为单次测量", 22, MUTED)
    for index, row in enumerate(voxel):
        if mobile:
            top = cpp_y + 110 + index * 191
            text(figure, 36, top, f"{row['voxel_size_m']:.2f} m  ·  {row['median_speedup_numpy_over_cpp']:.2f}×", 28, TEAL)
            axis = axes(figure, 152, top + 52, 571, 101, 22)
        else:
            x = 56 + index * 508
            text(figure, x, cpp_y + 114, f"{row['voxel_size_m']:.2f} m  ·  {row['median_speedup_numpy_over_cpp']:.2f}×", 29, TEAL)
            axis = axes(figure, x + 104, cpp_y + 170, 352, 95)
        bars(axis, ('NumPy', 'C++'), [row[backend]['median_seconds'] for backend in ('numpy', 'cpp')],
             RUN_COLORS[:2], 2.42, 3, [0, 1, 2], [row[backend]['seconds'] for backend in ('numpy', 'cpp')])
        axis.set_xlabel('耗时中位数 / s', loc='right', labelpad=8)
    foot_y = 1882 if mobile else 1061
    text(figure, 36 if mobile else 56, foot_y,
         f"{len(benchmark['comparisons'])} 组配对：计数与 float32 质心一致。倍数 = NumPy / C++ 耗时中位数。",
         20 if mobile else 23, MUTED)
    text(figure, 36 if mobile else 56, foot_y + 38,
         '单地图离线计时；不代表在线 SLAM 提速或定位精度提升。', 22, GOLD)
    return save_figure(figure, directory, 'outdoor01_performance' + ('_mobile' if mobile else ''))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error('Use a new directory to preserve earlier figures')
    position_path = ROOT / 'data/outdoor01/recording/position_comparison.npz'
    metrics_path = ROOT / 'data/outdoor01/recording/metrics.json'
    recovery_path = ROOT / 'data/outdoor01/recovery/evidence.json'
    benchmark_path = ROOT / 'data/career/voxel_benchmark.json'
    inputs = {position_path, metrics_path, recovery_path, benchmark_path}
    position = load_position_errors(position_path, metrics_path)
    recovery = load_recovery_results(recovery_path)
    system = load_system_results(inputs)
    benchmark = json.loads(benchmark_path.read_text())
    voxel = summarize_trials(benchmark['trials'], [row['voxel_size_m'] for row in benchmark['summary']], benchmark['repeats'])
    if (voxel != benchmark['summary'] or len(benchmark['comparisons']) != len(voxel) * benchmark['repeats']
            or not all(row['counts_equal'] and row['xyz_max_abs_difference_m'] == 0
                       and row['intensity_max_abs_difference'] == 0 for row in benchmark['comparisons'])):
        raise ValueError('Voxel comparison labels are unsupported by the measurements')
    setup_style()
    plt.rcParams.update({'font.size': 20 * .72, 'axes.labelsize': 20 * .72,
                         'axes.labelcolor': MUTED, 'text.color': TEXT,
                         'xtick.color': MUTED, 'ytick.color': MUTED})
    args.output_dir.mkdir(parents=True)
    outputs = []
    for mobile in (False, True):
        outputs.extend([plot_position(position, args.output_dir, mobile),
                        plot_recovery(recovery, args.output_dir, mobile),
                        plot_performance(system, voxel, benchmark, args.output_dir, mobile)])
    sources = ('scripts/plot_homepage_showcase.py', 'scripts/plot_engineering.py',
               'scripts/plot_readme.py', 'scripts/benchmark_voxel.py')
    report = {
        'schema_version': 1,
        'scope': 'Existing Outdoor01 position and performance measurements, plus separate SIGKILL/corruption recorder experiments.',
        'position_statistics': {key: position[key] for key in
                                ('matched_poses', 'unmatched_poses', 'rmse_m', 'p95_m', 'maximum_m')},
        'recovery_results': recovery, 'system_results': system, 'voxel_summary': voxel,
        'inputs_sha256': {path.relative_to(ROOT).as_posix(): sha256(path) for path in sorted(inputs)},
        'source_sha256': {name: sha256(ROOT / name) for name in sources},
        'outputs': {path.name: {'size_bytes': path.stat().st_size, 'sha256': sha256(path)} for path in outputs},
        'python_version': platform.python_version(), 'numpy_version': np.__version__,
        'matplotlib_version': matplotlib.__version__,
    }
    (args.output_dir / 'showcase_figure_manifest.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    print(f'Saved {len(outputs)} responsive figures with validated source statistics and SHA256 hashes.')


if __name__ == '__main__':
    main()
