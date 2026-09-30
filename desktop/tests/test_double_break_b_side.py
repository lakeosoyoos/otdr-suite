"""A fiber broken twice prints both breaks.

A double break: the A trace dies at one point and the B trace at another, with
glass neither direction can see in between.  The A-side BROKE comes from Pass 1
(analyze_all), logged at the closure nearest A's end.  The B-side one comes from
scan_b_side_breaks, logged at the closure nearest B's end.  Both are relocated
into damage columns by their km afterwards (split_offsplice).  But the results
are keyed (fiber, closure) BEFORE the split, and when both ends are nearest to
the same closure the second key is taken: the B-side break was dropped.

A 432-fiber, 106.6 km span (1000 ns, 2025-10): fibers 427 and 432 die at
92.57 km from A and at 98.35 km from B.  Both ends fall nearest the same closure,
so the report printed "427 broke@92.6k | DZ 92.6-98.3k" and nothing at 98.3 km,
while fiber 428 (A dead at 12.56 km, B at 98.33 km, two different closures)
printed both.  The damage column at 98.36 km listed ten of the twelve fibers
that stop there.

Fixture doublebreak/ = eleven fibers of that span, both directions, every
identifier scrubbed (DBLBKA/DBLBKB, SITEA/SITEB), events and traces unchanged:
414-420 intact (they set the span), 421 broken at 98.36 km from both ends, 427
and 432 the double breaks, 428 the double break that already printed.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

from conftest import run_splicereport, FIXTURE_DIR, REPO_ROOT

FX = FIXTURE_DIR / "doublebreak"
SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def _engine(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_double_break_prints_the_b_side_break(tmp_path):
    rc, man, err = run_splicereport(FX / "A", FX / "B", tmp_path / "r.xlsx")
    assert rc == 0 and man and "cells" in man, err[-2000:]
    cols = {c["index"]: c for c in man["columns"]}
    broke = {}
    for c in man["cells"]:
        if c.get("is_flagged") and c.get("category") == "broke":
            broke.setdefault(c["fiber"], []).append(c)

    # the column every B trace stops in: 421 (both ends) and 428 (B side)
    at_b = {c["splice"] for c in broke[421]} & {c["splice"] for c in broke[428]}
    assert len(at_b) == 1, (broke[421], broke[428])
    si_b = at_b.pop()
    # 11 fibres: an event job (under 20 loaded), so the column the split
    # gave the B-side breaks is titled as an event, not "damage"; the split
    # that keys them apart (#360) has still run.
    assert cols[si_b]["kind"] == "event", cols[si_b]
    assert abs(cols[si_b]["km"] - 98.34) < 0.1, cols[si_b]

    for f in (427, 432):
        labels = {c["splice"]: c["label"] for c in broke.get(f, [])}
        assert len(labels) == 2, (f, labels)
        # the A side, as before
        a_side = [lab for si, lab in labels.items() if si != si_b]
        assert a_side == [f"{f} broke@92.6k | DZ 92.6-98.3k"], (f, labels)
        # the B side, in the column with the other fibers that stop there
        assert labels.get(si_b) == f"{f} broke@98.3k (B-only)", (f, labels)

    # the double break that already printed both is unchanged
    labels_428 = sorted(c["label"] for c in broke[428])
    assert labels_428 == ["428 broke@12.6k | DZ 12.6-98.3k",
                          "428 broke@98.3k (B-only)"], labels_428


def test_b_side_break_is_keyed_apart_only_where_split_moves_it():
    """The second key is used only where split must give the break a damage
    column of its own.  Anywhere split would fold it back onto a closure, or
    leave it where it was keyed, it would land on the cell it collided with,
    so there the old behaviour (drop it) stands."""
    _engine("""
        E._RUN_PULSE_SMEAR_KM = 0.0
        splices = [{'position_km': 10.0, 'position_km_refined': 10.0},
                   {'position_km': 50.0, 'position_km_refined': 50.0}]
        span = 100.0

        def fib(eof):
            return {'events': [{'dist_km': 0.0, 'is_end': False, 'splice_loss': 0.2},
                               {'dist_km': eof, 'is_end': True, 'splice_loss': 0.0}]}

        def run(b_eof, prior):
            fa = {1: fib(60.0), 2: fib(span), 3: fib(span), 4: fib(span)}
            fb = {1: fib(b_eof), 2: fib(span), 3: fib(span), 4: fib(span)}
            return E.scan_b_side_breaks(fa, fb, splices, {(1, 1): prior}, span)

        a_broke = {'fiber': 1, 'splice_idx': 1, 'is_broke': True,
                   'bidir_dist': 60.0, 'label': '1 broke@60.0k'}
        # B dies 80 km from A: 20 km from any closure, 20 km short of the end
        out = run(20.0, a_broke)
        assert list(out) == [(1, E.B_BREAK_KEY_BASE + 1)], out
        cell = out[(1, E.B_BREAK_KEY_BASE + 1)]
        assert cell['splice_idx'] == 1 and cell['is_broke'], cell
        assert cell['label'] == '1 broke@80.0k (B-only)', cell

        # ... and split gives each break its own damage column
        fa = {1: fib(60.0), 2: fib(span), 3: fib(span), 4: fib(span)}
        res, cols = E.split_offsplice_events_into_own_columns(
            {(1, 1): dict(a_broke), **out}, [dict(s) for s in splices],
            total_span_km=span, fibers_a=fa)
        kms = sorted(round(cols[k[1]]['position_km_refined'], 1)
                     for k, r in res.items() if r['fiber'] == 1)
        assert kms == [60.0, 80.0], (kms, res)
        assert all(k[1] < len(cols) for k in res), res
        assert all(cols[k[1]]['column_kind'] == 'damage' for k in res), cols

        # B dies 50.1 km from A, inside the fold of the closure at 50: split
        # would fold it onto that closure's cell -> dropped, as before
        assert run(49.9, a_broke) == {}
        # B dies 1.5 km from its own end: the tailbox zone, where split never
        # builds a column -> dropped, as before
        assert run(1.5, a_broke) == {}
        # the key held by an ordinary A cell, not a break -> unchanged
        reburn = {'fiber': 1, 'splice_idx': 1, 'is_broke': False,
                  'bidir_loss': 0.3, 'label': '1 .300'}
        assert run(20.0, reburn) == {}
        # no collision at all -> the natural key, as before
        out = run(20.0, None)
        assert list(out) == [(1, 1)], out
        print('OK')
    """)
