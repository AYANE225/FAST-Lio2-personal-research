"""Prepare a chronological Outdoor01 point-cloud replay from a complete recording.

Display sampling retains the first observed point in each voxel. It does not
average future observations, smooth surfaces, or change the estimated poses.
Optional SOR uses the full recording for an offline display-only selection;
retained points still appear at their original observation times.
The original recording is only read; recovery is deliberately not invoked.
"""
import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
import numbers
from pathlib import Path
import platform
import re
import time

import numpy as np
import scipy
from scipy.spatial.transform import Rotation

from map_writer import RecorderSettings, _empty_index, _read_json, _validate_block
from process_map import statistical_filter

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def first_voxel_indices(points, voxel_size_m):
    """Return ascending input indices of the earliest point in each XYZ voxel.

    Bins use floor(XYZ / voxel_size_m) at the zero origin. Input order is frame
    order followed by point order within each frame. XYZI values are unchanged.
    """
    points = np.asarray(points)
    if (points.ndim != 2 or points.shape[1] != 4 or points.dtype.kind not in 'fiu'
            or not np.isfinite(points).all()):
        raise ValueError('Expected finite real N x 4 XYZI points')
    if (isinstance(voxel_size_m, (bool, np.bool_))
            or not isinstance(voxel_size_m, numbers.Real)
            or not np.isfinite(voxel_size_m) or voxel_size_m <= 0):
        raise ValueError('voxel_size_m must be finite and positive')
    with np.errstate(over='ignore', invalid='ignore'):
        keys = np.floor(points[:, :3].astype(np.float64) / voxel_size_m)
    if not np.isfinite(keys).all() or np.any(np.abs(keys) >= 2**62):
        raise ValueError('Voxel coordinates exceed the supported integer range')
    _, first = np.unique(keys.astype(np.int64), axis=0, return_index=True)
    return np.sort(first).astype(np.int64)


def select_height_range(points, indices, height_range_m):
    """Optionally restrict the view to a fixed local-height interval, inclusively."""
    limits = np.asarray(height_range_m, dtype=float)
    if limits.shape != (2,) or not np.isfinite(limits).all() or limits[0] >= limits[1]:
        raise ValueError('Expected an increasing finite height range')
    heights = points[indices, 2]
    return indices[(heights >= limits[0]) & (heights <= limits[1])]


def select_statistical_inliers(points, indices, neighbors=24, std_ratio=2., workers=1):
    """Apply full-recording SOR without moving points or changing source order.

    This mask uses future neighbourhoods and is explicitly an offline display
    operation. It must not be described as a causal online filtering result.
    """
    _, keep, report = statistical_filter(points[indices], neighbors, std_ratio, workers)
    report['temporal_scope'] = 'offline_full_recording'
    return indices[keep], report


def build_timeline(frame_counts, stamps_ns, selected_indices, positions):
    """Use exclusive point offsets to reveal only fully observed frame prefixes."""
    counts, stamps, selected = map(np.asarray, (frame_counts, stamps_ns, selected_indices))
    positions = np.asarray(positions, dtype=float)
    if any(a.ndim != 1 or a.dtype.kind not in 'iu' for a in (counts, stamps, selected)):
        raise ValueError('Frame counts, nanosecond timestamps and point indices must be integer vectors')
    if (not len(counts) or len(counts) != len(stamps) or np.any(counts < 0)
            or positions.shape != (len(counts), 3) or not np.isfinite(positions).all()
            or any(int(b) <= int(a) for a, b in zip(stamps[:-1], stamps[1:]))):
        raise ValueError('Invalid frame counts, trajectory or non-increasing timestamps')
    total = sum(map(int, counts))
    if total > np.iinfo(np.int64).max:
        raise ValueError('Point counts exceed int64 range')
    if len(selected) and (np.any(selected < 0) or int(selected[-1]) >= total
                          or np.any(selected[1:] <= selected[:-1])):
        raise ValueError('Selected point indices must be strictly increasing and within the recording')
    recorded = np.cumsum(counts, dtype=np.int64)
    visible = np.searchsorted(selected, recorded, side='left')
    return {
        'schema_version': 1,
        'stamp_ns': [str(int(stamp)) for stamp in stamps],
        'elapsed_s': [(int(stamp) - int(stamps[0])) / 1.e9 for stamp in stamps],
        'recorded_point_prefix_counts': recorded.tolist(),
        'display_point_prefix_counts': visible.tolist(),
        'positions': positions.tolist(),
    }


def verify_source_files(run_directory, export_manifest):
    """Match the local full run against the previously published source hashes."""
    root = Path(run_directory).resolve()
    sources = json.loads(Path(export_manifest).read_text()).get('source_files')
    if not isinstance(sources, dict) or not sources:
        raise ValueError('The export manifest has no source checksums')
    for name, expected in sources.items():
        relative = Path(name)
        path = (root / relative).resolve()
        if relative.is_absolute() or '..' in relative.parts or not path.is_relative_to(root):
            raise ValueError(f'Invalid source path: {name}')
        if not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected):
            raise ValueError(f'Invalid source checksum: {name}')
        if not path.is_file() or sha256(path) != expected:
            raise ValueError(f'Source checksum mismatch: {name}')
    return sources


def read_recording(directory):
    """Read a finalized recording, validating every block without metadata writes.

    The writer's existing read-only block validator is reused so its schema and
    chain checks remain consistent with recovery, without calling recovery.
    """
    directory = Path(directory)
    settings_path = directory / 'settings.json'
    settings = RecorderSettings(**_read_json(settings_path)['settings'])
    settings_sha = sha256(settings_path)
    saved_index = _read_json(directory / 'index.json')
    manifest = _read_json(directory / 'manifest.json')
    if (manifest.get('completed') is not True or manifest.get('received_counts_verified') is not True
            or manifest.get('index_sha256') != sha256(directory / 'index.json')
            or manifest.get('settings_sha256') != settings_sha):
        raise ValueError('A complete, verified recording is required')
    count = saved_index.get('committed_blocks')
    if type(count) is not int or count <= 0:
        raise ValueError('The recording has no committed blocks')
    blocks = directory / 'blocks'
    expected = {f'block-{number:06d}' for number in range(count)}
    entries = list(blocks.iterdir())
    if {p.name for p in entries} != expected or any(p.is_symlink() or not p.is_dir() for p in entries):
        raise ValueError('Unexpected or incomplete recording block directories')
    index, clouds, rows, block_info = _empty_index(settings_sha), [], [], []
    for number in range(count):
        block = blocks / f'block-{number:06d}'
        index = _validate_block(block, index, settings, settings_sha)
        cloud = np.load(block / 'points.npy', allow_pickle=False, mmap_mode='r')
        if not np.isfinite(cloud).all():
            raise ValueError(f'Nonfinite recorded points: {block.name}')
        clouds.append(cloud)
        rows.extend(_read_json(block / 'frames.json', settings.chunk_frames * 2048 + 4096)['frames'])
        block_info.append({'block_id': number, 'files': {
            name: sha256(block / name) for name in ('block.json', 'frames.json', 'points.npy')}})
    if index != saved_index or any(manifest.get(name) != value for name, value in index.items()):
        raise ValueError('Recording manifest/index totals disagree with the blocks')
    if [row['sequence'] for row in rows] != list(range(len(rows))):
        raise ValueError('The display requires a consecutive sequence of recorded frames')
    return np.concatenate(clouds), rows, block_info


def load_trajectory(path, expected_stamps_ns):
    rows = [line.split() for line in Path(path).read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')]
    if len(rows) != len(expected_stamps_ns) or any(len(row) != 8 for row in rows):
        raise ValueError('Trajectory frame count or TUM column count differs from the recording')
    try:
        stamps = [Decimal(row[0]) * 1_000_000_000 for row in rows]
        if any(stamp != int(expected) for stamp, expected in zip(stamps, expected_stamps_ns)):
            raise ValueError('Trajectory timestamp differs from the recorded frame timestamp')
        values = np.asarray([row[1:] for row in rows], dtype=float)
    except (InvalidOperation, OverflowError) as error:
        raise ValueError('Invalid trajectory timestamp') from error
    if not np.isfinite(values).all():
        raise ValueError('Trajectory values must be finite')
    return values[:, :3]


def gravity_rotation(gravity):
    gravity = np.asarray(gravity, dtype=float)
    if gravity.shape != (3,) or not np.isfinite(gravity).all() or np.linalg.norm(gravity) < 1.e-9:
        raise ValueError('Expected a finite nonzero gravity vector')
    rotation, _ = Rotation.align_vectors([[0, 0, -1]], [gravity / np.linalg.norm(gravity)])
    return rotation.as_matrix()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--export-manifest', type=Path,
                        default=ROOT / 'data/outdoor01/recording/export_manifest.json')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--voxel-size-m', type=float, default=.10)
    parser.add_argument('--height-range-m', type=float, nargs=2, metavar=('MIN', 'MAX'),
                        help='Optional fixed local-height display range; does not modify the source recording')
    parser.add_argument('--sor', action='store_true',
                        help='Filter the full display cloud offline, retaining original coordinates and timestamps')
    parser.add_argument('--sor-neighbors', type=int, default=24)
    parser.add_argument('--sor-std-ratio', type=float, default=2.)
    parser.add_argument('--workers', type=int, default=1)
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error('Use a new output directory to preserve previous exports')
    started = time.perf_counter()
    sources = verify_source_files(args.run_dir, args.export_manifest)
    points, rows, blocks = read_recording(args.run_dir / 'recording')
    print(f'Verified {len(rows):,} frames / {len(points):,} points / {len(blocks)} blocks.', flush=True)
    stamps = np.array([row['stamp_sec'] * 1_000_000_000 + row['stamp_nanosec'] for row in rows], dtype=np.int64)
    positions = load_trajectory(args.run_dir / 'trajectory_tum.txt', stamps)
    gravity_path = args.run_dir / 'algorithm_logs/mat_out.txt'
    with gravity_path.open() as stream:
        first_row = np.fromstring(next(line for line in stream if line.strip()), sep=' ')
    if len(first_row) < 4 or not np.isfinite(first_row).all():
        raise ValueError('Invalid initial state row in mat_out.txt')
    gravity = first_row[-4:-1]
    rotation = gravity_rotation(gravity)
    # Preserve float32 XYZI storage, with the same rigid rotation on the poses.
    points[:, :3] = points[:, :3].astype(np.float64) @ rotation.T
    positions = positions @ rotation.T
    selected = first_voxel_indices(points, args.voxel_size_m)
    uncut_point_count = len(selected)
    if args.height_range_m is not None:
        selected = select_height_range(points, selected, args.height_range_m)
    height_point_count = len(selected)
    sor = None
    if args.sor:
        selected, sor = select_statistical_inliers(points, selected, args.sor_neighbors,
                                                   args.sor_std_ratio, args.workers)
    if not len(selected):
        raise ValueError('No display points remain in the requested range')
    display_points = points[selected]
    timeline = build_timeline(np.array([row['point_count'] for row in rows]), stamps, selected, positions)
    args.output_dir.mkdir(parents=True)
    display_points.astype('<f4', copy=False).tofile(args.output_dir / 'points.bin')
    (args.output_dir / 'timeline.json').write_text(json.dumps(timeline, separators=(',', ':'), allow_nan=False) + '\n')
    np.savez_compressed(args.output_dir / 'selected_indices.npz', selected_indices=selected)
    outputs = {name: {'size_bytes': (args.output_dir / name).stat().st_size,
                      'sha256': sha256(args.output_dir / name)}
               for name in ('points.bin', 'timeline.json', 'selected_indices.npz')}
    report = {
        'schema_version': 1, 'sequence': 'M3DGR Outdoor01', 'run': 'recording (0.5 m scan voxel)',
        'source_export_manifest_sha256': sha256(args.export_manifest), 'source_files': sources,
        'gravity_source_sha256': sha256(gravity_path), 'gravity_source_first_row': first_row.tolist(),
        'gravity_m_s2': gravity.tolist(), 'gravity_rotation': rotation.tolist(),
        'frame_count': len(rows), 'recorded_points': len(points), 'block_count': len(blocks),
        'point_count': len(display_points), 'voxel_size_m': args.voxel_size_m,
        'first_observations_before_height_selection': uncut_point_count,
        'first_observations_after_height_selection': height_point_count,
        'height_range_m': args.height_range_m,
        'statistical_filter': sor,
        'point_format': 'little-endian float32 XYZI; 16 bytes per point',
        'sampling': 'First observed point per floor(XYZ / voxel_size_m) bin in the rotated float32 cloud; input order retained.',
        'display_transform': 'Rotate this run\'s initial estimated gravity to -Z, no translation, no reference fit.',
        'time_scope': 'Seconds since the first recorded scan end. All counts are sensor-time prefixes, not live wall-clock samples.',
        'source_duration_s': timeline['elapsed_s'][-1], 'bounds': {
            'min': display_points[:, :3].min(axis=0).tolist(), 'max': display_points[:, :3].max(axis=0).tolist()},
        'color_ranges': {'height': [-2, 20], 'intensity': [0, 106]},
        'blocks': blocks, 'outputs': outputs,
        'source_sha256': {name: sha256(ROOT / name) for name in
                          ('scripts/prepare_mapping_progress.py', 'scripts/map_writer.py', 'scripts/process_map.py')},
        'python_version': platform.python_version(), 'numpy_version': np.__version__,
        'scipy_version': scipy.__version__, 'workers': args.workers,
        'preparation_seconds': time.perf_counter() - started,
        'scope': ('Offline display of recorded observations; no surface smoothing, future-point averaging or accuracy improvement. '
                  'When enabled, SOR uses the full recording, including future neighbourhoods, for display-only point selection.'),
    }
    (args.output_dir / 'preparation.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    print(f'Prepared {len(display_points):,} first observations over {report["source_duration_s"]:.3f} s.', flush=True)


if __name__ == '__main__':
    main()
