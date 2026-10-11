"""Benchmark offline voxel calls on one common float64 XYZI input.

Each trial uses a fresh Linux process. Core time includes the public Python
function and native wrapper, but excludes NPY loading, saving and parent-side
comparison. RSS is the worker's post-exec VmHWM, including interpreter/imports,
input, processing, saving and hashing. Raw ru_maxrss is retained separately:
on Linux it can inherit the parent's pre-exec footprint. Repeated timings on
one map are not independent scenes or evidence of online SLAM acceleration.
"""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
import time

import numpy as np

from process_map import get_voxel_function, read_pcd, sha256

ROOT = Path(__file__).resolve().parents[1]
THREAD_ENV = {'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1'}


def _positive(value, label, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{label} must be finite and positive')
    return value


def make_schedule(voxel_sizes, repeats):
    """Counterbalance order across repeats; all workers run sequentially."""
    _positive(repeats, 'repeats', integer=True)
    if not voxel_sizes or len(set(voxel_sizes)) != len(voxel_sizes):
        raise ValueError('Voxel sizes must be nonempty and unique')
    for size in voxel_sizes:
        _positive(size, 'voxel size')
    return [{'repeat': repeat, 'voxel_size_m': size, 'backend': backend}
            for repeat in range(1, repeats + 1) for size in voxel_sizes
            for backend in (('numpy', 'cpp') if repeat % 2 else ('cpp', 'numpy'))]


def _check_output(output, input_points):
    points, counts = output
    if (points.ndim != 2 or points.shape[1] != 4 or points.dtype != np.dtype('<f4')
            or counts.shape != (len(points),) or counts.dtype != np.dtype('int64')
            or not np.isfinite(points).all() or np.any(counts <= 0)):
        raise ValueError('Invalid voxel output shape, dtype, finite values or counts')
    total = int(np.sum(counts, dtype=object))  # Python integers cannot wrap on overflow.
    if total != input_points:
        raise ValueError(f'Occupancy does not conserve input points: {total} != {input_points}')
    return total


def compare_outputs(reference, candidate, input_points):
    """Check aligned output counts and float32 centroids against NumPy.

    Integer binning semantics are tested independently at exact/negative
    boundaries. We do not recover integer keys from rounded float32 centroids.
    """
    _positive(input_points, 'input_points', integer=True)
    total = _check_output(reference, input_points)
    _check_output(candidate, input_points)
    if reference[0].shape != candidate[0].shape or not np.array_equal(reference[1], candidate[1]):
        raise ValueError('Output voxel counts or ordered occupancies differ')
    xyz_difference = intensity_difference = 0.
    for start in range(0, len(reference[0]), 250000):
        before = reference[0][start:start + 250000].astype(np.float64)
        after = candidate[0][start:start + 250000].astype(np.float64)
        difference = np.abs(before - after)
        xyz_difference = max(xyz_difference, float(difference[:, :3].max(initial=0)))
        intensity_difference = max(intensity_difference, float(difference[:, 3].max(initial=0)))
        if (not np.allclose(after[:, :3], before[:, :3], rtol=2e-7, atol=2e-6)
                or not np.allclose(after[:, 3], before[:, 3], rtol=2e-7, atol=2e-5)):
            raise ValueError('Ordered centroids exceed float32 comparison tolerances')
    return {'counts_equal': True, 'summed_occupancy': total, 'output_points': len(reference[0]),
            'xyz_max_abs_difference_m': xyz_difference,
            'intensity_max_abs_difference': intensity_difference,
            'relative_tolerance': 2e-7, 'xyz_absolute_tolerance_m': 2e-6,
            'intensity_absolute_tolerance': 2e-5,
            'scope': 'Ordered counts and centroids; integer binning boundaries covered by unit tests.'}


def summarize_trials(trials, voxel_sizes, repeats):
    expected = {(row['repeat'], row['voxel_size_m'], row['backend'])
                for row in make_schedule(voxel_sizes, repeats)}
    actual = {(row['repeat'], row['voxel_size_m'], row['backend']) for row in trials}
    if actual != expected or len(trials) != len(expected):
        raise ValueError('Missing, duplicate or unexpected benchmark trials')
    for row in trials:
        for field in ('core_seconds', 'peak_process_rss_mib'):
            _positive(row[field], field)
        for field in ('input_points', 'output_points', 'summed_occupancy'):
            _positive(row[field], field, integer=True)
        if row['input_points'] != row['summed_occupancy'] or row['output_points'] > row['input_points']:
            raise ValueError('Invalid trial point accounting')
    if len({row['input_points'] for row in trials}) != 1:
        raise ValueError('Trials used different input counts')
    result = []
    for size in voxel_sizes:
        subset = [row for row in trials if row['voxel_size_m'] == size]
        if len({row['output_points'] for row in subset}) != 1:
            raise ValueError('Trials disagree on voxel count')
        item = {'voxel_size_m': size, 'output_points': subset[0]['output_points']}
        for backend in ('numpy', 'cpp'):
            selected = sorted((row for row in subset if row['backend'] == backend), key=lambda row: row['repeat'])
            seconds = [row['core_seconds'] for row in selected]
            rss = [row['peak_process_rss_mib'] for row in selected]
            item[backend] = {'seconds': seconds, 'median_seconds': float(np.median(seconds)),
                             'minimum_seconds': min(seconds), 'maximum_seconds': max(seconds),
                             'peak_process_rss_mib': rss, 'median_peak_process_rss_mib': float(np.median(rss))}
        item['median_speedup_numpy_over_cpp'] = item['numpy']['median_seconds'] / item['cpp']['median_seconds']
        result.append(item)
    return result


def run_worker(input_npy, backend, size, output_dir, library):
    if sys.platform != 'linux':
        raise ValueError('RSS measurement requires Linux /proc/self/status')
    _positive(size, 'voxel size')
    output_dir.mkdir(parents=True, exist_ok=False)
    load_start = time.perf_counter()
    points = np.load(input_npy, allow_pickle=False)
    if points.dtype != np.dtype('float64') or points.ndim != 2 or points.shape[1] != 4 or not len(points):
        raise ValueError('Benchmark input must be nonempty float64 N x 4 XYZI')
    load_seconds = time.perf_counter() - load_start
    function = get_voxel_function(backend, library if backend == 'cpp' else None)
    started = time.perf_counter()
    cloud, counts = function(points, size)
    seconds = time.perf_counter() - started
    summed = _check_output((cloud, counts), len(points))
    save_start = time.perf_counter()
    np.save(output_dir / 'points.npy', cloud, allow_pickle=False)
    np.save(output_dir / 'counts.npy', counts, allow_pickle=False)
    save_seconds = time.perf_counter() - save_start
    output_hashes = {name: sha256(output_dir / name) for name in ('points.npy', 'counts.npy')}
    memory = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
    peak_value, peak_unit = memory['VmHWM'].split()
    if peak_unit != 'kB':
        raise ValueError('Unexpected /proc VmHWM units')
    report = {'backend': backend, 'voxel_size_m': size, 'input_points': len(points),
              'output_points': len(cloud), 'summed_occupancy': summed,
              'core_seconds': seconds, 'load_seconds': load_seconds, 'save_seconds': save_seconds,
              'peak_process_rss_mib': int(peak_value) / 1024.,
              'rss_source': '/proc/self/status VmHWM (since exec)',
              'ru_maxrss_mib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.,
              'worker_pid': os.getpid(), 'output_sha256': output_hashes}
    (output_dir / 'trial.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')


def _environment(library):
    cpu = next((line.split(':', 1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines()
                if line.startswith('model name')), 'unknown')
    cache = library.parent / 'CMakeCache.txt'
    build = [line for line in cache.read_text().splitlines()
             if line.startswith(('CMAKE_BUILD_TYPE:', 'CMAKE_CXX_COMPILER:', 'CMAKE_CXX_FLAGS:',
                                 'CMAKE_CXX_FLAGS_RELEASE:', 'FASTLIO_ENABLE_SANITIZERS:'))] if cache.exists() else []
    compiler = next((line.partition('=')[2] for line in build
                     if line.startswith('CMAKE_CXX_COMPILER:')), None)
    compiler_version = (subprocess.run([compiler, '--version'], text=True, capture_output=True,
                                       check=True).stdout.splitlines()[0] if compiler else None)
    return {'cpu_model': cpu, 'logical_cpus': os.cpu_count(),
            'cpu_affinity': sorted(os.sched_getaffinity(0)), 'machine': platform.machine(),
            'kernel': platform.release(), 'python': platform.python_version(), 'numpy': np.__version__,
            'thread_environment': THREAD_ENV, 'cmake_cache': build,
            'compiler_path': compiler, 'compiler': compiler_version,
            'native_library_sha256': sha256(library)}


def run_benchmark(args):
    if sys.platform != 'linux':
        raise ValueError('This benchmark requires Linux /proc/self/status')
    schedule = make_schedule(args.voxel_sizes, args.repeats)
    library = (args.voxel_library or ROOT / 'output/native-build/libfastlio_voxel.so').resolve(strict=True)
    # Fail before allocating the full input if the requested library is unusable.
    get_voxel_function('cpp', library)(np.empty((0, 4)), .5)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    points, source = read_pcd(args.map_pcd)
    projection = json.loads(args.projection.read_text())
    rotation = np.asarray(projection['gravity_rotation'], dtype=np.float64)
    if (rotation.shape != (3, 3) or not np.isfinite(rotation).all()
            or not np.allclose(rotation.T @ rotation, np.eye(3), rtol=0, atol=1e-8)
            or not np.isclose(np.linalg.det(rotation), 1, rtol=0, atol=1e-8)):
        raise ValueError('gravity_rotation must be a proper rotation')
    points = points.astype(np.float64)
    points[:, :3] = points[:, :3] @ rotation.T
    input_path = args.output_dir / 'input.npy'
    np.save(input_path, points, allow_pickle=False)
    input_metadata = {'points': len(points), 'dtype': 'float64 XYZI', 'units': 'XYZ metres',
                      'source': source, 'projection_sha256': sha256(args.projection),
                      'gravity_rotation': rotation.tolist(), 'input_npy_sha256': sha256(input_path)}
    del points
    (args.output_dir / 'input_metadata.json').write_text(json.dumps(input_metadata, indent=2) + '\n')
    report = {'schema_version': 1, 'started_utc': started, 'input': input_metadata,
              'environment': _environment(library), 'repeats': args.repeats, 'trials': [], 'comparisons': [],
              'protocol': {'order': 'Repeats alternate NumPy/C++ and C++/NumPy; sizes in declared order.',
                           'processes': 'One fresh process per trial; sequential execution; no warmup.',
                           'timing_scope': 'Public voxel call including validation and native wrapper; excludes input load/save and parent comparison.',
                           'rss_scope': 'Linux /proc/self/status VmHWM / 1024, worker MiB since exec including interpreter, imports, input, processing, saving and hashing. Raw ru_maxrss separately retained; it may include parent pre-exec memory.',
                           'source_transform': 'Existing gravity rotation only, no alignment to reference trajectory.',
                           'grouping': 'floor(XYZ / voxel_size), zero origin, ordered integer XYZ keys.',
                           'limitations': ['One map, three sizes; repeats are software timings, not independent scenes.',
                                           'No CPU pinning or exclusive host; load may vary.',
                                           'Offline map filtering only; not online estimator speed or position accuracy.']}}
    code_paths = [ROOT / 'scripts' / name for name in ('benchmark_voxel.py', 'process_map.py', 'native_voxel.py')]
    code_paths += [ROOT / 'CMakeLists.txt'] + sorted((ROOT / 'cpp').rglob('*.cpp')) + sorted((ROOT / 'cpp').rglob('*.h')) + sorted((ROOT / 'cpp').rglob('*.hpp'))
    report['source_sha256'] = {str(path.relative_to(ROOT)): sha256(path) for path in code_paths}
    pending = {}
    for index, trial in enumerate(schedule):
        directory = args.output_dir / 'trials' / f'{index + 1:02d}_{trial["backend"]}'
        command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--input-npy', str(input_path),
                   '--backend', trial['backend'], '--voxel-size-m', str(trial['voxel_size_m']),
                   '--output-dir', str(directory), '--voxel-library', str(library)]
        load_before = list(os.getloadavg())
        result = subprocess.run(command, env={**os.environ, **THREAD_ENV}, text=True, capture_output=True)
        if result.returncode:
            raise ValueError(f'Worker {index + 1} failed ({result.returncode}): {result.stderr[-2000:]}')
        metrics = json.loads((directory / 'trial.json').read_text())
        report['trials'].append({**trial, **metrics, 'load_average_before': load_before})
        key = (trial['repeat'], trial['voxel_size_m'])
        pending.setdefault(key, {})[trial['backend']] = directory
        print(f'{index + 1}/{len(schedule)} {trial["backend"]} {trial["voxel_size_m"]:.2f} m '
              f'{metrics["core_seconds"]:.3f} s / {metrics["peak_process_rss_mib"]:.1f} MiB', flush=True)
        if len(pending[key]) == 2:
            outputs = [(np.load(pending[key][backend] / 'points.npy', mmap_mode='r'),
                        np.load(pending[key][backend] / 'counts.npy', mmap_mode='r')) for backend in ('numpy', 'cpp')]
            comparison = compare_outputs(*outputs, input_metadata['points'])
            report['comparisons'].append({'repeat': key[0], 'voxel_size_m': key[1], **comparison})
            del outputs, pending[key]
        (args.output_dir / 'progress.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    report['summary'] = summarize_trials(report['trials'], args.voxel_sizes, args.repeats)
    report['finished_utc'] = datetime.now(timezone.utc).isoformat()
    (args.output_dir / 'benchmark.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--map-pcd', type=Path)
    parser.add_argument('--projection', type=Path, default=ROOT / 'data/outdoor01/tcp/map_projection.json')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--voxel-library', type=Path)
    parser.add_argument('--voxel-sizes', type=float, nargs='+', default=[.5, .2, .1])
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--input-npy', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--backend', choices=('numpy', 'cpp'), help=argparse.SUPPRESS)
    parser.add_argument('--voxel-size-m', type=float, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.worker:
            if args.input_npy is None or args.backend is None or args.voxel_size_m is None:
                parser.error('Worker needs input-npy, backend and voxel-size-m')
            run_worker(args.input_npy, args.backend, args.voxel_size_m, args.output_dir, args.voxel_library)
        else:
            if args.map_pcd is None:
                parser.error('--map-pcd is required')
            run_benchmark(args)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == '__main__':
    sys.exit(main())
