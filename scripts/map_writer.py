"""Bounded asynchronous XYZI recording, with atomic blocks and checked recovery.

This module deliberately has no ROS dependencies. ``submit`` transfers ownership
of a payload to the writer: the caller must not mutate an accepted payload.
"""

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable

import numpy as np

SCHEMA_VERSION = 1
POINT_DTYPE = np.dtype("<f4")
INDEX_COUNTS = ("written_frames", "written_points", "written_input_points", "nonfinite_points",
                "nonfinite_intensity_points", "missing_intensity_frames")


class RecordingIntegrityError(ValueError):
    """A persisted file disagrees with its schema, checksum, or index."""


class RecordingBusyError(RuntimeError):
    """An active process still owns this recording."""


@dataclass(frozen=True)
class RecorderSettings:
    queue_capacity: int = 32
    chunk_points: int = 1_000_000
    max_frame_points: int = 250_000
    max_frame_bytes: int = 16_777_216
    chunk_frames: int = 256
    overflow: str = "fail"
    flush_interval: float = 2.0
    status_interval: float = 1.0
    nonfinite: str = "drop"
    missing_intensity: str = "zero"
    expected_frame_id: str = "camera_init"
    fsync: bool = True

    def __post_init__(self):
        for name in ("queue_capacity", "chunk_points", "max_frame_points", "max_frame_bytes", "chunk_frames"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_frame_points > self.chunk_points:
            raise ValueError("max_frame_points must not exceed chunk_points; frames are never split")
        for name in ("flush_interval", "status_interval"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        for name, choices in (("overflow", {"fail", "drop-newest"}),
                              ("nonfinite", {"drop", "error"}), ("missing_intensity", {"zero", "error"})):
            if getattr(self, name) not in choices:
                raise ValueError(f"invalid {name}")
        if not isinstance(self.expected_frame_id, str) or len(self.expected_frame_id.encode("utf-8")) > 256:
            raise ValueError("expected_frame_id must be at most 256 UTF-8 bytes")


@dataclass(frozen=True)
class DecodedPoints:
    points: np.ndarray
    nonfinite_points: int = 0
    missing_intensity: bool = False
    nonfinite_intensity_points: int = 0


def normalize_xyzi(points, *, nonfinite="drop", missing_intensity=False):
    """Convert to float32, omit invalid XYZ, and replace invalid intensity by 0."""
    if nonfinite not in {"drop", "error"}:
        raise ValueError("nonfinite must be drop or error")
    with np.errstate(over="ignore", invalid="ignore"):
        array = np.asarray(points, dtype=POINT_DTYPE)
    if array.ndim != 2 or array.shape[1] != 4:
        raise ValueError("points must have shape (N, 4): x, y, z, intensity")
    finite = np.isfinite(array[:, :3]).all(axis=1)
    omitted = int(len(array) - np.count_nonzero(finite))
    if omitted and nonfinite == "error":
        raise ValueError(f"{omitted} points have non-finite float32 coordinates")
    # Own the result so normalization never modifies the caller's payload.
    array = np.array(array[finite] if omitted else array, dtype=POINT_DTYPE, order="C", copy=True)
    invalid_intensity = ~np.isfinite(array[:, 3])
    intensity_count = int(np.count_nonzero(invalid_intensity))
    array[invalid_intensity, 3] = 0
    return DecodedPoints(array, omitted, bool(missing_intensity), intensity_count)


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(path, enabled):
    if enabled:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _atomic_json(path, value, fsync=True):
    data = _json_bytes(value)
    temporary = path.with_name("." + path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        if fsync:
            os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent, fsync)
    return hashlib.sha256(data).hexdigest()


def _write_npy(path, points, fsync):
    with path.open("wb") as handle:
        np.save(handle, points, allow_pickle=False)
        handle.flush()
        if fsync:
            os.fsync(handle.fileno())


def _acquire_lock(directory):
    handle = (directory / ".recorder.lock").open("a+b")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RecordingBusyError(f"a recorder or recovery process is active in {directory}") from exc
    return handle


def _empty_index(settings_sha256):
    return {"schema_version": SCHEMA_VERSION, "settings_sha256": settings_sha256,
            "committed_blocks": 0, "last_block_sha256": None, "first_sequence": None,
            "last_sequence": None, **dict.fromkeys(INDEX_COUNTS, 0)}


def _advance_index(index, metadata, block_sha256):
    result = dict(index)
    result["committed_blocks"] += 1
    result["last_block_sha256"] = block_sha256
    if result["first_sequence"] is None:
        result["first_sequence"] = metadata["first_sequence"]
    result["last_sequence"] = metadata["last_sequence"]
    for name in INDEX_COUNTS:
        result[name] += metadata[name]
    return result


@dataclass(frozen=True)
class _Frame:
    payload: Any
    sequence: int
    stamp_sec: int
    stamp_nanosec: int
    frame_id: str
    input_points: int
    nonmonotonic: bool


class AsyncMapWriter:
    """A single producer and background writer with a bounded pending queue.

    The producer validates only bounded header fields and enqueues a reference.
    All decoding, checksumming, fsync, and JSON writes happen in the worker.
    ``completed`` means a clean drain with no failed or dropped input frames.
    """

    def __init__(self, directory, settings=None, *, decoder: Callable | None = None, source=None):
        self.directory = Path(directory)
        self.settings = settings or RecorderSettings()
        self.decoder = decoder or (lambda payload: normalize_xyzi(payload, nonfinite=self.settings.nonfinite))
        self.directory.mkdir(parents=True, exist_ok=True)
        if any(self.directory.iterdir()):
            raise ValueError(f"output directory must be empty: {self.directory}")
        self._lock_file = _acquire_lock(self.directory)
        self._condition = threading.Condition()
        self._queue = deque()
        self._accepting = True
        self._stop_requested = False
        self._closed = False
        self._finalized = False
        self._error = None
        self._last_stamp = None
        self._started = time.monotonic()
        self._stats = dict.fromkeys(("received_frames", "received_points", "enqueued_frames", "enqueued_points",
                                    "dropped_frames", "dropped_points", "failed_frames", "failed_points",
                                    "nonmonotonic_frames", "duplicate_stamp_frames", "queue_high_water"), 0)
        self._stats["write_seconds"] = 0.0
        try:
            (self.directory / "blocks").mkdir()
            metadata = {"schema_version": SCHEMA_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
                        "point_columns": ["x", "y", "z", "intensity"], "point_dtype": "<f4",
                        "coordinate_units": "m", "settings": asdict(self.settings), "source": source or {}}
            self._settings_sha256 = _atomic_json(self.directory / "settings.json", metadata, self.settings.fsync)
            self._index = _empty_index(self._settings_sha256)
            self._index_sha256 = _atomic_json(self.directory / "index.json", self._index, self.settings.fsync)
            self._publish_status()
            self._thread = threading.Thread(target=self._run, name="map-writer", daemon=False)
            self._thread.start()
        except BaseException:
            self._lock_file.close()
            raise

    def _fail_locked(self, message):
        if self._error is None:
            self._error = str(message)[:2048]
        self._accepting = False
        self._stop_requested = True
        self._condition.notify_all()

    def submit(self, payload, *, stamp_sec, stamp_nanosec, frame_id, input_points=None, input_bytes=None):
        """Enqueue without disk I/O or waiting for queue space; return acceptance."""
        if input_points is None:
            input_points = len(payload)
        if input_bytes is None:
            input_bytes = payload.nbytes
        with self._condition:
            if not self._accepting:
                return False
            sequence = self._stats["received_frames"]
            self._stats["received_frames"] += 1
            count = input_points if type(input_points) is int and input_points >= 0 else 0
            self._stats["received_points"] += count
            problem = None
            if type(input_points) is not int or input_points < 0:
                problem = "input_points must be a nonnegative integer"
            elif input_points > self.settings.max_frame_points:
                problem = "input exceeds max_frame_points"
            elif type(input_bytes) is not int or input_bytes < 0 or input_bytes > self.settings.max_frame_bytes:
                problem = "input exceeds max_frame_bytes or has an invalid byte count"
            elif type(stamp_sec) is not int or type(stamp_nanosec) is not int or not 0 <= stamp_nanosec < 1_000_000_000:
                problem = "invalid original ROS timestamp"
            elif not isinstance(frame_id, str) or len(frame_id.encode("utf-8")) > 256:
                problem = "frame_id must contain at most 256 UTF-8 bytes"
            elif self.settings.expected_frame_id and frame_id != self.settings.expected_frame_id:
                problem = f"unexpected frame_id {frame_id!r}; expected {self.settings.expected_frame_id!r}"
            if problem:
                self._stats["failed_frames"] += 1
                self._stats["failed_points"] += count
                self._fail_locked(problem)
                return False
            stamp = (stamp_sec, stamp_nanosec)
            nonmonotonic = self._last_stamp is not None and stamp <= self._last_stamp
            if nonmonotonic:
                self._stats["nonmonotonic_frames"] += 1
                self._stats["duplicate_stamp_frames"] += int(stamp == self._last_stamp)
            self._last_stamp = stamp
            if len(self._queue) >= self.settings.queue_capacity:
                kind = "failed" if self.settings.overflow == "fail" else "dropped"
                self._stats[f"{kind}_frames"] += 1
                self._stats[f"{kind}_points"] += count
                if kind == "failed":
                    self._fail_locked("bounded writer queue is full")
                return False
            self._queue.append(_Frame(payload, sequence, stamp_sec, stamp_nanosec, frame_id, count, nonmonotonic))
            self._stats["enqueued_frames"] += 1
            self._stats["enqueued_points"] += count
            self._stats["queue_high_water"] = max(self._stats["queue_high_water"], len(self._queue))
            self._condition.notify()
            return True

    def fail(self, message):
        """Report an external adapter failure and request a clean queue drain."""
        with self._condition:
            self._fail_locked(message)

    def snapshot(self):
        with self._condition:
            snapshot = {**self._stats, **self._index}
            snapshot.update(schema_version=SCHEMA_VERSION, running=not self._closed,
                            accepting=self._accepting, queue_size=len(self._queue),
                            error=self._error, settings=asdict(self.settings),
                            received_counts_verified=True, elapsed_seconds=time.monotonic() - self._started)
            snapshot["pending_frames"] = (snapshot["received_frames"] - snapshot["written_frames"]
                                          - snapshot["dropped_frames"] - snapshot["failed_frames"])
            snapshot["pending_points"] = (snapshot["received_points"] - snapshot["written_input_points"]
                                          - snapshot["dropped_points"] - snapshot["failed_points"])
            snapshot["completed"] = (self._closed and self._finalized and not self._error and not snapshot["dropped_frames"]
                                     and not snapshot["failed_frames"] and not snapshot["pending_frames"])
            snapshot["index_sha256"] = self._index_sha256
            return snapshot

    def _publish_status(self):
        # This live snapshot is advisory. The block/index/manifest commit path
        # remains durable; syncing a heartbeat only stalls the data writer.
        _atomic_json(self.directory / "status.json", self.snapshot(), fsync=False)

    def _commit_block(self, points, rows):
        started = time.monotonic()
        block_id = self._index["committed_blocks"]
        blocks = self.directory / "blocks"
        temporary = blocks / f".block-{block_id:06d}.tmp"
        temporary.mkdir()
        _write_npy(temporary / "points.npy", points, self.settings.fsync)
        frames_sha = _atomic_json(temporary / "frames.json",
                                  {"schema_version": SCHEMA_VERSION, "block_id": block_id, "frames": rows},
                                  self.settings.fsync)
        metadata = {"schema_version": SCHEMA_VERSION, "block_id": block_id,
                    "settings_sha256": self._settings_sha256,
                    "previous_block_sha256": self._index["last_block_sha256"],
                    "points_sha256": _sha256_file(temporary / "points.npy"), "frames_sha256": frames_sha,
                    "point_dtype": "<f4", "point_columns": ["x", "y", "z", "intensity"],
                    "first_sequence": rows[0]["sequence"], "last_sequence": rows[-1]["sequence"],
                    "written_frames": len(rows), "written_points": len(points),
                    "written_input_points": sum(row["input_points"] for row in rows),
                    "nonfinite_points": sum(row["nonfinite_points"] for row in rows),
                    "nonfinite_intensity_points": sum(row["nonfinite_intensity_points"] for row in rows),
                    "missing_intensity_frames": sum(row["missing_intensity"] for row in rows)}
        block_sha = _atomic_json(temporary / "block.json", metadata, self.settings.fsync)
        _fsync_directory(temporary, self.settings.fsync)
        os.replace(temporary, blocks / f"block-{block_id:06d}")
        _fsync_directory(blocks, self.settings.fsync)
        index = _advance_index(self._index, metadata, block_sha)
        index_sha = _atomic_json(self.directory / "index.json", index, self.settings.fsync)
        with self._condition:
            self._index = index
            self._index_sha256 = index_sha
            self._stats["write_seconds"] += time.monotonic() - started

    def _run(self):
        rows, current = [], None
        point_count = 0
        last_flush = last_status = time.monotonic()
        try:
            buffer = np.empty((self.settings.chunk_points, 4), dtype=POINT_DTYPE)
            while True:
                with self._condition:
                    if not self._queue and not self._stop_requested:
                        self._condition.wait(timeout=min(self.settings.status_interval, self.settings.flush_interval))
                    current = self._queue.popleft() if self._queue else None
                    finished = self._stop_requested and current is None
                if current is not None:
                    decoded = self.decoder(current.payload)
                    if not isinstance(decoded, DecodedPoints):
                        decoded = normalize_xyzi(decoded, nonfinite=self.settings.nonfinite)
                    points = decoded.points
                    if (points.ndim != 2 or points.shape[1] != 4 or points.dtype != POINT_DTYPE
                            or len(points) + decoded.nonfinite_points != current.input_points
                            or len(points) > self.settings.max_frame_points):
                        raise ValueError("decoded point schema or input count does not match the frame")
                    if rows and (point_count + len(points) > self.settings.chunk_points
                                 or len(rows) >= self.settings.chunk_frames):
                        self._commit_block(buffer[:point_count], rows)
                        rows, point_count = [], 0
                        last_flush = time.monotonic()
                    if not rows:
                        # Idle waiting before the first frame is not buffer age.
                        last_flush = time.monotonic()
                    buffer[point_count:point_count + len(points)] = points
                    rows.append({"sequence": current.sequence, "stamp_sec": current.stamp_sec,
                                 "stamp_nanosec": current.stamp_nanosec, "frame_id": current.frame_id,
                                 "input_points": current.input_points, "point_offset": point_count,
                                 "point_count": len(points), "nonfinite_points": decoded.nonfinite_points,
                                 "missing_intensity": decoded.missing_intensity,
                                 "nonfinite_intensity_points": decoded.nonfinite_intensity_points,
                                 "nonmonotonic": current.nonmonotonic})
                    point_count += len(points)
                    current = None
                    del points, decoded
                now = time.monotonic()
                if rows and (finished or point_count == self.settings.chunk_points
                             or len(rows) == self.settings.chunk_frames or now - last_flush >= self.settings.flush_interval):
                    self._commit_block(buffer[:point_count], rows)
                    rows, point_count = [], 0
                    last_flush = time.monotonic()
                if finished:
                    break
                if now - last_status >= self.settings.status_interval:
                    self._publish_status()
                    last_status = time.monotonic()
        except Exception as exc:
            with self._condition:
                failed_frames = len(rows) + int(current is not None) + len(self._queue)
                failed_points = sum(row["input_points"] for row in rows)
                failed_points += current.input_points if current is not None else 0
                failed_points += sum(frame.input_points for frame in self._queue)
                self._queue.clear()
                self._stats["failed_frames"] += failed_frames
                self._stats["failed_points"] += failed_points
                self._fail_locked(f"{type(exc).__name__}: {exc}")
        finally:
            with self._condition:
                self._closed = True
                self._accepting = False
            try:
                final = self.snapshot()
                final["completed"] = (not final["error"] and not final["dropped_frames"]
                                      and not final["failed_frames"] and not final["pending_frames"])
                # The manifest is the last completion marker. A status snapshot
                # is advisory until this final file is present and checked.
                _atomic_json(self.directory / "status.json", final, self.settings.fsync)
                _atomic_json(self.directory / "manifest.json", final, self.settings.fsync)
                with self._condition:
                    self._finalized = True
            except Exception as exc:
                with self._condition:
                    self._fail_locked(f"final metadata write failed: {exc}")
                # A rename can succeed before directory fsync fails. Remove any
                # uncertain success marker, then publish failure if I/O permits.
                try:
                    (self.directory / "manifest.json").unlink(missing_ok=True)
                    _fsync_directory(self.directory, self.settings.fsync)
                except OSError:
                    pass
                for name in ("manifest.json", "status.json"):
                    try:
                        _atomic_json(self.directory / name, self.snapshot(), self.settings.fsync)
                    except OSError:
                        pass
            self._lock_file.close()

    def close(self):
        """Stop accepting frames and wait until queued work is drained or failed."""
        with self._condition:
            self._accepting = False
            self._stop_requested = True
            self._condition.notify_all()
        self._thread.join()
        return self.snapshot()


def _read_json(path, max_bytes=1 << 20):
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise RecordingIntegrityError(f"metadata size limit exceeded: {path}")
        result = json.loads(data)
        if not isinstance(result, dict) or result.get("schema_version") != SCHEMA_VERSION:
            raise RecordingIntegrityError(f"unsupported metadata schema: {path}")
        return result
    except (OSError, ValueError) as exc:
        if isinstance(exc, RecordingIntegrityError):
            raise
        raise RecordingIntegrityError(f"cannot read metadata {path}: {exc}") from exc


def _require(condition, message):
    if not condition:
        raise RecordingIntegrityError(message)


def _nonnegative_integer(value):
    return type(value) is int and value >= 0


def _validate_block(path, index, settings, settings_sha):
    metadata = _read_json(path / "block.json")
    block_id = index["committed_blocks"]
    _require(metadata.get("block_id") == block_id, f"block schema: incorrect block_id in {path}")
    _require(metadata.get("settings_sha256") == settings_sha, f"settings hash mismatch in {path}")
    _require(metadata.get("previous_block_sha256") == index["last_block_sha256"], f"block chain hash mismatch in {path}")
    for name in ("points", "frames"):
        file = path / ("points.npy" if name == "points" else "frames.json")
        _require(file.is_file() and _sha256_file(file) == metadata.get(f"{name}_sha256"),
                 f"{name} SHA256 hash mismatch in {path}")
    _require(metadata.get("point_dtype") == "<f4" and metadata.get("point_columns") == ["x", "y", "z", "intensity"],
             f"invalid point schema in {path}")
    for name in INDEX_COUNTS:
        _require(_nonnegative_integer(metadata.get(name)), f"invalid count schema: {name} in {path}")
    _require(0 < metadata["written_frames"] <= settings.chunk_frames, f"frame count schema exceeds limit in {path}")
    _require(metadata["written_points"] <= settings.chunk_points, f"point count schema exceeds limit in {path}")
    try:
        with (path / "points.npy").open("rb") as handle:
            version = np.lib.format.read_magic(handle)
            _require(version == (1, 0), f"unsupported NPY schema in {path}")
            shape, fortran, dtype = np.lib.format.read_array_header_1_0(handle)
            _require(shape == (metadata["written_points"], 4) and dtype.str == "<f4" and not fortran,
                     f"NPY shape or dtype schema mismatch in {path}")
            _require(handle.tell() + metadata["written_points"] * 16 == (path / "points.npy").stat().st_size,
                     f"NPY size mismatch in {path}")
    except (OSError, ValueError) as exc:
        if isinstance(exc, RecordingIntegrityError):
            raise
        raise RecordingIntegrityError(f"invalid NPY schema in {path}: {exc}") from exc
    frame_document = _read_json(path / "frames.json", settings.chunk_frames * 2048 + 4096)
    rows = frame_document.get("frames")
    _require(frame_document.get("block_id") == block_id and isinstance(rows, list)
             and len(rows) == metadata["written_frames"], f"frame metadata schema mismatch in {path}")
    total = dict.fromkeys(INDEX_COUNTS, 0)
    previous_sequence = index["last_sequence"]
    for row in rows:
        _require(isinstance(row, dict), f"invalid frame schema in {path}")
        for key in ("sequence", "input_points", "point_offset", "point_count", "nonfinite_points", "nonfinite_intensity_points"):
            _require(_nonnegative_integer(row.get(key)), f"invalid frame schema for {key} in {path}")
        _require(row["point_offset"] == total["written_points"], f"frame point offset mismatch in {path}")
        _require(row["input_points"] == row["point_count"] + row["nonfinite_points"]
                 and row["input_points"] <= settings.max_frame_points, f"frame count schema mismatch in {path}")
        _require(row["nonfinite_intensity_points"] <= row["point_count"], f"intensity count schema mismatch in {path}")
        _require(previous_sequence is None or row["sequence"] > previous_sequence, f"frame sequence schema mismatch in {path}")
        _require(type(row.get("stamp_sec")) is int and type(row.get("stamp_nanosec")) is int
                 and 0 <= row["stamp_nanosec"] < 1_000_000_000, f"timestamp schema mismatch in {path}")
        _require(isinstance(row.get("frame_id"), str) and len(row["frame_id"].encode("utf-8")) <= 256,
                 f"frame_id schema mismatch in {path}")
        _require(not settings.expected_frame_id or row["frame_id"] == settings.expected_frame_id,
                 f"unexpected frame_id in {path}")
        _require(type(row.get("missing_intensity")) is bool and type(row.get("nonmonotonic")) is bool,
                 f"frame flag schema mismatch in {path}")
        previous_sequence = row["sequence"]
        total["written_frames"] += 1
        total["written_points"] += row["point_count"]
        total["written_input_points"] += row["input_points"]
        total["nonfinite_points"] += row["nonfinite_points"]
        total["nonfinite_intensity_points"] += row["nonfinite_intensity_points"]
        total["missing_intensity_frames"] += row["missing_intensity"]
    _require(all(total[name] == metadata[name] for name in INDEX_COUNTS), f"block totals schema mismatch in {path}")
    _require(metadata.get("first_sequence") == rows[0]["sequence"] and metadata.get("last_sequence") == rows[-1]["sequence"],
             f"block sequence schema mismatch in {path}")
    return _advance_index(index, metadata, _sha256_file(path / "block.json"))


def recover_recording(directory):
    """Validate every block; adopt complete orphan blocks and quarantine temps.

    Recovery never infers an unobserved end of input or lost-frame counts. A
    recovered interrupted recording therefore remains ``completed=False``.
    Existing indexed corruption is rejected without rewriting the index.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise RecordingIntegrityError(f"recording directory does not exist: {directory}")
    with _acquire_lock(directory):
        settings_document = _read_json(directory / "settings.json")
        try:
            settings = RecorderSettings(**settings_document["settings"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RecordingIntegrityError(f"invalid settings schema: {exc}") from exc
        settings_sha = _sha256_file(directory / "settings.json")
        old_index = _read_json(directory / "index.json") if (directory / "index.json").exists() else None
        old_manifest = _read_json(directory / "manifest.json") if (directory / "manifest.json").exists() else None
        recovery_warnings = []
        if old_index:
            _require(old_index.get("settings_sha256") == settings_sha, "settings SHA256 hash mismatch")
            _require(_nonnegative_integer(old_index.get("committed_blocks")), "index block count schema mismatch")
        if old_manifest:
            _require(old_manifest.get("settings_sha256") == settings_sha, "manifest settings hash mismatch")
            paired = old_index is not None and old_manifest.get("index_sha256") == _sha256_file(directory / "index.json")
            if not paired:
                _require(old_manifest.get("completed") is False, "manifest/index SHA256 hash mismatch")
                # A failed directory fsync can follow a successful index rename.
                # Validate that index against every block before reconstructing
                # an explicitly incomplete manifest; never trust its old totals.
                recovery_warnings.append("unfinished manifest/index SHA256 hash mismatch; rebuilt from verified blocks")
        blocks = directory / "blocks"
        _require(blocks.is_dir(), "blocks directory is missing")
        count, maximum, temporary_count = 0, -1, 0
        # Scan once without keeping the entire recording's metadata in memory.
        with os.scandir(blocks) as entries:
            for entry in entries:
                match = re.fullmatch(r"block-(\d{6,})", entry.name)
                if match and entry.is_dir(follow_symlinks=False):
                    number = int(match.group(1))
                    _require(entry.name == f"block-{number:06d}", "noncanonical block filename")
                    count += 1
                    maximum = max(maximum, number)
                elif re.fullmatch(r"\.block-\d{6,}\.tmp", entry.name) and entry.is_dir(follow_symlinks=False):
                    temporary_count += 1
                else:
                    raise RecordingIntegrityError(f"unexpected block entry: {entry.name}")
        _require(maximum + 1 == count, "missing or duplicate completed block sequence")
        indexed_count = old_index["committed_blocks"] if old_index else 0
        _require(indexed_count <= count, "index refers to missing completed blocks")
        index = _empty_index(settings_sha)
        if old_index and not indexed_count:
            _require(old_index == index, "empty index schema mismatch")
        for number in range(count):
            index = _validate_block(blocks / f"block-{number:06d}", index, settings, settings_sha)
            if old_index and number + 1 == indexed_count:
                _require(index == old_index, "paired block/index hash or totals mismatch")
        adopted = count - indexed_count
        intact = bool(old_manifest and old_manifest.get("completed") is True and not adopted and not temporary_count)
        if intact:
            for name, value in index.items():
                _require(old_manifest.get(name) == value, "completed manifest/index mismatch")
            _require(old_manifest.get("error") is None
                     and old_manifest.get("running") is False
                     and old_manifest.get("accepting") is False
                     and old_manifest.get("received_counts_verified") is True,
                     "completed manifest has contradictory status")
            for name in ("failed_frames", "failed_points", "dropped_frames", "dropped_points",
                         "pending_frames", "pending_points", "queue_size"):
                _require(type(old_manifest.get(name)) is int and old_manifest[name] == 0,
                         "completed manifest has loss or pending work")
            for received, enqueued, written in (("received_frames", "enqueued_frames", "written_frames"),
                                                ("received_points", "enqueued_points", "written_input_points")):
                _require(_nonnegative_integer(old_manifest.get(received))
                         and _nonnegative_integer(old_manifest.get(enqueued))
                         and old_manifest[received] == old_manifest[enqueued] == index[written],
                         "completed manifest violates received/written count conservation")
            return {**old_manifest, "integrity_ok": True, "adopted_blocks": 0, "quarantined_temporary_blocks": 0}
        if temporary_count:
            incomplete = directory / "incomplete"
            incomplete.mkdir(exist_ok=True)
            with os.scandir(blocks) as entries:
                for entry in entries:
                    if re.fullmatch(r"\.block-\d{6,}\.tmp", entry.name):
                        destination = incomplete / entry.name
                        _require(not destination.exists(), "temporary block quarantine destination already exists")
                        os.replace(entry.path, destination)
            _fsync_directory(incomplete, settings.fsync)
            _fsync_directory(blocks, settings.fsync)
        index_sha = _atomic_json(directory / "index.json", index, settings.fsync)
        report = {**index, "schema_version": SCHEMA_VERSION, "settings": asdict(settings),
                  "settings_sha256": settings_sha, "index_sha256": index_sha,
                  "completed": False, "running": False, "accepting": False,
                  "integrity_ok": not recovery_warnings, "received_counts_verified": False, "recovered": True,
                  "recovery_warnings": recovery_warnings,
                  "adopted_blocks": adopted, "quarantined_temporary_blocks": temporary_count,
                  "error": "recovered recording; end of input and unwritten frame counts are unverified"}
        for name in ("received_frames", "received_points", "enqueued_frames", "enqueued_points", "dropped_frames",
                     "dropped_points", "failed_frames", "failed_points", "pending_frames", "pending_points",
                     "queue_high_water", "write_seconds", "nonmonotonic_frames", "duplicate_stamp_frames"):
            report[name] = None
        report["queue_size"] = 0
        _atomic_json(directory / "manifest.json", report, settings.fsync)
        _atomic_json(directory / "status.json", report, settings.fsync)
        return report
