"""Opt-in adapter integration check; requires the sourced ROS Python environment."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(os.environ.get('RUN_ROS_TESTS') != '1', reason='set RUN_ROS_TESTS=1 after sourcing ROS')
def test_private_context_recorder_spins_and_stops_cleanly(tmp_path):
    # Without an executor bound to the initialized Context, the process exits
    # before SIGINT and leaves an incomplete manifest.
    output = tmp_path / 'recording'
    environment = os.environ.copy()
    environment['ROS_DOMAIN_ID'] = '197'
    environment['ROS_LOCALHOST_ONLY'] = '1'
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/map_recorder.py'),
               'record', '--output', str(output), '--chunk-points', '20', '--max-frame-points', '20']
    process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        while not (output / 'status.json').exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        # Wait for at least several executor cycles after creating the subscription.
        time.sleep(.35)
        assert process.poll() is None, 'recorder exited instead of spinning its private ROS context'
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == 0, stderr.decode() + stdout.decode()
        report = json.loads((output / 'manifest.json').read_text())
        assert report['completed'] is True and report['written_frames'] == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
