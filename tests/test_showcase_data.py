"""Reject inconsistent quantitative evidence before producing homepage figures."""
import importlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


def api():
    try:
        return importlib.import_module('plot_homepage_showcase')
    except ModuleNotFoundError:
        pytest.fail('showcase evidence loading is not implemented')


def position_fixture(tmp_path, errors=(.1, .2)):
    path = tmp_path / 'position.npz'
    np.savez(path, timestamps=[1., 2.], reference_xyz=np.zeros((2, 3)),
             aligned_xyz=[[.1, 0, 0], [0, 0, .2]],
             residual_xyz_m=[[.1, 0, 0], [0, 0, .2]], error_m=errors)
    metrics = tmp_path / 'metrics.json'
    metrics.write_text(json.dumps({'position_reference_comparison': {
        'matched_poses': 2, 'unmatched_poses': 0, 'rmse_m': .15811388300841897,
        'error_m': {'p95': .195, 'maximum': .2},
        'scale': 1., 'fitted_time_offset_s': 0.,
    }}))
    return path, metrics


def test_position_statistics_come_from_the_unsmoothed_3d_residuals(tmp_path):
    result = api().load_position_errors(*position_fixture(tmp_path))
    assert result['rmse_m'] == pytest.approx(.15811388300841897)
    assert result['p95_m'] == pytest.approx(.195)
    np.testing.assert_array_equal(result['elapsed_s'], [0, 1])


def test_inconsistent_saved_position_errors_are_rejected(tmp_path):
    with pytest.raises(ValueError, match='residual'):
        api().load_position_errors(*position_fixture(tmp_path, errors=(.1, .1)))


def test_interrupted_run_cannot_be_presented_as_complete(tmp_path):
    path = ROOT / 'data/outdoor01/recovery/evidence.json'
    result = api().load_recovery_results(path)
    assert result['indexed_frames_before'] == 20
    assert result['verified_frames_after'] == 30
    changed = json.loads(path.read_text())
    changed['cases']['sigkill_before_index']['recovery']['report']['completed'] = True
    invalid = tmp_path / 'evidence.json'
    invalid.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match='incomplete'):
        api().load_recovery_results(invalid)
