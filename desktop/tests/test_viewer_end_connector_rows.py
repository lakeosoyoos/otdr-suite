"""The Viewer shows an end connector's two readings apart, each judged.

The boss, 2026-09-29: "We can't average connector losses A and B.  We have
to see them separately.  Flag them if over their threshold.  But then we also
give a bidi average."

The OTDR Suite table prints a launch connector on three rows (A->B, B->A,
Average).  It used to colour them from the report's ONE tag for that end,
which names the pair OR the worst side, never both: on a 432-fibre span
F118 read A 0.763 / B 0.716, both over the 0.649 one-direction gate, yet
only the Average went red and both direction rows passed.  Now
detect_launch_issues records each row's own verdict for the Viewer (the
same gates and the same trace confirm as the tag), and suite_viewer_table
flags each row from it:

  * a direction at "Connector loss (1 direction)" (LAUNCH_CONN_UNI_MIN_DB,
    off on a panel span, as in the report);
  * the Average at the pair gates, "Connector loss (bidirectional)"
    (min of the two, LAUNCH_CONN_LOSS_MIN_DB) and "Connector loss
    (bidirectional average)" (LAUNCH_CONN_AVG_MIN_DB, on at the Bidir
    connector loss value 0.500 since 2026-09-29).

The report itself does not move: its tags, cells and severity are the same
with or without the Viewer's table.  FastReporter mode already judged each
row on its own and is not touched.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
from conftest import REPO_ROOT

from test_launch_conn_loss import _FIXTURE, _run

VIEWER = (REPO_ROOT / "viewer" / "viewer.html").read_text(encoding="utf-8")

# That span's shapes: fibre -> (A reading, B reading) at the A end, and a
# helper that runs detect_launch_issues with the Viewer's readings on and
# builds the table's A-end cell from them.
_ROWS = """
    def a_end(pairs, **gates):
        for k, v in gates.items():
            setattr(E, k, v)
        fa = {f: _a(a) for f, (a, b) in pairs.items()}
        fb = {f: _b(b) for f, (a, b) in pairs.items()}
        readings = {}
        out = E.detect_launch_issues(fa, fb, spans_have_tailbox=False,
                                     readings=readings)
        t = E.suite_viewer_table(fa, fb, [], {}, launch_issues=out,
                                 readings=readings, span_km=78.2)
        cells = {int(f): c[0] for f, c in t['fibers'].items()}
        return cells, out, readings

    def rows(cell):
        return (bool(cell['a']['flag']), bool(cell['b']['flag']), bool(cell['flag']))
"""


def test_both_directions_over_the_gate_are_both_red_and_so_is_the_pair():
    """F118 and F121: the case the boss saw.  Both readings clear the
    one-direction gate and the pair clears the bidirectional one, so all
    three rows are red.  The report's own cell text is unchanged."""
    _run(_FIXTURE, _ROWS, """
        cells, out, _ = a_end({118: (0.763, 0.716), 121: (0.800, 0.690)})
        assert rows(cells[118]) == (True, True, True), rows(cells[118])
        assert rows(cells[121]) == (True, True, True), rows(cells[121])
        assert out[118]['a_tags'] == ['.740 LAUNCH'] and out[121]['a_tags'] == ['.745 LAUNCH']
        # each red row says why, with its own number
        assert cells[118]['a']['said'] == 'Connector loss (1 direction) 0.763 dB, limit 0.649 dB'
        assert cells[118]['b']['said'] == 'Connector loss (1 direction) 0.716 dB, limit 0.649 dB'
        assert cells[118]['said'] == ('Connector loss (bidirectional) 0.763 and 0.716 dB, '
                                      'both at or over 0.650 dB; Connector loss '
                                      '(bidirectional average) 0.740 dB, limit 0.500 dB'), cells[118]['said']
        print('OK')
    """)


def test_one_bad_direction_is_red_on_its_own_row_only():
    """F402 (A 0.766, healthy B 0.499) and F426 (A 0.645, B 0.719): the bad
    direction is red and the good one is not.  With the average gate off
    neither pair fails (neither clears the 0.65 minimum); with it on at its
    default 0.500 both averages (0.633, 0.682) fail as well."""
    _run(_FIXTURE, _ROWS, """
        cells, out, _ = a_end({402: (0.766, 0.499), 426: (0.645, 0.719)},
                              LAUNCH_CONN_AVG_MIN_DB=0.0)
        assert rows(cells[402]) == (True, False, False), rows(cells[402])
        assert rows(cells[426]) == (False, True, False), rows(cells[426])
        assert 'said' not in cells[402]['b'] and 'said' not in cells[402]
        cells, out, _ = a_end({402: (0.766, 0.499), 426: (0.645, 0.719)},
                              LAUNCH_CONN_AVG_MIN_DB=0.500)
        assert rows(cells[402]) == (True, False, True), rows(cells[402])
        assert rows(cells[426]) == (False, True, True), rows(cells[426])
        assert out[402]['a_tags'] == ['.766 LAUNCH A side']
        assert out[426]['a_tags'] == ['.719 LAUNCH B side']
        print('OK')
    """)


def test_the_average_is_red_when_it_clears_its_own_gate_too():
    """A customer profile with a 0.50 average gate (and 0.50 per direction).
    F402's average 0.6325 (prints .633) clears it, but the report's one tag
    names the A side, so the Average row used to pass.  Now A->B and the
    Average are red; B->A at 0.499 is not."""
    _run(_FIXTURE, _ROWS, """
        cells, out, _ = a_end({402: (0.766, 0.499)},
                              LAUNCH_CONN_UNI_MIN_DB=0.50, LAUNCH_CONN_AVG_MIN_DB=0.50)
        assert rows(cells[402]) == (True, False, True), rows(cells[402])
        assert cells[402]['said'] == ('Connector loss (bidirectional average) 0.633 dB, '
                                      'limit 0.500 dB'), cells[402]['said']
        assert out[402]['a_tags'] == ['.766 LAUNCH A side']      # the report's words
        print('OK')
    """)


def test_a_pair_under_every_gate_is_not_flagged():
    """F120 (0.642 / 0.562) passes each direction and, at the default 0.500
    average gate, fails only its Average (0.602).  A healthy pair fails
    nothing."""
    _run(_FIXTURE, _ROWS, """
        cells, out, readings = a_end({120: (0.642, 0.562), 1: (0.42, 0.30)})
        assert rows(cells[120]) == (False, False, True), rows(cells[120])
        assert out[120]['a_tags'] == ['.602 LAUNCH']
        assert rows(cells[1]) == (False, False, False) and 1 not in out
        assert 'verdict' not in readings[(1, 'endA')]
        # the numbers are still there, on their rows
        assert (cells[1]['a']['loss'], cells[1]['b']['loss']) == (0.42, 0.30)
        print('OK')
    """)


def test_a_reading_the_trace_cannot_reproduce_flags_no_row():
    """The same phantom-proofing as the report's tag: a stored loss the
    trace does not back up is not believed, on any row."""
    _run(_FIXTURE, _ROWS, """
        E.measure_grey_loss_from_sor_event = (
            lambda r, e, **k: 0.30 if e['dist_km'] < 2.0 else e['splice_loss'])
        cells, out, readings = a_end({118: (0.763, 0.716)})
        assert rows(cells[118]) == (False, False, False) and out == {}
        assert 'verdict' not in readings[(118, 'endA')]
        print('OK')
    """)


def test_on_a_panel_span_the_one_direction_gate_stands_down():
    """A tie between reels is graded on the pair (the report's rule): the
    direction rows stay clear, the pair still fails.  F402's pair fails only
    on its average (0.633 over the default 0.500)."""
    _run(_FIXTURE, _ROWS, """
        E._is_panel_span = lambda fibers: True
        cells, out, _ = a_end({118: (0.763, 0.716), 402: (0.766, 0.499), 1: (0.42, 0.30)})
        assert rows(cells[118]) == (False, False, True), rows(cells[118])
        assert rows(cells[402]) == (False, False, True), rows(cells[402])
        assert rows(cells[1]) == (False, False, False) and 1 not in out
        print('OK')
    """)


def test_the_report_is_the_same_with_or_without_the_viewer_rows():
    _run(_FIXTURE, _ROWS, """
        pairs = {118: (0.763, 0.716), 402: (0.766, 0.499), 426: (0.645, 0.719),
                 120: (0.642, 0.562)}
        fa = {f: _a(a) for f, (a, b) in pairs.items()}
        fb = {f: _b(b) for f, (a, b) in pairs.items()}
        plain = E.detect_launch_issues(fa, fb, spans_have_tailbox=False, readings=None)
        E.VIEWER_READINGS = None
        seen = E.detect_launch_issues(fa, fb, spans_have_tailbox=False, readings={})
        assert plain == seen, (plain, seen)
        print('OK')
    """)


def test_each_red_row_tells_its_own_reason_in_the_viewer():
    """The Viewer colours the rows from the table as before; the tooltip
    now leads with the row's own reason, then the report's words."""
    i = VIEWER.index('function paintSuiteBidiGrid(')
    body = VIEWER[i:VIEWER.index('\n}\n', i)]
    assert "const saidOf = (c, x, own) => [own && own.said," in body
    assert "const said = flagged ? saidOf(c, x, which === 'avg' ? x : leg) : '';" in body
    # the flag itself still comes from the table, row by row
    assert "return !!leg.flag" in body and "return !!x.flag ||" in body
