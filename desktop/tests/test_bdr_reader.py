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
        # First event is the launch, last is the end of fibre.
        assert d['events'][0]['dist_km'] == pytest.approx(0.0, abs=0.001)
        assert d['events'][-1]['is_end']
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
        # The .bdr carries float metres; the .sor derives distance from an
        # integer time-of-travel, so it quantizes.  They agree to a few
        # metres at 110 km — three orders of magnitude inside the 250 m
        # closure-clustering gap.
        assert be['dist_km'] == pytest.approx(se['dist_km'], abs=0.005)
        assert be['splice_loss'] == pytest.approx(se['splice_loss'], abs=5e-4)
        assert be['is_end'] == se['is_end']


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


@pytest.mark.parametrize('fiber,km', [
    (19, 40.2974),     # B's own closure 30.6 m away, inside the window
    (17, 21.8287),     # same shape, 33.2 m
])
def test_a_neighbour_inside_the_window_is_refused(tmp_path, fiber, km):
    """The premise check, on the two ORPVL records that found it.

    FastReporter declined to pair these events with the near neighbour the
    other direction detected, and transplanted a value close to zero.  Our
    projected window lands ON that neighbour, so fitting it returns the
    neighbour's step — 82 mdB and 33 mdB wrong, enough to carry a cell over
    the .160 line.  The engine must decline, not measure."""
    hits = [(f, s, v) for f, s, v in _silent_cases(tmp_path)
            if f == fiber and abs(s['Position'] / 1000.0 - km) < 0.01]
    assert len(hits) == 1, hits
    assert hits[0][2] is None, (
        'measured %r where the silent direction has its own event inside '
        'the window' % (hits[0][2],))
