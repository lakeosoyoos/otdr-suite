"""The Viewer's loss gate follows the TABLE on screen, not the folders.

Click-through audit 2026-09-29 (main 70bd1a8): with BOTH the A and B folders
set but only A traces added (Dir "A ->"), the single-direction FastReporter
grid is what renders, yet ``oneDirOnly()`` read ``gInfo.fibers_a`` /
``fibers_b`` -- the folders -- saw both, and graded every A->B loss at the
bidirectional ``reburn`` gate (0.160, label "(following Splice Report 0.160
dB)").  On a 1152-fibre job, F2 A->B at 63.97 km = -0.199 showed as a
fail.  With only the A folder set, the same trace was graded at
``single_dir`` 0.200, "one direction".  Robert's intent (commit for #371): single-direction readings use
the Unidir. splice loss row.  Since 2026-09-30 a one-direction load is graded
as the Uni report grades it, at ``uni_bend`` (Robert: "use 0.250 for
one-direction loads"); which loads count as one direction is unchanged.

THE RULE.  A fibre is paired exactly as the bidir grids pair it
(renderSuiteBidiGrid / renderFrBidiGrid): a visible trace in each direction.
No pair on screen -> the single-direction grid renders -> ``uni_bend``.
Nothing on screen at all -> the folders stand in (the gate the first load
will be judged at).  The uni report keeps its own ``uni_bend`` rule.

There is no JS engine in CI, so the gate is a Python mirror below; the one
decision this file is about -- what ``oneDirOnly`` reads -- is parsed OUT OF
viewer.html, so reverting to the folder rule flips the model and the
scenarios fail (checked against main b4b30bb: 5 of 9 fail).
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

T = {'reburn': 0.160, 'uni_bend': 0.250, 'single_dir': 0.200}
ONE_DIR = T['uni_bend']        # a one-direction load: the Uni report's gate


def _viewer_src():
    with open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8') as fh:
        return fh.read()


def _fn(src, name):
    i = src.index('function ' + name + '(')
    return src[i:src.index('\n}\n', i)]


def one_dir_rule(src=None):
    """'on-screen' when oneDirOnly pairs the visible traces first, 'folders'
    when it only reads gInfo.fibers_a / fibers_b."""
    body = _fn(src if src is not None else _viewer_src(), 'oneDirOnly')
    pairs_visible = ('gTraces.filter(t => t.visible)' in body
                     and "d.has('a') && d.has('b')" in body)
    reads_folders = 'gInfo.fibers_a' in body and 'gInfo.fibers_b' in body
    assert pairs_visible or reads_folders, 'oneDirOnly changed shape: update this test'
    return 'on-screen' if pairs_visible else 'folders'


def gate(traces, folders, src_report='sr', rule=None):
    """Mirror of reportGateDb(): traces = [(fiber, dir, visible)],
    folders = (nA, nB)."""
    rule = rule or one_dir_rule()
    if src_report == 'uni':
        return T['uni_bend']
    nA, nB = folders
    one = (nA > 0) != (nB > 0)
    if rule == 'on-screen':
        vis = [(f, d) for f, d, v in traces if v]
        if vis:
            by = {}
            for f, d in vis:
                by.setdefault(f, set()).add(d)
            one = not any({'a', 'b'} <= s for s in by.values())
    return ONE_DIR if one else T['reburn']


def clears(loss, g):
    """clearsAt: the printed |loss| at or over the gate fails."""
    return round(abs(loss) * 1000) / 1000 >= g - 1e-9


# ─── the audit case ──────────────────────────────────────────────────────

def test_only_a_added_with_both_folders_set_is_graded_one_direction():
    g = gate([(2, 'a', True)], folders=(1152, 1152))
    assert g == ONE_DIR
    assert not clears(-0.199, g)          # F2 A->B @ 63.97 km, the audit case


def test_it_matches_the_a_folder_alone():
    assert (gate([(2, 'a', True)], folders=(1152, 1152))
            == gate([(2, 'a', True)], folders=(1152, 0)))


def test_a_on_one_fibre_and_b_on_another_is_still_unpaired():
    assert gate([(2, 'a', True), (3, 'b', True)], folders=(1152, 1152)) == ONE_DIR


def test_a_hidden_b_leaves_the_fibre_unpaired():
    assert gate([(2, 'a', True), (2, 'b', False)], folders=(1152, 1152)) == ONE_DIR


# ─── what must not move ──────────────────────────────────────────────────

def test_a_paired_fibre_keeps_the_bidirectional_gate():
    assert gate([(2, 'a', True), (2, 'b', True)], folders=(1152, 1152)) == T['reburn']
    # one pair is enough: the bidir grid renders (singles are named in the hint)
    assert gate([(2, 'a', True), (2, 'b', True), (5, 'a', True)],
                folders=(1152, 1152)) == T['reburn']


def test_nothing_on_screen_falls_back_to_the_folders():
    assert gate([], folders=(1152, 1152)) == T['reburn']
    assert gate([], folders=(1152, 0)) == ONE_DIR
    assert gate([(2, 'a', False)], folders=(1152, 1152)) == T['reburn']


def test_the_uni_report_keeps_its_own_rule():
    assert gate([(2, 'a', True)], folders=(1152, 1152), src_report='uni') == T['uni_bend']


# ─── the source wiring the mirror assumes ────────────────────────────────

def test_the_source_wiring():
    vw = _viewer_src()
    assert one_dir_rule(vw) == 'on-screen'
    lines = [l.strip() for l in _fn(vw, 'reportGateDb').splitlines()]
    assert lines[1] == "if (gSourceReport === 'uni' || oneDirOnly()) return gThresholds.uni_bend;"
    assert lines[2] == 'return gThresholds.reburn;'
    # label, warning band and the Summary Report line read the same predicate
    assert "(gSourceReport !== 'uni' && oneDirOnly()) ? ', one direction'" in _fn(vw, 'gateLabel')
    assert '(leg || oneDirOnly()) ? T.single_dir_warn : T.reburn_warn' in _fn(vw, 'warnFor')
    assert "meta.push(['Loss gate', gateLabel()]);" in vw
    # the gate can move with every trace added or hidden, so the box and its
    # label repaint with the table, before anything is graded
    body = _fn(vw, 'renderEventTable')
    assert body.index('syncGateUI();') < body.index('renderFastReporterGrid(')


def test_the_folder_rule_would_fail_the_audit_case():
    """The pre-fix rule, run through the same mirror, reproduces the defect."""
    g = gate([(2, 'a', True)], folders=(1152, 1152), rule='folders')
    assert g == T['reburn'] and clears(-0.199, g)
