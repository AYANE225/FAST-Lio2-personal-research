"""Real child-process checks for the isolated recording recovery experiment."""

import hashlib
import importlib
import json
from pathlib import Path
import signal
import subprocess
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.map_writer import AsyncMapWriter, RecorderSettings


def api():
    try:
        return importlib.import_module("scripts.benchmark_recording_recovery")
    except ModuleNotFoundError:
        pytest.fail("the isolated recording recovery experiment is not implemented")


def file_hashes(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*") if path.is_file()}


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    directory = tmp_path_factory.mktemp("recovery-source") / "recording"
    writer = AsyncMapWriter(directory, RecorderSettings(
        queue_capacity=16, chunk_points=32, max_frame_points=3, chunk_frames=4))
    for sequence, count in enumerate([1, 2, 3, 1, 2, 3, 1, 2, 3, 1]):
        assert writer.submit(np.full((count, 4), sequence, dtype="<f4"),
                             stamp_sec=10, stamp_nanosec=sequence, frame_id="camera_init")
    assert writer.close()["completed"]
    return directory


@pytest.fixture(scope="module")
def experiment(source, tmp_path_factory):
    output = tmp_path_factory.mktemp("recovery-results") / "run"
    before = file_hashes(source)
    report = api().run_experiment(source, output, frame_limit=8, frames_per_block=2)
    return source, output, before, report


def test_source_prefix_is_copied_without_mutating_the_source(experiment):
    source, output, before, report = experiment
    assert file_hashes(source) == before
    assert report["source"]["files_unchanged"]
    assert report["source"]["selected_frame_count"] == 8
    assert report["source"]["selected_point_count"] == 15
    expected = np.concatenate([np.full((count, 4), sequence, dtype="<f4")
                               for sequence, count in enumerate([1, 2, 3, 1, 2, 3, 1, 2])])
    np.testing.assert_array_equal(np.load(output / "input" / "points.npy"), expected)
    assert json.loads((output / "evidence.json").read_text()) == report


def test_completed_control_retains_verified_completion(experiment):
    case = experiment[3]["cases"]["normal_completion"]
    assert case["child"]["returncode"] == 0
    assert case["child"]["signal"] is None
    result = case["recovery"]["report"]
    assert result["completed"] and result["received_counts_verified"]
    assert (result["committed_blocks"], result["written_frames"], result["written_points"]) == (4, 8, 15)
    assert result["received_frames"] == 8
    assert case["validation"]["block_files_unchanged"]
    assert case["validation"]["frame_payloads_match_source_prefix"]


def test_sigkill_before_index_adopts_complete_block_without_claiming_completion(experiment):
    case = experiment[3]["cases"]["sigkill_before_index"]
    assert case["child"]["returncode"] == -signal.SIGKILL
    assert case["child"]["signal"] == "SIGKILL"
    assert case["child"]["boundary_event"]["condition"] == "complete_block_before_index"
    before = case["before"]
    assert before["manifest"] is None
    assert (before["index"]["committed_blocks"], before["index"]["written_frames"],
            before["index"]["written_points"]) == (2, 4, 7)
    assert len(before["blocks"]) == 3
    result = case["recovery"]["report"]
    assert result["integrity_ok"] and result["adopted_blocks"] == 1
    assert (result["committed_blocks"], result["written_frames"], result["written_points"]) == (3, 6, 12)
    assert not result["completed"] and not result["received_counts_verified"]
    assert result["received_frames"] is None and result["pending_frames"] is None
    assert case["validation"]["block_files_unchanged"]
    assert case["validation"]["frame_payloads_match_source_prefix"]


def test_sigkill_during_temporary_npy_quarantines_the_partial_bytes(experiment):
    case = experiment[3]["cases"]["sigkill_partial_temporary_block"]
    assert case["child"]["returncode"] == -signal.SIGKILL
    event = case["child"]["boundary_event"]
    assert event["condition"] == "partial_temporary_npy"
    assert 0 < event["persisted_bytes"] < event["complete_npy_bytes"]
    assert len(case["before"]["temporary_blocks"]) == 1
    assert not case["after"]["temporary_blocks"]
    assert len(case["after"]["quarantined_blocks"]) == 1
    result = case["recovery"]["report"]
    assert (result["committed_blocks"], result["written_frames"], result["written_points"]) == (2, 4, 7)
    assert result["adopted_blocks"] == 0 and result["quarantined_temporary_blocks"] == 1
    assert not result["completed"] and result["received_points"] is None
    assert case["validation"]["quarantined_files_unchanged"]
    assert case["validation"]["block_files_unchanged"]
    assert case["validation"]["frame_payloads_match_source_prefix"]


def test_corrupt_committed_block_is_rejected_without_rewriting_evidence(experiment):
    case = experiment[3]["cases"]["corrupted_committed_block"]
    assert case["child"]["returncode"] == 0
    assert case["recovery"]["outcome"] == "rejected"
    assert case["recovery"]["exception_type"] == "RecordingIntegrityError"
    assert "SHA256" in case["recovery"]["error"]
    assert case["recovery"]["report"] is None
    assert case["before"] == case["after"]
    assert case["validation"]["index_file_unchanged"]
    assert case["validation"]["block_files_unchanged"]
    assert case["corruption"]["original_sha256"] != case["corruption"]["corrupted_sha256"]


def test_existing_output_is_refused_even_when_empty(source, tmp_path):
    with pytest.raises(FileExistsError):
        api().run_experiment(source, tmp_path, frame_limit=8, frames_per_block=2)
    assert not list(tmp_path.iterdir())


def test_output_beneath_source_is_refused_before_creating_files(source):
    output = source / "nested-experiment"
    before = file_hashes(source)
    with pytest.raises(ValueError, match="source"):
        api().run_experiment(source, output, frame_limit=8, frames_per_block=2)
    assert not output.exists() and file_hashes(source) == before


def test_cli_reports_existing_output_without_running_a_case(source, tmp_path):
    api()
    result = subprocess.run([sys.executable, str(ROOT / "scripts/benchmark_recording_recovery.py"),
                             "--source-recording", str(source), "--output-dir", str(tmp_path),
                             "--frames", "8", "--frames-per-block", "2"],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode != 0 and "exist" in result.stderr.lower()
    assert not list(tmp_path.iterdir())


def test_public_export_omits_machine_details_without_changing_measured_evidence(experiment, tmp_path):
    source, output, _, report = experiment
    raw_path = output / "evidence.json"
    raw_bytes = raw_path.read_bytes()
    public_path = tmp_path / "evidence.json"
    public_path.write_text("previous public export")
    result = subprocess.run([sys.executable, str(ROOT / "scripts/benchmark_recording_recovery.py"),
                             "--export-public", str(raw_path), "--output-dir", str(tmp_path)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    public = json.loads(public_path.read_text())
    assert raw_path.read_bytes() == raw_bytes
    assert public["raw_report_sha256"] == hashlib.sha256(raw_bytes).hexdigest()
    assert public["exporter_sha256"] == hashlib.sha256(
        (ROOT / "scripts/benchmark_recording_recovery.py").read_bytes()).hexdigest()
    assert public["export_note"]
    assert public["source"] == {**report["source"], "directory": "recording"}
    for field in ("input", "parameters", "implementation_sha256", "expectations_met"):
        assert public[field] == report[field]
    for name, case in public["cases"].items():
        original = report["cases"][name]
        for field in ("before", "after", "validation", "corruption"):
            assert case[field] == original[field]
        assert case["child"] == {key: value for key, value in original["child"].items()
                                  if key not in {"command", "pid"}}
        assert case["recovery"]["elapsed_seconds"] == original["recovery"]["elapsed_seconds"]
        assert case["recovery"]["report"] == original["recovery"]["report"]
    assert public["cases"]["corrupted_committed_block"]["recovery"]["error"] == (
        "points SHA256 hash mismatch in corrupted_committed_block/recording/blocks/block-000001")
    serialized = public_path.read_text()
    assert str(source) not in serialized and str(output) not in serialized and str(ROOT) not in serialized
    with pytest.raises(ValueError, match="raw report"):
        api().export_public_evidence(raw_path, raw_path)
    assert raw_path.read_bytes() == raw_bytes
