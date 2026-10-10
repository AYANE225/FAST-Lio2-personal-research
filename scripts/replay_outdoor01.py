#!/usr/bin/env python3
"""Replay a converted Outdoor01 bag with an isolated FAST-LIO build and recorder.

Source the ROS environment first. The build must belong to this experiment:
FAST-LIO writes its diagnostic Log files relative to its compiled ROOT_DIR.
Only processes launched by this script are stopped. No ROS daemon is needed.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time

import numpy as np
import psutil
import yaml

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def validate_scan_coverage(input_starts, processed_starts, initialization_frames):
    """Require every expected scan after a declared initialization prefix."""
    inputs = np.asarray(input_starts, dtype=float)
    processed = np.asarray(processed_starts, dtype=float)
    if (inputs.ndim != 1 or processed.ndim != 1 or not np.isfinite(inputs).all()
            or not np.isfinite(processed).all() or np.any(np.diff(inputs) <= 0)
            or not 0 <= initialization_frames < len(inputs)):
        raise ValueError('Invalid scan coverage inputs')
    expected = inputs[initialization_frames:]
    if (len(processed) != len(expected) or np.any(np.diff(processed) <= 0)
            or np.max(np.abs(processed - expected)) > 1e-5):
        raise ValueError('Scan coverage is incomplete or disagrees with input timestamps')
    return {'input_lidar_frames': len(inputs), 'processed_frames': len(processed),
            'leading_frames_without_odometry': initialization_frames,
            'missing_frames_after_initialization': 0,
            'maximum_input_stamp_match_difference_s': float(np.max(np.abs(processed - expected)))}


def record_cleanup_error(summary, action, error):
    message = f'{action}: {type(error).__name__}: {error}'
    errors = summary.setdefault('cleanup_errors', [])
    if message not in errors:
        errors.append(message)
    summary['completed'] = False
    summary.setdefault('error', message)


def stop_process(role, process, summary, *, sample=None, pump_events=None):
    """Reap an owned process group even if shutdown monitoring fails."""
    def wait_for_exit(timeout):
        deadline, next_sample = time.monotonic() + timeout, 0
        while process.poll() is None and time.monotonic() < deadline:
            if sample is not None and time.monotonic() >= next_sample:
                try:
                    sample()
                except (Exception, KeyboardInterrupt) as error:
                    record_cleanup_error(summary, f'{role} resource sample', error)
                next_sample = time.monotonic() + 1
            try:
                if pump_events is not None:
                    pump_events()
                else:
                    time.sleep(.05)
            except (Exception, KeyboardInterrupt) as error:
                record_cleanup_error(summary, f'{role} shutdown events', error)
                time.sleep(.05)
        return process.poll() is not None

    def send(signum):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            process.poll()  # The child may exit between poll() and killpg().

    if process.poll() is None:
        send(signal.SIGINT)
        if not wait_for_exit(25):
            summary[f'{role}_forced_stop'] = True
            send(signal.SIGTERM)
            if not wait_for_exit(5):
                send(signal.SIGKILL)
                process.wait(timeout=5)
    summary[f'{role}_exit_code'] = process.returncode


def stop_processes(processes, summary, *, sample=None, pump_events=None):
    """Attempt every owned process; preserve errors in the final summary."""
    for role, process in reversed(processes):
        try:
            stop_process(role, process, summary, sample=sample, pump_events=pump_events)
        except (Exception, KeyboardInterrupt) as error:
            record_cleanup_error(summary, f'stop {role}', error)


def run(args):
    import rclpy
    from nav_msgs.msg import Odometry
    from std_srvs.srv import Trigger

    workspace, output = args.workspace.resolve(), args.output.resolve()
    source = workspace / 'src/FAST_LIO'
    binary = workspace / 'install/fast_lio/lib/fast_lio/fastlio_mapping'
    if not binary.is_file() or not (source / 'Log').is_dir():
        raise ValueError('Use an isolated, built workspace with src/FAST_LIO/Log')
    # Avoid concurrent trials overwriting the compiled-in diagnostic directory.
    lock = (workspace / '.replay.lock').open('a+b')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    output.mkdir(parents=True, exist_ok=False)
    settings = yaml.safe_load(args.config.read_text())
    parameters = settings['/**']['ros__parameters']
    if parameters['pcd_save']['pcd_save_en'] or parameters['publish']['map_en']:
        raise ValueError('This runner requires independent recording and disabled accumulated map publishing')
    if not parameters['publish']['scan_publish_en'] or not parameters['publish']['dense_publish_en']:
        raise ValueError('Recorder comparison needs all prefiltered registered scan points')
    parameters['map_file_path'] = str(output / 'unused_service_map.pcd')
    (output / 'config.yaml').write_text(yaml.safe_dump(settings, sort_keys=False))
    bag_info = yaml.safe_load((args.bag / 'metadata.yaml').read_text())['rosbag2_bagfile_information']
    input_lidar = np.load(args.input_timestamps, allow_pickle=False)['lidar']
    input_starts = input_lidar[:, 1].astype(float) * 1e-9
    input_topics = {item['topic_metadata']['name']: item['message_count']
                    for item in bag_info['topics_with_message_count']}
    if input_topics.get(parameters['common']['lid_topic']) != len(input_starts):
        raise ValueError('Bag LiDAR count differs from the supplied input timestamp ledger')
    expected_poses = len(input_starts) - args.initialization_frames
    validate_scan_coverage(input_starts, input_starts[args.initialization_frames:], args.initialization_frames)
    duration = bag_info['duration']['nanoseconds'] * 1e-9
    roles = ('mapping', 'playback', 'observer', 'recorder')
    for i, role in enumerate(roles):
        port = args.port_base + i
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
        peers = ''.join(f'<Peer Address="127.0.0.1:{args.port_base + j}"/>' for j in range(4) if j != i)
        (output / f'cyclonedds_{role}.xml').write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<CycloneDDS xmlns="https://cdds.io/config"><Domain Id="any">'
            '<General><Transport>tcp</Transport><AllowMulticast>false</AllowMulticast></General>'
            f'<Discovery><Peers>{peers}</Peers></Discovery><TCP><Port>{port}</Port></TCP>'
            '</Domain></CycloneDDS>\n')
    os.environ['ROS_DOMAIN_ID'] = str(args.domain)
    os.environ['RMW_IMPLEMENTATION'] = 'rmw_cyclonedds_cpp'
    os.environ['ROS_LOCALHOST_ONLY'] = '1'
    os.environ['CYCLONEDDS_URI'] = (output / 'cyclonedds_observer.xml').as_uri()
    os.environ['ROS_LOG_DIR'] = str(output / 'ros_logs')
    summary = {'sequence': 'M3DGR Outdoor01', 'started_utc': datetime.now(timezone.utc).isoformat(),
               'playback_rate': 1.0, 'bag_duration_s': duration, 'domain': args.domain,
               'config_source': args.config.name, 'python': platform.python_version(),
               'dds_transport': 'Cyclone DDS TCP on localhost', 'source_sha256': {},
               'measurement_scope': 'Core algorithm timing excludes input preprocessing, output publication and recording. '
                                    'Process RSS/CPU sampled about once per second during playback and final drain.',
               'workspace': str(workspace), 'bag': str(args.bag.resolve())}
    summary['input_timestamps_sha256'] = hashlib.sha256(args.input_timestamps.read_bytes()).hexdigest()
    summary['expected_odometry_count'] = expected_poses
    summary['initialization_frames'] = args.initialization_frames
    for name in ('src/laserMapping.cpp', 'src/preprocess.cpp', 'src/preprocess.h',
                 'include/use-ikfom.hpp', 'CMakeLists.txt'):
        summary['source_sha256'][name] = hashlib.sha256((source / name).read_bytes()).hexdigest()
    processes, streams, poses, resources, stamps = [], [], [], [], []
    observer = None
    trajectory = resource_stream = None
    monitors = {}
    playback_start = None

    def start(role, command):
        stream = (output / f'{role}.log').open('x')
        streams.append(stream)
        env = os.environ.copy()
        env['CYCLONEDDS_URI'] = (output / f'cyclonedds_{role}.xml').as_uri()
        process = subprocess.Popen(command, cwd=output, env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        processes.append((role, process))
        summary[f'{role}_command'] = command
        summary[f'{role}_pid'] = process.pid
        return process

    def pump_events():
        if observer is not None and rclpy.ok():
            rclpy.spin_once(observer, timeout_sec=.05)
        else:
            time.sleep(.05)

    def stop(role, process):
        stop_process(role, process, summary, sample=lambda: sample(enforce_limits=False),
                     pump_events=pump_events)

    def sample(enforce_limits=True):
        if playback_start is None:
            return
        row = {'elapsed_s': time.monotonic() - playback_start, 'odometry_count': len(poses)}
        for role, monitor in monitors.items():
            try:
                row[role] = {'rss_bytes': monitor.memory_info().rss,
                             'cpu_percent': monitor.cpu_percent(), 'threads': monitor.num_threads()}
            except psutil.NoSuchProcess:
                continue
        status = output / 'recording/status.json'
        if status.exists():
            report = json.loads(status.read_text())
            row['recorder_status'] = {key: report[key] for key in
                ('received_frames', 'written_frames', 'written_points', 'queue_size',
                 'queue_high_water', 'dropped_frames', 'failed_frames', 'write_seconds', 'error')}
        resources.append(row)
        resource_stream.write(json.dumps(row) + '\n')
        resource_stream.flush()
        if enforce_limits and sum(row.get(role, {}).get('rss_bytes', 0) for role in monitors) > 10 * 1024**3:
            raise RuntimeError('Stopping this trial: combined process RSS exceeded 10 GiB')
        if enforce_limits and psutil.virtual_memory().available < 2 * 1024**3:
            raise RuntimeError('Stopping this trial: available system RAM below 2 GiB')

    def receive(message):
        stamp = message.header.stamp
        position, orientation = message.pose.pose.position, message.pose.pose.orientation
        values = [position.x, position.y, position.z, orientation.x, orientation.y, orientation.z, orientation.w]
        poses.append([stamp.sec + stamp.nanosec * 1e-9, *values])
        stamps.append(stamp.sec * 1_000_000_000 + stamp.nanosec)
        trajectory.write(f'{stamp.sec}.{stamp.nanosec:09d} ' + ' '.join(f'{v:.9f}' for v in values) + '\n')
        trajectory.flush()

    def spin_until(predicate, timeout, description):
        deadline = time.monotonic() + timeout
        while not predicate():
            for role, process in processes:
                if role != 'playback' and process.poll() is not None:
                    raise RuntimeError(f'{role} exited unexpectedly: {process.returncode}; see its log')
            if time.monotonic() > deadline:
                raise RuntimeError(f'Timed out waiting for {description}')
            rclpy.spin_once(observer, timeout_sec=.1)

    try:
        rclpy.init()
        observer = rclpy.create_node('outdoor01_engineering_monitor')
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            rclpy.spin_once(observer, timeout_sec=.1)
        other_nodes = [n for n in observer.get_node_names() if n != observer.get_name()]
        if other_nodes:
            raise RuntimeError(f'Chosen ROS domain is already occupied: {other_nodes}')
        trajectory = (output / 'trajectory_tum.txt').open('x')
        resource_stream = (output / 'resources.jsonl').open('x')
        observer.create_subscription(Odometry, '/Odometry', receive, 1000)
        client = observer.create_client(Trigger, '/map_save')
        mapping = start('mapping', [str(binary), '--ros-args', '--params-file', str(output / 'config.yaml')])
        monitors['mapping'] = psutil.Process(mapping.pid)
        if not client.wait_for_service(timeout_sec=20):
            raise RuntimeError('Mapping service did not become ready')
        recorder = start('recorder', [sys.executable, str(ROOT / 'scripts/map_recorder.py'), 'record',
            '--output', str(output / 'recording'), '--queue-capacity', '32', '--chunk-points', '250000',
            '--max-frame-points', '250000', '--max-frame-bytes', '16777216',
            '--overflow', 'fail', '--qos-depth', '10', '--nonfinite', 'error'])
        monitors['recorder'] = psutil.Process(recorder.pid)
        spin_until(lambda: bool(observer.get_subscriptions_info_by_topic('/cloud_registered')), 20, 'recorder subscription')
        spin_until(lambda: all(observer.get_subscriptions_info_by_topic(topic) for topic in
            (parameters['common']['lid_topic'], parameters['common']['imu_topic'])), 20, 'sensor subscriptions')
        # Allow the reliable publisher to match its new subscriber before starting the bag.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            rclpy.spin_once(observer, timeout_sec=.1)
        for monitor in monitors.values():
            monitor.cpu_percent()
        playback = start('playback', ['ros2', 'bag', 'play', str(args.bag.resolve()), '--rate', '1.0',
            '--delay', '3', '--read-ahead-queue-size', '2000', '--disable-keyboard-controls'])
        playback_start = time.monotonic()
        next_sample, next_progress = playback_start + 1, playback_start + 30
        print(f'READY domain={args.domain}: full Outdoor01 1x, independent recorder', flush=True)
        while playback.poll() is None:
            rclpy.spin_once(observer, timeout_sec=.05)
            now = time.monotonic()
            if mapping.poll() is not None or recorder.poll() is not None:
                raise RuntimeError('Mapping or recorder stopped before playback completed')
            if now - playback_start > duration + 90:
                raise RuntimeError('Playback exceeded sequence duration plus 90 seconds')
            if now >= next_sample:
                sample()
                next_sample = now + 1
            if now >= next_progress:
                last = resources[-1]
                print(f'PROGRESS t={now-playback_start:.1f}s poses={len(poses)} '
                      f'mapping={last["mapping"]["rss_bytes"]/1048576:.1f}MiB '
                      f'recorder={last["recorder"]["rss_bytes"]/1048576:.1f}MiB', flush=True)
                next_progress = now + 30
        if playback.returncode:
            raise RuntimeError(f'Playback failed: {playback.returncode}')
        deadline = time.monotonic() + 30
        while len(poses) < expected_poses and time.monotonic() < deadline:
            rclpy.spin_once(observer, timeout_sec=.05)
            if time.monotonic() >= next_sample:
                sample()
                next_sample = time.monotonic() + 1
        if len(poses) != expected_poses:
            raise RuntimeError(f'Incomplete input coverage: expected {expected_poses} odometry poses, got {len(poses)}')
        stop('mapping', mapping)
        # Drain DDS deliveries before stopping the recorder; its worker drains on SIGINT.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            rclpy.spin_once(observer, timeout_sec=.05)
            if time.monotonic() >= next_sample:
                sample()
                next_sample = time.monotonic() + 1
        stop('recorder', recorder)
        for role, process in (('mapping', mapping), ('recorder', recorder)):
            if process.returncode != 0 or summary.get(f'{role}_forced_stop'):
                raise RuntimeError(f'{role} did not stop cleanly')
        from map_writer import recover_recording
        recording = recover_recording(output / 'recording')
        summary['recording'] = recording
        if not recording['completed']:
            raise RuntimeError('Recorder did not persist a complete clean session')
        cloud_stamps, point_count = [], 0
        for block_id in range(recording['committed_blocks']):
            block = output / 'recording/blocks' / f'block-{block_id:06d}'
            for row in json.loads((block / 'frames.json').read_text())['frames']:
                cloud_stamps.append(row['stamp_sec'] * 1_000_000_000 + row['stamp_nanosec'])
                point_count += row['input_points']
        if cloud_stamps != stamps:
            raise RuntimeError('Registered cloud timestamp sequence differs from Odometry sequence')
        array = np.asarray(poses)
        if len(array) < 2 or not np.isfinite(array).all() or np.any(np.diff(array[:, 0]) <= 0):
            raise RuntimeError('Invalid or nonmonotonic estimated trajectory')
        summary['odometry_count'] = len(poses)
        summary['recorder_stamps_equal_odometry'] = True
        summary['recorded_input_points'] = point_count
        (output / 'algorithm_logs').mkdir()
        for path in (source / 'Log').iterdir():
            if path.is_file():
                shutil.copy2(path, output / 'algorithm_logs' / path.name)
        timing = np.loadtxt(output / 'algorithm_logs/fast_lio_time_log.csv', delimiter=',', skiprows=1, ndmin=2)
        summary['coverage'] = validate_scan_coverage(input_starts, timing[:, 0], args.initialization_frames)
        summary['completed'] = not summary.get('cleanup_errors')
    except (Exception, KeyboardInterrupt) as error:
        summary['completed'] = False
        summary['error'] = f'{type(error).__name__}: {error}'
    finally:
        stop_processes(processes, summary, sample=lambda: sample(enforce_limits=False),
                       pump_events=pump_events)
        cleanup_actions = []
        if observer:
            cleanup_actions.append(('destroy observer', observer.destroy_node))
        cleanup_actions.append(('shutdown ROS', lambda: rclpy.shutdown() if rclpy.ok() else None))
        for stream in [*streams, trajectory, resource_stream]:
            if stream:
                cleanup_actions.append(('close output stream', stream.close))
        cleanup_actions.append(('release workspace lock', lock.close))
        for action, cleanup in cleanup_actions:
            try:
                cleanup()
            except (Exception, KeyboardInterrupt) as error:
                record_cleanup_error(summary, action, error)
        summary['finished_utc'] = datetime.now(timezone.utc).isoformat()
        write_json(output / 'run_summary.json', summary)
    print(json.dumps({key: summary[key] for key in ('completed', 'odometry_count', 'error') if key in summary}), flush=True)
    return 0 if summary.get('completed') else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--bag', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/mid360_recording.yaml')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--domain', type=int, default=190)
    parser.add_argument('--port-base', type=int, default=56020)
    parser.add_argument('--input-timestamps', type=Path, default=ROOT / 'data/outdoor01/tcp/input_timestamps.npz')
    parser.add_argument('--initialization-frames', type=int, default=7,
                        help='Declared initialization prefix for this Outdoor01/configuration protocol')
    args = parser.parse_args()
    if not 0 <= args.domain <= 232 or not 1024 <= args.port_base <= 65532:
        parser.error('Invalid ROS domain or TCP port range')
    previous_handler = signal.getsignal(signal.SIGTERM)

    def interrupt(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt('received SIGTERM')

    signal.signal(signal.SIGTERM, interrupt)
    try:
        return run(args)
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == '__main__':
    raise SystemExit(main())
