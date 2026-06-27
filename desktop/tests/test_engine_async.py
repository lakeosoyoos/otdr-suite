"""Runtime regression for the async live-progress engine driver.

The hub runs engines as concurrent subprocesses (run_engine_live →
_engine_start / _engine_poll / _engine_tail) so a heavy report can't freeze the
page or drop the Streamlit server connection, and the tech sees live progress.

app.py is importable in bare mode (Streamlit logs a harmless ScriptRunContext
warning), so we drive the st-free job helpers directly with tiny subprocesses.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
import app  # noqa: E402  (bare-mode import; emits a harmless context warning)


def _drain(job, timeout_s=30, max_iters=4000):
    state = 'running'
    for _ in range(max_iters):
        state = app._engine_poll(job, timeout_s)
        if state != 'running':
            return state
        time.sleep(0.01)
    return state


def test_async_engine_done_streams_output():
    """A finished job yields a CompletedProcess: manifest from the real stdout,
    chatter from the stderr log; temp logs are cleaned up."""
    job = app._engine_start([
        sys.executable, '-c',
        "import sys; sys.stderr.write('step one\\n'); print('MANIFEST_LINE')"])
    assert _drain(job) == 'done'
    proc = job['result']
    assert isinstance(proc, subprocess.CompletedProcess)
    assert proc.returncode == 0
    assert 'MANIFEST_LINE' in proc.stdout
    assert 'step one' in proc.stderr
    out_path = job['out_path']
    app._engine_cleanup(job)
    assert not os.path.exists(out_path)


def test_async_engine_tail_reads_live_progress():
    """_engine_tail surfaces the engine's stderr progress WHILE it runs (the
    child is unbuffered), which is what the live status line shows."""
    job = app._engine_start([
        sys.executable, '-c',
        "import sys, time; sys.stderr.write('working fiber 7\\n'); "
        "sys.stderr.flush(); time.sleep(0.6)"])
    seen = False
    for _ in range(300):
        app._engine_poll(job, 30)
        tail = app._engine_tail(job, 1)
        if tail and 'fiber 7' in tail[0]:
            seen = True
            break
        time.sleep(0.01)
    _drain(job)
    app._engine_cleanup(job)
    assert seen, "engine_tail must surface live stderr progress while running"


def test_async_engine_timeout_kills_process():
    """The timeout ceiling kills a wedged engine and reports 'timeout'."""
    job = app._engine_start([sys.executable, '-c', 'import time; time.sleep(60)'])
    state = 'running'
    for _ in range(600):
        state = app._engine_poll(job, timeout_s=0.3)
        if state != 'running':
            break
        time.sleep(0.02)
    assert state == 'timeout'
    assert job['proc'].poll() is not None
    app._engine_cleanup(job)


def test_async_engine_cancel_kills_process():
    """Cancel kills the subprocess and marks the job cancelled."""
    job = app._engine_start([sys.executable, '-c', 'import time; time.sleep(60)'])
    app._engine_poll(job, 30)
    app._engine_cancel(job)
    assert job['state'] == 'cancelled'
    assert job['proc'].poll() is not None
    app._engine_cleanup(job)
