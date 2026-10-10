"""PointCloud2 decoding without requiring ROS Python packages."""

import importlib
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def api():
    try:
        return importlib.import_module("scripts.map_recorder")
    except ModuleNotFoundError:
        pytest.fail("the PointCloud2 recorder has not been implemented")


def field(name, offset, datatype=7, count=1):
    return SimpleNamespace(name=name, offset=offset, datatype=datatype, count=count)


def cloud(*, bigendian=False, intensity=True):
    fields = [field("z", 0), field("x", 8), field("y", 16)]
    if intensity:
        fields.append(field("intensity", 20, datatype=4))
    data = bytearray(112)
    for row in range(2):
        for col in range(2):
            index = row * 2 + col + 1
            offset = row * 56 + col * 24
            for field_offset, value in [(0, index + 20), (8, index), (16, index + 10)]:
                struct.pack_into((">" if bigendian else "<") + "f", data, offset + field_offset, value)
            struct.pack_into((">" if bigendian else "<") + "H", data, offset + 20, index + 30)
    return SimpleNamespace(width=2, height=2, point_step=24, row_step=56,
                           is_bigendian=bigendian, fields=fields, data=data)


@pytest.mark.parametrize("bigendian", [False, True])
def test_decoder_respects_offsets_row_padding_endianness_and_integer_intensity(bigendian):
    decoded = api().pointcloud2_to_xyzi(cloud(bigendian=bigendian))
    assert decoded.points.dtype.str == "<f4"
    np.testing.assert_array_equal(decoded.points,
                                  [[1, 11, 21, 31], [2, 12, 22, 32], [3, 13, 23, 33], [4, 14, 24, 34]])
    assert decoded.nonfinite_points == 0 and not decoded.missing_intensity


def test_missing_intensity_is_explicit_or_rejected():
    module = api()
    decoded = module.pointcloud2_to_xyzi(cloud(intensity=False))
    assert decoded.missing_intensity
    np.testing.assert_array_equal(decoded.points[:, 3], [0, 0, 0, 0])
    with pytest.raises(ValueError, match="intensity"):
        module.pointcloud2_to_xyzi(cloud(intensity=False), missing_intensity="error")


def test_nonfinite_coordinates_are_counted_and_can_be_rejected():
    module = api()
    message = cloud()
    struct.pack_into("<f", message.data, 8, float("nan"))
    decoded = module.pointcloud2_to_xyzi(message)
    assert decoded.nonfinite_points == 1 and decoded.points.shape == (3, 4)
    np.testing.assert_array_equal(decoded.points[:, 0], [2, 3, 4])
    with pytest.raises(ValueError, match="non-finite"):
        module.pointcloud2_to_xyzi(message, nonfinite="error")


@pytest.mark.parametrize("problem", ["truncated", "bad_row_step", "missing_x", "bad_offset", "vector_x", "duplicate_x"])
def test_malformed_layout_fails_instead_of_recording_wrong_coordinates(problem):
    message = cloud()
    if problem == "truncated":
        message.data = message.data[:-1]
    elif problem == "bad_row_step":
        message.row_step = 40
    elif problem == "missing_x":
        message.fields = [item for item in message.fields if item.name != "x"]
    elif problem == "bad_offset":
        message.fields[1].offset = 22
    elif problem == "vector_x":
        message.fields[1].count = 2
    elif problem == "duplicate_x":
        message.fields.append(field("x", 4))
    with pytest.raises(ValueError):
        api().pointcloud2_to_xyzi(message)


def test_help_and_recovery_commands_do_not_import_ros(tmp_path):
    api()
    help_result = subprocess.run([sys.executable, str(ROOT / "scripts/map_recorder.py"), "--help"],
                                 capture_output=True, text=True)
    assert help_result.returncode == 0 and "recover" in help_result.stdout
    result = subprocess.run([sys.executable, str(ROOT / "scripts/map_recorder.py"), "recover", str(tmp_path / "missing")],
                            capture_output=True, text=True)
    assert result.returncode != 0 and "rclpy" not in result.stderr
