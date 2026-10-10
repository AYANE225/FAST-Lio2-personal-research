"""Check geometric and file-format contracts on small, hand-checkable data."""
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from process_map import (read_pcd, voxel_centroids, statistical_filter,
                         project_to_local_planes, export_tiles)


class MapProcessingTests(unittest.TestCase):
    def test_pcd_reads_fields_by_name_and_checks_exact_payload(self):
        # A different field order and a vector field must not move XYZ offsets.
        header = ('VERSION .7\nFIELDS intensity normal x y z\n'
                  'SIZE 2 4 4 4 4\nTYPE U F F F F\nCOUNT 1 3 1 1 1\n'
                  'WIDTH 2\nHEIGHT 1\nPOINTS 2\nDATA binary\n').encode()
        dtype = np.dtype([('intensity', '<u2'), ('normal', '<f4', (3,)),
                          ('x', '<f4'), ('y', '<f4'), ('z', '<f4')])
        payload = np.zeros(2, dtype=dtype)
        payload['x'], payload['y'], payload['z'] = [1, -2], [3, 4], [5, 6]
        payload['intensity'] = [20, 40]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'map.pcd'
            path.write_bytes(header + payload.tobytes())
            points, metadata = read_pcd(path)
            np.testing.assert_array_equal(points, [[1, 3, 5, 20], [-2, 4, 6, 40]])
            self.assertEqual(metadata['points'], 2)
            path.write_bytes(header + payload.tobytes()[:-1])
            with self.assertRaisesRegex(ValueError, 'payload|size|length'):
                read_pcd(path)

    def test_pcd_rejects_nonfinite_and_unsupported_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.pcd'
            header = ('FIELDS x y z intensity\nSIZE 4 4 4 4\nTYPE F F F F\n'
                      'COUNT 1 1 1 1\nWIDTH 1\nHEIGHT 1\nPOINTS 1\nDATA binary\n')
            path.write_bytes(header.encode() + np.array([[np.nan, 0, 0, 1]], '<f4').tobytes())
            with self.assertRaisesRegex(ValueError, 'finite'):
                read_pcd(path)
            path.write_bytes(header.replace('binary', 'binary_compressed').encode())
            with self.assertRaisesRegex(ValueError, 'binary'):
                read_pcd(path)

    def test_voxels_floor_negative_coordinates_and_average_all_fields(self):
        points = np.array([[-.2, 0, 0, 10], [-.8, 0, 0, 30],
                           [.1, 0, 0, 20], [.9, 0, 0, 40], [1.1, 0, 0, 50]])
        centroids, counts = voxel_centroids(points, 1)
        np.testing.assert_allclose(centroids, [[-.5, 0, 0, 20], [.5, 0, 0, 30], [1.1, 0, 0, 50]])
        np.testing.assert_array_equal(counts, [2, 2, 1])
        other, _ = voxel_centroids(points[::-1], 1)
        np.testing.assert_array_equal(centroids, other)
        for size in (0, -1, np.nan):
            with self.assertRaises(ValueError):
                voxel_centroids(points, size)

    def test_statistical_filter_removes_isolated_point_without_moving_retained_points(self):
        points = np.array([[x, y, 0, 25] for x in (0, .1, .2) for y in (0, .1, .2)] + [[5, 5, 5, 99]])
        filtered, keep, stats = statistical_filter(points, neighbors=4, std_ratio=1.5, workers=1)
        np.testing.assert_array_equal(keep, [True] * 9 + [False])
        np.testing.assert_array_equal(filtered, points[:9])
        self.assertEqual(stats['removed_points'], 1)

    def test_plane_projection_reduces_local_noise_and_bounds_displacement(self):
        points = np.array([[x, y, 0, 25] for x in np.arange(-3, 4) * .1
                           for y in np.arange(-3, 4) * .1], dtype=float)
        points[24, 2] = .02
        result, stats = project_to_local_planes(points, radius_m=.32, neighbors=32,
                                               min_neighbors=8, max_displacement_m=.025, workers=1)
        self.assertLess(abs(result[24, 2]), .005)
        self.assertLessEqual(np.linalg.norm(result[:, :3] - points[:, :3], axis=1).max(), .025001)
        np.testing.assert_array_equal(result[:, 3], points[:, 3])
        self.assertGreater(stats['moved_points'], 0)

    def test_plane_projection_preserves_unsupported_line_and_isolated_points(self):
        points = np.array([[x, 0, 0, 10] for x in np.arange(12) * .03] + [[10, 10, 10, 20]])
        result, stats = project_to_local_planes(points, radius_m=.3, neighbors=12,
                                               min_neighbors=6, max_displacement_m=.03, workers=1)
        np.testing.assert_allclose(result, points)
        self.assertEqual(stats['moved_points'], 0)

    def test_projection_bound_applies_to_final_float32_coordinates(self):
        points = np.array([[x, y, 0, 25] for x in np.arange(-3, 4) * .1
                           for y in np.arange(-3, 4) * .1], dtype=float)
        a, b = -.929957240950452, -.6044844934104867
        points[:, 2] = a * points[:, 0] + b * points[:, 1]
        normal = np.array([-a, -b, 1.])
        normal /= np.linalg.norm(normal)
        points[24, :3] += normal * .032774006853538734
        points[:, :3] += [100, 100, 100]
        points = points.astype(np.float32)
        result, _ = project_to_local_planes(points, radius_m=.5, neighbors=32,
                    min_neighbors=8, max_displacement_m=.03, workers=1)
        displacement = np.linalg.norm(result[:, :3].astype(float) - points[:, :3].astype(float), axis=1)
        self.assertLessEqual(float(displacement.max()), .03)
        np.testing.assert_array_equal(result[:, 3], points[:, 3])

    def test_export_is_lossless_little_endian_and_spatial_ids_are_stable(self):
        points = np.array([[-.1, 0, 0, 2], [.1, 0, 0, 4], [25, 0, 0, 8]], dtype=np.float32)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tiles = export_tiles(points, root, 'voxel/fine', tile_size_m=24)
            self.assertEqual({t['id'] for t in tiles}, {'x-1_y0_z0', 'x0_y0_z0', 'x1_y0_z0'})
            self.assertEqual(sum(t['point_count'] for t in tiles), 3)
            actual = []
            first_bytes = {}
            for tile in tiles:
                path = root / tile['url']
                actual.extend(np.frombuffer(gzip.decompress(path.read_bytes()), '<f4').reshape(-1, 4))
                first_bytes[tile['url']] = path.read_bytes()
            np.testing.assert_array_equal(np.asarray(actual), points)
            export_tiles(points, root, 'voxel/fine', tile_size_m=24)
            for name, data in first_bytes.items():
                self.assertEqual((root / name).read_bytes(), data)


if __name__ == '__main__':
    unittest.main()
