"""A lone bend far from every closure must print; nothing else may move.

scan_a_standalone_events' Test 2 reads a wide LSA at the fiber's predicted
splice at the nearest closure and drops a bend candidate unless 0.030 dB is
there.  Near a closure that separates a helix-drifted splice from a bend.  Far
from every closure it only grades the splice, so a well-made (or invisible)
splice dropped a real bend.  Clusters of 2+ fibers are printed later by
flag_consensus_bends and >= 0.160 ones by scan_b_events, but a LONE far bend
printed nowhere: a 48 km span lost a 0.131 dB bend seen by both directions 6 m
apart, 1.49 km from any closure, on a fiber whose splice there reads 0.000.

Test 2 now decides exactly as before.  A candidate it drops is only HELD when
_lone_far_bend_ok says it is plainly a bend (far, next to a splice closure,
seen by both directions at the same spot, both legs losing alike, beyond any
helix drift the fiber's length allows), and emit_far_lone_bends prints it
after flag_consensus_bends only where no pass already printed that fiber
there.  So every cell main prints stays exactly as it was.

Synthetic records only: the engine functions are the real ones.  Test 2's LSA
reading is pinned by replacing _narrow_lsa_loss.  Engine tests run in a clean
subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"

# 24 fibers, closures every ~6 km, both ends at 66.55 km, a good 0.03 dB
# splice on every fiber at every closure, 100 ns pulse (FR same-event
# tolerance 30 m).  extra: fiber -> (km, A loss, B loss, B offset m) for one
# more event; no_splice_at: drop the extra fibers' splice event at that
# closure (a splice too good for either event table); eof_short: the extra
# fibers' ends read that many km short (helix).  pin(reading) fixes Test 2.
_SPAN = """
SPAN = 66.55
CLOSURES = [6.03, 12.10, 17.75, 22.32, 29.21, 34.76, 39.69]


def pin(reading):
    E._narrow_lsa_loss = lambda fiber_data, position_km: reading


def ev(km, loss, end=False, refl=False):
    return {'dist_km': round(km, 4), 'splice_loss': loss, 'is_end': end,
            'is_reflective': end or refl,
            'type': '1E9999LS' if end else ('1F9999LS' if refl else '0F9999LS')}


def span(extra, no_splice_at=None, eof_short=0.0, n=24, pulse_ns=100,
         b_event=True, refl=False, closures=CLOSURES, short_km=0.0):
    fa, fb = {}, {}
    for f in range(1, n + 1):
        cks = [c for c in closures
               if not (f in extra and no_splice_at is not None
                       and abs(c - no_splice_at) < 1e-9)]
        # short_km: the extra fibers are that much shorter at BOTH ends, so
        # their B trace (and its own end) reads short of the cable span
        L = SPAN - (short_km if f in extra else 0.0)
        ea = [ev(c, 0.03) for c in cks]
        eb = [ev(L - c, 0.03) for c in cks]
        end = L - (eof_short if f in extra else 0.0)
        if f in extra:
            km, a_loss, b_loss, b_off_m = extra[f]
            ea.append(ev(km, a_loss, refl=refl))
            if b_event:
                eb.append(ev(L - km - b_off_m / 1000.0, b_loss))
        ea.append(ev(end, 0.0, end=True))
        eb.append(ev(L, 0.0, end=True))
        for evs in (ea, eb):
            evs.sort(key=lambda e: e['dist_km'])
        stamp = {'_launch_reel_absent': True, 'fxd_pulse_ns': pulse_ns}
        fa[f] = {'events': ea, **stamp}
        fb[f] = {'events': eb, **stamp}
    splices = [{'position_km': c, 'position_km_refined': c,
                'column_kind': 'splice'} for c in closures]
    return fa, fb, splices


def run(fa, fb, splices):
    held = {}
    a_st = E.scan_a_standalone_events(fa, splices, {}, SPAN, fibers_b=fb,
                                      held_far=held)
    allr = dict(a_st)
    allr.update(E.flag_consensus_bends(allr, fa, fb, splices, SPAN))
    lone = E.emit_far_lone_bends(allr, held, fa, splices)
    allr.update(lone)
    out, sp2 = E.split_offsplice_events_into_own_columns(
        allr, [dict(s) for s in splices], total_span_km=SPAN, fibers_a=fa)
    return a_st, held, lone, out, sp2


# The field case: ONE fiber, a two-way bend 2.52 km before the 29.21 km
# closure, A 0.123 / B 0.139 six metres apart, its splice there invisible.
LONE = {7: (26.688, 0.1234, 0.1390, 6.0)}
"""


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c",
                        header + _SPAN + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_lone_far_bend_prints_in_its_own_column():
    """The field case, end to end: scan_a's own output is unchanged (the
    candidate is only held), consensus prints nothing (one fiber), and the
    held bend reaches the grid in its own bend column at the event's km."""
    _run("""
        for reading in (0.012, None, -0.004):
            pin(reading)
            a_st, held, lone, out, sp2 = run(*span(LONE, no_splice_at=29.21))
            assert a_st == {}, a_st
            assert [k[0] for k in held] == [7], held
            assert [k[0] for k in lone] == [7], lone
            cols = [i for i, s in enumerate(sp2) if s.get('column_kind') == 'bend']
            assert len(cols) == 1 and abs(sp2[cols[0]]['position_km_refined'] - 26.688) < 1e-6, sp2
            cell = out[(7, cols[0])]
            assert cell['is_bend'] and cell['is_flagged'], cell
            assert cell['label'] == '7 BEND .131 bidi', cell['label']
            assert abs(cell['bidir_loss'] - 0.1312) < 1e-9, cell
        print('OK')
    """)


def test_own_splice_event_does_not_matter():
    """With the fiber's splice visible at the closure the outcome is the same:
    the rule never reads it, so it cannot be fooled by a B-direction view of a
    drifted splice mirrored back onto the closure."""
    _run("""
        pin(0.012)
        a_st, held, lone, out, sp2 = run(*span(LONE))
        assert [k[0] for k in lone] == [7], lone
        print('OK')
    """)


def test_cluster_keeps_consensus_cell():
    """Two fibers with the same far bend: consensus prints both, as on main,
    and the held copies are suppressed, so nothing changes label."""
    _run("""
        pin(0.012)
        two = {7: LONE[7], 8: (26.690, 0.1300, 0.1360, 5.0)}
        a_st, held, lone, out, sp2 = run(*span(two, no_splice_at=29.21))
        assert sorted(k[0] for k in held) == [7, 8], held
        assert lone == {}, lone
        labels = sorted(r['label'] for r in out.values() if r.get('is_bend'))
        assert labels == ['7 bend .131 bidi', '8 bend .133 bidi'], labels
        print('OK')
    """)


def test_near_closure_is_still_test2_only():
    """Within BEND_PERFIBER_WIN_KM of a closure Test 2 stays the only judge:
    nothing is held (gate 1 alone is pinned in test_each_gate_refuses_alone)."""
    _run("""
        pin(0.012)
        for km in (28.90, 28.76):
            a_st, held, lone, out, sp2 = run(*span({7: (km, 0.1234, 0.1389, 6.0)}))
            assert held == {} and lone == {}, (km, held)
        print('OK')
    """)


def test_one_direction_or_artefact_is_not_held():
    """A real bend is seen by both directions at the same spot and loses both
    ways.  No stored B event (grey read only), a B event beyond FR's same-event
    tolerance, a B leg that gains, strongly lopsided legs, a reflective event,
    or a bend/damage column as the nearest column: not held."""
    _run("""
        pin(0.012)
        E._grey_loss = lambda *a, **k: 0.10       # grey B clears bidir, reaches Test 2
        cases = {
            'grey B only':  span(LONE, no_splice_at=29.21, b_event=False),
            'B 60 m away':  span({7: (26.688, 0.1234, 0.1389, 60.0)}, no_splice_at=29.21),
            'B gains':      span({7: (26.688, 0.3000, -0.0200, 6.0)}, no_splice_at=29.21),
            'lopsided':     span({7: (26.688, 0.2400, 0.0500, 6.0)}, no_splice_at=29.21),
            'reflective':   span(LONE, no_splice_at=29.21, refl=True),
        }
        for name, (fa, fb, splices) in cases.items():
            a_st, held, lone, out, sp2 = run(fa, fb, splices)
            assert held == {} and lone == {}, (name, held)
        fa, fb, splices = span(LONE, no_splice_at=29.21)
        splices[4]['column_kind'] = 'bend'
        a_st, held, lone, out, sp2 = run(fa, fb, splices)
        assert held == {}, ('bend column nearest', held)
        print('OK')
    """)


def test_helix_drifted_splice_is_not_held():
    """The helix span's far end: fibers reading ~0.4 km short carry ONE event
    0.44-0.67 km before the last closure, which is their splice drifted short.
    Such an event must never be held, even seen by both directions: the
    end-of-fiber check refuses a fiber reading >= 0.15 km short, and the
    drift allowance refuses an event within 2x (drift + 0.3 km + one pulse)
    of its closure even on a normal-length fiber."""
    _run("""
        pin(0.012)
        drifted = {7: (39.69 - 0.60, 0.1234, 0.1389, 6.0)}
        a_st, held, lone, out, sp2 = run(*span(drifted, no_splice_at=39.69,
                                               eof_short=0.40))
        assert held == {}, ('reads short', held)
        a_st, held, lone, out, sp2 = run(*span(drifted, no_splice_at=39.69))
        assert held == {}, ('inside 2x allowance', held)
        far = {7: (39.69 - 0.90, 0.1234, 0.1389, 6.0)}
        a_st, held, lone, out, sp2 = run(*span(far, no_splice_at=39.69))
        assert [k[0] for k in held] == [7], ('beyond allowance', held)
        far_short = {7: (39.69 - 2.0, 0.1234, 0.1389, 6.0)}
        a_st, held, lone, out, sp2 = run(*span(far_short, no_splice_at=39.69,
                                               eof_short=0.40))
        assert held == {}, ('reads short, far beyond the allowance', held)
        a_st, held, lone, out, sp2 = run(*span(far_short, no_splice_at=39.69,
                                               eof_short=0.10))
        assert [k[0] for k in held] == [7], ('reads 0.10 short: normal', held)
        print('OK')
    """)


def test_launch_and_tailbox_zones_are_not_held():
    """Launch hardware and the tailbox are not plant: nothing held there."""
    _run("""
        pin(0.012)
        closures = [3.10, 9.20, 15.30, 21.40, 27.50]
        fa, fb, splices = span({7: (1.10, 0.1234, 0.1389, 6.0)}, closures=closures)
        for r in fa.values():
            r['_launch_reel_absent'] = False            # 3 km launch zone
        a_st, held, lone, out, sp2 = run(fa, fb, splices)
        assert held == {}, held
        print('OK')
    """)


def test_missing_pulse_metadata_fails_closed():
    """No pulse width on the records: the same-event tolerance falls to 20 m,
    so a 6 m twin still passes and a 25 m one does not."""
    _run("""
        pin(0.012)
        a_st, held, lone, out, sp2 = run(*span(LONE, no_splice_at=29.21, pulse_ns=None))
        assert [k[0] for k in held] == [7], held
        a_st, held, lone, out, sp2 = run(*span({7: (26.688, 0.1234, 0.1389, 25.0)},
                                               no_splice_at=29.21, pulse_ns=None))
        assert held == {}, held
        print('OK')
    """)


def test_wired_after_consensus_before_split_in_both_pipelines():
    """emit_far_lone_bends runs after flag_consensus_bends and before
    split_offsplice_events_into_own_columns, in the engine main() AND the
    runner the hub drives, and scan_a is handed the held dict in both."""
    for path, prefix in ((SPLICEREPORT_DIR / "splicereportmatchexfo.py", ""),
                         (SPLICEREPORT_DIR / "run_splicereport.py", "E.")):
        src = path.read_text(encoding='utf-8')
        body = src.split("def main(", 1)[1]
        a_name = "fibers_a" if not prefix else "fa"
        scan = body.index(prefix + "scan_a_standalone_events(")
        cons = body.index(prefix + "flag_consensus_bends(all_results")
        emit = body.index(prefix + "emit_far_lone_bends(all_results, held_far, "
                          + a_name + ", splices)")
        merge = body.index("all_results.update(far_lone)")
        split = body.index(prefix + "split_offsplice_events_into_own_columns(")
        assert scan < cons < emit < merge < split, path.name
        call = body[scan:body.index(")", body.index("held_far", scan))]
        assert "held_far=held_far" in call, path.name


def test_fiber_shorter_than_the_span_is_still_one_event():
    """FR mirrors B about B's OWN end.  A fiber 50 m shorter than the cable
    span (both ends) with its two legs 5 m apart is one event, not refused
    for its length (the cable-wide mirror would read 55 m and refuse it)."""
    _run("""
        pin(0.012)
        a_st, held, lone, out, sp2 = run(*span({7: (26.688, 0.1234, 0.1390, 5.0)},
                                               no_splice_at=29.21, short_km=0.05))
        assert [k[0] for k in lone] == [7], lone
        print('OK')
    """)


def test_each_gate_refuses_alone():
    """_lone_far_bend_ok called directly: a base case every gate passes, then
    ONE input changed per gate.  Each change alone must refuse, so deleting
    any gate (or any term of the helix drift allowance) fails this test."""
    _run("""
        fa, fb, splices = span(LONE, no_splice_at=29.21)
        ra, rb = fa[7], fb[7]
        e = next(x for x in ra['events'] if abs(x['dist_km'] - 26.688) < 1e-9)
        be = next(x for x in rb['events']
                  if not x['is_end'] and abs((SPAN - x['dist_km']) - 26.688) < 0.05)
        base = dict(ra=ra, rb=rb, e=e, loss=0.1234, b_value=0.1390,
                    b_source='event', b_event=be, best_d=2.522,
                    best_sp=splices[4], best_closure_km=29.21,
                    predicted_km=29.21, is_broken=False, total_span_a=SPAN,
                    cons_eof=SPAN, helix_half=0.0, launch_zone_km=0.3)

        def ok(**kw):
            args = dict(base)
            args.update(kw)
            return E._lone_far_bend_ok(**args)

        assert ok()
        refl_e = dict(e, is_reflective=True)
        type_e = dict(e, type='1F9999LS')
        refl_be = dict(be, is_reflective=True)
        sat_be = dict(be, type='2F9999LS')
        far_be = dict(be, dist_km=be['dist_km'] - 0.060)
        rb_noend = dict(rb, events=[x for x in rb['events'] if not x['is_end']])
        refusals = {
            '1 near a closure':          dict(best_d=0.45),
            '2 bend column nearest':     dict(best_sp=dict(splices[4], column_kind='bend')),
            '2 damage column nearest':   dict(best_sp=dict(splices[4], column_kind='damage')),
            '3 launch zone':             dict(launch_zone_km=27.0),
            '3 tailbox':                 dict(total_span_a=29.0),
            '4 broken fiber':            dict(is_broken=True),
            '4 reflective A':            dict(e=refl_e),
            '4 1F-typed A':              dict(e=type_e),
            '5 grey B':                  dict(b_source='grey_lsa'),
            '5 no B event':              dict(b_event=None),
            '5 no B record':             dict(rb=None),
            '5 reflective B':            dict(b_event=refl_be),
            '5 saturated B':             dict(b_event=sat_be),
            '5 B 60 m away':             dict(b_event=far_be),
            '5 B has no end':            dict(rb=rb_noend),
            '6 A gains':                 dict(loss=-0.01),
            '6 B gains':                 dict(b_value=-0.01),
            '6 B missing':               dict(b_value=None),
            '6 lopsided':                dict(loss=0.30, b_value=0.10),
            '7a reads short':            dict(cons_eof=SPAN + 0.20),
            '7a reads long':             dict(cons_eof=SPAN - 0.20),
        }
        for name, kw in refusals.items():
            assert not ok(**kw), name
        # 7b: a candidate 0.85 km out passes on the bare allowance
        # (0.3 km + one pulse, x2 = 0.62 km); each drift term alone refuses.
        near = dict(best_d=0.85, best_closure_km=26.688 + 0.85,
                    predicted_km=26.688 + 0.85)
        assert ok(**near)
        slow = dict(ra, fxd_pulse_ns=2500)
        terms = {
            'end-of-fiber offset': dict(cons_eof=SPAN + 0.14),
            'length model':        dict(predicted_km=26.688 + 0.85 - 0.20),
            'helix spread':        dict(helix_half=0.003),
            'pulse length':        dict(ra=slow),
        }
        for name, kw in terms.items():
            args = dict(near)
            args.update(kw)
            assert not ok(**args), name
        assert not ok(best_d=0.60, best_closure_km=26.688 + 0.60,
                      predicted_km=26.688 + 0.60), 'inside 2x the bare allowance'
        print('OK')
    """)


def test_emit_never_moves_a_cell_main_prints():
    """emit_far_lone_bends drops a held cell whenever printing it could move
    something main prints: any cell of any fiber within split's widest cluster
    gap (0.4 km) would share its column; a fold distance wide enough to fold
    it would re-key it onto the fiber's closure cell.  It also drops one at a
    closure discovery missed (most other fibers carry an event there)."""
    _run("""
        pin(0.012)
        fa, fb, splices = span(LONE, no_splice_at=29.21)
        held = {}
        E.scan_a_standalone_events(fa, splices, {}, SPAN, fibers_b=fb, held_far=held)
        assert [k[0] for k in held] == [7], held

        def emit(cells):
            return sorted(k[0] for k in E.emit_far_lone_bends(cells, held, fa, splices))

        def cell(f, km, **kw):
            return {'fiber': f, 'bidir_dist': km, 'is_flagged': True, **kw}

        assert emit({}) == [7]
        assert emit({(9, 4): cell(9, 26.70)}) == [], 'another fiber in its column'
        assert emit({(9, 4): cell(9, 26.688 + 0.45)}) == [7], 'beyond the 0.4 km gap'
        assert emit({(7, 4): cell(7, 28.19)}) == [7], 'own cell 1.5 km away'
        assert emit({(7, 4): cell(7, 26.80, is_flagged=False,
                                  is_borderline=True)}) == [], 'borderline'
        assert emit({(7, 4): cell(7, 26.33, is_flagged=False,
                                  is_broke=True)}) == [], 'own broke 0.36 km away'
        assert emit({(7, 4): cell(7, 26.69, label='7 .167')}) == [], 'scan_b loss'
        fold = E.BEND_SPLICE_FOLD_KM
        try:
            E.BEND_SPLICE_FOLD_KM = 3.0
            assert emit({}) == [], 'would fold into the closure column'
        finally:
            E.BEND_SPLICE_FOLD_KM = fold
        acct = E._event_explained_as_splice
        try:
            E._event_explained_as_splice = lambda *a, **k: True
            assert emit({}) == [], 'split would keep it as the splice'
        finally:
            E._event_explained_as_splice = acct
        assert emit({}) == [7]
        # a closure discovery missed: most other fibers carry an event there
        for f2 in range(1, 17):
            if f2 != 7:
                fa[f2]['events'].append(ev(26.70, 0.02))
        assert emit({}) == [], 'undiscovered closure'
        for f2 in range(3, 17):
            if f2 != 7:
                fa[f2]['events'] = [x for x in fa[f2]['events']
                                    if abs(x['dist_km'] - 26.70) > 1e-9]
        assert emit({}) == [7], 'two neighbours (9%) is still lone'
        print('OK')
    """)
