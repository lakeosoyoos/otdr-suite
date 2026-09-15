"""The BROKE cell carries the damage event's own loss (boss, 2026-09-15).

The mid-span-break block printed only WHERE a fiber quit ("2 broke@46.4k
(B-fill OK)").  The tech's sheet also records the LOSS of the damage event
itself — Lumen Span 2 Tooele↔Knolls fiber 2: an A event of 3.384 dB at
46.71 km with the fiber-end marker at 47.41 km, landing in the Damage
@46.37 km column.  "Match him the best we can and be data faithful."

Rule locked here: the damage event is the non-end A event closest to the
fiber's end, inside POSITION_TOL of it, that is NOT claimed by any OTHER
column (neighbour-aware local tolerance, so an upstream closure's own event
is never borrowed).  It prints only when it clears SINGLE_DIR_THRESHOLD on
the printed value, exactly like an A-only cell.

Engine runs in a clean subprocess (single sor_reader copy — the 3-engine
isolation rule).  Pattern follows test_midspan_break_upstream.py.
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


# Synthetic span: 60 km, closures at 13.0 / 30.2 / 45.0 km.  Fiber 2's A trace
# dies at km 30.0 (BROKE lands on the 30.2 column) and carries a damage event
# 0.7 km ahead of that end, at km 29.3 — 16 km from the nearest other closure,
# so nothing else can claim it.  B is broken too and reaches back to A-frame
# km 30.  Fibers 10-19 are healthy and only set the span median.
_SETUP = """
    SPAN = 60.0
    BREAK = 30.0
    DMG = BREAK - 0.7
    def _span(dmg_loss, extra_closure=None):
        kms = [13.0, 30.2, 45.0] + ([extra_closure] if extra_closure else [])
        kms.sort()
        splices = [{'position_km': km, 'column_kind': 'splice', 'count': 11}
                   for km in kms]
        fibers_a, fibers_b = {}, {}
        fibers_a[2] = {'events': [
            {'dist_km': DMG, 'splice_loss': dmg_loss, 'is_end': False, 'type': '0F'},
            {'dist_km': BREAK, 'splice_loss': 0.0, 'is_end': True, 'type': '1E'}]}
        fibers_b[2] = {'events': [
            {'dist_km': SPAN - BREAK, 'splice_loss': 0.0, 'is_end': True, 'type': '1E'}]}
        for f in range(10, 20):
            fibers_a[f] = {'events': [
                {'dist_km': SPAN, 'splice_loss': 0.0, 'is_end': True, 'type': '1E'}]}
            fibers_b[f] = {'events': [
                {'dist_km': SPAN, 'splice_loss': 0.0, 'is_end': True, 'type': '1E'}]}
        res = E.analyze_all(fibers_a, fibers_b, splices, E.REBURN_THRESHOLD)
        return next(c for c in res.values() if c.get('is_broke'))
"""


def test_broke_cell_carries_the_damage_loss():
    """Lumen F2's shape: a 3.384 dB event 0.7 km before the break prints in
    the BROKE cell, ahead of the existing B-fill suffix."""
    _run(_SETUP + "\x00" + """
        cell = _span(3.384)
        assert cell['label'].startswith('2 3.384 (A) broke@'), cell['label']
        assert cell['label'].endswith('(B-fill OK)'), cell['label']
        assert abs(cell['damage_loss'] - 3.384) < 1e-9, cell
        assert abs(cell['a_loss'] - 3.384) < 1e-9, cell
        assert abs(cell['bidir_dist'] - BREAK) < 1e-9, cell
        print('OK')
    """)


def test_below_the_single_direction_gate_the_label_is_unchanged():
    """A .151 dB step at the break is under SINGLE_DIR_THRESHOLD — the cell
    reads exactly as it does today, and carries no loss."""
    _run(_SETUP + "\x00" + """
        assert E.SINGLE_DIR_THRESHOLD > 0.151, E.SINGLE_DIR_THRESHOLD
        cell = _span(0.151)
        assert cell['label'] == '2 broke@%.1fk (B-fill OK)' % BREAK, cell['label']
        assert cell['damage_loss'] is None, cell
        assert cell['a_loss'] is None, cell
        print('OK')
    """)


def test_an_upstream_closures_own_event_is_not_borrowed():
    """With a real closure sitting at the same km as the pre-break event, that
    event belongs to the closure's column — the BROKE cell must not take it."""
    _run(_SETUP + "\x00" + """
        cell = _span(3.384, extra_closure=DMG)
        assert cell['label'] == '2 broke@%.1fk (B-fill OK)' % BREAK, cell['label']
        assert cell['damage_loss'] is None, cell
        print('OK')
    """)
