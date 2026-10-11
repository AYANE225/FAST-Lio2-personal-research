"""A regression gate must fail closed on incomplete or incomparable evidence."""
import copy
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


def run_data(name='recording'):
    return {key: json.loads((ROOT / f'data/outdoor01/{name}/{key}.json').read_text())
            for key in ('metrics', 'run_summary')}


def example_limits():
    return {'schema_version': 1, 'description': 'Post-hoc example tolerances.',
            'absolute': {'core_p95_ms': 20., 'combined_rss_mib': 350.,
                         'position_rmse_m': .3, 'queue_fill_ratio': 1.},
            'relative': {'core_p95_ms': .2, 'combined_rss_mib': .1,
                         'position_rmse_m': .05}}


def evaluate(baseline, candidate, limits=None):
    from check_regression import evaluate_regression
    return evaluate_regression(baseline, candidate, example_limits() if limits is None else limits)


def test_identical_complete_run_passes_without_mutating_inputs():
    baseline = run_data()
    snapshot = copy.deepcopy(baseline)
    report = evaluate(baseline, baseline)
    assert report['exit_code'] == 0
    assert report['status'] == 'passed'
    assert report['checks'] and all(row['status'] == 'passed' for row in report['checks'])
    assert baseline == snapshot


def test_real_surf03_is_comparable_but_exceeds_example_twenty_percent_time_limit():
    report = evaluate(run_data(), run_data('surf03'))
    assert report['exit_code'] == 1
    failures = [row['name'] for row in report['checks'] if row['status'] == 'failed']
    assert failures == ['candidate.core_p95_ms.relative']
    row = next(row for row in report['checks'] if row['name'] == failures[0])
    assert row['actual'] == pytest.approx(13.97054)
    assert row['limit'] == pytest.approx(13.7453088)


@pytest.mark.parametrize('metric,field,value', [
    ('core_processing_time_ms', 'p95', 18.),
    ('position_reference_comparison', 'rmse_m', .28),
])
def test_performance_and_position_regressions_return_failed(metric, field, value):
    candidate = run_data()
    candidate['metrics'][metric][field] = value
    if metric == 'core_processing_time_ms':
        candidate['metrics'][metric]['p99'] = 19.
    report = evaluate(run_data(), candidate)
    assert report['exit_code'] == 1
    assert any(row['status'] == 'failed' and row['name'].endswith('.relative')
               for row in report['checks'])


@pytest.mark.parametrize('path,value', [
    (('run_summary', 'completed'), False),
    (('run_summary', 'mapping_exit_code'), 7),
    (('run_summary', 'recording', 'completed'), False),
    (('run_summary', 'recording', 'error'), 'write failed'),
    (('metrics', 'recording', 'stamps_equal_odometry'), False),
])
def test_incomplete_or_failed_run_cannot_pass(path, value):
    candidate = run_data()
    target = candidate
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    report = evaluate(run_data(), candidate)
    assert report['exit_code'] == 1
    assert report['status'] == 'failed'


@pytest.mark.parametrize('path,value', [
    (('run_summary', 'sequence'), 'different sequence'),
    (('run_summary', 'input_timestamps_sha256'), 'a' * 64),
    (('run_summary', 'playback_rate'), 2.),
    (('run_summary', 'dds_transport'), 'different transport'),
    (('metrics', 'timing_scope'), 'includes publication'),
    (('metrics', 'resources', 'cpu_convention'), 'machine percent'),
    (('metrics', 'position_reference_comparison', 'reference_sha256'), 'a' * 64),
    (('metrics', 'position_reference_comparison', 'pairing_tolerance_s'), .01),
    (('metrics', 'position_reference_comparison', 'alignment'), 'Sim(3)'),
    (('metrics', 'position_reference_comparison', 'scale'), 1.1),
])
def test_protocol_mismatch_stops_performance_judgement(path, value):
    candidate = run_data()
    target = candidate
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    report = evaluate(run_data(), candidate)
    assert report['exit_code'] == 2
    assert report['status'] == 'incomparable'
    assert report['checks'] == []
    assert report['protocol_differences']


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1., True, '12', None])
def test_invalid_metric_is_not_a_pass(value):
    candidate = run_data()
    candidate['metrics']['core_processing_time_ms']['p95'] = value
    assert evaluate(run_data(), candidate)['status'] == 'invalid'


def test_missing_fields_inconsistent_counts_and_hidden_nan_are_invalid():
    candidates = []
    missing = run_data()
    del missing['run_summary']['recording']['pending_frames']
    candidates.append(missing)
    count = run_data()
    count['metrics']['recording']['frames'] -= 1
    candidates.append(count)
    count = run_data()
    count['run_summary']['coverage']['processed_frames'] -= 1
    candidates.append(count)
    count = run_data()
    count['run_summary']['recording']['enqueued_points'] -= 1
    candidates.append(count)
    hidden = run_data()
    hidden['metrics']['extra'] = {'value': float('nan')}
    candidates.append(hidden)
    for candidate in candidates:
        report = evaluate(run_data(), candidate)
        assert report['exit_code'] == 2
        assert report['status'] == 'invalid'
        assert report['errors']


@pytest.mark.parametrize('difference', [0.2, 1.000001e-5])
def test_coverage_rejects_timestamp_matches_outside_supported_tolerance(difference):
    candidate = run_data()
    for key in ('metrics', 'run_summary'):
        candidate[key]['coverage']['maximum_input_stamp_match_difference_s'] = difference
    report = evaluate(run_data(), candidate)
    assert report['status'] == 'invalid' and report['exit_code'] == 2
    assert not report['checks']


def test_coverage_accepts_timestamp_match_at_supported_tolerance():
    candidate = run_data()
    for key in ('metrics', 'run_summary'):
        candidate[key]['coverage']['maximum_input_stamp_match_difference_s'] = 1e-5
    assert evaluate(run_data(), candidate)['exit_code'] == 0


@pytest.mark.parametrize('cpu_percent', [.1, .7])
def test_exported_constant_resource_samples_allow_mean_rounding(cpu_percent):
    from evaluate_engineering import resource_metrics
    candidate = run_data()
    candidate['metrics']['resources'] = resource_metrics([
        {'mapping': {'rss_bytes': 1048576, 'cpu_percent': cpu_percent},
         'recorder': {'rss_bytes': 1048576, 'cpu_percent': .2}}
        for _ in range(3)
    ])
    report = evaluate(run_data(), candidate)
    assert report['status'] == 'passed', report['errors']
    assert report['exit_code'] == 0


@pytest.mark.parametrize('mean', [.1 - 1e-6, .1 + 1e-6])
def test_resource_mean_materially_outside_sample_range_is_invalid(mean):
    from evaluate_engineering import resource_metrics
    candidate = run_data()
    resources = resource_metrics([
        {'mapping': {'rss_bytes': 1048576, 'cpu_percent': .1},
         'recorder': {'rss_bytes': 1048576, 'cpu_percent': .2}}
        for _ in range(3)
    ])
    resources['mapping']['cpu_percent']['mean'] = mean
    candidate['metrics']['resources'] = resources
    report = evaluate(run_data(), candidate)
    assert report['status'] == 'invalid' and report['exit_code'] == 2
    assert not report['checks']


def test_performance_limit_remains_strict_at_next_float_above_threshold():
    baseline, candidate = run_data(), run_data()
    threshold = (baseline['metrics']['core_processing_time_ms']['p95'] *
                 (1 + example_limits()['relative']['core_p95_ms']))
    timing = candidate['metrics']['core_processing_time_ms']
    timing['p95'] = math.nextafter(threshold, math.inf)
    timing['p99'] = max(timing['p99'], timing['p95'])
    timing['maximum'] = max(timing['maximum'], timing['p99'])
    report = evaluate(baseline, candidate)
    assert report['status'] == 'failed' and report['exit_code'] == 1
    failures = [row['name'] for row in report['checks'] if row['status'] == 'failed']
    assert failures == ['candidate.core_p95_ms.relative']


@pytest.mark.parametrize('quantity', ['rss_mib', 'cpu_percent'])
@pytest.mark.parametrize('fault', ['zero_combined', 'excess_peak', 'wrong_mean', 'sample_count'])
def test_joint_resource_statistics_must_agree_with_process_summaries(quantity, fault):
    candidate = run_data()
    resources = candidate['metrics']['resources']
    combined = resources['combined'][quantity]
    if fault == 'zero_combined':
        for key in combined:
            if key != 'count':
                combined[key] = 0.
    elif fault == 'excess_peak':
        combined['maximum'] = sum(resources[role][quantity]['maximum']
                                  for role in ('mapping', 'recorder')) + 1.
    elif fault == 'wrong_mean':
        combined['mean'] *= .95
    else:
        resources['combined']['samples'] = 1
        for key in ('rss_mib', 'cpu_percent'):
            resources['combined'][key]['count'] = 1
    report = evaluate(run_data(), candidate)
    assert report['exit_code'] == 2 and report['status'] == 'invalid'
    assert not report['checks']


@pytest.mark.parametrize('field', ['write_seconds', 'elapsed_seconds', 'quaternion_max_norm_error'])
@pytest.mark.parametrize('fault', ['boolean', 'missing', 'negative'])
def test_recording_numeric_fields_are_required_real_numbers(field, fault):
    candidate = run_data()
    sources = ([candidate['metrics']['recording'], candidate['run_summary']['recording']]
               if field == 'write_seconds' else
               [candidate['run_summary' if field == 'elapsed_seconds' else 'metrics']['recording']])
    for source in sources:
        if fault == 'missing':
            del source[field]
        else:
            source[field] = True if fault == 'boolean' else -1.
    report = evaluate(run_data(), candidate)
    assert report['exit_code'] == 2 and report['status'] == 'invalid'


def test_recording_write_time_copies_must_agree():
    candidate = run_data()
    candidate['metrics']['recording']['write_seconds'] += 1
    assert evaluate(run_data(), candidate)['exit_code'] == 2


def test_recording_quaternion_error_must_respect_export_validation():
    candidate = run_data()
    candidate['metrics']['recording']['quaternion_max_norm_error'] = 1e-3
    assert evaluate(run_data(), candidate)['exit_code'] == 2


@pytest.mark.parametrize('field', ['queue_capacity', 'chunk_points', 'max_frame_points',
                                 'max_frame_bytes', 'chunk_frames', 'flush_interval',
                                 'status_interval', 'fsync'])
def test_recording_settings_reject_numbers_and_booleans_of_the_wrong_type(field):
    candidate = run_data()
    for key in ('metrics', 'run_summary'):
        candidate[key]['recording']['settings'][field] = 1 if field == 'fsync' else True
    assert evaluate(run_data(), candidate)['exit_code'] == 2


def test_recording_settings_require_declared_fields_in_both_copies():
    candidate = run_data()
    for key in ('metrics', 'run_summary'):
        del candidate[key]['recording']['settings']['flush_interval']
    assert evaluate(run_data(), candidate)['exit_code'] == 2


@pytest.mark.parametrize('fault', ['zero_blocks', 'more_blocks_than_frames',
                                 'insufficient_frame_capacity', 'insufficient_point_capacity',
                                 'adopted_blocks_exceed_total'])
def test_recording_blocks_must_account_for_written_frames_and_points(fault):
    candidate = run_data()
    record = candidate['run_summary']['recording']
    blocks = record['committed_blocks']
    if fault == 'zero_blocks':
        blocks = 0
    elif fault == 'more_blocks_than_frames':
        blocks = record['written_frames'] + 1
    elif fault == 'insufficient_frame_capacity':
        blocks = (record['written_frames'] - 1) // record['settings']['chunk_frames']
    elif fault == 'insufficient_point_capacity':
        blocks = (record['written_points'] - 1) // record['settings']['chunk_points']
    else:
        record['adopted_blocks'] = blocks + 1
    record['committed_blocks'] = blocks
    candidate['metrics']['recording']['committed_blocks'] = blocks
    report = evaluate(run_data(), candidate)
    assert report['status'] == 'invalid' and report['exit_code'] == 2


@pytest.mark.parametrize('change', ['unknown', 'missing', 'negative', 'bool', 'nan', 'queue'])
def test_invalid_or_misspelled_limits_are_rejected(change):
    limits = example_limits()
    if change == 'unknown':
        limits['relative']['core_p59_ms'] = .2
    elif change == 'missing':
        del limits['absolute']['position_rmse_m']
    elif change == 'queue':
        limits['absolute']['queue_fill_ratio'] = 1.1
    else:
        limits['relative']['core_p95_ms'] = {'negative': -1, 'bool': True,
                                             'nan': float('nan')}[change]
    assert evaluate(run_data(), run_data(), limits)['exit_code'] == 2


def write_inputs(tmp_path):
    for name in ('baseline', 'candidate'):
        directory = tmp_path / name
        directory.mkdir()
        for key, value in run_data().items():
            (directory / f'{key}.json').write_text(json.dumps(value))
    limits = tmp_path / 'limits.json'
    limits.write_text(json.dumps(example_limits()))
    return [sys.executable, str(ROOT / 'scripts/check_regression.py'),
            '--baseline-dir', str(tmp_path / 'baseline'),
            '--candidate-dir', str(tmp_path / 'candidate'), '--limits', str(limits)]


def test_cli_writes_report_with_hashes_and_success_code(tmp_path):
    command = write_inputs(tmp_path)
    result = subprocess.run(command + ['--output', str(tmp_path / 'report.json')],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / 'report.json').read_text())
    expected = hashlib.sha256((tmp_path / 'candidate/metrics.json').read_bytes()).hexdigest()
    assert report['inputs']['candidate']['metrics']['sha256'] == expected
    assert report['inputs']['limits']['sha256']


@pytest.mark.parametrize('content', ['{"coverage": {}, "coverage": {}}', '{"x": NaN}', '{', '[]'])
def test_cli_bad_json_is_code_two_and_never_success(tmp_path, content):
    command = write_inputs(tmp_path)
    (tmp_path / 'candidate/metrics.json').write_text(content)
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 2
    assert json.loads(result.stdout)['status'] == 'invalid'


def test_cli_failing_criteria_and_unwritable_report_have_nonzero_codes(tmp_path):
    command = write_inputs(tmp_path)
    path = tmp_path / 'candidate/run_summary.json'
    value = json.loads(path.read_text())
    value['completed'] = False
    path.write_text(json.dumps(value))
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 1
    result = subprocess.run(command + ['--output', str(tmp_path / 'baseline')],
                            capture_output=True, text=True)
    assert result.returncode == 2
