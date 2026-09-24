"""
.bdr ingestion — FastReporter's bidirectional report as a Splice Report input.

A .bdr holds BOTH directions of one fiber in one file.  These tests pin the
three things that make it safe to feed the engine:

  1. It decodes at all, on two unrelated spans shot years and instruments
     apart (SEANOR 110 km / 2500 ns / FTB-2, ORPVL 55 km / 100 ns / FTBx-730C).
  2. The direction binding is right.  This is the one that matters: bind an
     event list to the wrong trace and every measurement is fitted against
     the wrong glass, silently.  SEANOR carries the same two fibers as .sor
     fixtures, so the .bdr result is compared against the .sor result
     directly — same trace samples, same events, same losses.
  3. One .bdr folder fills BOTH direction maps in load_all.
"""
import os
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
FIX = os.path.join(HERE, 'fixtures')
BDR = os.path.join(FIX, 'bdr')
sys.path.insert(0, os.path.join(ROOT, 'OTDR Suite', 'splicereport'))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), '..', 'splicereport'))

import sor_reader324802a as sr                            # noqa: E402
# The .bdr reader lives INSIDE sor_reader324802a: a new engine module would
# freeze fleet hot-updates until every tech reinstalls (see the .bdr banner
# in that file).  `B` is kept as an alias so these tests read as if it were
# its own module — and it pins the fold, since an accidental move back to a
# separate file fails the ENGINE_FILES guard in test_autoupdate.
B = sr


SEANOR = os.path.join(BDR, 'SEANOR109_1550_1550.bdr')
ORPVL = os.path.join(BDR, 'ORPVL.ZYO-OR-DES-0048.1550.0001_1550.bdr')


# ── 1. Both shapes decode ────────────────────────────────────────────

@pytest.mark.parametrize('name', sorted(
    f for f in os.listdir(BDR) if f.lower().endswith('.bdr')))
def test_every_fixture_bdr_decodes(name):
    """Every vendored .bdr yields two directions with a trace and events."""
    r = B.parse_bdr(os.path.join(BDR, name))
    for side in ('a', 'b'):
        d = r[side]
        assert d['num_points'] > 1000
        assert len(d['events']) >= 2
        assert d['exfo_res_m'] and d['exfo_res_m'] > 0
        assert d['exfo_raw'] is not None
        # The launch sits at 0 and one record is the end of fibre.  A
        # tie-panel key whose span start was set on the launch connector
        # (Las Cruces, 5 ns) also stores the OTDR's own port a launch reel
        # (~1.03 km) upstream (on fiber 2 a small event 4.7 m before the
        # launch too), and the receive reel's far end past the end record;
        # every long-span key has none of these.
        pre = [e for e in d['events'] if e['dist_km'] < -0.001]
        assert all(e['dist_km'] > -5.0 for e in pre), pre
        assert d['events'][len(pre)]['dist_km'] == pytest.approx(0.0, abs=0.001)
        end_i = [i for i, e in enumerate(d['events']) if e['is_end']][0]
        assert all(e['dist_km'] > d['events'][end_i]['dist_km'] for e in d['events'][end_i + 1:])
        # Exactly one end-of-fibre record per direction.
        assert sum(1 for e in d['events'] if e['is_end']) == 1


def test_orpvl_shape():
    """The ORPVL shape: 80 km / 100 ns / span-start included, and the two
    directions are two DIFFERENT instruments (the file records both)."""
    r = B.parse_bdr(ORPVL)
    a, b = r['a'], r['b']
    assert a['num_points'] == b['num_points'] == 62677
    assert a['wavelength'] == pytest.approx(1550.0)
    assert a['ior'] == pytest.approx(1.468325)
    assert a['fxd_pulse_ns'] == pytest.approx(100.0)
    assert a['otdr_serial'] != b['otdr_serial']
    # Locations are stored once, oriented A->B; the B direction is the swap.
    assert (a['gen_loc_a'], a['gen_loc_b']) == (b['gen_loc_b'], b['gen_loc_a'])
    assert a['gen_loc_a'] == 'ORPVL'
    # FR's own merged bidirectional table rides along untouched.
    assert len(r['merged']) > 0


# ── 2. Direction binding, checked against the .sor of the same fiber ──

@pytest.mark.parametrize('side,sor_name', [
    ('a', 'SEANOR109_1550.sor'),
    ('b', 'NORSEA109_1550.sor'),
])
def test_bdr_matches_the_sor_of_the_same_fiber(side, sor_name):
    """The decisive test.  SEANOR fiber 109 exists as a .bdr AND as the two
    per-direction .sor files.  If the direction binding, the trace decode
    and the event mapping are right, the .bdr side must reproduce the .sor
    exactly — not approximately."""
    d = B.parse_bdr(SEANOR)[side]
    s = sr.parse_sor_full(os.path.join(FIX, 'frsilent', sor_name), trim=False)

    # Same proprietary trace, sample for sample.  A swapped binding fails
    # here immediately: A's samples are nothing like B's.
    assert len(d['exfo_raw']) == len(s['exfo_raw'])
    assert np.array_equal(d['exfo_raw'], s['exfo_raw'])

    assert d['exfo_res_m'] == pytest.approx(s['exfo_res_m'], rel=1e-12)
    assert d['ior'] == pytest.approx(s['ior'])
    assert len(d['events']) == len(s['events'])

    for be, se in zip(d['events'], s['events']):
        # EXACT, not approximate.  Both paths now read EXFO's own float64 out
        # of the proprietary block, so a fiber read from .sor and the same
        # fiber read from .bdr must produce the identical number in every
        # column.  These used to carry tolerances (5 m on distance, 0.5 mdB on
        # loss) because the .sor path derived distance from the integer
        # time-of-travel and took loss/slope/reflectance from KeyEvents int16.
        # Most of our work arrives as .sor, so that quantization was the whole
        # gap between a .sor report and FastReporter.
        assert be['dist_km'] == se['dist_km']
        assert be['splice_loss'] == se['splice_loss']
        assert be['slope'] == se['slope']
        assert be['reflection'] == se['reflection']
        assert be['is_end'] == se['is_end']
        assert be['is_reflective'] == se['is_reflective']


def test_binding_is_not_positional():
    """Guard the binding rule itself.  Each direction's first event carries
    a CurveLevel equal to its OWN trace's InjectionLevel; that is what binds
    list to trace.  If the two were ever swapped, the injection levels would
    land on the wrong sides."""
    r = B.parse_bdr(ORPVL)
    for side in ('a', 'b'):
        d = r[side]
        head = d['events'][0]
        rec = head.get('_bdr_record') or {}
        assert rec.get('CurveLevel') == pytest.approx(
            d['exfo_injection_level'], abs=1e-6)


def test_fr_exact_loss_runs_on_a_bdr_side():
    """The .bdr carries FR's own LSA cursors, so the FR-exact fitter needs no
    window prediction — feed a side's stored cursors back through it and it
    reproduces FR's stored loss."""
    d = B.parse_bdr(SEANOR)['a']
    n_exact = n = 0
    for rec in d['exfo_events']:
        if rec.get('_is_section'):
            continue
        loss = rec.get('Loss')
        if loss is None or loss != loss:        # launch / end carry NaN
            continue
        cur = (rec.get('CursorAPosition'), rec.get('CursorBPosition'),
               rec.get('SubCursorAPosition'), rec.get('SubCursorBPosition'))
        if any(c is None for c in cur):
            continue
        got = sr.measure_fr_exact_loss(d, cur[0], cur[1], cur[2], cur[3])
        if got is None:
            continue
        n += 1
        if abs(got - loss) < 1e-6:
            n_exact += 1
    assert n >= 5
    # Machine-exact on this fixture; the assertion is deliberately strict
    # because anything less means the trace, the pitch or the cursors moved.
    assert n_exact == n


# ── 3. One folder fills both directions ──────────────────────────────

def test_load_all_fills_both_directions_from_one_bdr_folder(tmp_path):
    import shutil
    import splicereportmatchexfo as E
    d = tmp_path / 'bdr'
    d.mkdir()
    shutil.copy(SEANOR, d / 'SEANOR109_1550_1550.bdr')
    shutil.copy(os.path.join(BDR, 'SEANOR110_1550_1550.bdr'),
                d / 'SEANOR110_1550_1550.bdr')
    fa, fb = E.load_all(str(d), str(d))
    assert sorted(fa) == sorted(fb) == [109, 110]
    assert fa[109]['_source'] == fb[109]['_source'] == 'bdr'
    # A and B must be DIFFERENT records, not the same object twice.
    assert fa[109] is not fb[109]
    assert not np.array_equal(fa[109]['exfo_raw'], fb[109]['exfo_raw'])


def test_one_bdr_folder_is_read_once_when_only_a_is_given(tmp_path):
    """Pointing only the A box at a .bdr folder still fills both sides."""
    import shutil
    import splicereportmatchexfo as E
    d = tmp_path / 'bdr'
    d.mkdir()
    shutil.copy(SEANOR, d / 'SEANOR109_1550_1550.bdr')
    fa, fb = E.load_all(str(d), '')
    assert list(fa) == list(fb) == [109]


def test_a_truncated_bdr_is_refused_not_half_loaded():
    """A file that cannot supply both directions must raise, not return one
    side — a half-loaded fiber reads downstream as a dead direction."""
    import tempfile
    with open(SEANOR, 'rb') as fh:
        head = fh.read(40000)
    with tempfile.NamedTemporaryFile(suffix='.bdr', delete=False) as tf:
        tf.write(head)
        path = tf.name
    try:
        with pytest.raises(ValueError):
            B.parse_bdr(path)
    finally:
        os.unlink(path)


def test_is_bdr_set_refuses_a_mixed_folder():
    import folder_intake as fi
    assert fi.is_bdr_set(['x/a.bdr', 'x/b.bdr'])
    assert not fi.is_bdr_set(['x/a.bdr', 'x/b.sor'])
    assert not fi.is_bdr_set([])


# ── 4. The .bdr filename rule ────────────────────────────────────────

@pytest.mark.parametrize('name,expect', [
    # FastReporter appends its own wavelength to the .sor stem, so the
    # suffix doubles.  Without the repeated strip this reads 1550.
    ('SEANOR109_1550_1550.bdr', 109),
    ('SEANOR120_1550_1550.bdr', 120),
    ('ORPVL.ZYO-OR-DES-0048.1550.0001_1550.bdr', 1),
    ('LAGDUR0001.bdr', 1),
    ('._SEANOR109_1550_1550.bdr', None),
])
def test_bdr_fiber_number(name, expect):
    import splicereportmatchexfo as E
    assert E._bdr_fiber_num(name) == expect


@pytest.mark.parametrize('name,expect', [
    ('Norsea001_1550.sor', 1),
    ('VERSLK001_131015501625 .json', 1),
    ('Seattle to Spokane d.0431.sor', 431),
    ('LAGDUR0001.sor', 1),
])
def test_sor_filename_rule_is_untouched(name, expect):
    """The .bdr rule is its own function precisely so the shared one — tuned
    against ~38k real filenames — does not move."""
    import splicereportmatchexfo as E
    assert E._extract_fiber_num(name) == expect


# ── 5. Silent-side calibration against FR's own stored answers ───────
#
# A .bdr stores, beside every one-sided event, the value FastReporter
# SYNTHESIZED for the direction that never detected it — the transplanted
# silent-side loss.  That makes a .bdr ground truth for a code path that
# serves .sor just as much: `_fr_exact_silent_loss` is one function, and
# these fixtures are the only place its output can be checked against FR
# without a human reading numbers off a screen.
#
# Until this, the transplant's calibration was 12 SEANOR .bdr — 110 km,
# launch reels at both ends, 2500 ns — and the suite pinned two cases
# (F150 and F439 in test_fr_silent_frame).  ORPVL is a geometry the rule
# was never tested on: 55 km, fully trimmed (launch at 0.000), no reels,
# 100 ns.  It holds there, and these tests are what keeps it holding.

ORPVL_SET = sorted(
    os.path.join(BDR, f) for f in os.listdir(BDR) if f.startswith('ORPVL'))

# The known floor.  When the terminal anchor is not usable the projection
# falls back to L_phys, which inherits the Bellcore grid — worth ~0.04 mdB
# (see _fr_proj_constant).  Four of the 61 measurable ORPVL records land in
# that band, the worst at 0.079 mdB.  Everything else is machine-exact.
FR_SILENT_TOL_MDB = 0.1


def _pass0(dir_path):
    """Load a .bdr folder and apply the runner's Pass-0, so the records are
    in the state `_fr_exact_silent_loss` is called with in production."""
    import splicereportmatchexfo as E
    fa, fb = E.load_all(dir_path, dir_path)
    reels = E.reciprocal_reels(fa, fb)
    for di, direction in enumerate((fa, fb)):
        reel, recv, absent, tol = reels[di]
        endmed = E._direction_end_median_km(direction)
        for r in direction.values():
            r['_raw_events'] = r['events']
            r['_launch_reel_km'] = reel
            r['_receive_reel_km'] = recv
            r['_launch_reel_absent'] = absent
            r['_launch_reel_tol_km'] = tol
            r['_trace_offset_km'] = E._trace_frame_offset_km(
                r, r['events'], reel, absent, tol)
            r['events'] = E._normalize_untrimmed_events(
                r['events'], reel, recv, absent, tol, endmed)
    return fa, fb


def _fr_silent_records(path):
    """FR's own silent-side answers, read out of the .bdr pair blocks.

    Each one-sided row is stored as a two-record block: the detecting
    direction's real record, and the synthesized one for the direction that
    saw nothing (Length 0, Type 0, no CurveLevel).  Yields
    (silent_record, loud_record).
    """
    stream = sr._inflate(path)
    blocks = sr._record_blocks(sr._decode_fields(stream))
    for b in blocks:
        sr._tag_sections(b)
    out = []
    for b in blocks:
        if len(b) != 2 or any(r.get('_merged') for r in b):
            continue
        sil = [r for r in b
               if r.get('Length') == 0.0 and r.get('Type') == 0
               and (r.get('CurveLevel') is None or r['CurveLevel'] != r['CurveLevel'])]
        loud = [r for r in b if r not in sil]
        if len(sil) == 1 and len(loud) == 1 and loud[0].get('CurveLevel') is not None:
            out.append((sil[0], loud[0]))
    return out


def _silent_cases(tmp_path):
    """Every ORPVL silent-side record, paired with the engine's answer.

    Direction is decided from the LOUD record — it is the one with a real
    detection, so its position must appear in its own direction's list.
    Deciding it from the SILENT record instead looks for a position that
    direction has no event at by definition, and silently assigns half the
    population backwards.
    """
    import shutil
    import splicereportmatchexfo as E
    d = tmp_path / 'orpvl'
    d.mkdir()
    for f in ORPVL_SET:
        shutil.copy(f, d / os.path.basename(f))
    fa, fb = _pass0(str(d))
    cases = []
    for path in ORPVL_SET:
        fib = E._bdr_fiber_num(os.path.basename(path))
        ra, rb = fa[fib], fb[fib]
        own = {'a': [x for x in ra['exfo_events'] if not x.get('_is_section')],
               'b': [x for x in rb['exfo_events'] if not x.get('_is_section')]}
        for sil, loud in _fr_silent_records(path):
            in_a = any(abs(x['Position'] - loud['Position']) < 2 for x in own['a'])
            in_b = any(abs(x['Position'] - loud['Position']) < 2 for x in own['b'])
            if in_a == in_b:
                continue
            rec_l, rec_s = (ra, rb) if in_a else (rb, ra)
            evt = [e for e in rec_l['_raw_events']
                   if abs(e['dist_km'] * 1000.0 - loud['Position']) < 2]
            if not evt:
                continue
            cases.append((fib, sil, E._fr_exact_silent_loss(rec_s, rec_l, evt[0])))
    return cases


def test_orpvl_fixtures_carry_silent_side_ground_truth():
    """Guard the fixtures themselves: if the stored silent records stop being
    readable, every assertion below would pass vacuously."""
    total = sum(len(_fr_silent_records(p)) for p in ORPVL_SET)
    assert len(ORPVL_SET) >= 3
    assert total >= 12, f'only {total} silent-side records found'


def test_transplant_reproduces_fastreporter_on_a_trimmed_span(tmp_path):
    """The calibration.  Every silent-side value the engine produces on the
    ORPVL set must be FR's own, within the known projection floor."""
    cases = _silent_cases(tmp_path)
    measured = [(f, s, v) for f, s, v in cases if v is not None]
    assert len(measured) >= 10, f'only {len(measured)} measured'
    worst = max(abs(v - s['Loss']) * 1000.0 for _f, s, v in measured)
    assert worst < FR_SILENT_TOL_MDB, f'worst {worst:.4f} mdB'
    # and most of them are not merely close — they are exact
    exact = sum(1 for _f, s, v in measured if abs(v - s['Loss']) * 1000.0 < 1e-6)
    assert exact >= int(0.8 * len(measured)), f'{exact}/{len(measured)} exact'


@pytest.mark.parametrize('fiber,km,expect', [
    (19, 40.2974, -0.000577),   # B's own closure 30.6 m away, inside the window
    (17, 21.8287, +0.002589),   # same shape, 33.2 m
])
def test_a_neighbour_inside_the_window_never_yields_the_neighbours_step(
        tmp_path, fiber, km, expect):
    """The two ORPVL records that found the premise check.

    FastReporter declined to pair these events with the near neighbour the
    other direction detected, and transplanted a value close to zero.  Our
    projected window lands ON that neighbour, so REFITTING it returns the
    neighbour's step — 82 mdB and 33 mdB wrong, enough to carry a cell over
    the .160 line.

    The premise check made the engine decline rather than return that.  On a
    .bdr it no longer has to: FR's own transplanted value is in the file, so
    the engine returns THAT — better than declining, and still never the
    neighbour's step.  The check itself is unchanged and still governs .sor
    input, where there is nothing to read."""
    hits = [(f, s, v) for f, s, v in _silent_cases(tmp_path)
            if f == fiber and abs(s['Position'] / 1000.0 - km) < 0.01]
    assert len(hits) == 1, hits
    got = hits[0][2]
    assert got is not None, 'should now return FR\'s own value, not decline'
    assert abs(got - expect) < 1e-6, f'{got!r} vs FR {expect!r}'
    # the failure mode this guards: the neighbour's step is tens of mdB away
    assert abs(got) < 0.01, f'{got!r} looks like a neighbour step'


# ── 6. A .bdr record must reach the measurement paths ────────────────
#
# `_grey_loss` and the narrow-bend measure dispatch on `_source`, and both
# fall through to `return None` for anything they do not name.  The .bdr
# loader stamps `_source = 'bdr'`, so listing only 'sor' there meant every
# silent-side measurement on a .bdr returned None — one-sided events got no
# bidirectional value at all, on every fiber, with no error anywhere.
#
# It was invisible to the calibration tests above because those call
# `_fr_exact_silent_loss` directly rather than through the dispatcher.  These
# go through the front door.

def test_grey_loss_reaches_the_transplant_on_a_bdr(tmp_path):
    """A one-sided event on a .bdr must come back with FR's own value, not
    None.  This is the front-door version of the calibration above."""
    import splicereportmatchexfo as E
    d = tmp_path / 'orpvl'
    d.mkdir()
    import shutil
    for f in ORPVL_SET:
        shutil.copy(f, d / os.path.basename(f))
    fa, fb = _pass0(str(d))

    checked = 0
    for fib in sorted(fa):
        ra, rb = fa[fib], fb[fib]
        assert ra['_source'] == 'bdr'
        b_span = [e for e in rb['events'] if e['is_end']][0]['dist_km']
        for ea in ra['events']:
            if ea['is_end'] or ea['dist_km'] < 1.0:
                continue
            # only where B genuinely detected nothing
            mirrored = [e for e in rb['events'] if not e['is_end']
                        and abs((b_span - e['dist_km']) - ea['dist_km']) < 0.05]
            if mirrored:
                continue
            v = E._grey_loss(rb, ea['dist_km'], twin=(ra, ea))
            direct = E._fr_exact_silent_loss(rb, ra, ea)
            if direct is None:
                continue
            assert v is not None, (
                'a .bdr silent side measured None through _grey_loss while '
                'the transplant returns %r — the _source dispatch dropped it'
                % (direct,))
            assert v == pytest.approx(direct, abs=1e-12)
            checked += 1
    assert checked >= 5, f'only {checked} one-sided events exercised'


def test_narrow_bend_measure_accepts_a_bdr(tmp_path):
    """The second dispatch site, same defect, same fix."""
    import shutil
    import splicereportmatchexfo as E
    d = tmp_path / 'orpvl'
    d.mkdir()
    shutil.copy(ORPVL_SET[0], d / os.path.basename(ORPVL_SET[0]))
    fa, _fb = _pass0(str(d))
    rec = fa[sorted(fa)[0]]
    ev = [e for e in rec['events'] if not e['is_end'] and e['dist_km'] > 1.0][0]
    assert E._narrow_lsa_loss(rec, ev["dist_km"]) is not None


# ── FR's per-direction legs behind each merged mean ────────────────────────
# A merged row stores FR's bidirectional loss; the EventAB / EventBA
# containers under it store the two per-direction measurements it averaged.
# An FR-comparison view needs the split, not just the mean, so these pin the
# recovery — and one of them pins the bug that made it wrong at first.

@pytest.mark.parametrize('name', [
    'ORPVL.ZYO-OR-DES-0048.1550.0001_1550.bdr',
    'ORPVL.ZYO-OR-DES-0048.1550.0017_1550.bdr',
    'SEANOR109_1550_1550.bdr',
])
def test_every_merged_event_row_carries_both_legs(name):
    merged = B.parse_bdr(os.path.join(BDR, name))['merged']
    events = [r for r in merged if 'Type' in r]
    assert events, 'no merged event rows decoded'
    for r in events:
        assert r.get('_ab') is not None, f"no A leg at {r.get('Position')}"
        assert r.get('_ba') is not None, f"no B leg at {r.get('Position')}"


@pytest.mark.parametrize('name', [
    'ORPVL.ZYO-OR-DES-0048.1550.0003_1550.bdr',
    'SEANOR110_1550_1550.bdr',
])
def test_the_two_legs_average_to_fastreporters_own_bidi_loss(name):
    """The arithmetic FR itself used.  Exact, not close: both legs and the
    mean are stored as float64, so any deviation means the legs were read
    off the wrong row."""
    merged = B.parse_bdr(os.path.join(BDR, name))['merged']
    checked = 0
    for r in merged:
        if 'Type' not in r:
            continue
        a, b, m = r.get('_ab'), r.get('_ba'), r.get('Loss')
        vals = [a.get('Loss'), b.get('Loss'), m]
        if not all(isinstance(v, float) and not np.isnan(v) for v in vals):
            continue
        assert abs((a['Loss'] + b['Loss']) / 2.0 - m) < 5e-9, (
            f"legs at {r.get('Position'):.1f} m average to "
            f"{(a['Loss'] + b['Loss']) / 2.0!r}, row stores {m!r}")
        checked += 1
    assert checked >= 5, f'only {checked} rows had a usable mean'


def test_a_section_rows_legs_do_not_overwrite_the_events():
    """Every merged position appears TWICE — once for the event row, once for
    the section that follows it.  Keying the legs on the position value lets
    the section's legs land on the event, which silently replaces FR's event
    measurements with section attenuations.  The join is on the record's
    stream offset for exactly this reason."""
    merged = B.parse_bdr(ORPVL)['merged']
    events = [r for r in merged if 'Type' in r]
    sections = [r for r in merged if 'Type' not in r]
    shared = ({r['Position'] for r in events if isinstance(r.get('Position'), float)}
              & {r['Position'] for r in sections if isinstance(r.get('Position'), float)})
    assert shared, 'fixture no longer has event/section rows at one position'
    for r in events:
        if r.get('Position') not in shared:
            continue
        a = r.get('_ab')
        assert a is not None and isinstance(a.get('CursorAPosition'), float), (
            'event row lost its own legs to the section at the same position')


def test_the_silent_side_is_identifiable_from_the_legs():
    """FR marks the direction it never detected with CurveLevel NaN.  That
    flag is how a comparison view knows a number was synthesised rather than
    measured, so it has to survive the decode."""
    merged = B.parse_bdr(ORPVL)['merged']
    synth = 0
    for r in merged:
        if 'Type' not in r:
            continue
        for leg in (r.get('_ab'), r.get('_ba')):
            cl = (leg or {}).get('CurveLevel')
            if isinstance(cl, float) and np.isnan(cl):
                synth += 1
    assert synth, 'no synthetic legs found — the CurveLevel flag was lost'


# ── FR's 0.100 dB/km slope floor ───────────────────────────────────────────
# FastReporter will not accept a fit slope below 0.100 dB/km.  Read off its
# Markers tab directly: one event driven through 12 left-window lengths (20 ->
# 3918 samples) plus a second event through 3 more.  Plain OLS matched FR at
# every length whose fitted slope was >= 0.1 and diverged at every length
# below it, by 2.7 to 24.5 mdB, growing as the slope fell.  These two fibers
# are the ones that sent us looking: both sit on a neighbour's recovery tail,
# where the squeezed window fits flatter than glass can be.

F212 = os.path.join(BDR, 'ORPVL.ZYO-OR-DES-0048.1550.0212_1550.bdr')
F354 = os.path.join(BDR, 'ORPVL.ZYO-OR-DES-0048.1550.0354_1550.bdr')


@pytest.mark.parametrize('path,side,ca,cb,sa,sb,expect', [
    # fiber 212 A, 20-sample window — FR's Markers tab reads -0.049 here, set
    # by hand on a trace byte-identical to ours.  Plain OLS returned +0.0089,
    # and that 58 mdB is what pushed this cell over the 0.100 gate.
    (F212, 'a', 21856.7, 21881.0, 21831.2, 26880.7, -0.049001),
    # fiber 212 B, 48-sample window.  Plain OLS returned +0.0183.
    (F212, 'b', 33259.8, 33319.8, 33198.5, 38319.5, 0.009715),
    # fiber 354 B, 38-sample window — FR reads 0.0237, a knife-edge 0.0993
    # bidirectional mean against a 0.100 gate.  Plain OLS returned +0.0541.
    (F354, 'b', 10905.4, 10929.6, 10856.9, 15929.3, 0.023699),
    # fiber 354 A, 72-sample window — the floor does NOT bind here, so this
    # one pins that a short window alone is not what triggers it.
    (F354, 'a', 44215.0, 44282.6, 44123.1, 49282.3, -0.015509),
])
def test_slope_floor_reproduces_fastreporter(path, side, ca, cb, sa, sb, expect):
    rec = B.parse_bdr(path)[side]
    got = B.measure_fr_exact_loss(rec, ca, cb, sa, sb)
    assert got is not None
    assert abs(got - expect) < 5e-4, f'{got!r} vs FR {expect!r}'


def test_the_floor_is_the_value_read_off_fastreporter():
    """0.100 dB/km, not a tuned constant.  Inverting FR's own answers for the
    slope it must have used gave 0.101 / 0.098 / 0.103 / 0.096 / 0.095 on the
    five diverging readings — a constant, not a trend.  A corpus sweep over
    432 fibers peaks sharply at 0.100 and falls away on both sides."""
    assert B.FR_SLOPE_FLOOR_DB_KM == 0.100


def test_the_floor_never_touches_a_healthy_fit():
    """It binds on under 1% of records.  Every fixture whose windows fit at a
    normal fibre slope must be byte-identical to the pre-floor result, which
    is what the SEANOR expectations elsewhere in this file already pin."""
    import numpy as _np
    touched = 0
    checked = 0
    for name in ('SEANOR109_1550_1550.bdr',
                 'ORPVL.ZYO-OR-DES-0048.1550.0001_1550.bdr'):
        d = B.parse_bdr(os.path.join(BDR, name))
        for side in ('a', 'b'):
            rec = d[side]
            res = rec.get('exfo_res_m')
            raw = rec.get('exfo_raw')
            if not res or raw is None:
                continue
            y = 64.0 - _np.asarray(raw).astype(float) / 1024.0
            floor = B.FR_SLOPE_FLOOR_DB_KM * res / 1000.0
            for e in (rec.get('exfo_events') or []):
                sa = e.get('SubCursorAPosition')
                ca = e.get('CursorAPosition')
                if not isinstance(sa, float) or not isinstance(ca, float):
                    continue
                i1, i2 = int(round(sa / res)), int(round(ca / res))
                if not (0 <= i1 < i2 < len(y)) or (i2 - i1) < 8:
                    continue
                checked += 1
                m, _ = _np.polyfit(_np.arange(i1, i2 + 1, dtype=float),
                                   y[i1:i2 + 1], 1)
                if m < floor:
                    touched += 1
    assert checked > 20, f'only {checked} windows checked'
    assert touched == 0, (
        f'{touched} of {checked} healthy windows hit the floor — it is meant '
        f'to fire on event tails, not on glass')


def test_a_reflective_event_still_gets_its_position_and_slope_from_the_block():
    """FR stores NO loss for a reflective event — 0 of 1,713 in the Zayo
    corpus.  The upgrade used to bail on the whole record when the block's
    Loss was NaN, which silently left every reflective event's distance on the
    quantized time-of-travel.  Loss stays absent; position and slope must not."""
    s = sr.parse_sor_full(os.path.join(FIX, 'frsilent', 'SEANOR109_1550.sor'),
                          trim=False)
    d = B.parse_bdr(SEANOR)['a']
    refl = [(a, b) for a, b in zip(s['events'], d['events'])
            if a.get('is_reflective')]
    assert refl, 'fixture carries no reflective event'
    for a, b in refl:
        assert a['dist_km'] == b['dist_km']
        assert a['slope'] == b['slope']
        assert a['reflection'] == b['reflection']


def test_the_upgrade_is_skipped_when_the_lists_do_not_line_up():
    """The guard is what makes this safe on a file whose proprietary block is
    truncated or differently populated: no 1:1 alignment, no upgrade, and the
    KeyEvents values stand.  Pinned by construction rather than by fixture —
    a mismatched length must leave the events untouched."""
    s = sr.parse_sor_full(os.path.join(FIX, 'frsilent', 'SEANOR109_1550.sor'),
                          trim=False)
    ev = [e for e in (s.get('exfo_events') or []) if not e.get('_is_section')]
    assert len(ev) == len(s['events']), (
        'this fixture aligns 1:1 — if that stops being true the guard above '
        'is silently disabling the upgrade and the other tests are vacuous')


# ── the 0.500 dB/km ceiling ────────────────────────────────────────────────
# The other end of the same rule.  Found by inverting FR's own answers on the
# records the floor did not explain: every silent-side record whose window
# fits STEEPER than 0.5 dB/km implies a slope of 0.500000, five of them to
# within 1e-12 dB/km.  Physically the mirror of the floor -- 0.5 is ~2.6x real
# SMF, so a window fitting that steep is sitting on an event, not on fiber.

F228 = os.path.join(BDR, 'ORPVL.ZYO-OR-DES-0048.1550.0228_1550.bdr')


def test_slope_ceiling_reproduces_fastreporter():
    """f0228 A at 14.635 km: a 31-sample window squeezed against a neighbour
    fits at 0.900 dB/km.  Plain OLS returned -0.0372; FR stores -0.014295."""
    rec = B.parse_bdr(F228)['a']
    got = B.measure_fr_exact_loss(rec, 14635.4, 14671.1, 14595.8, 19670.8)
    assert got is not None
    assert abs(got - (-0.014295)) < 5e-4, f'{got!r} vs FR -0.014295'


def test_the_ceiling_is_the_value_read_off_fastreporter():
    assert B.FR_SLOPE_CEIL_DB_KM == 0.500


def test_the_band_brackets_real_fiber_by_a_wide_margin():
    """Both edges are sanity limits, not tuning.  Real SMF at 1550 nm runs
    ~0.19 dB/km, so the band has to sit comfortably either side of that or it
    would be clamping ordinary glass."""
    assert B.FR_SLOPE_FLOOR_DB_KM < 0.19 < B.FR_SLOPE_CEIL_DB_KM
    assert B.FR_SLOPE_FLOOR_DB_KM < 0.19 / 1.5
    assert B.FR_SLOPE_CEIL_DB_KM > 0.19 * 2.0


# ── FR's own synthetic value, read rather than re-derived ──────────────────
# Where one direction detected an event and the other did not, FR synthesises
# a value for the silent side so it has two numbers to average.  We can
# reproduce that synthesis from the trace for 99.4% of records.  The rest are
# squeezed between the silent side's own neighbouring event and the projected
# position, and FR's stored figure there is NOT the 4-point of the cursors it
# stored beside it — driving FR's own Markers tab to those cursors returns OUR
# number, not its stored one, so no fit will ever reproduce it.
#
# When the input is a .bdr, FR's answer is already in the file.

F355 = os.path.join(BDR, 'ORPVL.ZYO-OR-DES-0048.1550.0355_1550.bdr')


def test_parse_bdr_exposes_fastreporters_own_synthetic_values():
    d = B.parse_bdr(F355)
    legs = [z for side in ('a', 'b') for z in (d[side].get('fr_synthetic') or [])]
    assert legs, 'no synthetic legs exposed'
    for z in legs:
        assert isinstance(z['loss'], float) and not np.isnan(z['loss'])
        assert isinstance(z['position_m'], float)
        assert len(z['cursors_m']) == 4


def test_a_sor_has_no_synthetic_legs_so_the_reconstruction_still_runs():
    """The read-through must not change the .sor path: FR never ran there, so
    there is nothing to read and the transplant has to do the work."""
    s = sr.parse_sor_full(os.path.join(FIX, 'frsilent', 'SEANOR109_1550.sor'),
                          trim=False)
    assert not s.get('fr_synthetic')


def test_the_engine_returns_fastreporters_value_where_no_fit_can():
    """f0355 A at 36.886 km — an 8-sample window wedged against a neighbour
    35.7 m away.  Reconstructing it gives -0.0014; FR stores +0.094276, and
    FR's own Markers tab at those cursors reads -0.004, so the stored figure
    is not marker math at all.  On a .bdr we read it."""
    import splicereportmatchexfo as E
    d = B.parse_bdr(F355)
    fr = [z for z in d['a']['fr_synthetic']
          if abs(z['position_m'] - 36886.4) < 30.0]
    assert len(fr) == 1, fr
    assert abs(fr[0]['loss'] - 0.094276) < 1e-5, fr[0]['loss']
    # and the reconstruction genuinely disagrees, which is why reading matters
    sa, ca, cb, sb = fr[0]['cursors_m']
    rebuilt = B.measure_fr_exact_loss(d['a'], ca, cb, sa, sb)
    assert rebuilt is not None
    assert abs(rebuilt - fr[0]['loss']) > 0.05, (
        'fixture no longer exercises the unreachable case')


def test_the_match_tolerance_is_frs_own_pulse_plus_20():
    """Half a pulse was too tight: our projection constant lands 4-11 samples
    short of FR's stored cursor, which is imprecision in the projection, not a
    different event.  FR's own event-matching tolerance is pulse + 20 m."""
    import splicereportmatchexfo as E
    d = B.parse_bdr(F355)
    pulse = E._pulse_length_m(d['a'])
    assert 9.0 < pulse < 11.0, pulse          # 100 ns in glass
    assert pulse + 20.0 > 14.1                # covers the worst observed gap
