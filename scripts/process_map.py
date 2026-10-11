"""Prepare traceable display clouds from a binary PCD; never edit estimator data.

Coordinates are metres. The optional display transform only rotates the initial
estimated gravity to -Z; it is not fitted to reference positions. Binary browser
tiles contain little-endian float32 [x, y, z, intensity], gzip-compressed.
"""
import argparse
from functools import partial
import gzip
import hashlib
import json
from pathlib import Path
import platform
import time

import numpy as np
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_pcd(path):
    """Read scalar XYZ/intensity by field name, validating the entire payload.

    Supports PCD DATA binary with arbitrary numeric fields/counts. ASCII and
    binary_compressed are deliberately rejected rather than misinterpreted.
    PCD binary is little endian (the format has no endian flag).
    """
    path = Path(path)
    header = {}
    with path.open('rb') as stream:
        for _ in range(100):
            raw = stream.readline(4097)
            if not raw or len(raw) > 4096:
                raise ValueError('Missing or oversized PCD header')
            try:
                words = raw.decode('ascii').strip().split()
            except UnicodeDecodeError as error:
                raise ValueError('Non-ASCII PCD header') from error
            if not words or words[0].startswith('#'):
                continue
            key, *values = words
            if key in header:
                raise ValueError(f'Duplicate PCD header: {key}')
            header[key] = values
            if key == 'DATA':
                offset = stream.tell()
                break
        else:
            raise ValueError('No PCD DATA entry')
    if header.get('DATA') != ['binary']:
        raise ValueError('Only PCD DATA binary is supported')
    try:
        names = header['FIELDS']
        sizes = [int(v) for v in header['SIZE']]
        types = header['TYPE']
        counts = [int(v) for v in header.get('COUNT', ['1'] * len(names))]
        point_count = int(header['POINTS'][0])
        width, height = int(header['WIDTH'][0]), int(header['HEIGHT'][0])
    except (KeyError, IndexError, ValueError) as error:
        raise ValueError('Invalid PCD field/size/dimension header') from error
    if not names or len(set(names)) != len(names) or not (len(names) == len(sizes) == len(types) == len(counts)):
        raise ValueError('Inconsistent or duplicate PCD fields')
    if point_count <= 0 or width < 1 or height < 1 or width * height != point_count:
        raise ValueError('Inconsistent PCD point dimensions')
    descriptors = []
    for name, size, kind, count in zip(names, sizes, types, counts):
        valid_sizes = (4, 8) if kind == 'F' else (1, 2, 4, 8)
        if kind not in ('F', 'I', 'U') or size not in valid_sizes or count <= 0:
            raise ValueError(f'Unsupported PCD field {name}')
        dtype = f'<{kind.lower()}{size}'
        descriptors.append((name, dtype) if count == 1 else (name, dtype, (count,)))
    dtype = np.dtype(descriptors)
    if path.stat().st_size != offset + point_count * dtype.itemsize:
        raise ValueError('PCD payload size does not match POINTS and field stride')
    for name in ('x', 'y', 'z', 'intensity'):
        if name not in names or counts[names.index(name)] != 1:
            raise ValueError(f'PCD requires scalar {name}')
    mapped = np.memmap(path, mode='r', offset=offset, dtype=dtype, shape=(point_count,))
    points = np.column_stack([mapped[key] for key in ('x', 'y', 'z', 'intensity')]).astype('<f4')
    if not np.isfinite(points).all():
        raise ValueError('PCD XYZ/intensity must be finite')
    return points, {'points': point_count, 'fields': names, 'encoding': 'binary',
                    'stride_bytes': dtype.itemsize, 'size_bytes': path.stat().st_size,
                    'sha256': sha256(path)}


def validate_points(points):
    points = np.asarray(points)
    if points.ndim != 2 or points.shape[1] != 4 or not np.isfinite(points).all():
        raise ValueError('Expected finite N x 4 XYZ/intensity points')
    return points


def voxel_centroids(points, voxel_size_m):
    """Average all XYZI samples in each zero-origin voxel, in XYZ grid order."""
    points = validate_points(points)
    if not np.isfinite(voxel_size_m) or voxel_size_m <= 0:
        raise ValueError('voxel_size_m must be finite and positive')
    if len(points) == 0:
        return np.empty((0, 4), dtype='<f4'), np.empty(0, dtype=np.int64)
    coordinates = np.floor(points[:, :3].astype(np.float64) / voxel_size_m)
    if np.abs(coordinates).max() >= 2**62:
        raise ValueError('Voxel coordinates exceed supported integer range')
    keys = coordinates.astype(np.int64)
    order = np.lexsort((keys[:, 2], keys[:, 1], keys[:, 0]))
    keys = keys[order]
    start = np.r_[0, np.flatnonzero(np.any(keys[1:] != keys[:-1], axis=1)) + 1]
    counts = np.diff(np.r_[start, len(points)])
    sums = np.add.reduceat(points[order].astype(np.float64), start, axis=0)
    return (sums / counts[:, None]).astype('<f4'), counts


def get_voxel_function(backend='numpy', library_path=None):
    """Select an explicit implementation; never substitute a missing native library."""
    if backend == 'numpy':
        if library_path is not None:
            raise ValueError('voxel-library requires voxel-backend cpp')
        return voxel_centroids
    if backend == 'cpp':
        from native_voxel import voxel_centroids_native
        return partial(voxel_centroids_native, library_path=library_path)
    raise ValueError(f'Unknown voxel backend: {backend}')


def statistical_filter(points, neighbors=24, std_ratio=2.0, workers=1):
    """SOR: keep mean k-neighbour distances <= global mean + ratio * sample std.

    The query point is excluded. This static density filter cannot identify
    moving objects and may remove distant sparse structures.
    """
    points = validate_points(points)
    if not isinstance(neighbors, int) or neighbors < 2 or len(points) <= neighbors:
        raise ValueError('SOR needs at least neighbors + 1 points, neighbors >= 2')
    if not np.isfinite(std_ratio) or std_ratio <= 0:
        raise ValueError('std_ratio must be finite and positive')
    tree = cKDTree(points[:, :3])
    means = np.empty(len(points), dtype=float)
    for start in range(0, len(points), 50000):
        stop = min(start + 50000, len(points))
        distances, _ = tree.query(points[start:stop, :3], k=neighbors + 1, workers=workers)
        means[start:stop] = distances[:, 1:].mean(axis=1)
    mean, std = float(means.mean()), float(means.std(ddof=1))
    threshold = mean + std_ratio * std
    keep = means <= threshold
    return points[keep].copy(), keep, {
        'neighbors': neighbors, 'std_ratio': std_ratio, 'std_ddof': 1,
        'mean_neighbor_distance_m': mean, 'std_neighbor_distance_m': std,
        'threshold_m': threshold, 'input_points': len(points),
        'retained_points': int(keep.sum()), 'removed_points': int((~keep).sum())}


def project_to_local_planes(points, radius_m=.3, neighbors=32, min_neighbors=10,
                            max_displacement_m=.03, max_surface_variation=.02,
                            min_spread_ratio=.15, workers=1):
    """One pass of bounded, Gaussian-weighted local plane projection.

    The fit uses only the unmodified input. It rejects poorly supported,
    line-like, non-planar or excessively displaced fits. This is not polynomial
    MLS, edge classification, or a correction to the estimated trajectory.
    """
    points = validate_points(points)
    if (radius_m <= 0 or not np.isfinite(radius_m) or neighbors < min_neighbors
            or min_neighbors < 3 or max_displacement_m <= 0
            or not np.isfinite(max_displacement_m)
            or not 0 < max_surface_variation < 1 or not 0 < min_spread_ratio <= 1):
        raise ValueError('Invalid local plane projection parameters')
    output = points.astype('<f4', copy=True)
    tree = cKDTree(points[:, :3])
    accepted = np.zeros(len(points), dtype=bool)
    for start in range(0, len(points), 5000):
        stop = min(start + 5000, len(points))
        distances, indices = tree.query(points[start:stop, :3], k=neighbors,
                                        distance_upper_bound=radius_m, workers=workers)
        valid = np.isfinite(distances)
        safe_indices = np.minimum(indices, max(len(points) - 1, 0))
        positions = points[safe_indices, :3].astype(np.float64)
        weights = np.exp(-np.square(distances / (radius_m / 2)))
        weights[~valid] = 0
        total = weights.sum(axis=1)
        center = np.einsum('nk,nkj->nj', weights, positions) / total[:, None]
        offsets = positions - center[:, None, :]
        covariance = np.einsum('nk,nki,nkj->nij', weights, offsets, offsets) / total[:, None, None]
        eigenvalues, eigenvectors = np.linalg.eigh(covariance)
        normals = eigenvectors[:, :, 0]
        variation = eigenvalues[:, 0] / np.maximum(eigenvalues.sum(axis=1), 1e-20)
        spread = eigenvalues[:, 1] / np.maximum(eigenvalues[:, 2], 1e-20)
        displacement = np.einsum('ni,ni->n', points[start:stop, :3] - center, normals)
        eligible = ((valid.sum(axis=1) >= min_neighbors) & (variation <= max_surface_variation)
                    & (spread >= min_spread_ratio) & (eigenvalues[:, 1] > 1e-12)
                    & (abs(displacement) <= max_displacement_m))
        projected = (points[start:stop, :3] - displacement[:, None] * normals).astype('<f4')
        # Apply the bound to the coordinates we actually store, including rounding.
        stored_displacement = np.linalg.norm(projected.astype(float) - points[start:stop, :3], axis=1)
        eligible &= stored_displacement <= max_displacement_m
        output[start:stop, :3][eligible] = projected[eligible]
        accepted[start:stop] = eligible
    displacement = np.linalg.norm(output[:, :3].astype(float) - points[:, :3], axis=1)
    moved = displacement > 1e-6
    return output, {'method': 'bounded weighted local plane projection; one pass',
                    'radius_m': radius_m, 'neighbors': neighbors, 'min_neighbors': min_neighbors,
                    'max_displacement_m': max_displacement_m,
                    'max_surface_variation': max_surface_variation, 'min_spread_ratio': min_spread_ratio,
                    'input_points': len(points), 'retained_points': len(output),
                    'eligible_points': int(accepted.sum()), 'moved_points': int(moved.sum()),
                    'displacement_p50_m': float(np.median(displacement)) if len(points) else 0,
                    'displacement_p95_m': float(np.percentile(displacement, 95)) if len(points) else 0,
                    'displacement_max_m': float(displacement.max()) if len(points) else 0}


def bounds(points):
    return {'min': points[:, :3].min(axis=0).astype(float).tolist(),
            'max': points[:, :3].max(axis=0).astype(float).tolist()}


def export_tiles(points, output_dir, prefix, tile_size_m=24):
    """Export independent spatial tiles; IDs refer to the same cells at every LOD."""
    points = validate_points(points)
    if not np.isfinite(tile_size_m) or tile_size_m <= 0:
        raise ValueError('tile_size_m must be finite and positive')
    root = Path(output_dir)
    prefix = Path(prefix)
    if prefix.is_absolute() or '..' in prefix.parts:
        raise ValueError('Tile prefix must be a relative subdirectory')
    directory = root / prefix
    directory.mkdir(parents=True, exist_ok=True)
    if not len(points):
        return []
    keys = np.floor(points[:, :3].astype(float) / tile_size_m).astype(np.int64)
    order = np.lexsort((keys[:, 2], keys[:, 1], keys[:, 0]))
    sorted_keys = keys[order]
    starts = np.r_[0, np.flatnonzero(np.any(sorted_keys[1:] != sorted_keys[:-1], axis=1)) + 1, len(points)]
    tiles = []
    for first, last in zip(starts[:-1], starts[1:]):
        ix, iy, iz = sorted_keys[first]
        identity = f'x{ix}_y{iy}_z{iz}'
        relative = (prefix / f'{identity}.bin.gz').as_posix()
        cloud = points[order[first:last]].astype('<f4')
        # Gzip mtime is fixed; no filename field, so repeated exports match.
        payload = gzip.compress(cloud.tobytes(), compresslevel=6, mtime=0)
        (root / relative).write_bytes(payload)
        tiles.append({'id': identity, 'url': relative, 'point_count': len(cloud),
                      'bounds': bounds(cloud), 'byte_size': len(payload),
                      'sha256': hashlib.sha256(payload).hexdigest()})
    return tiles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--map-pcd', type=Path, required=True)
    parser.add_argument('--projection', type=Path, default=ROOT / 'data/outdoor01/tcp/map_projection.json')
    parser.add_argument('--trajectory', type=Path, default=ROOT / 'data/outdoor01/tcp/trajectory_tum.txt')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'output/map_processing')
    parser.add_argument('--viewer-data', type=Path, default=ROOT / 'docs/viewer/data')
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--voxel-backend', choices=('numpy', 'cpp'), default='numpy')
    parser.add_argument('--voxel-library', type=Path)
    # An explicit fixed box, chosen for a facade/ground detail, not error minimisation.
    parser.add_argument('--roi', type=float, nargs=6, metavar=('X0', 'X1', 'Y0', 'Y1', 'Z0', 'Z1'),
                        default=[10, 30, -12, 8, -2, 10])
    args = parser.parse_args()
    if args.workers < 1:
        parser.error('--workers must be positive')
    try:
        voxel_function = get_voxel_function(args.voxel_backend, args.voxel_library)
        # Resolve explicit native-library failures before reading the full map.
        voxel_function(np.empty((0, 4)), .5)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    roi = np.asarray(args.roi).reshape(3, 2)
    if not np.isfinite(roi).all() or np.any(roi[:, 1] <= roi[:, 0]):
        parser.error('--roi must contain finite increasing coordinate ranges')
    for directory in (args.output_dir, args.viewer_data):
        directory.mkdir(parents=True, exist_ok=True)
    manifest_path = args.viewer_data / 'manifest.json'
    if manifest_path.exists():
        parser.error('Viewer manifest already exists; use a new export directory')
    started = time.perf_counter()
    points, metadata = read_pcd(args.map_pcd)
    projection = json.loads(args.projection.read_text())
    rotation = np.asarray(projection['gravity_rotation'], dtype=float)
    if (rotation.shape != (3, 3) or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-8)
            or not np.isclose(np.linalg.det(rotation), 1)):
        raise ValueError('gravity_rotation must be a proper rotation')
    points = points.astype(np.float64)
    points[:, :3] = points[:, :3] @ rotation.T
    trajectory = np.loadtxt(args.trajectory)
    transformed = trajectory[:, 1:4] @ rotation.T
    (args.viewer_data / 'trajectory.json').write_text(json.dumps({
        'positions': transformed.tolist(), 'timestamps': trajectory[:, 0].tolist(),
        'description': 'Raw estimated trajectory, gravity rotation only; no smoothing or GT fit.'}, separators=(',', ':')) + '\n')
    report = {'source': metadata, 'display_transform': projection['display_transform'],
              'voxel_backend': args.voxel_backend,
              'gravity_rotation': rotation.tolist(), 'workers': args.workers,
              'python_version': platform.python_version(), 'numpy_version': np.__version__,
              'input_read_rotate_seconds': time.perf_counter() - started,
              'voxel_origin_m': [0, 0, 0], 'tile_size_m': 24, 'voxel_levels': [],
              'transform_dtype': 'float64', 'tile_dtype': 'little-endian float32'}
    viewer = {'schema_version': 1, 'title': 'Outdoor01 · MID-360', 'units': 'm', 'up_axis': 'Z',
              'source': {**metadata, 'sequence': 'M3DGR Outdoor01',
                         'display_transform': projection['display_transform'],
                         'gravity_rotation': rotation.tolist()}, 'bounds': bounds(points),
              'color_ranges': {'height': [-2, 20], 'intensity': [0, 106]},
              'trajectory': {'url': 'trajectory.json', 'count': len(trajectory)},
              'tile_size_m': 24, 'default_variant': 'sor', 'variants': []}
    voxel_variant = {'id': 'voxel', 'label': '体素地图',
                     'description': '每体素取质心；显示层处理，原始位姿不变。',
                     'bounds': bounds(points), 'lods': []}
    medium = None
    for identity, size in (('coarse', .5), ('medium', .2), ('fine', .1)):
        then = time.perf_counter()
        cloud, counts = voxel_function(points, size)
        seconds = time.perf_counter() - then
        np.savez_compressed(args.output_dir / f'voxel_{identity}.npz', points=cloud, counts=counts)
        tiles = export_tiles(cloud, args.viewer_data, f'voxel/{identity}')
        voxel_variant['lods'].append({'id': identity, 'voxel_size_m': size,
                                      'point_count': len(cloud), 'tiles': tiles})
        report['voxel_levels'].append({'id': identity, 'voxel_size_m': size,
            'input_points': len(points), 'retained_points': len(cloud),
            'reduction_percent': (1 - len(cloud) / len(points)) * 100,
            'voxel_seconds': seconds, 'summed_occupancy': int(counts.sum()),
            'tile_bytes': sum(tile['byte_size'] for tile in tiles)})
        print(f'VOXEL {size:.2f} m: {len(cloud):,} points in {seconds:.2f} s', flush=True)
        if identity == 'medium':
            medium = cloud
    voxel_variant['point_count'] = voxel_variant['lods'][-1]['point_count']
    viewer['variants'].append(voxel_variant)
    then = time.perf_counter()
    filtered, keep, filter_report = statistical_filter(medium, workers=args.workers)
    filter_report['seconds'] = time.perf_counter() - then
    report['statistical_filter'] = filter_report
    np.savez_compressed(args.output_dir / 'sor_medium.npz', points=filtered, keep=keep)
    sor_variant = {'id': 'sor', 'label': '去除稀疏离群点',
                   'description': '0.20 m 体素 + SOR (24 邻点、2σ)；可能损失远处稀疏结构。',
                   'point_count': len(filtered), 'bounds': bounds(filtered), 'lods': []}
    coarse_sor, _ = voxel_function(filtered, .5)
    for identity, size, cloud in (('coarse', .5, coarse_sor), ('medium', .2, filtered)):
        sor_variant['lods'].append({'id': identity, 'voxel_size_m': size,
            'point_count': len(cloud), 'tiles': export_tiles(cloud, args.viewer_data, f'sor/{identity}')})
    viewer['variants'].append(sor_variant)
    raw_local = points[np.all((points[:, :3] >= roi[:, 0]) & (points[:, :3] < roi[:, 1]), axis=1)]
    local, _ = voxel_function(raw_local, .05)
    if len(local) <= 24:
        raise ValueError('ROI has too few points; select an explicit larger region')
    clean_local, _, local_filter = statistical_filter(local, neighbors=24, std_ratio=2, workers=args.workers)
    smooth, projection_report = project_to_local_planes(clean_local, radius_m=.3,
        neighbors=32, min_neighbors=10, max_displacement_m=.03, workers=args.workers)
    np.savez_compressed(args.output_dir / 'local_comparison.npz',
                        raw=raw_local, voxel=local, sor=clean_local, plane=smooth, roi=roi)
    report['local_comparison'] = {'roi_xyz_m': roi.tolist(), 'voxel_size_m': .05,
                                 'raw_points': len(raw_local), 'voxel_points': len(local),
                                 'statistical_filter': local_filter, 'plane_projection': projection_report}
    report['total_processing_seconds'] = time.perf_counter() - started
    report['scope'] = ('Offline display processing only. Point reduction, smoothing and render time '
                       'do not measure localisation accuracy or online mapping speed.')
    (args.output_dir / 'processing_report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    (args.viewer_data / 'processing_report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    viewer['processing_report_url'] = 'processing_report.json'
    manifest_path.write_text(json.dumps(viewer, indent=2, ensure_ascii=False) + '\n')
    print(f'Exported {sum(len(v["lods"]) for v in viewer["variants"])} LOD levels; source untouched.', flush=True)


if __name__ == '__main__':
    main()
