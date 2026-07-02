"""Regression: the viewer trace cache must key on the file's mtime.

_load_trace_cached keyed only on (directory, filename), so when a tech re-shot a
fiber and overwrote the same filename the viewer served the STALE trace for the
process lifetime; worse, a None cached while the file was mid-copy made that
fiber 404 forever.  mtime is now part of the cache key — a changed file re-parses.
"""
import subprocess
import sys
import textwrap

from conftest import VIEWER_DIR


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(VIEWER_DIR)!r})\n"
              "import trace_server as T\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_cache_reparses_when_mtime_changes():
    _run("""
        import numpy as np
        calls = {'n': 0}
        def fake_parse(path, trim=False):
            calls['n'] += 1
            return {'trace': np.zeros(300), 'events': [], 'exfo_sampling_period': 5e-08}
        # Patch the module globals _load_trace_cached looks up at call time.
        T.parse_sor_full = fake_parse
        T._sor_ior_from_events = lambda r: 1.468
        T._sor_first_pos_m = lambda r, res: 0.0
        T._load_trace_cached.cache_clear()

        T._load_trace_cached('/d', 'F0001_1550.sor', 111)   # parse #1
        T._load_trace_cached('/d', 'F0001_1550.sor', 111)   # same mtime -> cached
        T._load_trace_cached('/d', 'F0001_1550.sor', 222)   # new mtime  -> parse #2
        assert calls['n'] == 2, calls   # a stale (dir,fn)-only key would give 1
        print('OK')
    """)
