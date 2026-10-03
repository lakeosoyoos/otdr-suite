"""The bidirectional loss box flags the Average only (Robert 2026-10-02).

"When we have our bidi loss threshold set at the top of the event panel, it
needs to only flag the bidi average in the event panels below.  The threshold
set there can't flag one direction of a fiber even if the loss is greater."

So in both two-direction tables (OTDR Suite and the FR layout) a splice's
A->B / B->A loss cell is never red or yellow on its own reading.  What stays:
  - the Average row, at the box's gate;
  - a connector's direction, at its one-direction gate (the boss 2026-09-29:
    connectors are seen separately);
  - a direction's reflectance;
  - the report's own flag on a direction (an event only one direction read).
A file loaded one way among paired ones is a row of the same table with no
Average (as FastReporter lists it), so its own row is the one place a
two-direction table reads the single-direction gate (soloGate).
No Node here, so the JS is checked at the source, like the other Viewer tests.
"""
import re

from conftest import REPO_ROOT

VIEWER = (REPO_ROOT / 'viewer' / 'viewer.html').read_text(encoding='utf-8')


def _fn(name):
    m = re.search(r'function %s\([^)]*\) \{[^\n]*\}' % name, VIEWER)
    assert m, name
    return m.group(0)


def test_a_splice_direction_has_no_loss_gate():
    assert _fn('legGateFor') == ('function legGateFor(reflective) '
                                 '{ return reflective ? gateFor(true, true) : null; }')
    assert _fn('legWarnFor') == ('function legWarnFor(reflective) '
                                 '{ return reflective ? warnFor(true, true) : null; }')
    # null never flags and never turns a cell yellow
    assert 'if (loss == null || isNaN(loss) || gate == null) return false;' in VIEWER


def test_both_two_direction_tables_judge_a_direction_by_leg_gate():
    # FR layout: P/F, Warning and the cell colour
    assert 'return clearsAt(leg.loss, x.row.solo ? soloGate(x) : legGateFor(isRefl(x)));' in VIEWER
    assert 'clearsAt(leg.loss, x.row.solo ? soloWarn(x) : legWarnFor(isRefl(x)));' in VIEWER
    assert 'gated ? (x.row.solo ? soloGate(x) : legGateFor(isRefl(x))) : null,' in VIEWER
    assert 'judged ? (x.row.solo ? soloWarn(x) : legWarnFor(isRefl(x))) : null))' in VIEWER
    # OTDR Suite: the report's own flag first, then the leg gate
    assert ("|| (!c.isEnd && legOk(leg) && clearsAt(leg.loss, "
            "legGateFor(!!x.reflective)));") in VIEWER
    assert 'clearsAt(leg.loss, legWarnFor(!!x.reflective));' in VIEWER
    # nothing in either table reads the single-direction gate for a leg of
    # a paired fibre: only a file with no partner (no Average) does
    assert VIEWER.count('gateFor(isRefl(x), true)') == 1
    assert 'const soloGate = x => gateFor(isRefl(x), true);' in VIEWER
    assert 'gateFor(!!x.reflective, true)' not in VIEWER
    assert VIEWER.count('warnFor(isRefl(x), true)') == 1
    assert 'const soloWarn = x => warnFor(isRefl(x), true);' in VIEWER
    assert 'warnFor(!!x.reflective, true)' not in VIEWER


def test_the_average_still_flags_at_the_box():
    assert "if (which === 'avg') return clearsAt(x.row.loss, gateFor(isRefl(x), false));" in VIEWER
    assert "clearsAt(x.loss, gateFor(!!x.reflective, false))" in VIEWER
    assert 'if (!reflective) return leg ? gThresholds.single_dir : activeGateDb();' in VIEWER


def test_no_table_reads_the_single_direction_gate_for_a_splice():
    assert 'gateFor(false, true)' not in VIEWER
