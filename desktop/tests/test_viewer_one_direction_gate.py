"""Only one direction loaded: the Viewer grades loss at the Unidir. splice
loss row, not the bidirectional gate.

Robert 2026-09-29: with the Unidir. splice loss row switched off and only one
side of the traces loaded, the Viewer still flagged every loss at the bidir
0.160.  Nothing can be paired then, so every reading is single-direction:
the gate is SINGLE_DIR_THRESHOLD (1e9 when the row is off, which the label
prints as "loss grading off").  A+B loads and the uni report keep their gates.
"""
import os

from conftest import REPO_ROOT, import_trace_server

ROOT = str(REPO_ROOT)


def _viewer_src():
    with open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8') as fh:
        return fh.read()


def _fn(src, name):
    i = src.index('function ' + name + '(')
    return src[i:src.index('\n}\n', i)]


def test_one_direction_is_judged_at_the_uni_report_gate():
    """Robert 2026-09-30: "use 0.250 for one-direction loads" -- a load with
    one direction only is graded as the Uni report grades it, the same gate
    its one-direction table carries, not the Splice Report's 0.200 row."""
    vw = _viewer_src()
    one = _fn(vw, 'oneDirOnly')
    assert 'gInfo.fibers_a' in one and 'gInfo.fibers_b' in one
    assert 'return (nA > 0) !== (nB > 0);' in one
    gate = _fn(vw, 'reportGateDb').splitlines()
    assert gate[1].strip() == "if (gSourceReport === 'uni' || oneDirOnly()) return gThresholds.uni_bend;"
    assert gate[2].strip() == 'return gThresholds.reburn;'
    # the strip names the gate it follows
    assert "(gSourceReport === 'uni' || oneDirOnly()) ? 'Uni report'" in _fn(vw, 'gateLabel')
    # the default matches the engine's UNI_BEND_THRESHOLD
    assert 'uni_bend: 0.250' in vw
    assert 'return reportGateDb();' in _fn(vw, 'activeGateDb')


def test_the_warning_band_and_the_override_follow_the_same_gate():
    vw = _viewer_src()
    assert '((leg || oneDirOnly()) ? T.single_dir_warn : T.reburn_warn)' in _fn(vw, 'warnFor')
    # typing the report's own value back into the loss box clears the override
    assert 'gGateOverride = (Math.abs(v - reportGateDb()) < 1e-9) ? null : v;' in vw
    assert "', one direction'" in _fn(vw, 'gateLabel')


def test_the_gate_is_snapshotted_before_gInfo_moves():
    """The gate now depends on gInfo too: a second direction arriving must
    count as the gate moving, so the table repaints."""
    vw = _viewer_src()
    body = vw[vw.index('async function loadInfo'):]
    assert body.index('const wasGate = activeGateDb();') < body.index('gInfo = await r.json();')


def test_an_unticked_uni_row_reaches_the_viewer_as_off():
    TS = import_trace_server()
    assert TS._gates({'SINGLE_DIR_THRESHOLD': 1e9})['single_dir'] >= 1e6
