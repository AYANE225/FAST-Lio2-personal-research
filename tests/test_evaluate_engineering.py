"""Position comparisons use a rigid transform without scale fitting."""
import numpy as np
import pytest

from scripts.evaluate_engineering import align_positions, pair_reference_positions


def test_rigid_alignment_recovers_rotation_and_translation():
    estimated = np.array([[0., 0., 0.], [1., 0., 0.], [0., 2., 0.], [0., 0., 3.], [4., 1., 2.]])
    rotation = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
    reference = estimated @ rotation.T + [5., 6., 7.]
    result = align_positions(estimated, reference)
    np.testing.assert_allclose(result['aligned_xyz'], reference, atol=1e-12)
    np.testing.assert_allclose(result['rotation'], rotation, atol=1e-12)
    assert abs(np.linalg.det(result['rotation']) - 1) < 1e-12
    scaled = align_positions(estimated * 2, reference)
    assert scaled['error_m'].max() > .1


def test_reference_pairing_keeps_every_saved_reference_and_only_omits_unavailable_times():
    trajectory = np.zeros((5, 8)); trajectory[:, 0] = [10, 11, 12, 13, 14]
    mask, reference = pair_reference_positions(trajectory, np.array([10., 11., 12.]), np.ones((3, 3)))
    np.testing.assert_array_equal(mask, [True, True, True, False, False])
    assert reference.shape == (3, 3)
    with pytest.raises(ValueError, match='reference'):
        pair_reference_positions(trajectory[1:], np.array([10., 11., 12.]), np.ones((3, 3)))
