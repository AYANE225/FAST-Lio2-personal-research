"""Check experiment accounting and refuse incorrect results before reporting speed."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


def test_comparison_preserves_counts_and_measures_coordinates_and_intensity_separately():
    from benchmark_voxel import compare_outputs
    reference = np.array([[-.5, 0, 0, 10], [.5, 0, 0, 20]], dtype=np.float32)
    candidate = reference.copy()
    candidate[1, 0] += 1e-6
    candidate[0, 3] += 1e-5
    report = compare_outputs((reference, np.array([2, 3], dtype=np.int64)),
                             (candidate, np.array([2, 3], dtype=np.int64)), 5)
    assert report['counts_equal'] is True
    assert report['summed_occupancy'] == 5
    assert 0 < report['xyz_max_abs_difference_m'] < 2e-6
    assert 0 < report['intensity_max_abs_difference'] < 2e-5


@pytest.mark.parametrize('fault', ['dropped', 'reordered', 'wrong_counts', 'nan', 'negative', 'large_difference'])
def test_comparison_rejects_results_that_cannot_support_a_speed_claim(fault):
    from benchmark_voxel import compare_outputs
    points = np.array([[-.5, 0, 0, 10], [.5, 0, 0, 20]], dtype=np.float32)
    counts = np.array([2, 3], dtype=np.int64)
    candidate, candidate_counts = points.copy(), counts.copy()
    if fault == 'dropped':
        candidate, candidate_counts = candidate[:1], candidate_counts[:1]
    elif fault == 'reordered':
        candidate, candidate_counts = candidate[::-1], candidate_counts[::-1]
    elif fault == 'wrong_counts':
        candidate_counts[:] = [1, 4]
    elif fault == 'nan':
        candidate[0, 0] = np.nan
    elif fault == 'negative':
        candidate_counts[:] = [-1, 6]
    else:
        candidate[0, 0] += .01
    with pytest.raises(ValueError):
        compare_outputs((points, counts), (candidate, candidate_counts), 5)


def test_schedule_alternates_backend_order_and_summary_keeps_all_repeats():
    from benchmark_voxel import make_schedule, summarize_trials
    schedule = make_schedule([.5], 3)
    assert [row['backend'] for row in schedule] == ['numpy', 'cpp', 'cpp', 'numpy', 'numpy', 'cpp']
    assert [row['repeat'] for row in schedule] == [1, 1, 2, 2, 3, 3]
    trials = []
    for row, seconds in zip(schedule, (6., 2., 3., 8., 10., 4.)):
        trials.append({**row, 'core_seconds': seconds, 'peak_process_rss_mib': 100.,
                       'input_points': 100, 'output_points': 12, 'summed_occupancy': 100})
    summary = summarize_trials(trials, [.5], 3)
    assert summary[0]['numpy']['median_seconds'] == 8.
    assert summary[0]['cpp']['median_seconds'] == 3.
    assert summary[0]['median_speedup_numpy_over_cpp'] == pytest.approx(8 / 3)
    assert summary[0]['numpy']['seconds'] == [6., 8., 10.]
    with pytest.raises(ValueError):
        summarize_trials(trials[:-1], [.5], 3)
    bad = copy.deepcopy(trials)
    bad[0]['core_seconds'] = float('nan')
    with pytest.raises(ValueError):
        summarize_trials(bad, [.5], 3)


def test_worker_reports_real_call_and_separate_io_times(tmp_path):
    points = np.array([[-.2, 0, 0, 10], [-.8, 0, 0, 30], [.4, 0, 0, 20]], dtype=np.float64)
    source = tmp_path / 'input.npy'
    np.save(source, points)
    output = tmp_path / 'worker'
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/benchmark_voxel.py'), '--worker',
                             '--input-npy', str(source), '--backend', 'numpy',
                             '--voxel-size-m', '1', '--output-dir', str(output)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads((output / 'trial.json').read_text())
    assert report['input_points'] == 3 and report['output_points'] == 2
    assert report['summed_occupancy'] == 3
    for key in ('core_seconds', 'load_seconds', 'save_seconds', 'peak_process_rss_mib'):
        assert report[key] > 0
    np.testing.assert_allclose(np.load(output / 'points.npy'), [[-.5, 0, 0, 20], [.4, 0, 0, 20]])
    np.testing.assert_array_equal(np.load(output / 'counts.npy'), [2, 1])


def test_environment_reports_configured_compiler_when_path_default_differs(tmp_path, monkeypatch):
    from benchmark_voxel import _environment
    compiler = Path(shutil.which('c++')).resolve()
    selected = tmp_path / 'selected compiler'
    selected.symlink_to(compiler)
    expected = subprocess.check_output([str(selected), '--version'], text=True).splitlines()[0]
    default = tmp_path / 'c++'
    default.write_text('#!/bin/sh\nprintf "unrelated PATH compiler\\n"\n')
    default.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path))
    library = tmp_path / 'libfastlio_voxel.so'
    library.write_bytes(b'library identity for metadata test')
    (tmp_path / 'CMakeCache.txt').write_text(f'CMAKE_CXX_COMPILER:FILEPATH={selected}\n')
    environment = _environment(library)
    assert environment['compiler'] == expected
    assert environment['compiler_path'] == str(selected)


def test_environment_does_not_invent_compiler_for_library_without_cmake_cache(tmp_path):
    from benchmark_voxel import _environment
    library = tmp_path / 'libfastlio_voxel.so'
    library.write_bytes(b'library without build metadata')
    environment = _environment(library)
    assert environment['compiler'] is None
    assert environment['compiler_path'] is None


def test_process_map_backend_selection_does_not_silently_fallback(tmp_path):
    from process_map import get_voxel_function
    points = np.array([[.1, .1, .1, 20], [.2, .2, .2, 40]], dtype=np.float64)
    actual = get_voxel_function('numpy')(points, .5)
    np.testing.assert_allclose(actual[0], [[.15, .15, .15, 30]])
    np.testing.assert_array_equal(actual[1], [2])
    with pytest.raises(ValueError):
        get_voxel_function('misspelled')
    with pytest.raises(ValueError):
        get_voxel_function('numpy', tmp_path / 'unused.so')
    with pytest.raises((FileNotFoundError, OSError)):
        get_voxel_function('cpp', tmp_path / 'missing.so')(points, .5)


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux /proc and fork/exec RSS semantics')
def test_worker_rss_excludes_parent_memory_inherited_by_getrusage(tmp_path):
    # Linux ru_maxrss can retain the allocating parent's pre-exec footprint.
    # Reporting it as the worker's own high-water mark overstates small jobs.
    source = tmp_path / 'input.npy'
    np.save(source, np.array([[.1, 0, 0, 1]], dtype=np.float64))
    output = tmp_path / 'worker'
    command = [sys.executable, str(ROOT / 'scripts/benchmark_voxel.py'), '--worker',
               '--input-npy', str(source), '--backend', 'numpy', '--voxel-size-m', '1',
               '--output-dir', str(output)]
    parent = ('import json,subprocess,sys; allocation=bytearray(192*1024*1024); '
              'subprocess.run(json.loads(sys.argv[1]),check=True)')
    result = subprocess.run([sys.executable, '-c', parent, json.dumps(command)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads((output / 'trial.json').read_text())
    assert report['peak_process_rss_mib'] < 128
    assert report['ru_maxrss_mib'] > 192
    assert report['rss_source'] == '/proc/self/status VmHWM (since exec)'
