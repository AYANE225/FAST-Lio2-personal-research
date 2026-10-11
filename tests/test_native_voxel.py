"""Exercise the native voxel API against hand-computed and NumPy results.

Build the library once with CMake before running this module. Tests never compile
or substitute a Python implementation for the requested native backend.
"""
import os
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from native_voxel import voxel_centroids_native as native_voxels


def test_floor_bins_negative_coordinates_and_averages_all_fields():
    points = np.array([[-.2, 0, 0, 10], [.1, 0, 0, 20],
                       [-.8, 0, 0, 30], [.9, 0, 0, 40], [1.1, 0, 0, 50]])
    centroids, counts = native_voxels(points, 1)
    np.testing.assert_allclose(centroids, [[-.5, 0, 0, 20], [.5, 0, 0, 30],
                                           [1.1, 0, 0, 50]])
    np.testing.assert_array_equal(counts, [2, 2, 1])
    assert centroids.dtype == np.float32
    assert counts.dtype == np.int64


def test_double_precision_input_preserves_bins_next_to_boundaries():
    points = np.array([[np.nextafter(1., 0.), 0, 0, 10], [1., 0, 0, 30],
                       [np.nextafter(-1., -2.), 0, 0, 50], [-1., 0, 0, 70]])
    centroids, counts = native_voxels(points, 1)
    np.testing.assert_array_equal(counts, [1, 1, 1, 1])
    np.testing.assert_array_equal(centroids[:, 3], [50, 70, 10, 30])


def test_output_order_uses_integer_x_then_y_then_z():
    points = np.array([[.1, .1, 1.1, 50], [.1, 1.1, .1, 60],
                       [-.1, 9., 9., 10], [.1, -.1, 5., 20],
                       [.1, .1, -.1, 30], [.1, .1, .1, 40]])
    centroids, counts = native_voxels(points, 1)
    np.testing.assert_array_equal(centroids[:, 3], [10, 20, 30, 40, 50, 60])
    np.testing.assert_array_equal(counts, np.ones(6, dtype=np.int64))


@pytest.mark.parametrize('voxel_size_m', [.5, .2, .1])
def test_random_cloud_matches_numpy_reference(voxel_size_m):
    from process_map import voxel_centroids
    rng = np.random.default_rng(20261011)
    points = rng.uniform(-2, 2, size=(6000, 4))
    points[:, 3] = rng.uniform(0, 255, size=len(points))
    expected, expected_counts = voxel_centroids(points, voxel_size_m)
    actual, counts = native_voxels(points, voxel_size_m)
    np.testing.assert_array_equal(counts, expected_counts)
    np.testing.assert_allclose(actual, expected, rtol=2e-6, atol=1e-7)
    assert counts.sum() == len(points)


@pytest.mark.parametrize('dtype', [np.float32, np.float64, np.int32, np.uint16])
def test_numeric_inputs_are_accepted_without_modification(dtype):
    points = np.array([[0, 0, 0, 10], [1, 0, 0, 30]], dtype=dtype)
    before = points.copy()
    centroids, counts = native_voxels(points, 2)
    np.testing.assert_array_equal(centroids, [[.5, 0, 0, 20]])
    np.testing.assert_array_equal(counts, [2])
    np.testing.assert_array_equal(points, before)


def test_noncontiguous_and_big_endian_arrays_are_normalized():
    points = np.array([[0, 0, 0, 10], [1, 0, 0, 30]], dtype='>f8')
    centroids, counts = native_voxels(points[::-1], 2)
    np.testing.assert_array_equal(centroids, [[.5, 0, 0, 20]])
    np.testing.assert_array_equal(counts, [2])


def test_unaligned_contiguous_input_is_safe_for_native_double_loads():
    points = np.ndarray((2, 4), dtype=np.float64, buffer=bytearray(65), offset=1)
    points[:] = [[0, 0, 0, 10], [1, 0, 0, 30]]
    before = points.copy()
    assert points.flags.c_contiguous and not points.flags.aligned
    # CI supplies a UBSan library so an unsafe double* access fails even on x86.
    centroids, counts = native_voxels(
        points, 2, library_path=os.environ.get('FASTLIO_TEST_VOXEL_LIBRARY'))
    np.testing.assert_array_equal(centroids, [[.5, 0, 0, 20]])
    np.testing.assert_array_equal(counts, [2])
    np.testing.assert_array_equal(points, before)


def test_empty_input_has_stable_shapes_and_dtypes():
    centroids, counts = native_voxels(np.empty((0, 4)), 1)
    assert centroids.shape == (0, 4)
    assert counts.shape == (0,)
    assert centroids.dtype == np.float32
    assert counts.dtype == np.int64


@pytest.mark.parametrize('points', [np.zeros(4), np.zeros((2, 3)),
                                    np.zeros((2, 5)), np.zeros((2, 1, 4))])
def test_rejects_incorrect_input_shape(points):
    with pytest.raises(ValueError, match='N x 4|shape'):
        native_voxels(points, 1)


@pytest.mark.parametrize('dtype', ['U2', object, np.complex128, np.bool_])
def test_rejects_nonreal_or_nonnumeric_dtype(dtype):
    with pytest.raises(ValueError, match='real|numeric|dtype'):
        native_voxels(np.zeros((2, 4), dtype=dtype), 1)


@pytest.mark.parametrize('value', [np.nan, np.inf, -np.inf])
@pytest.mark.parametrize('column', [0, 3])
def test_rejects_nonfinite_xyz_and_intensity(value, column):
    points = np.zeros((2, 4))
    points[1, column] = value
    with pytest.raises(ValueError, match='finite'):
        native_voxels(points, 1)


@pytest.mark.parametrize('voxel_size_m', [0, -1, np.nan, np.inf, -np.inf,
                                         True, '1', [1], 1j])
def test_rejects_invalid_voxel_size_even_for_empty_input(voxel_size_m):
    with pytest.raises(ValueError, match='voxel_size_m'):
        native_voxels(np.empty((0, 4)), voxel_size_m)


@pytest.mark.parametrize('coordinate,voxel_size_m', [
    (float(2**62), 1), (-float(2**62), 1), (1., np.nextafter(0., 1.))])
def test_rejects_out_of_range_voxel_keys(coordinate, voxel_size_m):
    with pytest.raises(ValueError, match='integer range|voxel.*range'):
        native_voxels(np.array([[coordinate, 0, 0, 1]]), voxel_size_m)


@pytest.mark.parametrize('sign', [-1, 1])
def test_accepts_keys_immediately_inside_supported_range(sign):
    coordinate = sign * np.nextafter(float(2**62), 0.)
    centroids, counts = native_voxels(np.array([[coordinate, 0, 0, 1]]), 1)
    np.testing.assert_array_equal(centroids[:, 0], np.float32(coordinate))
    np.testing.assert_array_equal(counts, [1])


def test_rejects_double_accumulation_overflow():
    points = np.array([[0, 0, 0, np.finfo(np.float64).max]] * 2)
    with pytest.raises(ValueError, match='overflow|range'):
        native_voxels(points, 1)


@pytest.mark.parametrize('column', [0, 3])
def test_rejects_float32_centroid_overflow(column):
    points = np.zeros((2, 4))
    points[0, 0] = -.5
    points[1, column] = float(np.finfo(np.float32).max) * 2
    size = 1e30 if column == 0 else 1
    with pytest.raises(ValueError, match='float32|overflow|range'):
        native_voxels(points, size)


def test_missing_explicit_library_raises_without_numpy_fallback(tmp_path):
    with pytest.raises(FileNotFoundError, match='libfastlio_voxel|shared library'):
        native_voxels(np.zeros((1, 4)), 1, tmp_path / 'libfastlio_voxel.so')


def test_default_library_is_relative_to_repository_not_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    centroids, counts = native_voxels(np.array([[.5, 0, 0, 2]]), 1)
    np.testing.assert_array_equal(centroids, [[.5, 0, 0, 2]])
    np.testing.assert_array_equal(counts, [1])


def test_repeated_calls_return_owned_deterministic_arrays():
    rng = np.random.default_rng(711)
    points = rng.normal(size=(2000, 4))
    first, first_counts = native_voxels(points, .25)
    saved = first.copy()
    saved_counts = first_counts.copy()
    for _ in range(40):
        centroids, counts = native_voxels(points, .25)
        np.testing.assert_array_equal(centroids, saved)
        np.testing.assert_array_equal(counts, saved_counts)
        centroids[:] = 0
        counts[:] = 0
    np.testing.assert_array_equal(first, saved)
    np.testing.assert_array_equal(first_counts, saved_counts)


def test_recovery_after_native_failure_does_not_reuse_partial_output():
    invalid = np.array([[-.5, 0, 0, 1], [.5, 0, 0, 1e100]])
    for _ in range(20):
        with pytest.raises(ValueError, match='float32|overflow|range'):
            native_voxels(invalid, 1)
        centroids, counts = native_voxels(np.array([[.5, 0, 0, 2]]), 1)
        np.testing.assert_array_equal(centroids, [[.5, 0, 0, 2]])
        np.testing.assert_array_equal(counts, [1])
