"""Render portfolio figures from the published benchmark and regression JSON."""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from benchmark_voxel import summarize_trials
from plot_engineering import setup_style

ROOT = Path(__file__).resolve().parents[1]
COLORS = ('#2563a6', '#087f7c')
FAIL = '#c33b37'


def save_figure(figure, directory, name):
    for extension in ('png', 'svg'):
        figure.savefig(directory / f'{name}.{extension}', dpi=180, facecolor='white',
                       metadata={'Date': None} if extension == 'svg' else {})
    plt.close(figure)


def plot_voxel_benchmark(report, directory):
    rows = summarize_trials(report['trials'], [row['voxel_size_m'] for row in report['summary']], report['repeats'])
    if rows != report['summary'] or len(report['comparisons']) != len(rows) * report['repeats']:
        raise ValueError('Benchmark summary/comparison count disagrees with trials')
    figure, axes = plt.subplots(1, 2, figsize=(15, 7))
    figure.subplots_adjust(left=.065, right=.975, bottom=.24, top=.76, wspace=.22)
    figure.text(.065, .93, 'C++17 离线体素处理：速度与内存', fontsize=21, weight='bold', color='#172b42')
    figure.text(.065, .872, f"M3DGR Outdoor01 · {report['input']['points']:,} 点 · "
                f"同一重力旋转后 float64 输入 · 每实现/体素 {report['repeats']} 次独立进程计时", color='#52616e', fontsize=11)
    x = np.arange(len(rows))
    settings = [(axes[0], 'seconds', 'median_seconds', '体素调用耗时 (s)', '(a) 柱：中位数；点：每轮实测'),
                (axes[1], 'peak_process_rss_mib', 'median_peak_process_rss_mib',
                 '工作进程峰值 RSS (MiB)', '(b) 包含解释器、输入、处理、保存及哈希')]
    for ax, values_key, median_key, ylabel, title in settings:
        maxima = []
        for index, backend in enumerate(('numpy', 'cpp')):
            positions = x + (-.19 if index == 0 else .19)
            medians = [row[backend][median_key] for row in rows]
            ax.bar(positions, medians, width=.32, color=COLORS[index], alpha=.86,
                   label='NumPy 排序归约' if index == 0 else 'C++ 哈希累加 + 排序', zorder=2)
            for position, row, median in zip(positions, rows, medians):
                values = row[backend][values_key]
                maxima.extend(values)
                ax.scatter(position + np.linspace(-.055, .055, len(values)), values, s=23,
                           marker='o' if index == 0 else '^', facecolor='white',
                           edgecolor='#233747', linewidth=.8, zorder=4)
                label = f'{median:.3f}' if values_key == 'seconds' else f'{median:.1f}'
                ax.annotate(label, (position, max(values)), xytext=(0, 7), textcoords='offset points',
                            ha='center', fontsize=10, color='#172b42')
        ax.set_ylim(0, max(maxima) * 1.30)
        ax.set_xticks(x, [f"{row['voxel_size_m']:.2f} m\n{row['output_points']:,} 体素" for row in rows])
        ax.set(ylabel=ylabel, title=title)
        ax.grid(axis='y', alpha=.18)
        ax.set_axisbelow(True)
        ax.legend(loc='upper left', fontsize=9, frameon=False)
    ratios = '   /   '.join(f"{row['voxel_size_m']:.2f} m：{row['median_speedup_numpy_over_cpp']:.2f}×" for row in rows)
    figure.text(.065, .14, f'NumPy / C++ 耗时中位数比值     {ratios}', fontsize=12, weight='bold', color=COLORS[1])
    difference = max(row['xyz_max_abs_difference_m'] for row in report['comparisons'])
    intensity = max(row['intensity_max_abs_difference'] for row in report['comparisons'])
    figure.text(.065, .087, f"{len(report['comparisons'])} 次对照：计数相同、点数守恒；有序质心最大 XYZ 差 {difference:g} m，强度差 {intensity:g}。", fontsize=10)
    figure.text(.065, .043, 'Linux VmHWM（exec 后）；单地图计时重复，未独占主机。离线点云处理结果不代表在线 SLAM 提速或定位精度提升。', fontsize=9, color='#52616e')
    save_figure(figure, directory, 'voxel_benchmark')


def plot_regression(report, directory):
    if report['status'] not in ('passed', 'failed'):
        raise ValueError('Cannot plot performance judgement for invalid/incomparable runs')
    checks = {item['name']: item for item in report['checks']}
    figure, axes = plt.subplots(1, 3, figsize=(15, 6))
    figure.subplots_adjust(left=.065, right=.97, bottom=.27, top=.72, wspace=.3)
    figure.text(.065, .93, '回归检查：0.3 m 配置超过耗时容差', fontsize=21, weight='bold', color='#172b42')
    figure.text(.065, .857, '基线：0.5 m 扫描体素    候选：0.3 m 扫描体素    相同序列与评估口径 · 各一次完整回放', color='#52616e', fontsize=11)
    for ax, key, title, unit, digits in zip(axes, ('core_p95_ms', 'combined_rss_mib', 'position_rmse_m'),
                                          ('核心处理耗时 p95', 'mapping + recorder 采样峰值', '位置参考 RMSE'),
                                          ('ms', 'MiB', 'm'), (2, 1, 3)):
        baseline = checks[f'baseline.{key}.absolute']['actual']
        candidate = checks[f'candidate.{key}.absolute']['actual']
        absolute, relative = checks[f'candidate.{key}.absolute'], checks[f'candidate.{key}.relative']
        limit = min(absolute['limit'], relative['limit'])
        passed = absolute['status'] == 'passed' and relative['status'] == 'passed'
        color = COLORS[1] if passed else FAIL
        ax.bar([0, 1], [baseline, candidate], color=[COLORS[0], color], width=.55, alpha=.88, zorder=2)
        ax.axhline(limit, color=FAIL, ls='--', lw=1.25, label=f'候选上限 {limit:.{digits}f} {unit}', zorder=3)
        for x, value in enumerate((baseline, candidate)):
            ax.annotate(f'{value:.{digits}f}', (x, value), xytext=(0, 7), textcoords='offset points', ha='center', fontsize=12)
        ax.set_xticks([0, 1], ['基线 0.5 m', '候选 0.3 m'])
        ax.set(ylabel=unit, title=title, ylim=(0, max(baseline, candidate, limit) * 1.43))
        ax.grid(axis='y', alpha=.18)
        ax.set_axisbelow(True)
        ax.legend(loc='upper left', fontsize=9, frameon=False)
        change = (candidate / baseline - 1) * 100
        ax.text(.5, -.23, f"{'通过' if passed else '未通过'} · 相对基线 {change:+.2f}%", transform=ax.transAxes,
                ha='center', color=color, weight='bold', fontsize=12)
    figure.text(.065, .10, '示例相对容差：耗时 +20%，RSS +10%，RMSE +5%；绝对上限：20 ms、350 MiB、0.30 m。阈值依据已有数据后设。', fontsize=10)
    figure.text(.065, .045, '检查已导出摘要；不重算原始测量。RSS 约 1 Hz 采样；RTK 杆臂未补偿；无跨场景或统计显著性结论。', fontsize=9, color='#52616e')
    save_figure(figure, directory, 'regression_check')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data/career')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'output/career_figures')
    args = parser.parse_args()
    setup_style()
    plt.rcParams.update({'font.size': 11, 'axes.titlepad': 18, 'axes.labelcolor': '#344054',
                         'xtick.color': '#344054', 'ytick.color': '#344054', 'svg.hashsalt': 'fastlio-career'})
    args.output_dir.mkdir(parents=True, exist_ok=True)
    plot_voxel_benchmark(json.loads((args.data_dir / 'voxel_benchmark.json').read_text()), args.output_dir)
    plot_regression(json.loads((args.data_dir / 'regression_surf03.json').read_text()), args.output_dir)


if __name__ == '__main__':
    main()
