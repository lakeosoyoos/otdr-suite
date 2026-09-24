"""The Splice Report engine reads the group index the FILE STATES.

It used to BACK-DERIVE it, inverting the Bellcore formula
`ior = tot * 0.02998 / (dist_km * 1000)` over the first event with a non-zero
time-of-travel.  Eleven measurement call sites fed off that, and it was wrong
twice over:

  * it inverts through the ROUNDED 0.02998, which is 25.6 ppm long.  Before
    #247 that cancelled exactly, because `dist_km` had been produced by the
    same constant.  #247 made `dist_km` EXFO's float64 `Position`, so there
    is no round trip left and the rounding became a real bias — visible bare
    on a long first event, +25.2 ppm off a 117 km one.  `dist_km` is also
    rounded to 4 dp and sits in the DENOMINATOR, so on a first event 1 km out
    the ±0.05 m is worth ±50 ppm on its own and the error stops being a bias
    and becomes noise.
  * it needs a usable event.  A fiber reflective-dead AT the launch connector
    has exactly one event, at tot 0, so the loop finds nothing and returns a
    hardcoded 1.46820 — against a stated 1.47000 that is 1,224 ppm.

Two stated values are in the file and both are present on 115 of 115 .sor
fixtures: EXFO's float64 `Ior` in the proprietary block, and the Bellcore
FxdParams group index.  Scored against FastReporter's own marker Lengths
(whole numbers of samples, so the population pins the pitch independently of
any IOR field) over the 88 fixtures that carry enough markers:

    stated Ior (float64)     0.00 ppm median,    0.00 worst
    Bellcore group index     0.00 ppm median,    3.41 worst
    back-derivation         18.58 ppm median,   65.78 worst

Engine tests run in a clean subprocess — the three engines ship separate
sor_reader324802a.py copies and must never share a process.
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
FIX = REPO_ROOT / "desktop" / "tests" / "fixtures"
# Two fibers whose only event is the launch reflection at tot 0 — the
# back-derivation has nothing to invert on either.
ENDLAUNCH = FIX / "endlaunch"
BDR_DIR = FIX / "bdr"


def _run(body):
    header = ("import sys, glob, os\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import sor_reader324802a as sr\n"
              "import splicereportmatchexfo as E\n"
              f"FIX = {str(FIX)!r}\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout


# ── the constant ─────────────────────────────────────────────────────────

def test_tot_constant_is_exact_not_rounded():
    """c / 1e10, to the last bit.  The rounded 0.02998 is 25.6 ppm long and
    only looked harmless while the IOR it paired with was derived through it."""
    _run("""
        assert sr._TOT_M_PER_UNIT == 299_792_458.0 / 1e10, sr._TOT_M_PER_UNIT
        assert sr._TOT_M_PER_UNIT != 0.02998
        # the rounding it replaces, stated as the error it was
        ppm = (0.02998 - sr._TOT_M_PER_UNIT) / sr._TOT_M_PER_UNIT * 1e6
        assert 25.0 < ppm < 26.0, ppm
        print('OK')
    """)


# ── the required case: no usable event at all ────────────────────────────

def test_no_usable_event_still_gets_the_stated_ior():
    """THE case the derivation cannot serve.  Both fixtures are dead at the
    launch connector, so `_sor_ior_from_events` falls through to its hardcoded
    1.46820 while the file plainly states 1.47000 — 1,224 ppm, which drew the
    Viewer's trace 171 m off at the far end of a 139.8 km acquisition."""
    _run(f"""
        for name in ('HOWLAN309_1550.sor', 'LAGDUR0036.sor'):
            p = os.path.join({str(ENDLAUNCH)!r}, name)
            r = sr.parse_sor_full(p)
            usable = [e for e in r['events']
                      if e.get('time_of_travel') and e['time_of_travel'] > 0
                      and e.get('dist_km') and e['dist_km'] > 0.5]
            assert not usable, (name, len(usable))   # the premise
            assert sr._sor_ior_from_events(r) == 1.46820, name
            assert sr._sor_ior(r) == 1.47, (name, sr._sor_ior(r))
            assert r['ior'] == 1.47, (name, r['ior'])
            # and the pitch that follows from it
            bad = 299_792_458.0 * r['exfo_sampling_period'] / 2.0 / 1.46820
            good = sr._sor_res_m(r)
            ppm = (bad - good) / good * 1e6
            assert 1200 < ppm < 1250, (name, ppm)
            # what that was worth at the far end of this file's own trace
            drift_m = len(r['trace']) * (bad - good)
            assert drift_m > 150.0, (name, drift_m)
        print('OK')
    """)


# ── the stated value wins, everywhere ────────────────────────────────────

def test_stated_ior_preferred_over_the_derivation():
    """On a file that HAS a usable event, the stated value still wins — the
    derivation is 18.58 ppm off at the median and its sign depends on where
    the fiber's first event happens to sit."""
    _run("""
        n = moved = 0
        for p in sorted(glob.glob(FIX + '/**/*.sor', recursive=True)):
            if os.path.basename(p).startswith('._'):
                continue
            r = sr.parse_sor_full(p)
            if r is None:
                continue
            n += 1
            assert r.get('ior'), p          # every fixture states one
            assert sr._sor_ior(r) == float(r['ior']), p
            if sr._sor_ior_from_events(r) != sr._sor_ior(r):
                moved += 1
        assert n == 195, n
        # if this ever hit 0 the change would be inert and the test vacuous
        assert moved > 100, moved
        print('OK')
    """)


def test_marker_pitch_and_stated_ior_agree():
    """`_sor_res_m` prefers `exfo_res_m` — FastReporter's own marker-pinned
    pitch, the input `measure_fr_exact_loss` already indexes on.  The reader
    now carries that key on every file with a proprietary block: pinned by
    the marker vote where three markers exist, and the stated-IOR pitch
    itself where they don't (29 of the 119 fixtures, 689 of the 864 ZAYO
    BETA 432 files).  Which one answers is never visible in a result: on a
    file with the vote the two agree to 5e-14 relative, which is float
    rounding in a median-of-candidates and 0.00005 ppm against the 18.58 ppm
    the derivation was out by.  So sharing FR's pitch costs nothing, the
    legacy paths can no longer disagree with the FR-exact path about where a
    sample sits, and the FR-exact path is no longer refused on a
    single-direction .sor for want of a pitch."""
    _run("""
        both = only_ior = 0
        worst = 0.0
        for p in sorted(glob.glob(FIX + '/**/*.sor', recursive=True)):
            if os.path.basename(p).startswith('._'):
                continue
            r = sr.parse_sor_full(p)
            if r is None:
                continue
            from_ior = (299_792_458.0 * float(r['exfo_sampling_period'])
                        / 2.0 / float(r['ior']))
            if r.get('exfo_res_m'):
                both += 1
                worst = max(worst, abs(r['exfo_res_m'] - from_ior) / from_ior)
                assert sr._sor_res_m(r) == r['exfo_res_m'], p
            else:
                only_ior += 1
                assert sr._sor_res_m(r) == from_ior, p
        assert (both, only_ior) == (195, 0), (both, only_ior)
        # 5e-14 is the measured worst over 3,256 production traces; anything
        # above 1e-9 would mean the two sources genuinely disagree.
        assert worst < 1e-9, worst
        print('OK')
    """)


def test_bdr_directions_also_state_their_ior():
    """A .bdr carries one acquisition per direction and each states its own
    group index, so the .bdr path needs no derivation either."""
    _run(f"""
        n = 0
        for p in sorted(glob.glob({str(BDR_DIR)!r} + '/*.bdr')):
            pair = sr.parse_bdr(p)
            if not pair:
                continue
            for side in ('a', 'b'):
                d = pair[side]
                n += 1
                assert d.get('ior'), (p, side)
                assert sr._sor_ior(d) == float(d['ior']), (p, side)
                assert sr._sor_res_m(d) == d['exfo_res_m'], (p, side)
        assert n == 96, n          # 48 fixtures, two directions each
        print('OK')
    """)


# ── the measurement paths still measure ──────────────────────────────────

def test_the_measurement_paths_still_return_values():
    """A pitch change that quietly turned live measurements into None would be
    a regression the printed workbook could hide — the grid prints only
    flagged cells, so a lost measurement shows up as a missing flag, not an
    error.  Count the returns."""
    _run("""
        got = {'evt': 0, 'grey': 0, 'spike': 0, 'refl': 0}
        for p in sorted(glob.glob(FIX + '/span_A/*.sor')):
            r = sr.parse_sor_full(p)
            for e in r['events']:
                if e.get('is_end'):
                    continue
                if sr.measure_grey_loss_from_sor_event(r, e) is not None:
                    got['evt'] += 1
                if sr.measure_grey_loss_from_sor(r, e['dist_km']) is not None:
                    got['grey'] += 1
                if e.get('is_reflective'):
                    if sr.measure_reflective_spike(r, e['dist_km']) is not None:
                        got['spike'] += 1
            if sr.solve_backscatter_anchors(r):
                got['refl'] += 1
        for k, v in got.items():
            assert v > 0, (k, got)
        print('OK')
    """)


# ── nothing may quietly go back ──────────────────────────────────────────

# Every live use of the back-derivation or the rounded constant that this
# change deliberately LEFT ALONE, with the reason.  Anything not on this list
# is a measurement path that has gone back, and the guard below fails.
_ALLOWED = {
    # the last-resort guess itself, kept byte-identical because it IS a guess
    # and this is what the guess has always returned (see its docstring)
    'splicereport/sor_reader324802a.py':
        [# the KeyEvents SEED distance, overwritten a few hundred lines later
         # by EXFO's float64 Position (#247) on every file that carries the
         # proprietary block.  Left as-is: it is what a file WITHOUT that
         # block has always reported, and measurement no longer reads it.
         'dist_km = (tot * 0.02998 / IOR) / 1000.0',
         'ior = (tot * 0.02998) / (dk * 1000.0)',
         'return _sor_ior_from_events(sor_data, default=default)',
         # the .bdr tot round-trip: it SYNTHESISES a time_of_travel from a
         # float64 Position so a .bdr event round-trips through the same
         # formula the .sor KeyEvents parse uses.  Not a measurement, and
         # changing it would desynchronise the two parses.
         '_TOT_C = 0.02998'],
    # the JSON / trace-break helpers.  These index on FxdParams acq_range and
    # point count, not on the pitch, and they are not among the eleven
    # measurement sites; the synthetic-break tots are written to be read back
    # by the same pair of helpers.
    'splicereport/splicereportmatchexfo.py':
        ['return idx * 0.02998 * 2 * acq_range / (1000.0 * ior * pts)',
         'return int(round(km * 1000.0 * ior * pts / (0.02998 * 2 * acq_range)))',
         "'time_of_travel': int(round((bk_km * 1000.0 * ior / 0.02998) * 2)),",
         "'time_of_travel': int(round(((bk_km + 0.1) * 1000.0 * ior / 0.02998)"
         " * 2)),"],
}


def _code_lines(path):
    """Source with comments and string literals removed.

    Both of these files explain at length what the rounded constant was and
    why the derivation had to go, so a plain text scan trips over its own
    documentation.  Tokenising and dropping COMMENT/STRING leaves only code.
    """
    import io
    import tokenize
    src = path.read_text(encoding='utf-8')
    drop = set()
    with io.StringIO(src) as fh:
        for tok in tokenize.generate_tokens(fh.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                for ln in range(tok.start[0], tok.end[0] + 1):
                    drop.add(ln)
    return [(i, line.strip())
            for i, line in enumerate(src.splitlines(), 1)
            if i not in drop]


def test_no_measurement_path_back_derives_the_ior():
    """The point of the change, pinned in source.

    `_sor_ior_from_events` survives as the last resort inside `_sor_ior`, and
    a handful of non-measurement uses of the rounded constant are listed above
    with their reasons.  What must not come back is a MEASUREMENT path calling
    either.  Both files are checked because the eleven call sites were split
    across them."""
    bad = []
    for rel, allowed in _ALLOWED.items():
        for i, line in _code_lines(REPO_ROOT / rel):
            if '0.02998' not in line and '_sor_ior_from_events(' not in line:
                continue
            if line.startswith('def _sor_ior_from_events'):
                continue
            if any(a in line for a in allowed):
                continue
            bad.append(f'{rel}:{i}: {line}')
    assert not bad, ('a measurement path is back on the back-derivation or '
                     'the rounded constant:\n' + '\n'.join(bad))


def test_the_eleven_call_sites_are_on_the_stated_value():
    """Each of the eleven wants either the group index or just a pitch; every
    one now asks the file.  Counted in source so a partial revert fails here
    rather than in a field report."""
    reader = (REPO_ROOT / 'splicereport/sor_reader324802a.py'
              ).read_text(encoding='utf-8')
    engine = (REPO_ROOT / 'splicereport/splicereportmatchexfo.py'
              ).read_text(encoding='utf-8')
    # the reader's five, plus the two accessors' own bodies
    assert reader.count('_sor_res_m(sor_data') >= 5, reader.count('_sor_res_m(')
    assert '_sor_ior, _sor_res_m, _TOT_M_PER_UNIT)' in engine
    for frag in ('_sor_ior(fiber_rec)', '_sor_ior(rb)', '_sor_res_m(r, 1.468)',
                 '_sor_res_m(fiber_data, 1.468)', '_sor_res_m(fiber_rec)'):
        assert frag in engine, frag
