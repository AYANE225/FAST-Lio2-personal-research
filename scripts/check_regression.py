"""Check exported engineering summaries against explicit regression tolerances.

Exit codes: 0 passed, 1 valid/comparable evidence failed a criterion,
2 invalid data, incompatible measurement protocols, or an output error.
This checks summaries; it does not recompute raw timings or reference matches.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys


LIMITATIONS = [
    'Checks exported summaries only; does not recompute raw logs, timings or trajectories.',
    'Protocol equality covers declared fields, not unrecorded hardware/load differences.',
    'Example tolerances are post-hoc engineering choices, not industry standards or statistical tests.',
    'Position RMSE uses reused RTK positions with uncorrected lever arm and one SE(3) alignment.',
]
METRICS = ('core_p95_ms', 'combined_rss_mib', 'position_rmse_m')
COUNTERS = (
    'committed_blocks', 'dropped_frames', 'dropped_points', 'duplicate_stamp_frames',
    'enqueued_frames', 'enqueued_points', 'failed_frames', 'failed_points',
    'missing_intensity_frames', 'nonfinite_intensity_points', 'nonfinite_points',
    'nonmonotonic_frames', 'pending_frames', 'pending_points', 'queue_high_water',
    'queue_size', 'received_frames', 'received_points', 'written_frames',
    'written_input_points', 'written_points', 'adopted_blocks', 'quarantined_temporary_blocks',
)
ZERO_COUNTERS = (
    'dropped_frames', 'dropped_points', 'duplicate_stamp_frames', 'failed_frames',
    'failed_points', 'missing_intensity_frames', 'nonfinite_intensity_points',
    'nonfinite_points', 'nonmonotonic_frames', 'pending_frames', 'pending_points', 'queue_size',
)


def _get(value, path):
    for key in path.split('.'):
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f'Missing field: {path}')
        value = value[key]
    return value


def _number(value, label, *, integer=False, minimum=0):
    allowed = (int,) if integer else (int, float)
    if type(value) not in allowed:
        raise ValueError(f'{label} must be a number' + (' (integer)' if integer else ''))
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (minimum is not None and value < minimum):
        raise ValueError(f'{label} must be finite and >= {minimum}')
    return value


def _finite_tree(value, label):
    if isinstance(value, dict):
        for key, item in value.items():
            _finite_tree(item, f'{label}.{key}')
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _finite_tree(item, f'{label}[{index}]')
    elif type(value) in (int, float):
        _number(value, label, minimum=None)
    elif value is not None and type(value) not in (str, bool):
        raise ValueError(f'{label} contains a non-JSON value')


def _boolean(value, label):
    if type(value) is not bool:
        raise ValueError(f'{label} must be boolean')


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be a nonempty string')


def _digest(value, label):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise ValueError(f'{label} must be a lowercase SHA256')


def _equal(left, right, label):
    if left != right:
        raise ValueError(f'Inconsistent {label}: {left!r} != {right!r}')


def _stats(stats, count, label):
    _number(_get(stats, 'count'), f'{label}.count', integer=True, minimum=1)
    _equal(stats['count'], count, f'{label} sample count')
    order = ('minimum', 'median', 'p95', 'p99', 'maximum')
    values = [_number(_get(stats, name), f'{label}.{name}') for name in order]
    mean = _number(_get(stats, 'mean'), f'{label}.mean')
    # A floating-point reduction can put a constant sample mean just outside
    # its range. This tolerance checks summary consistency, not performance limits.
    below_minimum = (mean < values[0] and
                     not math.isclose(mean, values[0], rel_tol=1e-12, abs_tol=0.))
    above_maximum = (mean > values[-1] and
                     not math.isclose(mean, values[-1], rel_tol=1e-12, abs_tol=0.))
    if values != sorted(values) or below_minimum or above_maximum:
        raise ValueError(f'Inconsistent {label} distribution')


def _validate_resources(resources):
    for key in ('scope', 'cpu_convention'):
        _text(_get(resources, key), f'resources.{key}')
    for process in ('mapping', 'recorder', 'combined'):
        item = _get(resources, process)
        samples = _number(_get(item, 'samples'), f'{process}.samples', integer=True, minimum=1)
        for key in ('rss_mib', 'cpu_percent'):
            _stats(_get(item, key), samples, f'{process}.{key}')
    parts = [resources[key] for key in ('mapping', 'recorder')]
    combined = resources['combined']
    if any(part['samples'] > combined['samples'] for part in parts):
        raise ValueError('Inconsistent resources: combined samples omit process samples')
    # resource_metrics sums available processes on each shared sampling row.
    # A process may be absent during startup/shutdown, so counts can differ.
    for key in ('rss_mib', 'cpu_percent'):
        lower = max(part[key]['maximum'] for part in parts)
        upper = sum(part[key]['maximum'] for part in parts)
        peak = combined[key]['maximum']
        if ((peak < lower and not math.isclose(peak, lower, rel_tol=1e-9, abs_tol=1e-9))
                or (peak > upper and not math.isclose(peak, upper, rel_tol=1e-9, abs_tol=1e-9))):
            raise ValueError(f'Inconsistent resources.{key}: combined peak outside process bounds')
        expected_mean = sum(part[key]['mean'] * (part['samples'] / combined['samples'])
                            for part in parts)
        if not math.isclose(combined[key]['mean'], expected_mean, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(f'Inconsistent resources.{key}: combined mean disagrees with process samples')


def _validate_recording_settings(settings):
    for key in ('queue_capacity', 'chunk_points', 'max_frame_points', 'max_frame_bytes', 'chunk_frames'):
        _number(_get(settings, key), f'recording.settings.{key}', integer=True, minimum=1)
    if settings['max_frame_points'] > settings['chunk_points']:
        raise ValueError('Recording max_frame_points exceeds chunk_points')
    for key in ('flush_interval', 'status_interval'):
        if _number(_get(settings, key), f'recording.settings.{key}') == 0:
            raise ValueError(f'recording.settings.{key} must be positive')
    _boolean(_get(settings, 'fsync'), 'recording.settings.fsync')
    for key, choices in (('overflow', ('fail', 'drop-newest')), ('nonfinite', ('drop', 'error')),
                         ('missing_intensity', ('zero', 'error'))):
        if _get(settings, key) not in choices:
            raise ValueError(f'Invalid recording.settings.{key}')
    frame_id = _get(settings, 'expected_frame_id')
    if not isinstance(frame_id, str) or len(frame_id.encode('utf-8')) > 256:
        raise ValueError('recording.settings.expected_frame_id must be at most 256 UTF-8 bytes')


def _validate_run(run):
    _finite_tree(run, 'run')
    metrics, summary = _get(run, 'metrics'), _get(run, 'run_summary')
    for key in ('sequence', 'dds_transport', 'measurement_scope'):
        _text(_get(summary, key), key)
    for key in ('playback_rate', 'bag_duration_s'):
        if _number(_get(summary, key), key) == 0:
            raise ValueError(f'{key} must be positive')
    _digest(_get(summary, 'input_timestamps_sha256'), 'input_timestamps_sha256')
    for key in ('expected_odometry_count', 'initialization_frames', 'odometry_count', 'recorded_input_points'):
        _number(_get(summary, key), key, integer=True)
    for key in ('mapping_exit_code', 'recorder_exit_code', 'playback_exit_code'):
        _number(_get(summary, key), key, integer=True, minimum=None)
    for key in ('completed', 'recorder_stamps_equal_odometry'):
        _boolean(_get(summary, key), key)
    coverage = _get(metrics, 'coverage')
    for source in (coverage, _get(summary, 'coverage')):
        for key in ('input_lidar_frames', 'processed_frames', 'leading_frames_without_odometry',
                    'missing_frames_after_initialization'):
            _number(_get(source, key), f'coverage.{key}', integer=True)
        difference = _number(_get(source, 'maximum_input_stamp_match_difference_s'), 'stamp difference')
        # Match replay_outdoor01.validate_scan_coverage's exported protocol.
        if difference > 1e-5:
            raise ValueError('Input stamp difference exceeds supported 1e-5 s tolerance')
    _equal(coverage, summary['coverage'], 'coverage summaries')
    _equal(coverage['input_lidar_frames'], coverage['processed_frames'] +
           coverage['leading_frames_without_odometry'] + coverage['missing_frames_after_initialization'],
           'input/processed/missing counts')
    _equal(summary['expected_odometry_count'], coverage['input_lidar_frames'] - summary['initialization_frames'],
           'expected odometry count')
    _equal(coverage['processed_frames'], summary['odometry_count'], 'odometry count')
    _stats(_get(metrics, 'core_processing_time_ms'), summary['odometry_count'], 'core_processing_time_ms')
    over = _number(_get(metrics, 'core_processing_frames_over_100ms'), 'frames over 100 ms', integer=True)
    if over > summary['odometry_count']:
        raise ValueError('Frames over 100 ms exceeds total frames')
    _boolean(_get(metrics, 'preprocess_column_used'), 'preprocess_column_used')
    _text(_get(metrics, 'timing_scope'), 'timing_scope')
    resources = _get(metrics, 'resources')
    _validate_resources(resources)
    reference = _get(metrics, 'position_reference_comparison')
    _digest(_get(reference, 'reference_sha256'), 'reference_sha256')
    _text(_get(reference, 'alignment'), 'alignment')
    for key in ('matched_poses', 'unmatched_poses'):
        _number(_get(reference, key), key, integer=True)
    _equal(reference['matched_poses'] + reference['unmatched_poses'], summary['odometry_count'], 'reference count')
    for key in ('scale', 'pairing_tolerance_s'):
        if _number(_get(reference, key), key) == 0:
            raise ValueError(f'{key} must be positive')
    _number(_get(reference, 'fitted_time_offset_s'), 'fitted_time_offset_s', minimum=None)
    _number(_get(reference, 'rmse_m'), 'rmse_m')
    _stats(_get(reference, 'error_m'), reference['matched_poses'], 'position error')
    if not reference['error_m']['mean'] <= reference['rmse_m'] <= reference['error_m']['maximum']:
        raise ValueError('Position RMSE outside mean/maximum error bounds')
    record = _get(summary, 'recording')
    _equal(_number(_get(record, 'schema_version'), 'recording.schema_version', integer=True), 1, 'schema version')
    for key in COUNTERS:
        _number(_get(record, key), f'recording.{key}', integer=True)
    for key in ('completed', 'accepting', 'running', 'received_counts_verified', 'integrity_ok'):
        _boolean(_get(record, key), f'recording.{key}')
    for key in ('write_seconds', 'elapsed_seconds'):
        _number(_get(record, key), f'recording.{key}')
    write_seconds = _number(_get(metrics, 'recording.write_seconds'), 'metrics.recording.write_seconds')
    _equal(write_seconds, record['write_seconds'], 'recording write time')
    quaternion_error = _number(_get(metrics, 'recording.quaternion_max_norm_error'),
                               'recording.quaternion_max_norm_error')
    # Match evaluate_engineering's quaternion validation before export.
    if quaternion_error > 1e-7:
        raise ValueError('Quaternion norm error exceeds supported 1e-7 tolerance')
    error = _get(record, 'error')
    if error is not None and not isinstance(error, str):
        raise ValueError('recording.error must be null or a string')
    for suffix in ('frames', 'points'):
        written = record['written_frames' if suffix == 'frames' else 'written_input_points']
        _equal(record[f'enqueued_{suffix}'], written + record[f'failed_{suffix}'] + record[f'pending_{suffix}'],
               f'enqueued {suffix}')
        _equal(record[f'received_{suffix}'], record[f'enqueued_{suffix}'] + record[f'dropped_{suffix}'],
               f'received {suffix}')
    _equal(summary['recorded_input_points'], record['written_input_points'], 'recorded input points')
    if record['written_points'] > record['written_input_points']:
        raise ValueError('Written points exceeds input points')
    for source in (_get(record, 'settings'), _get(metrics, 'recording.settings')):
        _validate_recording_settings(source)
    _equal(record['settings'], metrics['recording']['settings'], 'recording settings')
    blocks = record['committed_blocks']
    if (blocks > record['written_frames']
            or record['written_frames'] > blocks * record['settings']['chunk_frames']
            or record['written_points'] > blocks * record['settings']['chunk_points']
            or record['adopted_blocks'] > blocks):
        raise ValueError('Inconsistent recording block counts or block capacity')
    for left, right in (('frames', 'written_frames'), ('points', 'written_points'),
                        ('committed_blocks', 'committed_blocks'), ('dropped_frames', 'dropped_frames'),
                        ('failed_frames', 'failed_frames'), ('pending_frames', 'pending_frames'),
                        ('nonfinite_points', 'nonfinite_points'), ('queue_high_water', 'queue_high_water')):
        actual = _number(_get(metrics, f'recording.{left}'), left, integer=True)
        _equal(actual, record[right], f'recording {left}')
    for key in ('frame_point_counts_equal_mapping_log', 'stamps_equal_odometry'):
        _boolean(_get(metrics, f'recording.{key}'), key)
    return metrics, summary


def _validate_limits(limits):
    _finite_tree(limits, 'limits')
    if not isinstance(limits, dict) or set(limits) != {'schema_version', 'description', 'absolute', 'relative'}:
        raise ValueError('Limits require exactly schema_version, description, absolute, relative')
    _equal(_number(limits['schema_version'], 'schema_version', integer=True), 1, 'limits schema')
    _text(limits['description'], 'limits.description')
    for group, names in (('absolute', {*METRICS, 'queue_fill_ratio'}), ('relative', set(METRICS))):
        if not isinstance(limits[group], dict) or set(limits[group]) != names:
            raise ValueError(f'Unknown or missing {group} limit keys; expected {sorted(names)}')
        for key, value in limits[group].items():
            _number(value, f'{group}.{key}')
    if not 0 < limits['absolute']['queue_fill_ratio'] <= 1:
        raise ValueError('queue_fill_ratio must be in (0, 1]')


def _protocol(metrics, summary):
    fields = {key: summary[key] for key in (
        'sequence', 'input_timestamps_sha256', 'playback_rate', 'bag_duration_s',
        'initialization_frames', 'expected_odometry_count', 'dds_transport', 'measurement_scope')}
    fields.update({key: _get(metrics, key) for key in (
        'timing_scope', 'preprocess_column_used', 'resources.scope', 'resources.cpu_convention')})
    for key in ('reference_sha256', 'pairing_tolerance_s', 'alignment', 'scale',
                'fitted_time_offset_s', 'matched_poses'):
        fields[f'reference.{key}'] = metrics['position_reference_comparison'][key]
    return fields


def _empty_report():
    return {'schema_version': 1, 'status': 'invalid', 'exit_code': 2, 'errors': [],
            'protocol_differences': [], 'checks': [], 'limitations': list(LIMITATIONS)}


def evaluate_regression(baseline, candidate, limits):
    """Validate two exported run dictionaries, then evaluate independent limits."""
    report = _empty_report()
    try:
        _validate_limits(limits)
        before = _validate_run(baseline)
        after = _validate_run(candidate)
    except ValueError as error:
        report['errors'].append(str(error))
        return report
    before_protocol, after_protocol = _protocol(*before), _protocol(*after)
    report['protocol_differences'] = [
        {'field': key, 'baseline': before_protocol[key], 'candidate': value}
        for key, value in after_protocol.items() if value != before_protocol[key]]
    if report['protocol_differences']:
        report['status'] = 'incomparable'
        return report
    report['protocol'] = before_protocol
    report['tolerance_description'] = limits['description']

    def check(name, actual, limit, comparison='<='):
        passed = actual == limit if comparison == '==' else actual <= limit
        report['checks'].append({'name': name, 'actual': actual, 'limit': limit,
                                 'comparison': comparison, 'status': 'passed' if passed else 'failed'})

    measured = []
    for label, (metrics, summary) in (('baseline', before), ('candidate', after)):
        for key in ('completed', 'recorder_stamps_equal_odometry'):
            check(f'{label}.{key}', summary[key], True, '==')
        for key in ('mapping_exit_code', 'recorder_exit_code', 'playback_exit_code'):
            check(f'{label}.{key}', summary[key], 0, '==')
        record = summary['recording']
        for key, target in (('completed', True), ('integrity_ok', True), ('received_counts_verified', True),
                            ('accepting', False), ('running', False), ('error', None)):
            check(f'{label}.recording.{key}', record[key], target, '==')
        for key in ZERO_COUNTERS:
            check(f'{label}.recording.{key}', record[key], 0, '==')
        check(f'{label}.missing_frames_after_initialization',
              metrics['coverage']['missing_frames_after_initialization'], 0, '==')
        check(f'{label}.initialization_frames', metrics['coverage']['leading_frames_without_odometry'],
              summary['initialization_frames'], '==')
        check(f'{label}.processed_frames', summary['odometry_count'], summary['expected_odometry_count'], '==')
        check(f'{label}.recorded_frames', record['written_frames'], summary['odometry_count'], '==')
        check(f'{label}.recorded_points', record['written_points'], record['written_input_points'], '==')
        for key in ('frame_point_counts_equal_mapping_log', 'stamps_equal_odometry'):
            check(f'{label}.recording.{key}', metrics['recording'][key], True, '==')
        values = {'core_p95_ms': metrics['core_processing_time_ms']['p95'],
                  'combined_rss_mib': metrics['resources']['combined']['rss_mib']['maximum'],
                  'position_rmse_m': metrics['position_reference_comparison']['rmse_m']}
        for key, value in values.items():
            check(f'{label}.{key}.absolute', value, limits['absolute'][key])
        check(f'{label}.queue_fill_ratio', record['queue_high_water'] / record['settings']['queue_capacity'],
              limits['absolute']['queue_fill_ratio'])
        measured.append(values)
    for key in METRICS:
        threshold = measured[0][key] * (1 + limits['relative'][key])
        if not math.isfinite(threshold):
            report.update(status='invalid', exit_code=2, checks=[], errors=['Relative threshold overflow'])
            return report
        check(f'candidate.{key}.relative', measured[1][key], threshold)
    failed = any(row['status'] == 'failed' for row in report['checks'])
    report.update(status='failed' if failed else 'passed', exit_code=1 if failed else 0)
    return report


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate JSON key: {key}')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f'Non-finite JSON constant: {value}')


def load_json(path):
    """Read exactly the bytes hashed below, rejecting duplicate/non-finite JSON."""
    path = Path(path)
    payload = path.read_bytes()
    value = json.loads(payload, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    return value, {'name': path.name, 'size_bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-dir', type=Path, required=True)
    parser.add_argument('--candidate-dir', type=Path, required=True)
    parser.add_argument('--limits', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    report = _empty_report()
    inputs = {}
    try:
        runs = []
        for label, directory in (('baseline', args.baseline_dir), ('candidate', args.candidate_dir)):
            run, inputs[label] = {}, {}
            for key in ('metrics', 'run_summary'):
                run[key], inputs[label][key] = load_json(directory / f'{key}.json')
            runs.append(run)
        limits, inputs['limits'] = load_json(args.limits)
        report = evaluate_regression(*runs, limits)
    except (ValueError, OSError) as error:
        report['errors'].append(str(error))
    report['inputs'] = inputs
    payload = json.dumps(report, indent=2, allow_nan=False) + '\n'
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload)
        except OSError as error:
            print(f'Cannot write regression report: {error}', file=sys.stderr)
            return 2
    print(payload, end='')
    return report['exit_code']


if __name__ == '__main__':
    sys.exit(main())
