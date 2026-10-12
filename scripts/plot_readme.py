"""Draw compact README figures from published Outdoor01 measurements."""
import argparse
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from benchmark_voxel import summarize_trials
from plot_engineering import setup_style

ROOT = Path(__file__).resolve().parents[1]
INK = '#19354a'
MUTED = '#526779'
GRID = '#e4ebf0'
RUN_COLORS = ('#8796a8', '#087fb0', '#cf6a27')
BACKEND_COLORS = ('#427bb6', '#168b83')


def load_system_results(inputs):
    results = {}
    for run in ('udp', 'tcp', 'recording', 'surf03'):
        directory = ROOT / 'data/outdoor01' / run
        metrics_path = directory / 'metrics.json'
        inputs.add(metrics_path)
        metrics = json.loads(metrics_path.read_text())
        coverage = metrics['coverage']
        expected = coverage['input_lidar_frames'] - coverage['leading_frames_without_odometry']
        if expected != coverage['processed_frames'] + coverage['missing_frames_after_initialization']:
            raise ValueError(f'{run}: frame coverage is inconsistent')
        results[run] = {
            'processed_frames': coverage['processed_frames'],
            'expected_frames_after_initialization': expected,
            'missing_frames_after_initialization': coverage['missing_frames_after_initialization'],
        }
        if run == 'udp':
            continue
        timing_path = directory / 'algorithm_logs/fast_lio_time_log.csv'
        position_path = directory / 'position_comparison.npz'
        resources_path = directory / 'resources.jsonl'
        inputs.update((timing_path, position_path, resources_path))
        timing = np.loadtxt(timing_path, delimiter=',', skiprows=1)
        with np.load(position_path, allow_pickle=False) as position:
            errors = position['error_m']
        resources = [json.loads(line) for line in resources_path.read_text().splitlines()]
        if run == 'tcp':
            rss_bytes = [row['rss_bytes'] for row in resources]
            saved_peak = metrics['resources']['observed_peak_rss_mib']
        else:
            rss_bytes = [sum(row.get(role, {}).get('rss_bytes', 0)
                             for role in ('mapping', 'recorder')) for row in resources]
            saved_peak = metrics['resources']['combined']['rss_mib']['maximum']
        peak = float(max(rss_bytes) / 1048576)
        p95 = float(np.percentile(timing[:, 1] * 1000, 95))
        rmse = float(np.sqrt(np.mean(errors ** 2)))
        reference = metrics['position_reference_comparison']
        if len(timing) != coverage['processed_frames'] or len(errors) != reference['matched_poses']:
            raise ValueError(f'{run}: source row counts disagree with the published summary')
        if not np.allclose([peak, p95, rmse],
                           [saved_peak, metrics['core_processing_time_ms']['p95'], reference['rmse_m']],
                           rtol=1e-10, atol=1e-12):
            raise ValueError(f'{run}: source measurements disagree with the published summary')
        results[run].update(combined_peak_rss_mib=peak, core_p95_ms=p95,
                            position_reference_rmse_m=rmse)
    return results


def save_figure(figure, directory, name):
    for extension in ('png', 'svg'):
        figure.savefig(directory / f'{name}.{extension}', dpi=160, facecolor='white',
                       metadata={'Date': None} if extension == 'svg' else {})
    plt.close(figure)


def draw_bars(axis, labels, values, colors, title, unit, ticks, limit, digits):
    positions = np.arange(len(values))
    axis.barh(positions, values, height=.48, color=colors, zorder=2)
    for row, (value, color) in enumerate(zip(values, colors)):
        if value == 0:
            axis.scatter([0], [row], s=32, facecolor='white', edgecolor=color,
                         linewidth=2, zorder=3, clip_on=False)
        axis.text(value + limit * .03, row, f'{value:.{digits}f}',
                  va='center', color=INK, fontsize=13, weight='bold')
    axis.set_yticks(positions, labels)
    axis.set_ylim(len(values) - .5, -.6)
    axis.set_xlim(0, limit)
    axis.set_xticks(ticks)
    axis.set_xlabel(unit, loc='right', color=MUTED, fontsize=11)
    axis.set_title(title, loc='left', fontsize=15, color=INK, weight='bold', pad=15)
    axis.grid(axis='x', color=GRID, linewidth=.8)
    axis.set_axisbelow(True)
    axis.tick_params(axis='both', length=0, pad=7, labelsize=11)
    for side in ('left', 'right', 'top'):
        axis.spines[side].set_visible(False)
    axis.spines['bottom'].set_color(GRID)


def plot_system_comparison(results, directory, mobile=False):
    if mobile:
        figure, axes = plt.subplots(4, 1, figsize=(5.3, 13.1))
        figure.subplots_adjust(left=.27, right=.91, top=.86, bottom=.17, hspace=.88)
        title, subtitle = '公开序列回放实测', 'Outdoor01 · 每配置一次完整 1× 回放'
        figure.text(.06, .961, title, fontsize=21, weight='bold', color=INK)
        figure.text(.06, .928, subtitle, fontsize=12, color=MUTED)
        figure.text(.06, .903, 'Core Ultra 9 285K · ROS 2 Humble · Release', fontsize=11, color=MUTED)
    else:
        figure, axes = plt.subplots(2, 2, figsize=(11.6, 7.8))
        figure.subplots_adjust(left=.14, right=.96, top=.78, bottom=.22, wspace=.58, hspace=.93)
        figure.text(.05, .935, '公开序列回放实测', fontsize=23, weight='bold', color=INK)
        figure.text(.05, .87, 'Outdoor01 · 每配置一次完整 1× 回放 · Core Ultra 9 285K · ROS 2 Humble · Release',
                    fontsize=12, color=MUTED)
    axes = np.asarray(axes).flatten()
    run_names = ('tcp', 'recording', 'surf03')
    labels = ('原 TCP', '分块 0.5 m', '分块 0.3 m')
    draw_bars(axes[0], ('原 UDP', '原 TCP'),
              [results[run]['missing_frames_after_initialization'] for run in ('udp', 'tcp')],
              (RUN_COLORS[2], RUN_COLORS[1]), '传输诊断：初始化后缺帧', '帧', [0, 150, 300], 440, 0)
    draw_bars(axes[1], labels, [results[run]['combined_peak_rss_mib'] for run in run_names],
              RUN_COLORS, '内存：同时采样峰值', 'RSS / MiB', [0, 200, 400, 600], 870, 1)
    draw_bars(axes[2], labels, [results[run]['core_p95_ms'] for run in run_names],
              RUN_COLORS, '核心耗时：p95', 'ms', [0, 5, 10, 15], 18, 2)
    draw_bars(axes[3], labels, [results[run]['position_reference_rmse_m'] for run in run_names],
              RUN_COLORS, '位置参考误差：RMSE', 'm', [0, .1, .2, .3], .33, 3)
    reduction = (1 - results['recording']['combined_peak_rss_mib'] / results['tcp']['combined_peak_rss_mib']) * 100
    completed = results['recording']['processed_frames']
    expected = results['recording']['expected_frames_after_initialization']
    if mobile:
        footer = [
            (f'分块 0.5 m：内存降低 {reduction:.1f}%，核心耗时增加', True),
            (f'完整输出 {completed:,} / {expected:,} 帧；0.5 / 0.3 m 为扫描体素', False),
            ('RSS 约 1 Hz；工程配置计入 mapping + recorder。', False),
            ('原基线未覆盖退出保存；工程配置覆盖受控退出。', False),
            ('核心计时不含输入预处理、发布和写盘。', False),
            ('位置参考：固定尺度 SE(3) 对齐；RTK 杆臂未补偿。', False),
        ]
        start, step = .118, .020
    else:
        footer = [
            (f'分块 0.5 m：内存降低 {reduction:.1f}%，核心耗时增加。完整输出 {completed:,} / {expected:,} 帧。', True),
            ('0.5 / 0.3 m 为扫描体素。RSS 约 1 Hz，工程配置计入 mapping + recorder；原基线未覆盖退出保存。', False),
            ('核心计时不含输入预处理、发布和写盘。位置参考：固定尺度 SE(3) 对齐，RTK 杆臂未补偿。', False),
        ]
        start, step = .125, .044
    for index, (line, emphasized) in enumerate(footer):
        figure.text(.06 if mobile else .05, start - index * step, line,
                    fontsize=(11.5 if mobile else 13) if emphasized else 10.5,
                    color=INK if emphasized else MUTED, weight='bold' if emphasized else 'normal')
    save_figure(figure, directory, 'readme_results_mobile' if mobile else 'readme_results')


def plot_voxel_comparison(report, rows, directory, mobile=False):
    if mobile:
        figure, axes = plt.subplots(3, 1, figsize=(5.3, 9.5))
        figure.subplots_adjust(left=.24, right=.9, top=.8, bottom=.19, hspace=.9)
        title_y, subtitle_y, legend_y = .948, .9, .86
    else:
        figure, axes = plt.subplots(1, 3, figsize=(11.6, 4.5))
        figure.subplots_adjust(left=.10, right=.96, top=.60, bottom=.29, wspace=.42)
        title_y, subtitle_y, legend_y = .90, .80, .71
    figure.text(.06 if mobile else .05, title_y, 'C++17 离线体素处理', fontsize=21, weight='bold', color=INK)
    subtitle = f"{report['input']['points']:,} 点 · 每实现 / 体素 {report['repeats']} 次计时"
    figure.text(.06 if mobile else .05, subtitle_y, subtitle, fontsize=12, color=MUTED)
    figure.text(.06 if mobile else .05, legend_y, '柱：耗时中位数   ○：每次测量   横轴单位：秒', fontsize=11, color=MUTED)
    for axis, row in zip(axes, rows):
        medians = [row[backend]['median_seconds'] for backend in ('numpy', 'cpp')]
        draw_bars(axis, ('NumPy', 'C++'), medians, BACKEND_COLORS,
                  f"{row['voxel_size_m']:.2f} m  ·  {row['median_speedup_numpy_over_cpp']:.2f}×", '',
                  [0, 1, 2], 2.38, 3)
        for index, backend in enumerate(('numpy', 'cpp')):
            trials = row[backend]['seconds']
            axis.scatter(trials, index + np.linspace(-.11, .11, len(trials)),
                         s=20, facecolor='white', edgecolor=INK, linewidth=.7, zorder=4)
    footers = [
        f"{len(report['comparisons'])} 组配对：计数与 float32 质心一致。",
        '倍数 = NumPy / C++ 耗时中位数；仅离线处理。',
        '单地图重复计时；非在线 SLAM 提速或定位精度结论。',
    ]
    for index, line in enumerate(footers):
        figure.text(.06 if mobile else .05, (.125 if mobile else .17) - index * (.035 if mobile else .06),
                    line, fontsize=11 if index == 0 else 10.5, color=INK if index == 0 else MUTED)
    save_figure(figure, directory, 'readme_voxel_mobile' if mobile else 'readme_voxel')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'output/readme-figures')
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    setup_style()
    plt.rcParams.update({'font.size': 12, 'text.color': INK, 'axes.labelcolor': MUTED,
                         'xtick.color': MUTED, 'ytick.color': MUTED, 'svg.hashsalt': 'fastlio-readme'})
    inputs = set()
    results = load_system_results(inputs)
    benchmark_path = ROOT / 'data/career/voxel_benchmark.json'
    inputs.add(benchmark_path)
    benchmark = json.loads(benchmark_path.read_text())
    rows = summarize_trials(benchmark['trials'],
                            [row['voxel_size_m'] for row in benchmark['summary']], benchmark['repeats'])
    if rows != benchmark['summary'] or len(benchmark['comparisons']) != len(rows) * benchmark['repeats']:
        raise ValueError('Benchmark summary/comparison count disagrees with trials')
    if not all(row['counts_equal'] and row['xyz_max_abs_difference_m'] == 0
               and row['intensity_max_abs_difference'] == 0 for row in benchmark['comparisons']):
        raise ValueError('The published benchmark does not support identical output labels')
    for mobile in (False, True):
        plot_system_comparison(results, args.output_dir, mobile)
        plot_voxel_comparison(benchmark, rows, args.output_dir, mobile)
    manifest = {
        'scope': 'Presentation of existing single-sequence replay and offline voxel measurements; no new benchmark run.',
        'system_results': results,
        'voxel_summary': rows,
        'inputs_sha256': {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted(inputs)},
    }
    (args.output_dir / 'readme_figure_manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Saved desktop/mobile README result and voxel figures (PNG/SVG), with source hashes.')


if __name__ == '__main__':
    main()
