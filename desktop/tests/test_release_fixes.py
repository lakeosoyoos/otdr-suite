"""Regression tests for the Tier-1 release fixes (sandbox).

Covers the boss-facing issues the 6-agent audit found in the f7f843c release:
  • multi-zip span load (per-direction zips) + macOS AppleDouble junk
  • Splice Report crash on a zero-closure span
  • (source-locked) Secret Sauce pair cap, viewer null-guard, error-report TLS

folder_intake is stdlib-only (no engine sor_reader), so it's safe to import in
process; the engine is exercised only in a clean child (sor_reader isolation).
"""
import os
import subprocess
import sys
import textwrap
import zipfile

from conftest import SPLICEREPORT_DIR

_REPO = str(SPLICEREPORT_DIR.parent)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
import folder_intake as fi  # noqa: E402  (stdlib-only, safe in-process)


def _touch(path, data=b'sordata'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


def test_find_otdr_files_skips_appledouble(tmp_path):
    d = str(tmp_path)
    _touch(os.path.join(d, 'SEANOR001_1550.sor'))
    _touch(os.path.join(d, '._SEANOR001_1550.sor'))                 # AppleDouble sidecar
    _touch(os.path.join(d, '__MACOSX', '._SEANOR002_1550.sor'))     # Mac-zip junk dir
    assert [os.path.basename(p) for p in fi.find_otdr_files(d)] == ['SEANOR001_1550.sor']


def test_find_otdr_files_with_zips_descends_into_per_direction_zips(tmp_path):
    """The boss's case: a span delivered as separate per-direction zips."""
    d = str(tmp_path)
    for name, members in (('HOWLAN 15SEC.zip', ['HOWLAN001_1550.sor', 'HOWLAN002_1550.sor']),
                          ('LANHOW 15SEC.zip', ['LANHOW001_1550.sor', 'LANHOW002_1550.sor'])):
        with zipfile.ZipFile(os.path.join(d, name), 'w') as zf:
            for m in members:
                zf.writestr(m, b'sordata')
    # Bare scan finds nothing — every trace is still inside a zip (the dead-end).
    assert fi.find_otdr_files(d) == []
    # Descending into the zips finds all four and they split into two directions.
    found = fi.find_otdr_files_with_zips(d, str(tmp_path / '_ex'))
    assert sorted(os.path.basename(p) for p in found) == [
        'HOWLAN001_1550.sor', 'HOWLAN002_1550.sor',
        'LANHOW001_1550.sor', 'LANHOW002_1550.sor']
    _da, _db, info = fi.materialize_two_directions(found, str(tmp_path / '_wd'))
    assert {info['a_prefix'], info['b_prefix']} == {'HOWLAN', 'LANHOW'}


def _run_engine(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, '-c', header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == 'OK', p.stdout


def test_scan_a_standalone_no_closures_does_not_crash():
    """Zero-closure span (3 fibers → discover_splices returns []): best_si stays
    None and splices[None] used to TypeError → "report failed / no manifest"."""
    _run_engine("""
        def _ev(k, end=False, ty='0F'):
            return {'dist_km': k, 'splice_loss': (0.0 if end else 0.3), 'type': ty,
                    'reflection': (-40.0 if end else -60.0), 'is_end': end,
                    'is_reflective': end, 'time_of_travel': 0.0}
        def _fiber(kms, eol=70.0):
            evs = [_ev(k) for k in kms] + [_ev(eol, end=True, ty='1E')]
            return {'_source': 'sor', '_trace_offset_km': 0.0,
                    'events': sorted(evs, key=lambda e: e['dist_km'])}
        fa = {f: _fiber([10.0, 30.0]) for f in range(1, 4)}   # too few to form a closure
        out = E.scan_a_standalone_events(fa, [], {}, 70.0, fibers_b=fa)   # splices=[] → zero closures
        assert isinstance(out, dict), type(out)
        assert all(si is not None for (_f, si) in out.keys()), 'poisoned (fnum, None) key'
        print('OK')
    """)


def test_tier1_source_locks():
    """Cheap locks for fixes not unit-tested above, so a revert is caught."""
    root = SPLICEREPORT_DIR.parent
    ss = (root / 'secretsauce' / 'run_secretsauce.py').read_text(encoding='utf-8')
    assert 'out_pairs[:MAX_EMIT_PAIRS]' in ss, 'Secret Sauce pair cap missing'
    viewer = (root / 'viewer' / 'viewer.html').read_text(encoding='utf-8')
    assert 'if (!gView) return null;' in viewer, 'viewer markerHit gView guard missing'
    assert 'id="btn-fit-x"' in viewer and 'id="btn-fit-y"' in viewer, 'viewer X/Y fit buttons missing'
    assert 'function fitX' in viewer and 'function fitY' in viewer, 'viewer X/Y fit functions missing'
    er = (root / 'error_report.py').read_text(encoding='utf-8')
    assert 'certifi.where()' in er, 'error_report TLS context missing'
    app = (root / 'app.py').read_text(encoding='utf-8')
    assert 'find_otdr_files_with_zips' in app, 'app.py not wired to zip-descend loader'
    assert 'accept_multiple_files=True' in app, 'span uploader not multi-zip'


# ── #1: multi-direction spans (Miller↔Topeka) — report dropped, stay consistent ──
def test_materialize_reports_dropped_extra_directions(tmp_path):
    files = []
    for pref, n in (('MILTOP', 6), ('TOPMIL', 6), ('MILTOPSH', 4), ('TOPMILSH', 4)):
        for i in range(1, n + 1):
            p = os.path.join(str(tmp_path), f'{pref}{i:03d}_1550.sor')
            _touch(p)
            files.append(p)
    _da, _db, info = fi.materialize_two_directions(sorted(files), str(tmp_path / '_wd'))
    assert {info['a_prefix'], info['b_prefix']} == {'MILTOP', 'TOPMIL'}   # 2 largest kept
    assert sorted(info['dropped']) == ['MILTOPSH', 'TOPMILSH']            # rest reported

def test_load_span_feeds_secret_sauce_only_the_two_chosen_directions():
    app = (SPLICEREPORT_DIR.parent / 'app.py').read_text(encoding='utf-8')
    assert "chosen = [p for p in files" in app, 'SS combined not restricted to chosen dirs'
    assert "_span.get('dropped')" in app, 'no loud warning for dropped direction groups'


# ── #2: unreadable folder must not crash the Viewer ──
def test_list_fibers_unreadable_folder_returns_empty(tmp_path):
    d = tmp_path / 'locked'
    d.mkdir()
    (d / 'SEANOR001_1550.sor').write_bytes(b'x')
    os.chmod(str(d), 0o000)
    try:
        try:
            os.listdir(str(d))
            import pytest
            pytest.skip('environment can still read a 000 dir (root?) — guard untestable here')
        except OSError:
            pass
        viewer_dir = str(SPLICEREPORT_DIR.parent / 'viewer')
        body = ("import sys\n"
                f"sys.path.insert(0, {viewer_dir!r})\n"
                "import trace_server as T\n"
                f"print('OK' if T.list_fibers({str(d)!r}) == [] else 'BAD')\n")
        p = subprocess.run([sys.executable, '-c', body], capture_output=True, text=True)
        assert p.returncode == 0 and p.stdout.strip().splitlines()[-1] == 'OK', \
            f"{p.stdout}\n{p.stderr}"
    finally:
        os.chmod(str(d), 0o755)   # restore so tmp cleanup can remove it


# ── #3: extra wavelength bands stripped from fiber numbers ──
def test_extract_fiber_num_strips_extra_wavelength_bands():
    _run_engine("""
        assert E._extract_fiber_num('LAGDUR0001_1577.sor') == 1,  E._extract_fiber_num('LAGDUR0001_1577.sor')
        assert E._extract_fiber_num('LAGDUR0042_1650.sor') == 42, E._extract_fiber_num('LAGDUR0042_1650.sor')
        assert E._extract_fiber_num('LAGDUR0007_1383.sor') == 7,  E._extract_fiber_num('LAGDUR0007_1383.sor')
        assert E._extract_fiber_num('X0007_131015501625.sor') == 7, E._extract_fiber_num('X0007_131015501625.sor')
        print('OK')
    """)


# ── tie-panel: 1-digit ILA suffix jammed onto the zero-padded port ──
def test_extract_fiber_num_tiepanel_jammed_port():
    """Zach's tie-panel folders (ILA 1→5/6, panel A|B ports 145-288) name files
    ``PTL1PTL60145`` / ``DNW1DNW50148`` — a 1-digit ILA suffix butted straight
    against the 4-digit zero-padded port with no delimiter.  The rightmost digit
    run read as 60145/50148 → every fiber landed past the stray-fiber ceiling →
    the whole folder was dropped and the run aborted.  Trust the zero-padded port
    (real on-disk files verified 2026-07-14), but never a genuine 4-digit fiber."""
    _run_engine("""
        assert E._extract_fiber_num('PTL1PTL60145.sor') == 145, E._extract_fiber_num('PTL1PTL60145.sor')
        assert E._extract_fiber_num('PTL1PTL60001.sor') == 1,   E._extract_fiber_num('PTL1PTL60001.sor')
        assert E._extract_fiber_num('DNW1DNW50148.sor') == 148, E._extract_fiber_num('DNW1DNW50148.sor')
        assert E._extract_fiber_num('DNW6DNW10121.sor') == 121, E._extract_fiber_num('DNW6DNW10121.sor')  # real on-disk, run=610121
        assert E._extract_fiber_num('ELMMIL0064_1550.sor') == 64, E._extract_fiber_num('ELMMIL0064_1550.sor')  # letter-delimited control
        assert E._extract_fiber_num('MILELM1152_1550.sor') == 1152, E._extract_fiber_num('MILELM1152_1550.sor')  # real 4-digit fiber, no leading zero → untouched
        print('OK')
    """)

def test_tier1b_source_locks():
    root = SPLICEREPORT_DIR.parent
    ts = (root / 'viewer' / 'trace_server.py').read_text(encoding='utf-8')
    assert 'except OSError' in ts, 'list_fibers not guarded against unreadable folders'
    rs = (root / 'splicereport' / 'run_splicereport.py').read_text(encoding='utf-8')
    assert 'stray-numbered' in rs, 'stray-fiber-number cap missing in the runner'
    app = (root / 'app.py').read_text(encoding='utf-8')
    assert 'not readable' in app, 'viewer unreadable-folder warning missing'


# ── viewer-doesn't-work fixes: no hardcoded Mac default folder + resilient boot ──
def test_trace_server_has_no_hardcoded_dev_folder():
    """The release shipped with a hardcoded Mac Downloads path as the default A/B
    folder — meaningless on a tech's Windows box and it auto-loaded an unreadable
    path.  Lock it to None so the Viewer opens on the explicit pick prompt."""
    ts = (SPLICEREPORT_DIR.parent / 'viewer' / 'trace_server.py').read_text(encoding='utf-8')
    assert '/Users/robertcolbert' not in ts, 'a hardcoded dev path leaked back into trace_server'
    assert "CONFIG = {'dir_a': None, 'dir_b': None}" in ts, 'CONFIG default is not empty'


def test_viewer_boot_is_cold_start_resilient():
    """A single failed /api/list at cold launch used to skip resizeCanvas() and
    leave the Viewer permanently blank.  Lock the resilient boot: canvas sized
    first, loadInfo retried, always drawn."""
    viewer = (SPLICEREPORT_DIR.parent / 'viewer' / 'viewer.html').read_text(encoding='utf-8')
    assert 'async function boot' in viewer, 'resilient boot() wrapper missing'
    assert 'viewer server not ready' in viewer, 'loadInfo cold-start guard missing'
    # The old fragile one-liner (resize only inside the .then) must be gone.
    assert 'loadInfo().then(() => { resizeCanvas(); return bootLoad(); });' not in viewer, \
        'fragile boot chain still present'
