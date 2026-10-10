#!/usr/bin/env python3
"""Record registered ROS 2 PointCloud2 frames without accumulating a full map."""

import argparse
from functools import partial
import json
from pathlib import Path
import signal
import sys
import threading

import numpy as np

try:
    from .map_writer import AsyncMapWriter, RecorderSettings, normalize_xyzi, recover_recording
except ImportError:
    from map_writer import AsyncMapWriter, RecorderSettings, normalize_xyzi, recover_recording


def pointcloud2_to_xyzi(message, *, missing_intensity="zero", nonfinite="drop"):
    """Decode scalar fields with their declared offsets, types, endian and strides.

    Output has four little-endian float32 columns in row-major point order.
    Invalid XYZ are dropped (or rejected); missing/invalid intensity becomes
    zero and is counted explicitly. Other PointCloud2 fields are not recorded.
    """
    if missing_intensity not in {"zero", "error"}:
        raise ValueError("missing_intensity must be zero or error")
    width, height = int(message.width), int(message.height)
    point_step, row_step = int(message.point_step), int(message.row_step)
    if width < 0 or height < 0 or point_step <= 0 or row_step < width * point_step:
        raise ValueError("invalid PointCloud2 width, height, point_step, or row_step")
    if len(message.data) != row_step * height:
        raise ValueError("PointCloud2 data length differs from row_step * height")
    fields = {}
    for field in message.fields:
        if field.name in {"x", "y", "z", "intensity"}:
            if field.name in fields:
                raise ValueError(f"duplicate PointCloud2 field {field.name}")
            fields[field.name] = field
    for name in ("x", "y", "z"):
        if name not in fields:
            raise ValueError(f"PointCloud2 is missing {name}")
    missing = "intensity" not in fields
    if missing and missing_intensity == "error":
        raise ValueError("PointCloud2 is missing intensity")
    datatype = {1: "i1", 2: "u1", 3: "i2", 4: "u2", 5: "i4", 6: "u4", 7: "f4", 8: "f8"}
    names, formats, offsets = [], [], []
    for name in ("x", "y", "z", "intensity"):
        if name not in fields:
            continue
        field = fields[name]
        if field.count != 1 or field.datatype not in datatype:
            raise ValueError(f"PointCloud2 {name} must be a supported scalar numeric field")
        dtype = np.dtype((">" if message.is_bigendian else "<") + datatype[field.datatype])
        if field.offset < 0 or field.offset + dtype.itemsize > point_step:
            raise ValueError(f"PointCloud2 {name} offset exceeds point_step")
        names.append(name)
        formats.append(dtype)
        offsets.append(field.offset)
    dtype = np.dtype({"names": names, "formats": formats, "offsets": offsets, "itemsize": point_step})
    structured = np.ndarray((height, width), dtype=dtype, buffer=message.data, strides=(row_step, point_step))
    points = np.zeros((height * width, 4), dtype="<f4")
    with np.errstate(over="ignore", invalid="ignore"):
        for column, name in enumerate(("x", "y", "z", "intensity")):
            if name in fields:
                points[:, column] = structured[name].reshape(-1)
    return normalize_xyzi(points, nonfinite=nonfinite, missing_intensity=missing)


def _record(args, ros_args):
    # Keep core unit tests, help, and recovery usable outside a ROS environment.
    try:
        import rclpy
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
        from rclpy.signals import SignalHandlerOptions
        from sensor_msgs.msg import PointCloud2
    except ImportError as exc:
        raise RuntimeError("ROS 2 Python packages are unavailable; source your ROS environment and use its Python") from exc

    settings = RecorderSettings(queue_capacity=args.queue_capacity, chunk_points=args.chunk_points,
                                max_frame_points=args.max_frame_points, max_frame_bytes=args.max_frame_bytes,
                                chunk_frames=args.chunk_frames, overflow=args.overflow,
                                flush_interval=args.flush_interval, status_interval=args.status_interval,
                                nonfinite=args.nonfinite, missing_intensity=args.missing_intensity,
                                expected_frame_id=args.expected_frame_id, fsync=not args.no_fsync)
    if args.qos_depth <= 0:
        raise ValueError("qos_depth must be positive")
    context, node, writer, executor = Context(), None, None, None
    stopping = threading.Event()
    handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    result = None
    try:
        rclpy.init(args=ros_args, context=context, signal_handler_options=SignalHandlerOptions.NO)
        for number in handlers:
            signal.signal(number, lambda signum, frame: stopping.set())
        node = rclpy.create_node("map_recorder", context=context)
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)
        writer = AsyncMapWriter(args.output, settings,
                                decoder=partial(pointcloud2_to_xyzi, missing_intensity=args.missing_intensity,
                                                nonfinite=args.nonfinite),
                                source={"topic": args.topic, "qos_depth": args.qos_depth,
                                        "qos_reliability": args.qos_reliability})

        def receive(message):
            try:
                writer.submit(message, stamp_sec=int(message.header.stamp.sec),
                              stamp_nanosec=int(message.header.stamp.nanosec),
                              frame_id=message.header.frame_id,
                              input_points=int(message.width) * int(message.height), input_bytes=len(message.data))
            except Exception as exc:
                writer.fail(f"PointCloud2 callback: {type(exc).__name__}: {exc}")

        qos = QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=args.qos_depth,
                         reliability=(ReliabilityPolicy.RELIABLE if args.qos_reliability == "reliable"
                                      else ReliabilityPolicy.BEST_EFFORT), durability=DurabilityPolicy.VOLATILE)
        subscription = node.create_subscription(PointCloud2, args.topic, receive, qos)
        node.get_logger().info(f"Recording {args.topic} in {args.expected_frame_id or 'the original frame'} to {args.output}")
        while context.ok() and not stopping.is_set() and not writer.snapshot()["error"]:
            executor.spin_once(timeout_sec=0.1)
        node.destroy_subscription(subscription)
    except Exception as exc:
        if writer is None:
            raise
        writer.fail(f"ROS recorder: {type(exc).__name__}: {exc}")
    finally:
        if writer is not None:
            result = writer.close()
        if executor is not None:
            executor.shutdown()
        if node is not None:
            node.destroy_node()
        if context.ok():
            context.shutdown()
        for number, handler in handlers.items():
            signal.signal(number, handler)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["completed"] else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record", help="subscribe and write bounded NPY blocks")
    record.add_argument("--output", type=Path, required=True, help="new or empty recording directory")
    record.add_argument("--topic", default="/cloud_registered")
    record.add_argument("--expected-frame-id", default="camera_init", help="reject other frames; empty disables the check")
    record.add_argument("--queue-capacity", type=int, default=32)
    record.add_argument("--chunk-points", type=int, default=1_000_000)
    record.add_argument("--max-frame-points", type=int, default=250_000)
    record.add_argument("--max-frame-bytes", type=int, default=16_777_216)
    record.add_argument("--chunk-frames", type=int, default=256, help="also bounds metadata for empty frames")
    record.add_argument("--overflow", choices=("fail", "drop-newest"), default="fail")
    record.add_argument("--nonfinite", choices=("drop", "error"), default="drop", help="policy for non-finite float32 XYZ")
    record.add_argument("--missing-intensity", choices=("zero", "error"), default="zero")
    record.add_argument("--flush-interval", type=float, default=2.0, help="maximum active-buffer age in seconds, excluding disk stalls")
    record.add_argument("--status-interval", type=float, default=1.0)
    record.add_argument("--qos-depth", type=int, default=10, help="bounded DDS KEEP_LAST history, separate from writer queue")
    record.add_argument("--qos-reliability", choices=("reliable", "best-effort"), default="reliable")
    record.add_argument("--no-fsync", action="store_true", help="disable durability flushes; reduces crash/power-loss guarantees")
    recover = commands.add_parser("recover", help="check hashes and reconcile an interrupted recording")
    recover.add_argument("directory", type=Path)
    args, ros_args = parser.parse_known_args(argv)
    if args.command == "recover" and ros_args:
        parser.error("unrecognized recovery arguments: " + " ".join(ros_args))
    try:
        if args.command == "recover":
            result = recover_recording(args.directory)
            print(json.dumps(result, sort_keys=True), flush=True)
            return 0 if result["completed"] else 2
        return _record(args, ros_args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"map_recorder: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
