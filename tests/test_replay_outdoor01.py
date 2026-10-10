"""A complete pair of downstream streams must also cover the input sequence."""
from contextlib import contextmanager
import os
import signal
import subprocess
import sys

import numpy as np
import pytest

from scripts import replay_outdoor01 as replay
from scripts.replay_outdoor01 import validate_scan_coverage


def test_scan_coverage_matches_all_inputs_after_declared_initialization():
    expected = np.arange(12, dtype=float) * .1 + 1000
    result = validate_scan_coverage(expected, expected[2:] + 1e-7, 2)
    assert result['processed_frames'] == 10
    assert result['input_lidar_frames'] == 12
    assert result['missing_frames_after_initialization'] == 0


@pytest.mark.parametrize('case', ['tail', 'middle', 'duplicate', 'other_time'])
def test_scan_coverage_rejects_truncation_loss_and_wrong_input(case):
    expected = np.arange(12, dtype=float) * .1 + 1000
    actual = expected[2:].copy()
    if case == 'tail':
        actual = actual[:-1]
    elif case == 'middle':
        actual = np.delete(actual, 4)
    elif case == 'duplicate':
        actual[4] = actual[3]
    else:
        actual += 1
    with pytest.raises(ValueError, match='coverage'):
        validate_scan_coverage(expected, actual, 2)


@contextmanager
def interruptible_process():
    """A real isolated process that takes long enough to sample during exit."""
    process = subprocess.Popen([sys.executable, '-u', '-c', '''
import signal
import time
def stop(signum, frame):
    time.sleep(.08)
    raise SystemExit(0)
signal.signal(signal.SIGINT, stop)
print('ready', flush=True)
while True:
    time.sleep(1)
'''], start_new_session=True, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'ready'
        yield process
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        process.stdout.close()


def test_resource_sampling_error_cannot_interrupt_child_shutdown():
    assert callable(getattr(replay, 'stop_process', None)), 'Shutdown must be independently callable'
    summary = {'completed': False, 'error': 'available system RAM below 2 GiB'}

    def failed_sample():
        raise RuntimeError('available system RAM below 2 GiB')

    with interruptible_process() as process:
        replay.stop_process('mapping', process, summary, sample=failed_sample)
        assert process.poll() == 0
        assert summary['mapping_exit_code'] == 0
        assert any('RAM' in message for message in summary['cleanup_errors'])
        assert not summary['completed']


def test_failed_stop_does_not_skip_other_owned_processes(monkeypatch):
    assert callable(getattr(replay, 'stop_processes', None)), 'All owned processes must be attempted'
    summary = {'completed': True}
    killpg = os.killpg
    with interruptible_process() as mapping, interruptible_process() as recorder:
        def denied_signal(pid, signum):
            if pid == recorder.pid:
                raise PermissionError('injected signal failure')
            killpg(pid, signum)

        with monkeypatch.context() as patch:
            patch.setattr(replay.os, 'killpg', denied_signal)
            replay.stop_processes([('mapping', mapping), ('recorder', recorder)], summary)
        assert mapping.poll() == 0
        assert summary['mapping_exit_code'] == 0
        assert not summary['completed']
        assert any('recorder' in message and 'PermissionError' in message
                   for message in summary['cleanup_errors'])
