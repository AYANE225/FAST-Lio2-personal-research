"""Storage guarantees for the bounded asynchronous map writer."""

import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def api():
    try:
        return importlib.import_module("scripts.map_writer")
    except ModuleNotFoundError:
        pytest.fail("the asynchronous map writer has not been implemented")


def settings(**overrides):
    values = dict(queue_capacity=16, chunk_points=4, max_frame_points=4,
                  max_frame_bytes=4096, chunk_frames=8, status_interval=0.02,
                  flush_interval=10, expected_frame_id="camera_init")
    values.update(overrides)
    return api().RecorderSettings(**values)


def submit(writer, points, sec=10, nanosec=0, **kwargs):
    return writer.submit(np.asarray(points, dtype="<f4").reshape(-1, 4),
                         stamp_sec=sec, stamp_nanosec=nanosec,
                         frame_id="camera_init", **kwargs)


def read_json(path):
    return json.loads(path.read_text())


def test_clean_shutdown_drains_and_preserves_frames_across_bounded_blocks(tmp_path):
    module = api()
    writer = module.AsyncMapWriter(tmp_path / "recording", settings())
    assert submit(writer, [[1, 2, 3, 4], [5, 6, 7, 8]], nanosec=11)
    assert submit(writer, [[9, 10, 11, 12], [13, 14, 15, 16], [17, 18, 19, 20]], nanosec=12)
    assert submit(writer, [], nanosec=13)
    result = writer.close()
    assert result["completed"]
    assert result["received_frames"] == result["enqueued_frames"] == result["written_frames"] == 3
    assert result["received_points"] == result["written_points"] == 5
    assert result["failed_frames"] == result["dropped_frames"] == 0
    assert result["queue_size"] == result["pending_frames"] == 0
    blocks = sorted((tmp_path / "recording" / "blocks").glob("block-*"))
    assert len(blocks) == 2
    points = [np.load(block / "points.npy", allow_pickle=False) for block in blocks]
    assert all(len(block) <= 4 and block.dtype.str == "<f4" for block in points)
    np.testing.assert_array_equal(np.concatenate(points), np.arange(1, 21).reshape(5, 4))
    rows = [row for block in blocks for row in read_json(block / "frames.json")["frames"]]
    assert [(row["stamp_sec"], row["stamp_nanosec"], row["point_offset"], row["point_count"])
            for row in rows] == [(10, 11, 0, 2), (10, 12, 0, 3), (10, 13, 3, 0)]
    assert [row["input_points"] for row in rows] == [2, 3, 0]
    assert all(row["frame_id"] == "camera_init" for row in rows)
    assert read_json(tmp_path / "recording" / "status.json")["completed"]
    assert read_json(tmp_path / "recording" / "manifest.json")["completed"]
    assert module.recover_recording(tmp_path / "recording")["completed"]


def test_advisory_status_avoids_fsync_but_final_manifest_is_durable(tmp_path, monkeypatch):
    module = api()
    original = module._atomic_json
    writes = []

    def track(path, value, fsync=True):
        writes.append((path.name, bool(value.get("running")), fsync))
        return original(path, value, fsync)

    monkeypatch.setattr(module, "_atomic_json", track)
    writer = module.AsyncMapWriter(tmp_path / "recording", settings())
    assert submit(writer, [[1, 2, 3, 4]])
    assert writer.close()["completed"]
    assert all(not sync for name, running, sync in writes if name == "status.json" and running)
    assert any(name == "manifest.json" and sync for name, _, sync in writes)
    assert any(name == "index.json" and sync for name, _, sync in writes)


def test_flush_age_starts_with_first_frame_after_idle(tmp_path, monkeypatch):
    module = api()
    entered = threading.Event()
    original = module.AsyncMapWriter._commit_block

    def commit(writer, points, rows):
        entered.set()
        return original(writer, points, rows)

    monkeypatch.setattr(module.AsyncMapWriter, "_commit_block", commit)
    writer = module.AsyncMapWriter(tmp_path / "recording", settings(flush_interval=.3, fsync=False))
    try:
        time.sleep(.35)
        assert submit(writer, [[1, 2, 3, 4]])
        assert not entered.wait(.1), "idle time must not trigger a one-frame flush"
        assert entered.wait(1), "active buffer must still be flushed within its configured age"
    finally:
        writer.close()


@pytest.mark.parametrize('changes', [
    {'error': 'queue data lost'}, {'dropped_frames': 1}, {'failed_frames': 1},
    {'pending_points': 1}, {'received_frames': 2}, {'running': True},
])
def test_recovery_rejects_contradictory_success_manifest(tmp_path, changes):
    module = api()
    directory = tmp_path / 'recording'
    writer = module.AsyncMapWriter(directory, settings())
    assert submit(writer, [[1, 2, 3, 4]])
    assert writer.close()['completed']
    path = directory / 'manifest.json'
    manifest = read_json(path)
    manifest.update(changes)
    path.write_text(json.dumps(manifest))
    with pytest.raises(module.RecordingIntegrityError, match='completed manifest'):
        module.recover_recording(directory)


@pytest.mark.parametrize("overflow", ["fail", "drop-newest"])
def test_saturated_queue_is_nonblocking_and_counts_loss(tmp_path, monkeypatch, overflow):
    module = api()
    entered, release = threading.Event(), threading.Event()
    real_write = module._write_npy

    def slow_disk(path, points, fsync):
        entered.set()
        assert release.wait(5), "test did not release the blocked disk writer"
        real_write(path, points, fsync)

    monkeypatch.setattr(module, "_write_npy", slow_disk)
    writer = module.AsyncMapWriter(tmp_path / "recording",
                                   settings(queue_capacity=1, chunk_frames=1, overflow=overflow))
    try:
        assert submit(writer, [[1, 2, 3, 4]])
        assert entered.wait(5)
        assert submit(writer, [[5, 6, 7, 8]], nanosec=1)
        assert not submit(writer, [[9, 10, 11, 12]], nanosec=2)
        snapshot = writer.snapshot()
        assert snapshot["queue_high_water"] == snapshot["queue_size"] == 1
        assert snapshot["received_frames"] == 3
        assert snapshot["enqueued_frames"] == 2
        assert bool(snapshot["error"]) == (overflow == "fail")
    finally:
        release.set()
        result = writer.close()
    assert result["written_frames"] == 2
    assert result["dropped_frames"] == (1 if overflow == "drop-newest" else 0)
    assert result["failed_frames"] == (1 if overflow == "fail" else 0)
    assert result["received_points"] == (result["written_points"] + result["dropped_points"]
                                          + result["failed_points"])
    assert not result["completed"]


def test_disk_error_counts_buffered_and_queued_frames_and_is_reported(tmp_path, monkeypatch):
    module = api()
    entered, release = threading.Event(), threading.Event()

    def broken_disk(path, points, fsync):
        entered.set()
        assert release.wait(5)
        raise OSError("disk full")

    monkeypatch.setattr(module, "_write_npy", broken_disk)
    writer = module.AsyncMapWriter(tmp_path / "recording", settings(chunk_frames=1))
    try:
        assert submit(writer, [[1, 2, 3, 4]])
        assert entered.wait(5)
        assert submit(writer, [[5, 6, 7, 8]], nanosec=1)
    finally:
        release.set()
        result = writer.close()
    assert not result["completed"]
    assert result["written_frames"] == 0
    assert result["failed_frames"] == result["failed_points"] == 2
    assert result["pending_frames"] == result["queue_size"] == 0
    assert "disk full" in result["error"]
    assert not read_json(tmp_path / "recording" / "manifest.json")["completed"]


def test_frame_limits_fail_before_decoder_runs(tmp_path):
    module = api()

    def decoder(payload):
        pytest.fail("an oversized payload must not reach the decoder")

    writer = module.AsyncMapWriter(tmp_path / "recording", settings(max_frame_bytes=16), decoder=decoder)
    assert not writer.submit(object(), stamp_sec=1, stamp_nanosec=0, frame_id="camera_init",
                             input_points=1, input_bytes=17)
    result = writer.close()
    assert result["received_frames"] == result["failed_frames"] == 1
    assert result["enqueued_frames"] == 0
    assert "max_frame_bytes" in result["error"]


def test_zero_point_frames_are_bounded_by_frame_limit(tmp_path):
    writer = api().AsyncMapWriter(tmp_path / "recording", settings(chunk_frames=2))
    for stamp in range(5):
        assert submit(writer, [], nanosec=stamp)
    result = writer.close()
    assert result["completed"] and result["written_frames"] == 5
    assert result["written_points"] == 0 and result["committed_blocks"] == 3


def test_arrival_order_is_preserved_and_nonincreasing_stamps_are_counted(tmp_path):
    writer = api().AsyncMapWriter(tmp_path / "recording", settings())
    for sec in [3, 2, 2, 4]:
        assert submit(writer, [[sec, 0, 0, 0]], sec=sec)
    result = writer.close()
    assert result["nonmonotonic_frames"] == 2
    assert result["duplicate_stamp_frames"] == 1
    rows = read_json(tmp_path / "recording" / "blocks" / "block-000000" / "frames.json")["frames"]
    assert [row["stamp_sec"] for row in rows] == [3, 2, 2, 4]
    assert [row["sequence"] for row in rows] == [0, 1, 2, 3]


def test_nonfinite_coordinate_filter_is_accounted_without_hiding_input_count(tmp_path):
    writer = api().AsyncMapWriter(tmp_path / "recording", settings())
    assert submit(writer, [[1, 2, 3, float("nan")], [float("inf"), 0, 0, 9]])
    result = writer.close()
    assert result["completed"]
    assert result["received_points"] == result["written_input_points"] == 2
    assert result["written_points"] == result["nonfinite_points"] == 1
    assert result["nonfinite_intensity_points"] == 1
    points = np.load(tmp_path / "recording" / "blocks" / "block-000000" / "points.npy")
    np.testing.assert_array_equal(points, [[1, 2, 3, 0]])


def test_invalid_frame_fails_and_rejects_reuse_of_existing_directory(tmp_path):
    module = api()
    output = tmp_path / "recording"
    writer = module.AsyncMapWriter(output, settings())
    assert not writer.submit(np.zeros((1, 4), dtype="f4"), stamp_sec=0, stamp_nanosec=1_000_000_000,
                             frame_id="camera_init")
    assert writer.close()["failed_frames"] == 1
    before = (output / "manifest.json").read_bytes()
    with pytest.raises(ValueError, match="empty|exists"):
        module.AsyncMapWriter(output, settings())
    assert (output / "manifest.json").read_bytes() == before


def test_recovery_reconciles_committed_blocks_and_quarantines_temporary_files(tmp_path):
    module = api()
    output = tmp_path / "recording"
    writer = module.AsyncMapWriter(output, settings(chunk_frames=1))
    initial_index = (output / "index.json").read_bytes()
    for stamp in range(2):
        assert submit(writer, [[stamp, 0, 0, 1]], nanosec=stamp)
    writer.close()
    (output / "manifest.json").unlink()
    (output / "index.json").write_bytes(initial_index)
    temporary = output / "blocks" / ".block-000002.tmp"
    temporary.mkdir()
    (temporary / "points.npy").write_bytes(b"incomplete")
    report = module.recover_recording(output)
    assert report["integrity_ok"] and not report["completed"]
    assert report["adopted_blocks"] == 2 and report["quarantined_temporary_blocks"] == 1
    assert report["written_frames"] == report["written_points"] == 2
    assert report["received_frames"] is None
    assert not report["received_counts_verified"]
    assert not temporary.exists() and list((output / "incomplete").iterdir())
    manifest = read_json(output / "manifest.json")
    assert manifest["index_sha256"] == hashlib.sha256((output / "index.json").read_bytes()).hexdigest()
    assert not module.recover_recording(output)["completed"]


@pytest.mark.parametrize("filename", ["points.npy", "frames.json", "block.json", "index.json", "settings.json"])
def test_recovery_rejects_modified_data_or_paired_metadata(tmp_path, filename):
    module = api()
    output = tmp_path / "recording"
    writer = module.AsyncMapWriter(output, settings(chunk_frames=1))
    assert submit(writer, [[1, 2, 3, 4]])
    writer.close()
    path = output / filename if filename in {"index.json", "settings.json"} else output / "blocks" / "block-000000" / filename
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(module.RecordingIntegrityError, match="hash|SHA256|size"):
        module.recover_recording(output)


def test_recovery_rejects_wrong_schema_even_with_updated_metadata_hashes(tmp_path):
    module = api()
    output = tmp_path / "recording"
    writer = module.AsyncMapWriter(output, settings(chunk_frames=1))
    assert submit(writer, [[1, 2, 3, 4]])
    writer.close()
    block = output / "blocks" / "block-000000"
    rows = read_json(block / "frames.json")
    rows["frames"][0]["point_offset"] = 2
    (block / "frames.json").write_text(json.dumps(rows))
    metadata = read_json(block / "block.json")
    metadata["frames_sha256"] = hashlib.sha256((block / "frames.json").read_bytes()).hexdigest()
    (block / "block.json").write_text(json.dumps(metadata))
    index = read_json(output / "index.json")
    index["last_block_sha256"] = hashlib.sha256((block / "block.json").read_bytes()).hexdigest()
    (output / "index.json").write_text(json.dumps(index))
    manifest = read_json(output / "manifest.json")
    manifest["index_sha256"] = hashlib.sha256((output / "index.json").read_bytes()).hexdigest()
    (output / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(module.RecordingIntegrityError, match="offset|schema"):
        module.recover_recording(output)


def test_recovery_refuses_a_live_writer(tmp_path):
    module = api()
    writer = module.AsyncMapWriter(tmp_path / "recording", settings())
    try:
        with pytest.raises(module.RecordingBusyError):
            module.recover_recording(tmp_path / "recording")
    finally:
        writer.close()


def test_process_crash_keeps_only_fully_committed_blocks_recoverable(tmp_path):
    module = api()
    output = tmp_path / "recording"
    program = """
import os, sys, time
from pathlib import Path
import numpy as np
from scripts import map_writer as module
original = module._write_npy
def crash_on_second_block(path, points, fsync):
    if path.parent.name == '.block-000001.tmp':
        path.write_bytes(b'partial numpy file')
        os._exit(17)
    original(path, points, fsync)
module._write_npy = crash_on_second_block
settings = module.RecorderSettings(queue_capacity=2, chunk_points=4, max_frame_points=4,
                                  chunk_frames=1, status_interval=0.01)
writer = module.AsyncMapWriter(Path(sys.argv[1]), settings)
for stamp in range(2):
    writer.submit(np.ones((1, 4), dtype='f4'), stamp_sec=stamp, stamp_nanosec=0,
                  frame_id='camera_init')
time.sleep(10)
os._exit(99)
"""
    result = subprocess.run([sys.executable, "-c", program, str(output)], cwd=ROOT,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 17, result.stderr
    report = module.recover_recording(output)
    assert report["written_frames"] == 1 and report["quarantined_temporary_blocks"] == 1
    assert not report["completed"] and report["received_frames"] is None


@pytest.mark.parametrize("filename", ["status.json", "manifest.json"])
def test_final_metadata_failure_cannot_leave_a_successful_manifest(tmp_path, monkeypatch, filename):
    module = api()
    original = module._atomic_json

    def fail_completion(path, value, fsync=True):
        if path.name == filename and value.get("completed"):
            raise OSError("final metadata failure")
        return original(path, value, fsync)

    monkeypatch.setattr(module, "_atomic_json", fail_completion)
    writer = module.AsyncMapWriter(tmp_path / "recording", settings())
    assert submit(writer, [[1, 2, 3, 4]])
    result = writer.close()
    assert not result["completed"] and "final metadata" in result["error"]
    assert not read_json(tmp_path / "recording" / "manifest.json")["completed"]
    assert not read_json(tmp_path / "recording" / "status.json")["completed"]
    assert not module.recover_recording(tmp_path / "recording")["completed"]


def test_recovery_adopts_atomic_block_after_index_write_failure(tmp_path, monkeypatch):
    module = api()
    original = module._atomic_json

    def fail_second_index(path, value, fsync=True):
        if path.name == "index.json" and value.get("committed_blocks") == 2:
            raise OSError("index write failed")
        return original(path, value, fsync)

    monkeypatch.setattr(module, "_atomic_json", fail_second_index)
    writer = module.AsyncMapWriter(tmp_path / "recording", settings(chunk_frames=1))
    assert submit(writer, [[1, 2, 3, 4]], nanosec=1)
    assert submit(writer, [[5, 6, 7, 8]], nanosec=2)
    result = writer.close()
    assert not result["completed"] and result["written_frames"] == result["failed_frames"] == 1
    assert "index write failed" in result["error"]
    monkeypatch.setattr(module, "_atomic_json", original)
    report = module.recover_recording(tmp_path / "recording")
    assert report["adopted_blocks"] == 1 and report["written_frames"] == 2
    assert not report["completed"] and report["received_frames"] is None


def test_recovery_reports_unfinished_manifest_mismatch_after_index_rename(tmp_path, monkeypatch):
    module = api()
    original = module._atomic_json

    def fail_after_index_rename(path, value, fsync=True):
        digest = original(path, value, fsync)
        if path.name == "index.json" and value.get("committed_blocks") == 1:
            raise OSError("directory fsync failed after index rename")
        return digest

    monkeypatch.setattr(module, "_atomic_json", fail_after_index_rename)
    writer = module.AsyncMapWriter(tmp_path / "recording", settings(chunk_frames=1))
    assert submit(writer, [[1, 2, 3, 4]])
    assert not writer.close()["completed"]
    monkeypatch.setattr(module, "_atomic_json", original)
    report = module.recover_recording(tmp_path / "recording")
    assert report["written_frames"] == 1 and not report["completed"]
    assert not report["integrity_ok"] and report["recovery_warnings"]
    assert "manifest/index" in report["recovery_warnings"][0]
