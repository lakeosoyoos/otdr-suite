"""/api/fr_table: the trace server hands FR's bidirectional table to the Viewer.

The server never imports the Splice Report engine (three engines, three
sor_reader copies, never one process): it runs the runner's --fr-table in a
subprocess and caches the rows per file pair and mtime.  Runs in a clean
subprocess itself so the Viewer's reader is the only one loaded here.
"""
import os
import shutil
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT, VIEWER_DIR

FIX = REPO_ROOT / "desktop" / "tests" / "fixtures" / "zayo_sor"
RUNNER = REPO_ROOT / "splicereport" / "run_splicereport.py"


def _run(body, tmp_path):
    da, db = tmp_path / "A", tmp_path / "B"
    da.mkdir(); db.mkdir()
    shutil.copy(FIX / "ORPVL.ZYO-OR-DES-0048.1550.0017.sor", da)
    shutil.copy(FIX / "ZYO-OR-DES-0048.ORPVL.1550.0017.sor", db)
    header = ("import sys, os, json, subprocess\n"
              f"sys.path.insert(0, {str(VIEWER_DIR)!r})\n"
              "import trace_server as T\n"
              f"T.CONFIG['dir_a'] = {str(da)!r}\n"
              f"T.CONFIG['dir_b'] = {str(db)!r}\n"
              f"T.CONFIG['engine_argv'] = [sys.executable, {str(RUNNER)!r}]\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_fr_tables_come_from_the_engine_subprocess_and_are_cached(tmp_path):
    _run("""
        res = T.fr_tables([17, 5])
        assert res['error'] is None, res['error']
        assert res['missing'] == [5], res['missing']          # no such fibre in the folders
        rows = res['tables']['17']
        assert len(rows) == 11 and rows[0]['status'] == 64 and rows[-1]['status'] == 128
        assert rows[1]['section'] and rows[1]['section']['a']['loss'] is not None
        # served from the cache now: the engine must not be needed again
        real = subprocess.run
        def boom(*a, **k): raise AssertionError('engine run twice for an unchanged pair')
        subprocess.run = boom
        try:
            again = T.fr_tables([17])
        finally:
            subprocess.run = real
        assert again['tables']['17'] == rows and again['error'] is None
        # a changed file (new mtime) is recomputed
        pa = T._fiber_path(T.CONFIG['dir_a'], 17)
        os.utime(pa, (os.path.getmtime(pa) + 5, os.path.getmtime(pa) + 5))
        assert T.fr_tables([17])['tables']['17'] == rows
        # the JSON the browser gets is finite
        s = json.dumps(T._finite(res))
        assert 'NaN' not in s and 'Infinity' not in s
        print('OK')
    """, tmp_path)


def test_a_broken_engine_is_an_error_not_a_crash(tmp_path):
    _run("""
        T.CONFIG['engine_argv'] = [sys.executable, '-c', 'import sys; sys.exit(3)']
        res = T.fr_tables([17])
        assert res['tables'] == {} and res['missing'] == [17] and res['error']
        T.CONFIG['engine_argv'] = [sys.executable, '-c', 'print("not json")']
        res = T.fr_tables([17])
        assert res['tables'] == {} and res['error']
        print('OK')
    """, tmp_path)


def test_the_route_and_the_hub_hand_over_are_wired():
    src = open(VIEWER_DIR / "trace_server.py", encoding="utf-8").read()
    assert "u.path == '/api/fr_table'" in src
    assert "'engine_argv': None" in src
    hub = open(REPO_ROOT / "app.py", encoding="utf-8").read()
    assert "trace_server.CONFIG['engine_argv']" in hub and "--run-splicereport" in hub


def test_the_pairs_never_go_on_the_command_line(tmp_path):
    """Windows caps a command line at 32,767 characters.  A pair takes 130 to
    220 of them once quoted, so a tray or a whole cable passed the cap
    somewhere between 150 and 250 fibres, the engine never started and FR
    mode showed 'engine failed'.  The pairs now go in a file the engine
    reads, so the command line is the same length whatever the count, and
    the file is removed afterwards."""
    _run("""
        seen = []
        real = subprocess.run
        def spy(cmd, *a, **k):
            spec = cmd[cmd.index('--fr-table-file') + 1]
            with open(spec, encoding='utf-8') as fh:
                seen.append((list(cmd), json.load(fh), spec))
            return real(cmd, *a, **k)
        subprocess.run = spy
        try:
            res = T.fr_tables([17])
        finally:
            subprocess.run = real
        assert res['error'] is None and len(res['tables']['17']) == 11
        cmd, pairs, spec = seen[0]
        pa = T._fiber_path(T.CONFIG['dir_a'], 17)
        pb = T._fiber_path(T.CONFIG['dir_b'], 17)
        assert pairs == [[17, pa, pb]]
        assert not any(T.CONFIG['dir_a'] in c or T.CONFIG['dir_b'] in c for c in cmd)
        assert not os.path.exists(spec)
        print('OK')
    """, tmp_path)


def test_a_whole_cable_fits_on_a_windows_command_line(tmp_path):
    """864 fibre pairs in long folder paths: the old command line was over
    100,000 characters, three times the Windows limit."""
    _run("""
        root = '/Users/a.technician/OneDrive - Company/Jobs/2026/Span 12 North/'
        T._fiber_path = lambda d, f: root + ('A' if d == T.CONFIG['dir_a'] else 'B') + '/F%04d_1550.sor' % f
        os.path.getmtime = lambda p: 1.0
        cmds = []
        def fake(cmd, *a, **k):
            cmds.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, json.dumps({'ok': True, 'tables': {}}), '')
        subprocess.run = fake
        T.fr_tables(list(range(1, 865)))
        line = subprocess.list2cmdline(cmds[0])
        assert len(line) < 1000, len(line)
        print('OK')
    """, tmp_path)
