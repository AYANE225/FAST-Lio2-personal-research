"""Run isolated, ROS-independent recording interruption and recovery cases.

Only a bounded prefix of already persisted source XYZI frames is replayed.
SIGKILL is sent by the parent after a child reports a specific write boundary;
this is a process interruption experiment, not a power-loss or queue-recovery
test. The original recording is only read, never passed to recover_recording.
Point payloads stay in the new output directory. Use --export-public RAW_REPORT
--output-dir PUBLIC_DIR to regenerate public evidence.json with source, frame,
block and implementation hashes. Public export may replace its destination;
recording experiments always require a new output directory.
"""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import selectors
import signal
import subprocess
import sys
import threading
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import map_writer


def _write_json(path, value):
    with path.open("x") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


def _files(directory):
    return {str(path.relative_to(directory)): {
        "sha256": map_writer._sha256_file(path), "bytes": path.stat().st_size}
        for path in sorted(directory.rglob("*")) if path.is_file()}


def _frame_hash(points, row):
    return {key: row[key] for key in ("stamp_sec", "stamp_nanosec", "frame_id", "point_count")} | {
        "xyzi_sha256": hashlib.sha256(points.tobytes(order="C")).hexdigest()}


def _load_source_prefix(source, frame_limit):
    """Validate selected blocks with the writer's read-only schema/hash checks."""
    documents = {name: map_writer._read_json(source / f"{name}.json")
                 for name in ("settings", "index", "manifest")}
    settings_sha = map_writer._sha256_file(source / "settings.json")
    if (documents["manifest"].get("completed") is not True
            or documents["manifest"].get("index_sha256") != map_writer._sha256_file(source / "index.json")
            or documents["index"].get("settings_sha256") != settings_sha):
        raise ValueError("source must have a completed manifest paired with its index and settings")
    settings = map_writer.RecorderSettings(**documents["settings"]["settings"])
    index = map_writer._empty_index(settings_sha)
    paths = [source / f"{name}.json" for name in documents]
    rows, payloads, point_offset = [], [], 0
    for number in range(documents["index"]["committed_blocks"]):
        block = source / "blocks" / f"block-{number:06d}"
        index = map_writer._validate_block(block, index, settings, settings_sha)
        paths.extend(block / name for name in ("block.json", "frames.json", "points.npy"))
        points = np.load(block / "points.npy", mmap_mode="r", allow_pickle=False)
        for row in map_writer._read_json(block / "frames.json")["frames"]:
            start = row["point_offset"]
            payload = np.array(points[start:start + row["point_count"]], copy=True)
            rows.append({**_frame_hash(payload, row), "source_sequence": row["sequence"],
                         "source_block": str(block.relative_to(source)),
                         "source_point_offset": start, "point_offset": point_offset,
                         "source_input_points": row["input_points"]})
            payloads.append(payload)
            point_offset += len(payload)
            if len(rows) == frame_limit:
                break
        if len(rows) == frame_limit:
            break
    if len(rows) != frame_limit:
        raise ValueError(f"source contains fewer than {frame_limit} persisted frames")
    provenance = {"directory": str(source), "metadata": documents,
                  "validation_scope": "Manifest/index/settings pairing and all selected source blocks; remaining blocks are not scanned.",
                  "selected_frame_count": len(rows), "selected_point_count": point_offset,
                  "selected_frames": rows,
                  "files": {str(path.relative_to(source)): {
                      "sha256": map_writer._sha256_file(path), "bytes": path.stat().st_size}
                      for path in paths}}
    return np.concatenate(payloads), rows, provenance


def _snapshot(directory):
    """Capture on-disk claims and bytes; validity is reported by recovery."""
    blocks = []
    for block in sorted((directory / "blocks").glob("block-*")):
        metadata = map_writer._read_json(block / "block.json")
        blocks.append({"directory": block.name, "files": _files(block),
                       "written_frames": metadata["written_frames"],
                       "written_points": metadata["written_points"],
                       "first_sequence": metadata["first_sequence"],
                       "last_sequence": metadata["last_sequence"]})
    return {"index": map_writer._read_json(directory / "index.json"),
            "manifest": (map_writer._read_json(directory / "manifest.json")
                         if (directory / "manifest.json").exists() else None),
            "top_level_files": {name: map_writer._sha256_file(directory / name)
                                for name in ("settings.json", "index.json", "manifest.json", "status.json")
                                if (directory / name).exists()},
            "blocks": blocks,
            "temporary_blocks": [{"directory": path.name, "files": _files(path)}
                                 for path in sorted((directory / "blocks").glob(".block-*.tmp"))],
            "quarantined_blocks": [{"directory": path.name, "files": _files(path)}
                                   for path in sorted((directory / "incomplete").glob(".block-*.tmp"))]}


def _recorded_frames(directory):
    frames = []
    for block in sorted((directory / "blocks").glob("block-*")):
        points = np.load(block / "points.npy", mmap_mode="r", allow_pickle=False)
        for row in map_writer._read_json(block / "frames.json")["frames"]:
            start = row["point_offset"]
            frames.append(_frame_hash(points[start:start + row["point_count"]], row))
    return frames


def _run_worker(input_dir, directory, mode):
    document = json.loads((input_dir / "input.json").read_text())
    started = time.monotonic()

    def pause_at_boundary(condition, **details):
        print(json.dumps({"condition": condition, "block_id": 2, "fsync": True,
                          "worker_elapsed_seconds": time.monotonic() - started, **details}), flush=True)
        threading.Event().wait()  # The parent observes this marker, then sends SIGKILL.

    real_atomic_json = map_writer._atomic_json
    real_write_npy = map_writer._write_npy

    def write_index(path, value, fsync=True):
        if path.name == "index.json" and value.get("committed_blocks") == 3:
            pause_at_boundary("complete_block_before_index", next_index=value)
        return real_atomic_json(path, value, fsync)

    def write_partial_npy(path, points, fsync):
        if path.parent.name == ".block-000002.tmp":
            buffer = io.BytesIO()
            np.save(buffer, points, allow_pickle=False)
            data = buffer.getvalue()
            prefix = data[:len(data) // 2]
            with path.open("wb") as handle:
                handle.write(prefix)
                handle.flush()
                os.fsync(handle.fileno())
            pause_at_boundary("partial_temporary_npy", persisted_bytes=len(prefix),
                              complete_npy_bytes=len(data))
        return real_write_npy(path, points, fsync)

    # These hooks exist only in this fresh child; map_writer.py is unchanged.
    if mode == "before_index":
        map_writer._atomic_json = write_index
    elif mode == "partial_temporary":
        map_writer._write_npy = write_partial_npy
    points = np.load(input_dir / "points.npy", mmap_mode="r", allow_pickle=False)
    writer = map_writer.AsyncMapWriter(directory, map_writer.RecorderSettings(**document["settings"]),
                                       source={"experiment": "recording_recovery",
                                               "input_points_sha256": document["points_sha256"]})
    for row in document["frames"]:
        start = row["point_offset"]
        accepted = writer.submit(points[start:start + row["point_count"]],
                                 stamp_sec=row["stamp_sec"], stamp_nanosec=row["stamp_nanosec"],
                                 frame_id=row["frame_id"])
        if not accepted:
            writer.close()
            raise RuntimeError("experiment input was not accepted")
    result = writer.close()
    if not result["completed"]:
        raise RuntimeError(f"control writer did not complete: {result}")
    print(json.dumps({"completed": True, "written_frames": result["written_frames"]}), flush=True)


def _run_child(input_dir, case_dir, mode, timeout_seconds):
    case_dir.mkdir()
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", mode,
               "--input-dir", str(input_dir), "--output-dir", str(case_dir / "recording")]
    started_at, started = datetime.now(timezone.utc).isoformat(), time.monotonic()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    marker, event = "", None
    try:
        if mode != "normal":
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                if not selector.select(timeout_seconds):
                    raise TimeoutError("child did not reach the requested write boundary")
                marker = process.stdout.readline()
            if not marker:
                raise RuntimeError("child exited before the requested write boundary")
            event = json.loads(marker)
            expected = "complete_block_before_index" if mode == "before_index" else "partial_temporary_npy"
            if event.get("condition") != expected:
                raise RuntimeError(f"unexpected child boundary: {event}")
            event["parent_observed_seconds"] = time.monotonic() - started
            process.kill()
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except BaseException:
        if process.poll() is None:
            process.kill()
        stdout, stderr = process.communicate(timeout=5)
        (case_dir / "stdout.log").write_text(marker + stdout)
        (case_dir / "stderr.log").write_text(stderr)
        raise
    (case_dir / "stdout.log").write_text(marker + stdout)
    (case_dir / "stderr.log").write_text(stderr)
    expected_code = 0 if mode == "normal" else -signal.SIGKILL
    if process.returncode != expected_code:
        raise RuntimeError(f"unexpected child return code {process.returncode}: {stderr}")
    return {"command": command, "pid": process.pid, "returncode": process.returncode,
            "signal": "SIGKILL" if process.returncode == -signal.SIGKILL else None,
            "started_at": started_at, "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": time.monotonic() - started, "boundary_event": event,
            "stdout": marker + stdout, "stderr": stderr}


def run_experiment(source, output, *, frame_limit=60, frames_per_block=10, timeout_seconds=30):
    """Create fresh case directories and return the independently checkable evidence."""
    source, output = Path(source).resolve(), Path(output).resolve()
    if output == source or source in output.parents:
        raise ValueError("output must be outside the read-only source recording")
    if output.exists():
        raise FileExistsError(f"output directory already exists: {output}")
    if (type(frame_limit) is not int or type(frames_per_block) is not int
            or frames_per_block < 1 or frame_limit < 3 * frames_per_block):
        raise ValueError("frames must cover at least three positive-sized blocks")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    points, rows, provenance = _load_source_prefix(source, frame_limit)
    max_frame_points = max(1, max(row["point_count"] for row in rows))
    settings = map_writer.RecorderSettings(
        queue_capacity=frame_limit, chunk_points=max_frame_points * frames_per_block,
        max_frame_points=max_frame_points, max_frame_bytes=max_frame_points * 16,
        chunk_frames=frames_per_block, expected_frame_id=rows[0]["frame_id"],
        flush_interval=60, nonfinite="error", fsync=True)
    output.mkdir(parents=True, exist_ok=False)
    input_dir = output / "input"
    input_dir.mkdir()
    np.save(input_dir / "points.npy", points, allow_pickle=False)
    input_document = {"settings": asdict(settings), "frames": rows,
                      "points_sha256": map_writer._sha256_file(input_dir / "points.npy")}
    _write_json(input_dir / "input.json", input_document)
    source_frames = [_frame_hash(points[row["point_offset"]:row["point_offset"] + row["point_count"]], row)
                     for row in rows]
    cases = {}
    for name, mode in (("normal_completion", "normal"), ("sigkill_before_index", "before_index"),
                       ("sigkill_partial_temporary_block", "partial_temporary"),
                       ("corrupted_committed_block", "normal")):
        case_dir = output / name
        child = _run_child(input_dir, case_dir, mode, timeout_seconds)
        directory = case_dir / "recording"
        corruption = None
        if name == "corrupted_committed_block":
            path = directory / "blocks/block-000001/points.npy"
            original_sha = map_writer._sha256_file(path)
            offset = path.stat().st_size - 1
            with path.open("r+b") as handle:
                handle.seek(offset)
                old_byte = handle.read(1)[0]
                handle.seek(offset)
                handle.write(bytes([old_byte ^ 1]))
                handle.flush()
                os.fsync(handle.fileno())
            corruption = {"file": str(path.relative_to(directory)), "byte_offset": offset,
                          "original_byte": old_byte, "corrupted_byte": old_byte ^ 1,
                          "original_sha256": original_sha, "corrupted_sha256": map_writer._sha256_file(path)}
        before = _snapshot(directory)
        started = time.monotonic()
        try:
            result = map_writer.recover_recording(directory)
            recovery = {"outcome": "accepted", "report": result}
        except map_writer.RecordingIntegrityError as exc:
            recovery = {"outcome": "rejected", "report": None,
                        "exception_type": type(exc).__name__, "error": str(exc)}
        recovery["elapsed_seconds"] = time.monotonic() - started
        after = _snapshot(directory)
        recorded = _recorded_frames(directory) if recovery["outcome"] == "accepted" else None
        validation = {"block_files_unchanged": before["blocks"] == after["blocks"],
                      "index_file_unchanged": before["top_level_files"]["index.json"] == after["top_level_files"]["index.json"],
                      "quarantined_files_unchanged": before["temporary_blocks"] == after["quarantined_blocks"],
                      "frame_payloads_match_source_prefix": (recorded == source_frames[:len(recorded)]
                                                            if recorded is not None else None)}
        if corruption:
            valid = recovery["outcome"] == "rejected" and before == after
        else:
            result = recovery["report"] or {}
            expected_frames = frame_limit if mode == "normal" else (3 if mode == "before_index" else 2) * frames_per_block
            valid = (result.get("integrity_ok") is True and result.get("written_frames") == expected_frames
                     and result.get("completed") is (mode == "normal")
                     and result.get("received_counts_verified") is (mode == "normal")
                     and result.get("adopted_blocks") == (1 if mode == "before_index" else 0)
                     and result.get("quarantined_temporary_blocks") == (1 if mode == "partial_temporary" else 0)
                     and validation["frame_payloads_match_source_prefix"])
        validation["expectations_met"] = bool(valid and validation["block_files_unchanged"]
                                               and validation["quarantined_files_unchanged"])
        cases[name] = {"child": child, "before": before, "recovery": recovery,
                       "after": after, "validation": validation, "corruption": corruption}
    provenance["files_unchanged"] = all(
        map_writer._sha256_file(source / path) == metadata["sha256"]
        for path, metadata in provenance["files"].items())
    report = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
              "source": provenance, "input": {"files": _files(input_dir), "settings": asdict(settings)},
              "cases": cases, "environment": {"python": platform.python_version(), "numpy": np.__version__,
                                                "platform": platform.platform(), "machine": platform.machine()},
              "implementation_sha256": {"scripts/map_writer.py": map_writer._sha256_file(Path(map_writer.__file__)),
                                        "scripts/benchmark_recording_recovery.py": map_writer._sha256_file(Path(__file__))},
              "parameters": {"frames": frame_limit, "frames_per_block": frames_per_block,
                             "interrupt_block_id": 2, "timeout_seconds": timeout_seconds},
              "interpretation": [
                  "Input is a bounded prefix of persisted source XYZI frames; original ROS receipt or queue behavior is not replayed.",
                  "Third-block publication pauses after complete file/directory fsync and block rename, before the index write.",
                  "Temporary-file case injects a half-written NPY, fsyncs its bytes, then waits for parent SIGKILL.",
                  "SIGKILL tests process interruption on this filesystem, not power loss or hardware durability.",
                  "Recovery verifies persisted blocks; interrupted received/enqueued/lost counts remain unknown and completed stays false.",
                  "Recovery does not restore in-memory queues or resume appending to the same recording.",
                  "Before/after snapshots preserve on-disk claims; a completed manifest in the corruption case is not a validated completion.",
                  "Each recovery duration is one local run immediately after writing and hashing; it is not a throughput benchmark."],
              "expectations_met": provenance["files_unchanged"] and all(case["validation"]["expectations_met"]
                                                                          for case in cases.values())}
    _write_json(output / "evidence.json", report)
    if not report["expectations_met"]:
        raise RuntimeError(f"experiment expectations failed; inspect {output / 'evidence.json'}")
    return report


def export_public_evidence(raw_report, destination):
    """Regenerate a public report without modifying or rerunning the raw experiment."""
    raw_report, destination = Path(raw_report).resolve(), Path(destination).resolve()
    if raw_report == destination or (destination.exists() and raw_report.samefile(destination)):
        raise ValueError("public export must not overwrite the raw report")
    raw_bytes = raw_report.read_bytes()
    report = json.loads(raw_bytes)
    report["source"]["directory"] = "recording"
    for name, case in report["cases"].items():
        command = case["child"].pop("command")
        case["child"].pop("pid")
        recording_directory = command[command.index("--output-dir") + 1]
        if isinstance(case["recovery"].get("error"), str):
            case["recovery"]["error"] = case["recovery"]["error"].replace(
                recording_directory, f"{name}/recording")
    report["raw_report_sha256"] = hashlib.sha256(raw_bytes).hexdigest()
    report["exporter_sha256"] = map_writer._sha256_file(Path(__file__))
    report["export_note"] = (
        "Local source root replaced by 'recording'; child commands and PIDs omitted; "
        "absolute recording paths in recovery errors replaced with case-relative paths. "
        "Source-relative filenames, hashes, parameters, boundaries, timings and results are preserved. "
        "The raw report and point blocks remain local. implementation_sha256 identifies the code at "
        "experiment time; exporter_sha256 identifies the script used for this public export.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-recording", type=Path)
    parser.add_argument("--export-public", type=Path, metavar="RAW_REPORT",
                        help="regenerate public evidence.json from an existing raw report without replaying frames")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--frames-per-block", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--worker", choices=("normal", "before_index", "partial_temporary"), help=argparse.SUPPRESS)
    parser.add_argument("--input-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.export_public:
            if args.worker or args.source_recording:
                parser.error("--export-public cannot be combined with recording or worker inputs")
            export_public_evidence(args.export_public, args.output_dir / "evidence.json")
            print(args.output_dir / "evidence.json")
        elif args.worker:
            if args.input_dir is None:
                parser.error("worker requires --input-dir")
            _run_worker(args.input_dir, args.output_dir, args.worker)
        else:
            if args.source_recording is None:
                parser.error("--source-recording is required")
            run_experiment(args.source_recording, args.output_dir, frame_limit=args.frames,
                           frames_per_block=args.frames_per_block, timeout_seconds=args.timeout_seconds)
            print(args.output_dir / "evidence.json")
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
