"""A phantom bend column flags a member only when it reads a bend.

refine_closure_centers can decide that a candidate closure is a bend zone
rather than a splice (column_kind 'bend').  That verdict settles the
POSITION half of the bend rule for every fiber in the column: there is no
closure there to be offset from.  It does not settle the LOSS half.  Every
other bend cell in the report needs a positive loss whose printed value
reaches BEND_THRESHOLD (0.090 dB), and a gainer is never a bend.

analyze_all and scan_b_events used to skip that half for phantom columns
(`is_bend = ... or _is_phantom_column`), so every member of the zone
printed as a flag whatever it read: on a 2500 ns span one bend column
carried 62 flagged cells, 55 of them under the gate, among them readings of
.004 and .007 and three gainers relabelled 'gainer' but still flagged.
FastReporter's own table has a row for all 62, matching to the millidecibel,
and flags none of them.

These tests hold the rule:
  * a phantom bend column member prints when its bidirectional reading is
    bend-sized, and as a bend;
  * a smaller reading, or a gainer, prints blank unless it clears the report
    threshold, exactly as it would in any other column;
  * a column quieted by the B-reciprocity veto keeps its report-threshold
    rule, unchanged;
  * a phantom DAMAGE column keeps today's rule (every member prints): the
    approved unidirectional sheet lists every fiber with a real step in a
    break-certified damage zone, so the bend gate is not obviously its rule
    and that is left as a decision of its own.

Engine tests run in a clean subprocess (one sor_reader copy per process).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def _run(*parts):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n"
              f"A_DIR = {str(FIXTURE_SPLICE_A_DIR)!r}\n"
              f"B_DIR = {str(FIXTURE_SPLICE_B_DIR)!r}\n")
    code = header + "".join(textwrap.dedent(b) for b in parts)
    p = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


# The real 24-fiber fixture: its closures, every member's reading at each,
# and a helper that runs the report passes with chosen columns turned into
# phantom zones.  Two closures are used: one whose members mostly read under
# the bend gate with three at or above it, and one that carries a gainer.
_FIXTURE = """
    import numpy as np
    fa, fb = E.load_all(A_DIR, B_DIR)[:2]
    cand = E.discover_splices(fa, fibers_b=fb)
    real, ph = E.refine_closure_centers(fa, cand, return_phantoms=True,
                                        fibers_b=fb)
    SPLICES = sorted(list(real) + list(ph),
                     key=lambda s: s.get('position_km_refined', s['position_km']))
    ends = sorted(e['dist_km'] for r in fa.values() for e in r['events']
                  if e['is_end'])
    SPAN = round(float(np.median(ends[int(len(ends) * 0.75):])), 2)

    def col(km):
        i = min(range(len(SPLICES)), key=lambda i: abs(
            SPLICES[i].get('position_km_refined', SPLICES[i]['position_km']) - km))
        got = SPLICES[i].get('position_km_refined', SPLICES[i]['position_km'])
        assert abs(got - km) < 0.2, (km, got)
        return i

    def readings(si):
        # Every member's bidirectional reading at column si as a splice
        # column: the flagged ones from the results, the passing ones from
        # the population the passes hand to the Viewer (stored pairs and
        # measured silent sides alike).
        pop = {}
        res = E.analyze_all(fa, fb, SPLICES, E.REBURN_THRESHOLD, population=pop)
        out = {k[0]: v['bidir_loss'] for k, v in pop.items()
               if k[1] == si and v.get('bidir_loss') is not None}
        out.update({k[0]: v['bidir_loss'] for k, v in res.items()
                    if k[1] == si and v.get('bidir_loss') is not None})
        return out

    def report(kinds, recip=()):
        # analyze_all + scan_b_events + the gainer rule, as the runner
        # chains them, with the columns in `kinds` made phantom zones.
        sp2 = [dict(s) for s in SPLICES]
        for si, kind in kinds.items():
            sp2[si]['column_kind'] = kind
        for si in recip:
            sp2[si]['b_recip_bend'] = 'test'
        res = E.analyze_all(fa, fb, sp2, E.REBURN_THRESHOLD)
        b = E.scan_b_events(fa, fb, sp2, E.REBURN_THRESHOLD, dict(res), SPAN)
        out = {**res, **b}
        E.apply_field_gainer_rule(out, SPAN)
        return out

    def printed(x):
        return float(f"{abs(x):.3f}") * (-1 if x < 0 else 1)
"""


def test_fixture_phantom_column_flags_only_bend_sized_members():
    """A real closure turned into a phantom bend column: the members that
    print blank in it are exactly the ones reading under 0.090 dB (and the
    gainer); the bend-sized ones print, as bends."""
    _run(_FIXTURE, """
        for km in (15.56, 48.06):
            si = col(km)
            base = readings(si)
            want = {f for f, v in base.items() if printed(v) >= E.BEND_THRESHOLD}
            under = {f for f, v in base.items() if printed(v) < E.BEND_THRESHOLD}
            assert under, f'{km}: fixture closure has no sub-gate member'
            out = report({si: 'bend'})
            got = {k[0] for k, v in out.items()
                   if k[1] == si and v.get('is_flagged', True)}
            assert got == want, (km, sorted(got ^ want),
                                 {f: base.get(f) for f in got ^ want})
            for f in got:
                c = out[(f, si)]
                assert c['is_bend'] and not c.get('is_gainer'), (km, f, c)
                assert c['label'] == f"{f} {E._format_loss(c['bidir_loss'])}", c
        # Not vacuous: 15.56 has bend-sized members, 48.06 has a gainer.
        assert any(printed(v) >= E.BEND_THRESHOLD
                   for v in readings(col(15.56)).values())
        assert any(v < 0 for v in readings(col(48.06)).values())
        print('OK')
    """)


def test_fixture_damage_column_keeps_todays_rule():
    """A phantom DAMAGE column is out of this rule's scope: every member it
    measures still prints, the gainer included (as a gainer)."""
    _run(_FIXTURE, """
        for km in (15.56, 48.06):
            si = col(km)
            base = readings(si)
            out = report({si: 'damage'})
            got = {k[0] for k, v in out.items()
                   if k[1] == si and v.get('is_flagged', True)}
            assert got == set(base), (km, sorted(got ^ set(base)))
        print('OK')
    """)


def test_fixture_other_columns_untouched():
    """Making one column a phantom zone changes nothing in any other column."""
    _run(_FIXTURE, """
        si = col(15.56)
        plain = report({})
        zoned = report({si: 'bend'})
        def key(out):
            return {k: (v.get('is_flagged', True), v.get('label'))
                    for k, v in out.items() if k[1] != si}
        assert key(plain) == key(zoned)
        print('OK')
    """)


def test_fixture_reciprocity_quiet_column_keeps_report_threshold():
    """A column demoted by the B-reciprocity veto flags its members at the
    report threshold only (PR #75), before and after this rule."""
    _run(_FIXTURE, """
        si = col(62.51)
        base = readings(si)
        want = {f for f, v in base.items()
                if abs(printed(v)) >= E.REBURN_THRESHOLD - 1e-9}
        assert want, 'fixture closure has no member over the report gate'
        out = report({si: 'bend'}, recip=(si,))
        got = {k[0] for k, v in out.items()
               if k[1] == si and v.get('is_flagged', True)}
        assert got == want, (sorted(got), sorted(want))
        print('OK')
    """)


# ── Synthetic records: each branch of the two passes on its own ──────────

_SYNTH = """
    def fiber(evs, eol=70.0):
        out = [{'dist_km': d, 'splice_loss': l, 'type': '0F',
                'reflection': -60.0, 'is_end': False} for (d, l) in evs]
        out.append({'dist_km': eol, 'splice_loss': 0.0, 'type': '1E',
                    'reflection': -40.0, 'is_end': True})
        return {'_source': 'sor', 'wavelength': 1550.0,
                'events': sorted(out, key=lambda e: e['dist_km'])}

    EOL = 70.0
    ZONE = 30.0
    def splices(kind):
        return [{'position_km': 10.0, 'position_km_refined': 10.0,
                 'column_kind': 'splice'},
                {'position_km': ZONE, 'position_km_refined': ZONE,
                 'column_kind': kind},
                {'position_km': 50.0, 'position_km_refined': 50.0,
                 'column_kind': 'splice'}]
"""


def test_analyze_all_phantom_member_rule():
    """Both A and B stored an event at the zone: the average decides."""
    _run(_SYNTH, """
        # fiber: (A loss, B loss) -> bidir
        CASES = {1: (0.105, -0.044),   # .0305: under the gate -> blank
                 2: (0.068, -0.082),   # -.007: small gainer -> blank
                 3: (0.200, 0.116),    # .158: bend-sized -> bend
                 4: (0.122, 0.063),    # .0925: just over the gate -> bend
                 5: (-0.300, -0.056)}  # -.178: gainer past the report gate
        fa = {f: fiber([(ZONE, a)]) for f, (a, b) in CASES.items()}
        fb = {f: fiber([(EOL - ZONE, b)]) for f, (a, b) in CASES.items()}
        res = E.analyze_all(fa, fb, splices('bend'), E.REBURN_THRESHOLD)
        E.apply_field_gainer_rule(res, EOL)
        got = {k[0] for k, v in res.items() if k[1] == 1 and v['is_flagged']}
        assert got == {3, 4, 5}, sorted(got)
        for f in (3, 4):
            assert res[(f, 1)]['is_bend'] and not res[(f, 1)].get('is_gainer'), res[(f, 1)]
        # The big gainer prints exactly as it would under a splice column:
        # flagged by the report threshold, categorised as a gainer.
        assert res[(5, 1)]['is_gainer'] and not res[(5, 1)]['is_bend'], res[(5, 1)]
        ref = E.analyze_all(fa, fb, splices('splice'), E.REBURN_THRESHOLD)
        E.apply_field_gainer_rule(ref, EOL)
        assert ref[(5, 1)]['is_gainer'] and ref[(5, 1)]['is_flagged']
        print('OK')
    """)


def test_analyze_all_phantom_member_grey_side():
    """A stored an event, B did not: B is measured (grey) and the average
    decides, as for a stored pair."""
    _run(_SYNTH, """
        GREY = {1: -0.046, 2: 0.080}     # f1: .110 A -> .032; f2: .110 A -> .095
        fa = {f: fiber([(ZONE, 0.110)]) for f in GREY}
        fb = {f: fiber([]) for f in GREY}
        for f in GREY:
            fb[f]['_f'] = f
        E._grey_loss = lambda fd, km, mirror=None, twin=None: GREY[fd['_f']]
        res = E.analyze_all(fa, fb, splices('bend'), E.REBURN_THRESHOLD)
        got = {k[0] for k, v in res.items() if k[1] == 1 and v['is_flagged']}
        assert got == {2}, sorted(got)
        assert res[(2, 1)]['is_bend'], res[(2, 1)]
        print('OK')
    """)


def test_scan_b_events_phantom_member_rule():
    """B stored an event the A pass did not claim: the same rule holds on
    both of the B pass's measured branches."""
    _run(_SYNTH, """
        # A+B branch: A also stored an event at the zone.
        fa = {1: fiber([(ZONE, -0.096)]),     # B .142 -> bidir .023 -> blank
              2: fiber([(ZONE, 0.060)])}      # B .142 -> bidir .101 -> bend
        fb = {1: fiber([(EOL - ZONE, 0.142)]),
              2: fiber([(EOL - ZONE, 0.142)])}
        res = E.scan_b_events(fa, fb, splices('bend'), E.REBURN_THRESHOLD, {}, EOL)
        got = {k[0] for k, v in res.items() if k[1] == 1}
        assert got == {2}, sorted(got)
        assert res[(2, 1)]['is_bend'], res[(2, 1)]

        # Grey branch: A stored nothing there and is measured.
        GREY = {3: -0.096, 4: 0.060}
        fa = {f: fiber([]) for f in GREY}
        for f in GREY:
            fa[f]['_f'] = f
        fb = {f: fiber([(EOL - ZONE, 0.142)]) for f in GREY}
        E._grey_loss = lambda fd, km, mirror=None, twin=None: GREY[fd['_f']]
        res = E.scan_b_events(fa, fb, splices('bend'), E.REBURN_THRESHOLD, {}, EOL)
        got = {k[0] for k, v in res.items() if k[1] == 1}
        assert got == {4}, sorted(got)
        assert res[(4, 1)]['is_bend'], res[(4, 1)]
        print('OK')
    """)


def test_bend_gate_is_the_printed_signed_value():
    """_clears_bend_gate reads the value the cell prints, signed."""
    _run("""
        g = E._clears_bend_gate
        assert g(0.08951) and g(0.090) and g(0.25)       # print .090 and up
        assert not g(0.0894) and not g(0.007) and not g(0.0)
        assert not g(-0.090) and not g(-0.5) and not g(None)
        print('OK')
    """)
