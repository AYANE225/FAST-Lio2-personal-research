"""Progressive displays must never include observations from a future frame."""
import hashlib
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
        return importlib.import_module('prepare_mapping_progress')
    except ModuleNotFoundError:
        pytest.fail('mapping-progress preparation is not implemented')


def test_voxels_keep_first_observation_and_acquisition_order():
    # Sorting by voxel or averaging would change these actual observations.
    points = np.array([[1.2, 0, 0, 10], [-.2, 0, 0, 20], [1.8, 0, 0, 90],
                       [.2, 0, 0, 30], [-.8, 0, 0, 80], [2.2, 0, 0, 40]])
    indices = api().first_voxel_indices(points, 1.)
    np.testing.assert_array_equal(indices, [0, 1, 3, 5])
    np.testing.assert_array_equal(points[indices, 3], [10, 20, 30, 40])


def test_offline_sor_preserves_original_indices_and_observation_values():
    # Dense, nonsymmetric cluster plus one isolated point. Source indices are
    # deliberately nonconsecutive, as after first-observation voxel sampling.
    points = np.zeros((13, 4), dtype=np.float32)
    selected = np.array([0, 2, 4, 6, 8, 10, 12])
    points[selected] = [[0, 0, 0, 11], [.01, 0, 0, 12], [0, .01, 0, 13],
                        [0, 0, .01, 14], [.01, .01, 0, 15], [.01, 0, .01, 16],
                        [20, 20, 20, 90]]
    before = points.copy()
    retained, report = api().select_statistical_inliers(points, selected, neighbors=2, std_ratio=1.)
    np.testing.assert_array_equal(retained, selected[:-1])
    np.testing.assert_array_equal(points, before)
    assert report['input_points'] == 7
    assert report['retained_points'] == 6
    assert report['temporal_scope'] == 'offline_full_recording'


@pytest.mark.parametrize('points,size', [
    ([[np.nan, 0, 0, 1]], 1.), ([[0, 0, 0, np.inf]], 1.),
    ([[1.e30, 0, 0, 1]], 1.e-10), ([[0, 0, 0, 1]], 0),
    ([[0, 0, 0, 1]], True), ([[0, 0, 0, 1]], np.nan),
])
def test_invalid_display_points_or_voxel_size_are_rejected(points, size):
    with pytest.raises(ValueError):
        api().first_voxel_indices(np.asarray(points), size)


def test_frame_prefixes_exclude_points_at_the_next_frames_offset():
    timeline = api().build_timeline(
        np.array([2, 0, 3]), np.array([10_000_000_007, 10_100_000_007, 10_200_000_007]),
        np.array([0, 2, 4]), np.array([[0., 0, 0], [1, 0, 0], [2, 0, 0]]))
    assert timeline['display_point_prefix_counts'] == [1, 1, 3]
    assert timeline['recorded_point_prefix_counts'] == [2, 2, 5]
    assert timeline['stamp_ns'][0] == '10000000007'
    np.testing.assert_allclose(timeline['elapsed_s'], [0, .1, .2], rtol=0, atol=1.e-15)


@pytest.mark.parametrize('counts,stamps,selected', [
    ([2, 3], [10, 10], [0, 2]), ([2, 3], [10, 9], [0, 2]),
    ([2, 3], [10, 11], [0, 5]), ([2, 3], [10, 11], [2, 0]),
    ([2, -1], [10, 11], [0]),
])
def test_invalid_timeline_cannot_misrepresent_acquisition(counts, stamps, selected):
    with pytest.raises(ValueError):
        api().build_timeline(np.asarray(counts), np.asarray(stamps), np.asarray(selected),
                             np.zeros((2, 3)))


def test_changed_source_or_path_outside_recording_is_rejected(tmp_path):
    source = tmp_path / 'run'
    source.mkdir()
    (source / 'trajectory_tum.txt').write_text('original')
    manifest = tmp_path / 'export.json'
    checksum = hashlib.sha256(b'original').hexdigest()
    manifest.write_text(json.dumps({'source_files': {'trajectory_tum.txt': checksum}}))
    api().verify_source_files(source, manifest)
    (source / 'trajectory_tum.txt').write_text('changed')
    with pytest.raises(ValueError, match='checksum'):
        api().verify_source_files(source, manifest)
    manifest.write_text(json.dumps({'source_files': {'../export.json': checksum}}))
    with pytest.raises(ValueError, match='path'):
        api().verify_source_files(source, manifest)


def test_read_only_recording_validation_preserves_files_and_rejects_corruption(tmp_path):
    from map_writer import AsyncMapWriter, RecorderSettings
    directory = tmp_path / 'recording'
    writer = AsyncMapWriter(directory, RecorderSettings(chunk_frames=1, fsync=False))
    writer.submit(np.array([[1., 2, 3, 4]], dtype='<f4'), stamp_sec=10, stamp_nanosec=7,
                  frame_id='camera_init')
    writer.submit(np.array([[5., 6, 7, 8]], dtype='<f4'), stamp_sec=10, stamp_nanosec=8,
                  frame_id='camera_init')
    assert writer.close()['completed']
    before = {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
    points, rows, info = api().read_recording(directory)
    np.testing.assert_array_equal(points, [[1, 2, 3, 4], [5, 6, 7, 8]])
    assert len(rows) == len(info) == 2
    after = {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
    assert before == after
    path = directory / 'blocks/block-000001/points.npy'
    payload = bytearray(path.read_bytes())
    payload[-1] ^= 1
    path.write_bytes(payload)
    with pytest.raises(ValueError, match='hash'):
        api().read_recording(directory)


def test_trajectory_timestamp_parsing_keeps_nanoseconds(tmp_path):
    path = tmp_path / 'trajectory.txt'
    path.write_text('1690000000.000000007 1 2 3 0 0 0 1\n1690000000.100000008 4 5 6 0 0 0 1\n')
    positions = api().load_trajectory(path, np.array([1690000000000000007, 1690000000100000008]))
    np.testing.assert_array_equal(positions, [[1, 2, 3], [4, 5, 6]])
    with pytest.raises(ValueError, match='timestamp'):
        api().load_trajectory(path, np.array([1690000000000000008, 1690000000100000008]))


def test_display_gravity_rotation_preserves_distances():
    gravity = np.array([1., 2, -9.])
    rotation = api().gravity_rotation(gravity)
    np.testing.assert_allclose(rotation @ gravity, [0, 0, -np.linalg.norm(gravity)], atol=1.e-12)
    np.testing.assert_allclose(rotation.T @ rotation, np.eye(3), atol=1.e-12)
    assert np.linalg.det(rotation) == pytest.approx(1.)
    with pytest.raises(ValueError):
        api().gravity_rotation([0, 0, 0])


def test_fixed_height_range_preserves_observation_order_and_boundary_points():
    points = np.array([[0, 0, 35, 1], [0, 0, -5, 2], [0, 0, 0, 3], [0, 0, 30, 4]])
    result = api().select_height_range(points, np.array([0, 1, 3]), [-5, 30])
    np.testing.assert_array_equal(result, [1, 3])
    with pytest.raises(ValueError, match='height range'):
        api().select_height_range(points, np.array([0, 1, 3]), [30, -5])
