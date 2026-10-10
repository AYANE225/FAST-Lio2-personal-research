"""Validate and publish compact evidence from an Outdoor01 engineering replay.

Uses the previously published RTK XYZ interpolation at the same timestamps.
Every saved reference sample must match; only poses outside that saved reference
range may be omitted. Neither scale nor a time offset is fitted.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

import numpy as np

try:
    from .replay_outdoor01 import validate_scan_coverage
    from .map_writer import recover_recording
except ImportError:
    from replay_outdoor01 import validate_scan_coverage
    from map_writer import recover_recording

ROOT = Path(__file__).resolve().parents[1]


def stats(values):
    values = np.asarray(values, dtype=float)
    if not values.size or not np.isfinite(values).all():
        raise ValueError('Statistics require finite nonempty data')
    return {'count': len(values), 'mean': float(values.mean()),
            'median': float(np.median(values)), 'p95': float(np.percentile(values, 95)),
            'p99': float(np.percentile(values, 99)), 'maximum': float(values.max()),
            'minimum': float(values.min())}


def align_positions(estimated, reference):
    estimated, reference = np.asarray(estimated, dtype=float), np.asarray(reference, dtype=float)
    if (estimated.ndim != 2 or estimated.shape[1] != 3 or estimated.shape != reference.shape
            or len(estimated) < 3 or not np.isfinite(estimated).all() or not np.isfinite(reference).all()):
        raise ValueError('Alignment requires at least three finite paired XYZ positions')
    center_est, center_ref = estimated.mean(axis=0), reference.mean(axis=0)
    u, singular_values, vt = np.linalg.svd((estimated - center_est).T @ (reference - center_ref))
    if singular_values[1] < 1e-12:
        raise ValueError('A line or single position cannot determine this rigid alignment')
    rotation = vt.T @ np.diag([1., 1., np.linalg.det(vt.T @ u.T)]) @ u.T
    translation = center_ref - rotation @ center_est
    aligned = estimated @ rotation.T + translation
    residual = aligned - reference
    return {'aligned_xyz': aligned, 'residual_xyz_m': residual,
            'error_m': np.linalg.norm(residual, axis=1), 'rotation': rotation,
            'translation': translation, 'singular_values': singular_values}


def pair_reference_positions(trajectory, reference_t, reference_xyz):
    t = np.asarray(trajectory)[:, 0]
    reference_t, reference_xyz = np.asarray(reference_t), np.asarray(reference_xyz)
    if (len(reference_t) < 3 or reference_xyz.shape != (len(reference_t), 3)
            or not np.isfinite(t).all() or not np.isfinite(reference_t).all()
            or np.any(np.diff(t) <= 0) or np.any(np.diff(reference_t) <= 0)):
        raise ValueError('Invalid reference or trajectory timestamps')
    right = np.minimum(np.searchsorted(reference_t, t), len(reference_t) - 1)
    left = np.maximum(right - 1, 0)
    indices = np.where(abs(reference_t[left] - t) < abs(reference_t[right] - t), left, right)
    accepted = abs(reference_t[indices] - t) <= 1e-6
    if (not np.array_equal(indices[accepted], np.arange(len(reference_t)))
            or np.any((~accepted) & (t >= reference_t[0]) & (t <= reference_t[-1]))):
        raise ValueError('Trajectory does not cover all saved reference samples at the same timestamps')
    return accepted, reference_xyz[indices[accepted]]


def resource_metrics(rows):
    results = {}
    for role in ('mapping', 'recorder', 'combined'):
        selected = [row for row in rows if role == 'combined' or role in row]
        if not selected:
            raise ValueError(f'Missing resource samples: {role}')
        rss, cpu = [], []
        for row in selected:
            parts = [row[key] for key in ('mapping', 'recorder') if key in row] if role == 'combined' else [row[role]]
            rss.append(sum(p['rss_bytes'] for p in parts) / 1048576)
            cpu.append(sum(p['cpu_percent'] for p in parts))
        results[role] = {'samples': len(selected), 'rss_mib': stats(rss), 'cpu_percent': stats(cpu)}
    results['scope'] = 'About 1 Hz samples through playback and controlled shutdown; may miss subsecond peaks.'
    results['cpu_convention'] = '100 percent is one logical CPU; combined sums mapping and recorder.'
    return results


def evaluate(run, output, reference_path):
    run, output = Path(run), Path(output)
    summary = json.loads((run / 'run_summary.json').read_text())
    if not summary.get('completed') or any(summary.get(f'{role}_exit_code') != 0
                                         for role in ('mapping', 'playback', 'recorder')):
        raise ValueError('Only a completed, cleanly stopped replay can be published as a full run')
    trajectory = np.loadtxt(run / 'trajectory_tum.txt', ndmin=2)
    timing = np.loadtxt(run / 'algorithm_logs/fast_lio_time_log.csv', delimiter=',', skiprows=1, ndmin=2)
    if (trajectory.shape[1] != 8 or not np.isfinite(trajectory).all() or not np.isfinite(timing).all()
            or len(timing) != len(trajectory) or np.any(np.diff(trajectory[:, 0]) <= 0)):
        raise ValueError('Invalid trajectory/timing data')
    quaternion_norm_error = float(np.max(abs(np.linalg.norm(trajectory[:, 4:], axis=1) - 1)))
    if quaternion_norm_error > 1e-7:
        raise ValueError('Estimated quaternion norm is invalid')
    ledger_path = ROOT / 'data/outdoor01/tcp/input_timestamps.npz'
    ledger_hash = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    if ledger_hash != summary['input_timestamps_sha256']:
        raise ValueError('Input timestamp ledger does not match the replay')
    input_starts = np.load(ledger_path, allow_pickle=False)['lidar'][:, 1] * 1e-9
    coverage = validate_scan_coverage(input_starts, timing[:, 0], summary['initialization_frames'])
    recording = recover_recording(run / 'recording')
    if not recording['completed'] or not recording['integrity_ok']:
        raise ValueError('Recording is incomplete or inconsistent')
    frame_rows, block_ids = [], []
    for block in range(recording['committed_blocks']):
        rows = json.loads((run / f'recording/blocks/block-{block:06d}/frames.json').read_text())['frames']
        frame_rows.extend(rows)
        block_ids.extend([block] * len(rows))
    stamps = np.asarray([row['stamp_sec'] * 1_000_000_000 + row['stamp_nanosec'] for row in frame_rows], dtype=np.int64)
    input_points = np.asarray([row['input_points'] for row in frame_rows], dtype=np.int64)
    written_points = np.asarray([row['point_count'] for row in frame_rows], dtype=np.int64)
    if (len(frame_rows) != len(trajectory) or np.max(abs(stamps * 1e-9 - trajectory[:, 0])) > 1e-6
            or not np.array_equal(input_points, timing[:, 2].astype(np.int64))):
        raise ValueError('Recorded frame stamps or point counts differ from mapping output')
    reference = np.load(reference_path, allow_pickle=False)
    accepted, xyz = pair_reference_positions(trajectory, reference['timestamps'], reference['reference_xyz'])
    alignment = align_positions(trajectory[accepted, 1:4], xyz)
    errors = alignment['error_m']
    resources = [json.loads(line) for line in (run / 'resources.jsonl').read_text().splitlines()]
    metrics = {'coverage': coverage, 'core_processing_time_ms': stats(timing[:, 1] * 1000),
        'core_processing_frames_over_100ms': int(np.count_nonzero(timing[:, 1] > .1)),
        'timing_scope': 'IMU processing through map insertion; excludes input preprocessing, publication and recording.',
        'preprocess_column_used': False,
        'resources': resource_metrics(resources),
        'position_reference_comparison': {'matched_poses': int(accepted.sum()),
            'unmatched_poses': int((~accepted).sum()), 'scale': 1., 'fitted_time_offset_s': 0.,
            'reference_source': 'data/outdoor01/tcp/position_comparison.npz',
            'reference_sha256': hashlib.sha256(reference_path.read_bytes()).hexdigest(),
            'pairing_tolerance_s': 1e-6, 'rmse_m': float(np.sqrt(np.mean(errors**2))),
            'error_m': stats(errors), 'alignment': 'One whole-trajectory SE(3), no scale or time-offset fit.',
            'limitations': ['The same previously interpolated RTK positions at all 4107 timestamps are reused.',
                           'RTK antenna lever arm is uncorrected; GT orientations are placeholders.',
                           'One run per configuration on one sequence; no significance claim.']},
        'recording': {'frames': len(frame_rows), 'points': int(written_points.sum()),
            'frame_point_counts_equal_mapping_log': True, 'stamps_equal_odometry': True,
            'quaternion_max_norm_error': quaternion_norm_error,
            **{key: recording[key] for key in ('committed_blocks', 'dropped_frames', 'failed_frames',
                'queue_high_water', 'write_seconds', 'pending_frames', 'nonfinite_points', 'settings')}}}
    output.mkdir(parents=True, exist_ok=False)
    (output / 'algorithm_logs').mkdir()
    for filename in ('trajectory_tum.txt', 'resources.jsonl', 'algorithm_logs/fast_lio_time_log.csv'):
        shutil.copy2(run / filename, output / filename)
    for name in ('manifest', 'settings', 'index'):
        shutil.copy2(run / f'recording/{name}.json', output / f'recording_{name}.json')
    for suffix, value in (('metrics', metrics), ('run_summary', {key: value for key, value in summary.items()
                         if key not in ('workspace', 'bag') and not key.endswith(('_command', '_pid'))})):
        (output / f'{suffix}.json').write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
    np.savez_compressed(output / 'position_comparison.npz', timestamps=trajectory[accepted, 0],
                        reference_xyz=xyz, **alignment)
    np.savez_compressed(output / 'recorded_frames.npz', stamp_ns=stamps, input_points=input_points,
                        point_count=written_points, block_id=np.asarray(block_ids, dtype=np.int64),
                        sequence=np.asarray([row['sequence'] for row in frame_rows], dtype=np.int64),
                        point_offset=np.asarray([row['point_offset'] for row in frame_rows], dtype=np.int64))
    source_files = ('run_summary.json', 'config.yaml', 'trajectory_tum.txt', 'resources.jsonl',
                    'algorithm_logs/fast_lio_time_log.csv', 'recording/manifest.json',
                    'recording/index.json', 'recording/settings.json')
    provenance = {'source_files': {name: hashlib.sha256((run / name).read_bytes()).hexdigest() for name in source_files},
                  'export_note': 'Machine paths/commands/PIDs omitted from public run summary. Full point blocks remain local.',
                  'recorded_frames_columns': 'stamp_ns, input_points, point_count, block_id, sequence, point_offset; all int64'}
    (output / 'export_manifest.json').write_text(json.dumps(provenance, indent=2) + '\n')
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--reference', type=Path, default=ROOT / 'data/outdoor01/tcp/position_comparison.npz')
    args = parser.parse_args()
    metrics = evaluate(args.run_dir, args.output_dir, args.reference)
    print(json.dumps(metrics, indent=2))


if __name__ == '__main__':
    main()
