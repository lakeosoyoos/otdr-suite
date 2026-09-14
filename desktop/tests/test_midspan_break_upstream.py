"""Upstream cells on a mid-span-broken fiber (TOOKNO↔KNOTOO).

analyze_all's mid-span-break block used to end in an UNCONDITIONAL
``continue``: once a fiber's A trace was found to end mid-span, EVERY column
of that fiber took the early exit.  Only the nearest-closure BROKE cell and
the columns past the break (B-fill / dead-zone) ever got written; the columns
UPSTREAM of the break — which still sit on perfectly good A-side glass — were
dropped without a cell.

Real miss: TOOKNO span 2 (A = TOOKNO 15sec 12.sep.26, B = KNOTOO 15sec
9.12.2026), fibers 1-33 break at km 46.36.  The boss's list has F2 1.143 @
13.02 km, F1 .762 @ 21.15 km, F13 1.300 @ 13.02 km and F26-33 .304 @ 25.47 km
— all present in the A traces, all blank in our bidi report.

These tests lock the three behaviors of the fixed block on one synthetic span:
  * an upstream column with a >= SINGLE_DIR_THRESHOLD A event is now produced
    as an A-only cell;
  * the nearest-closure BROKE cell is unchanged;
  * a column past the break still B-fills.

Engine runs in a clean subprocess (single sor_reader copy — the 3-engine
isolation rule).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def _run(body):
    body = "".join(textwrap.dedent(part) for part in body.split("\x00"))
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c", header + body],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


# Synthetic span: 60 km, closures at 13.0 / 30.2 / 45.0 km.
# Fiber 2 breaks at km 30.0 (nearest closure = the 30.2 column) and carries a
# 1.143 dB A event at the upstream 13.0 closure.  Its B trace is broken too,
# reaching back to A-frame km 30, and holds a .600 dB event at A-frame 45.
# Fibers 10-19 are healthy and only set the span median.
_SETUP = """
    SPAN = 60.0
    BREAK = 30.0
    splices = [{'position_km': km, 'column_kind': 'splice', 'count': 11}
               for km in (13.0, 30.2, 45.0)]
    fibers_a, fibers_b = {}, {}
    fibers_a[2] = {'events': [
        {'dist_km': 13.0, 'splice_loss': 1.143, 'is_end': False, 'type': '0F'},
        {'dist_km': BREAK, 'splice_loss': 0.0, 'is_end': True, 'type': '1E'}]}
    fibers_b[2] = {'events': [
        {'dist_km': SPAN - 45.0, 'splice_loss': 0.600, 'is_end': False, 'type': '0F'},
        {'dist_km': SPAN - BREAK, 'splice_loss': 0.0, 'is_end': True, 'type': '1E'}]}
    for f in range(10, 20):
        fibers_a[f] = {'events': [
            {'dist_km': SPAN, 'splice_loss': 0.0, 'is_end': True, 'type': '1E'}]}
        fibers_b[f] = {'events': [
            {'dist_km': SPAN, 'splice_loss': 0.0, 'is_end': True, 'type': '1E'}]}
    res = E.analyze_all(fibers_a, fibers_b, splices, E.REBURN_THRESHOLD)
"""


def test_upstream_a_event_is_reported_on_a_broken_fiber():
    """The boss's missing class: a real A event at a closure UPSTREAM of the
    break must come through as an A-only cell (B cannot reach past the break
    to confirm it), gated at SINGLE_DIR_THRESHOLD like every other A-only."""
    _run(_SETUP + "\x00" + """
        cell = res.get((2, 0))
        assert cell is not None, 'upstream A event dropped (cell never written)'
        assert cell['is_a_only'], cell
        assert cell['is_flagged'], cell
        assert abs(cell['a_loss'] - 1.143) < 1e-9, cell
        assert cell['b_loss'] is None, cell
        assert not cell['is_broke'] and not cell['is_bfill'], cell
        print('OK')
    """)


def test_broke_cell_unchanged():
    """The nearest-closure cell is still the BROKE cell, with its break km."""
    _run(_SETUP + "\x00" + """
        cell = res.get((2, 1))
        assert cell is not None and cell['is_broke'], cell
        assert cell['event_type'] == 'BROKE', cell
        assert abs(cell['bidir_dist'] - BREAK) < 1e-9, cell
        assert cell['label'].startswith('2 broke'), cell
        print('OK')
    """)


def test_column_past_the_break_still_bfills():
    """The early-exit narrowing must not cost us the B-fill past the break."""
    _run(_SETUP + "\x00" + """
        cell = res.get((2, 2))
        assert cell is not None and cell['is_bfill'], cell
        assert abs(cell['b_loss'] - 0.600) < 1e-9, cell
        assert cell['a_loss'] is None, cell
        print('OK')
    """)


def test_early_exit_is_conditional_in_source():
    """Source lock: the mid-span-break block must NOT end in an unconditional
    continue again.  The guard keeps the skip for the BROKE column, for
    anything past the break, and for a column within POSITION_TOL of the
    break (that one is the break seen from a neighbouring closure — skipping
    it avoids double-flagging)."""
    eng = (SPLICEREPORT_DIR / "splicereportmatchexfo.py").read_text(encoding="utf-8")
    guard = ("if (nearest_splice == si or sp_km > fiber_end\n"
             "                        or abs(sp_km - fiber_end) < POSITION_TOL):\n"
             "                    continue")
    assert guard in eng, \
        "mid-span-break early exit is no longer guarded — upstream A cells will drop"
