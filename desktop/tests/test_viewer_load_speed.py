"""Loading a whole cable: the numbers must not move when the work is cut.

2026-09-19, the field: "can we make selecting all the files faster?"  Selecting
every file in the FILES list loads a trace per file, and the Viewer was
spending 40 ms of every 47 on two things that were not parsing:

    [round(float(x), 5) for x in dist_km.tolist()]      23 ms per fiber
    a NUL-by-NUL walk of the proprietary block          ~5 ms per fiber

Both are gone -- a real 1152-fiber cable went from 46.5 s to 7.6 s -- and the
whole point of these tests is that the SERVED VALUES did not change while that
happened.  They pin the two things that made it safe:

  * `_round_exact` is Python's `round`, not np.round.  np.round scales, rints
    and unscales, so on a decimal midpoint it can land the other way.  Baseline
    subtraction puts the level on an exact half-mdB constantly: swapping in
    np.round moved 10,641 of ELMMIL0001's 39,173 samples by 1 mdB.
  * the proprietary-block field names found by one regex are exactly the names
    a NUL-by-NUL walk visits.

Checked while the change was made, beyond what a unit test can carry: every
trace the server serves for 30 real spans (both directions of ten of them, tie
panels, short shots, ILA reshoots), at full resolution and decimated, byte for
byte identical to the previous build.
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'viewer'))

import trace_server as TS                        # noqa: E402

# The three engines ship DIFFERENT sor_reader324802a.py copies and the suite
# imports more than one of them, so `import sor_reader324802a` here would hand
# back whichever landed in sys.modules first (it is the Splice Report's in a
# full run).  Load the VIEWER's file by path, under its own name.
import importlib.util                            # noqa: E402

_SR_PATH = os.path.join(ROOT, 'viewer', 'sor_reader324802a.py')
_spec = importlib.util.spec_from_file_location('viewer_sor_reader_under_test', _SR_PATH)
SR = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(SR)

from conftest import FIXTURE_SPLICE_A_DIR        # noqa: E402


def _signed_same(a, b):
    """Equal AND the same signed zero -- round() keeps the sign of -0.0, and
    a -0.0 that became 0.0 is a different byte in the JSON."""
    if math.isnan(a) and math.isnan(b):
        return True
    return a == b and math.copysign(1.0, a) == math.copysign(1.0, b)


def _fixture_files():
    return sorted(str(p) for p in FIXTURE_SPLICE_A_DIR.glob('*.sor'))[:4]


# ── _round_exact IS round() ──────────────────────────────────────────────

@pytest.mark.parametrize('nd', [3, 5])
def test_round_exact_matches_python_round_on_real_samples(nd):
    """The samples the Viewer actually serves, rounded both ways."""
    checked = 0
    for path in _fixture_files():
        r = SR.parse_sor_full(path, trim=False)
        assert r is not None
        y = -np.asarray(r['trace'], dtype=np.float64)
        y = y - float(np.median(y[:200]))          # the baseline step that ties them
        x = (np.arange(len(y), dtype=np.float64) * 1.7 + 12.5) / 1000.0
        for arr in (y, x):
            fast = TS._round_exact(arr, nd).tolist()
            ref = [round(float(v), nd) for v in arr.tolist()]
            assert len(fast) == len(ref)
            bad = [(i, a, b) for i, (a, b) in enumerate(zip(ref, fast))
                   if not _signed_same(a, b)]
            assert not bad, bad[:5]
            checked += len(ref)
    assert checked > 100_000                        # the test is worth its runtime


def test_round_exact_on_the_midpoints_that_break_np_round():
    """Every exact half in range, and one ulp either side of it."""
    for nd, scale in ((3, 1000.0), (5, 100000.0)):
        vals = []
        for k in range(-4000, 4000):
            mid = (k + 0.5) / scale
            vals += [mid, np.nextafter(mid, -np.inf), np.nextafter(mid, np.inf)]
        arr = np.asarray(vals, dtype=np.float64)
        fast = TS._round_exact(arr, nd).tolist()
        ref = [round(float(v), nd) for v in vals]
        bad = [(v, a, b) for v, a, b in zip(vals, ref, fast) if not _signed_same(a, b)]
        assert not bad, bad[:5]


def test_round_exact_keeps_negative_zero_and_the_specials():
    vals = [-0.0, 0.0, -1e-9, -0.0004, -0.0005, 0.0005, 1e-9,
            float('nan'), float('inf'), float('-inf')]
    fast = TS._round_exact(np.asarray(vals, dtype=np.float64), 3).tolist()
    ref = [round(float(v), 3) for v in vals]
    for v, a, b in zip(vals, ref, fast):
        assert _signed_same(a, b), (v, a, b)
    # -0.0 in, -0.0 out: `n + adj` would hand back 0.0 (IEEE: -0.0 + 0.0 = 0.0)
    assert math.copysign(1.0, fast[0]) == -1.0
    assert math.copysign(1.0, fast[3]) == -1.0


def test_round_exact_hands_out_of_range_values_to_python():
    """Past 2**53 scaled the midpoint reasoning stops holding, so it stops
    being used.  (Never a km or a dB -- this is the guard, not a path.)"""
    vals = [1e300, -1e300, 12.5, 0.25]
    fast = TS._round_exact(np.asarray(vals, dtype=np.float64), 3).tolist()
    assert fast == [round(v, 3) for v in vals]


def test_np_round_really_would_have_moved_the_levels():
    """The reason _round_exact exists rather than a one-line np.round: on a
    real trace the two disagree by a whole mdB, on thousands of samples."""
    r = SR.parse_sor_full(_fixture_files()[0], trim=False)
    y = -np.asarray(r['trace'], dtype=np.float64)
    y = y - float(np.median(y[:200]))
    ref = [round(float(v), 3) for v in y.tolist()]
    naive = np.round(y, 3).tolist()
    moved = [a for a, b in zip(ref, naive) if a != b]
    assert moved, 'fixture no longer reproduces the tie case this guards'
    assert TS._round_exact(y, 3).tolist() == ref


# ── what the cache holds, and what that buys ────────────────────────────

def test_the_cache_holds_arrays_rounded_level_and_raw_distance():
    """The shape the speed depends on, pinned so it is not tidied away.

    Level is rounded IN the cache because decimate_minmax picks each bucket's
    extremes by comparing it -- decimating raw levels chooses a different
    sample wherever rounding had made two tie.  Distance is NOT rounded there:
    it is only ever indexed, never compared, so rounding the ~2,000 that are
    sent gives the same numbers for none of the cost.
    """
    d = str(FIXTURE_SPLICE_A_DIR)
    fn = sorted(os.listdir(d))[0]
    t = TS._load_trace_cached(d, fn, os.stat(os.path.join(d, fn)).st_mtime_ns)
    assert isinstance(t['dist_km'], np.ndarray)
    assert isinstance(t['trace_db'], np.ndarray)
    ys = t['trace_db']
    assert np.array_equal(ys, TS._round_exact(ys, 3))        # already rounded
    xs = t['dist_km']
    assert not np.array_equal(xs, TS._round_exact(xs, 5))    # deliberately not


def test_a_served_trace_is_what_rounding_first_would_have_given():
    """End to end: both the full-resolution and the decimated trace match the
    values the old build produced -- full resolution rounded per sample, and
    decimation fed the rounded arrays."""
    d = str(FIXTURE_SPLICE_A_DIR)
    TS.set_dirs(d, None)
    try:
        fiber = TS.list_fibers(d)[0][0]
        fn = dict(TS.list_fibers(d))[fiber]
        raw = TS._load_trace_cached(d, fn, os.stat(os.path.join(d, fn)).st_mtime_ns)
        ref_x = [round(float(v), 5) for v in np.asarray(raw['dist_km']).tolist()]
        ref_y = [round(float(v), 3) for v in np.asarray(raw['trace_db']).tolist()]

        full = TS.load_trace('a', fiber)
        assert full['dist_km'] == ref_x
        assert full['trace_db'] == ref_y
        assert full['num_points'] == len(ref_y)
        assert 'decimated_from' not in full

        over = TS.load_trace('a', fiber, max_pts=2000)
        dx, dy = TS.decimate_minmax(ref_x, ref_y, 2000)      # the old call, verbatim
        assert over['dist_km'] == dx
        assert over['trace_db'] == dy
        assert over['num_points'] == len(dy) < len(ref_y)
        assert over['decimated_from'] == len(ref_y)
    finally:
        TS.set_dirs(None, None)


# ── the proprietary-block name scan ─────────────────────────────────────

# Fixtures whose proprietary block really carries an event table -- the
# splice fixtures decode a block but list no events, so a scan test on those
# would pass on an empty result.
_EVENT_FIXTURES = [
    os.path.join(HERE, 'fixtures', 'negtot', 'FTHNTXAD01_FTHNTXAD06_001.sor'),
    os.path.join(HERE, 'fixtures', 'launchreel', 'BARTUL063_1550.sor'),
    os.path.join(HERE, 'fixtures', 'frsilent', 'SEANOR109_1550.sor'),
    os.path.join(HERE, 'fixtures', 'satrefl', 'ELLINM0381_1550.sor'),
]


def _prop_stream(path):
    with open(path, 'rb') as fh:
        data = fh.read()
    return SR._decompress_proprietary(data, SR._parse_block_directory(data))


@pytest.mark.parametrize('path', _EVENT_FIXTURES, ids=lambda p: os.path.basename(p))
def test_the_name_regex_finds_what_a_nul_walk_finds(path):
    """_PROP_NAME_RE replaced a `stream.find(b'\\x00', pos)` loop over an 80 KB
    window -- 12,918 finds a file, nearly all of them inside RawSamples'
    binary.  Same window, same names, or the event table is not the same."""
    stream = _prop_stream(path)
    assert stream, 'fixture has no proprietary block to scan'
    et = stream.find(b'EventTable\x00')
    assert et >= 0
    end = min(len(stream) - 1, et + 80000)

    def _name_at(pos):
        z = stream.find(b'\x00', pos)
        if z < 0 or not (2 <= z - pos < 80):
            return None
        try:
            nm = stream[pos:z].decode('ascii')
        except UnicodeDecodeError:
            return None
        return nm if (nm.isprintable() and nm[0].isalpha()) else None

    walked, pos = [], et                         # the loop that used to be here
    while pos < end:
        z = stream.find(b'\x00', pos)
        if z < 0:
            break
        nm = _name_at(pos)
        if nm is not None:
            walked.append((pos, nm))
        pos = z + 1

    heads = [m.start() for m in
             SR._PROP_NAME_RE.finditer(stream, et, min(len(stream), end + 80))]
    if not heads or heads[0] != et:              # as the parser does: the run at
        heads.insert(0, et)                      # et_idx has no NUL in front
    found = []
    for pos in heads:
        if pos >= end:
            break
        nm = _name_at(pos)
        if nm is not None:
            found.append((pos, nm))

    assert found == walked
    assert len(walked) > 20                       # the fixture really exercises it
    assert any(nm == 'Position' for _off, nm in walked)


@pytest.mark.parametrize('path', _EVENT_FIXTURES, ids=lambda p: os.path.basename(p))
def test_the_block_still_yields_its_events(path):
    """What the scan is FOR: the truck's own event list, the one the FR table
    and the near-splice work read.  Compared old-parser-to-new across 84 real
    files (60 of them carrying these events) when the scan was replaced; this
    keeps a fixture-sized version of that in the suite."""
    r = SR.parse_sor_full(path, trim=False)
    ev = r.get('exfo_events') or []
    assert ev, 'fixture stopped yielding proprietary events'
    assert all(isinstance(e.get('Position'), float) for e in ev)
    # METRES, not kilometres.  This read `<= 500` and passed only because the
    # parser applied the same wrong unit and had already discarded every event
    # past half a kilometre -- the assertion was agreeing with the bug rather
    # than testing for it.  A fixture here runs to 110 km.
    assert all(-1.0 <= e['Position'] <= 500_000.0 for e in ev)
    assert any(e['Position'] > 500.0 for e in ev), \
        'past-500 m events dropped again: the km/m unit bug is back'
    assert any('CurveLevel' in e for e in ev)     # real events, not only sections
