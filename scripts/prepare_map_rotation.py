"""Prepare a fixed dense point set for a reproducible offline map animation.

Reads the published fine voxel tiles, verifies their hashes, then applies the
same statistical outlier filter used by process_map. No trajectory fitting or
surface projection is performed.
"""
import argparse
import gzip
import json
from pathlib import Path
import platform
import shutil
import time

import numpy as np

from process_map import bounds, sha256, statistical_filter

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error('--workers must be positive')
    if args.output_dir.exists():
        parser.error('Use a new output directory to preserve previous exports')

    started = time.perf_counter()
    manifest_path = ROOT / 'docs/viewer/data/manifest.json'
    manifest = json.loads(manifest_path.read_text())
    variant = next(item for item in manifest['variants'] if item['id'] == 'voxel')
    lod = min(variant['lods'], key=lambda item: item['voxel_size_m'])
    clouds = []
    for tile in lod['tiles']:
        path = (manifest_path.parent / tile['url']).resolve()
        if not path.is_relative_to(manifest_path.parent.resolve()):
            raise ValueError('Tile path must remain inside the published data directory')
        if path.stat().st_size != tile['byte_size'] or sha256(path) != tile['sha256']:
            raise ValueError(f'Tile checksum mismatch: {tile["url"]}')
        expected_bytes = tile['point_count'] * 16
        with gzip.open(path, 'rb') as stream:
            payload = stream.read(expected_bytes + 1)
        if len(payload) != expected_bytes:
            raise ValueError(f'Tile point count mismatch: {tile["url"]}')
        clouds.append(np.frombuffer(payload, dtype='<f4').reshape(-1, 4))
    points = np.concatenate(clouds)
    if len(points) != lod['point_count']:
        raise ValueError('Fine-level point count does not match the manifest')
    print(f'Loaded and verified {len(points):,} points from {len(clouds)} tiles.', flush=True)
    filtered, _, report = statistical_filter(points, neighbors=24, std_ratio=2., workers=args.workers)

    trajectory_path = manifest_path.parent / manifest['trajectory']['url']
    trajectory = json.loads(trajectory_path.read_text())
    positions = np.asarray(trajectory['positions'], dtype=float)
    if (positions.shape != (manifest['trajectory']['count'], 3)
            or not np.isfinite(positions).all()):
        raise ValueError('Invalid published trajectory')
    args.output_dir.mkdir(parents=True)
    cloud_path = args.output_dir / 'points.bin'
    filtered.astype('<f4', copy=False).tofile(cloud_path)
    shutil.copy2(trajectory_path, args.output_dir / 'trajectory.json')
    metadata = {
        'schema_version': 1,
        'source': manifest['source'],
        'source_manifest': 'docs/viewer/data/manifest.json',
        'source_manifest_sha256': sha256(manifest_path),
        'verified_input_tiles': len(clouds),
        'voxel_size_m': lod['voxel_size_m'],
        'statistical_filter': report,
        'point_count': len(filtered),
        'point_format': 'little-endian float32 XYZI, 16 bytes per point',
        'point_file_sha256': sha256(cloud_path),
        'point_file_bytes': cloud_path.stat().st_size,
        'bounds': bounds(filtered),
        'color_ranges': manifest['color_ranges'],
        'trajectory_count': len(positions),
        'trajectory_sha256': sha256(trajectory_path),
        'python_version': platform.python_version(),
        'numpy_version': np.__version__,
        'workers': args.workers,
        'preparation_seconds': time.perf_counter() - started,
        'source_sha256': {name: sha256(ROOT / name) for name in
                          ('scripts/prepare_map_rotation.py', 'scripts/process_map.py')},
        'scope': 'Offline display filter only; unchanged estimated poses; no surface smoothing or accuracy claim.',
    }
    (args.output_dir / 'preparation.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print(f'Prepared {len(filtered):,} points; removed {report["removed_points"]:,} sparse outliers.', flush=True)


if __name__ == '__main__':
    main()
