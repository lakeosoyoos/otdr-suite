"""Viewer FR event table: the FLIPPED B row must read its own frame.

WHY THIS FILE EXISTS.  The FR-table layout was validated cell-by-cell against
Zach's FR3 screenshots of a SINGLE A-direction trace (WSC_SUI_0001) — see
``test_viewer_ab_frame.py``.  Every one of those assertions passed while the
mirrored B row printed wrong numbers, because the flipped path was never
exercised.  Everything here therefore runs a FLIPPED trace.

THE RULE.  An SR-4731 event's stored ``slope`` describes the section that ENDS
at that event, in the trace's OWN frame.  Measured on WSC_SUI F71 A:

    section 0 -> 1.0376 km (the launch reel)  measures  0.189 dB/km
    event #2 (at 1.0376) stores slope 0.192
    event #1 (the OTDR port, at 0) stores slope 0.000

THE DEFECT.  ``renderFastReporterGrid`` builds its columns in DISPLAYED km, and
stacked mode mirrors a B trace (``dispKm``).  So for a flipped B row the column
at i+1 holds the SMALLER own-frame distance, and ``const att = b.slope`` read
the slope of the section on the far side of the marker.  Measured on WSC<->SUI,
288 sampled fibers / 2,609 B-direction sections, against the value the file
itself carries:

    Section Loss error   median 17 mdB   p90 80 mdB   max 20.9 dB
    21.0% of the sections it printed were off by >= 50 mdB
    282 further sections printed BLANK (B's port event stores slope 0.000)
    worst single cell: F453 49.3287 -> 65.0032 km, att 1.522 vs 0.189

Live on F71, section 54.8116 -> 60.4858 km of the B row:

    before   Att 0.207   Loss 1.176      after   Att 0.184   Loss 1.045
    (the file: event #4 at own-frame 11.2782 km stores slope 0.184)

and the B row's last display-order section, 65.0861 -> 66.0917 km, went from
blank to Length 1.0044 / Loss 0.188 / Att 0.187.  The A row is byte-identical
before and after — verified across all 2,521 sampled A-direction sections.

The rule keys on ``isFlipped``, NOT on ``t.dir === 'b'``: unstack the traces and
B is drawn in its own raw frame, where the plain rule is the right one again.
Both states are covered below.

Everything here is synthetic — CI has no .sor files.  The real-fiber checks at
the bottom skip when the WSC<->SUI set is not on disk.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

import pytest      # noqa: E402


def _viewer_src():
    return open(os.path.join(ROOT, 'viewer', 'viewer.html'),
                encoding='utf-8').read()


# ─── the grid model, with its one decision READ OUT OF THE SHIPPED SOURCE ──
#
# There is no JS engine in this repo's test environment, so the clustering and
# section arithmetic below are a Python mirror of renderFastReporterGrid —
# validated cell-for-cell against the RENDERED table on WSC_SUI F71 (all 13
# sections of both rows, both before and after the fix) before being written
# down here.  The one thing the mirror does NOT hardcode is the choice this
# file is about: which bounding event's slope the section takes.  That is
# parsed out of viewer.html, so reverting the fix flips the model back and the
# numbers below fail.

def att_rule_from_source(src=None):
    """Return 'flip-aware' | 'always-b' | 'always-a' for section()'s `att`."""
    src = src if src is not None else _viewer_src()
    i = src.index('function section(ti, i)')
    body = src[i:src.index('\n  }', i)]
    m = re.search(r'const att = (.+?);', body)
    assert m, 'section() no longer assigns `const att` — update this test'
    expr = ' '.join(m.group(1).split())
    if expr == 'isFlipped(traces[ti]) ? a.slope : b.slope':
        return 'flip-aware'
    if expr == 'b.slope':
        return 'always-b'
    if expr == 'a.slope':
        return 'always-a'
    raise AssertionError(
        "section()'s attenuation rule changed shape (%r) — this test models it, "
        "so teach the model the new form rather than deleting the check" % expr)


TOL = 0.20                      # renderFastReporterGrid's column tolerance


def disp_km(t, km):
    """dispKm: a flipped trace mirrors about far connector + A's launch reel."""
    return (t['far_conn_km'] + t['launch_a_km'] - km) if t['flipped'] else km


def build_columns(traces):
    items = []
    for ti, t in enumerate(traces):
        for e in t['events']:
            items.append((disp_km(t, e['dist_km']), ti, e))
    items.sort(key=lambda x: x[0])
    cols = []
    for km, ti, e in items:
        last = cols[-1] if cols else None
        if last and abs(km - last['km']) <= TOL and last['ev'][ti] is None:
            last['ev'][ti] = e
            last['km'] = (last['km'] * last['n'] + km) / (last['n'] + 1)
            last['n'] += 1
        else:
            ev = [None] * len(traces)
            ev[ti] = e
            cols.append({'km': km, 'n': 1, 'ev': ev})
    return cols


def section(cols, traces, ti, i, rule):
    a = cols[i]['ev'][ti]
    b = cols[i + 1]['ev'][ti]
    if a is None or b is None:
        return None
    length = abs(b['dist_km'] - a['dist_km'])
    if rule == 'always-b':
        att = b['slope']
    elif rule == 'always-a':
        att = a['slope']
    else:
        att = (a if traces[ti]['flipped'] else b)['slope']
    if not length > 0 or att is None or att == 0:
        return {'len': length, 'att': None, 'loss': None}
    return {'len': length, 'att': att, 'loss': length * att}


def terminating_slope(a, b):
    """What the FILE says, read with no frame logic at all: the slope belongs
    to the section ENDING at the event, so it is the event further out."""
    return (b if b['dist_km'] > a['dist_km'] else a)['slope']


# ─── a flipped B trace, shaped like the span that found this ──────────────
#
# Every section gets its OWN attenuation, so an off-by-one lands on a different
# number instead of hiding inside a cable whose sections all measure ~0.185.

def ev(km, slope, refl=False, end=False, tot=1):
    return {'dist_km': km, 'slope': slope, 'is_reflective': refl,
            'is_end': end, 'time_of_travel': tot, 'splice_loss': 0.1}


LAUNCH_A, CABLE, TAIL_B = 1.0376, 64.05, 1.0376


def a_trace():
    """A: port, launch connector, three splices, far connector, end."""
    return {
        'flipped': False, 'launch_a_km': LAUNCH_A,
        'far_conn_km': LAUNCH_A + CABLE,
        'events': [
            ev(0.0, 0.000, refl=True, tot=0),
            ev(LAUNCH_A, 0.192, refl=True),
            ev(LAUNCH_A + 16.0, 0.181),
            ev(LAUNCH_A + 32.0, 0.202),
            ev(LAUNCH_A + 48.0, 0.174),
            ev(LAUNCH_A + CABLE, 0.199, refl=True),
            ev(LAUNCH_A + CABLE + TAIL_B, 0.190, end=True),
        ],
    }


def b_trace(flipped=True):
    """B: the same cable shot the other way, through its own reel.  Its port
    event stores slope 0.000, which is what blanked the last section."""
    return {
        'flipped': flipped, 'launch_a_km': LAUNCH_A,
        'far_conn_km': LAUNCH_A + CABLE,
        'events': [
            ev(0.0, 0.000, refl=True, tot=0),
            ev(LAUNCH_A, 0.187, refl=True),
            ev(LAUNCH_A + 16.0, 0.207),
            ev(LAUNCH_A + 32.0, 0.184),
            ev(LAUNCH_A + 48.0, 0.191),
            ev(LAUNCH_A + CABLE, 0.186, refl=True),
            ev(LAUNCH_A + CABLE + TAIL_B, 0.192, end=True),
        ],
    }


def _b_sections(rule, flipped=True):
    traces = [a_trace(), b_trace(flipped)]
    cols = build_columns(traces)
    out = []
    for i in range(len(cols) - 1):
        s = section(cols, traces, 1, i, rule)
        if s is None:
            continue
        a, b = cols[i]['ev'][1], cols[i + 1]['ev'][1]
        out.append((s, terminating_slope(a, b)))
    return out


# ─── the tests ────────────────────────────────────────────────────────────

def test_a_flipped_row_takes_the_slope_of_the_event_it_ends_at():
    """The whole defect, on a mirrored trace: with the shipped rule every
    section's attenuation is the one the FILE stores for it."""
    rule = att_rule_from_source()
    got = _b_sections(rule)
    assert got, 'the fixture produced no B sections'
    for s, want in got:
        assert s['att'] == want, (
            'flipped section took %s where the file stores %s' % (s['att'], want))
        assert abs(s['loss'] - s['len'] * want) < 1e-12


def test_the_old_rule_really_does_get_this_fixture_wrong():
    """Guards the guard.  A test that passes under both rules would have let
    the bug through, which is exactly how it shipped."""
    wrong = [s['att'] for s, want in _b_sections('always-b') if s['att'] != want]
    assert wrong, 'the fixture cannot tell the two rules apart — it is useless'


def test_the_flipped_rows_last_section_is_not_blank():
    """B's port event stores slope 0.000, and the grid treats 0 as 'no data'.
    Read from the wrong end that zero blanked a real section — 282 of them on
    WSC<->SUI's 288 sampled fibers."""
    rule = att_rule_from_source()
    assert all(s['att'] is not None for s, _ in _b_sections(rule)), \
        'a flipped section still comes out blank'
    assert any(s['att'] is None for s, _ in _b_sections('always-b')), \
        'the fixture no longer reproduces the blanking'


def test_the_unflipped_row_is_untouched():
    """A is drawn in its own frame, so its sections must read exactly as they
    did before — 2,521 sampled A-direction sections were unchanged live."""
    rule = att_rule_from_source()
    traces = [a_trace(), b_trace()]
    cols = build_columns(traces)
    for i in range(len(cols) - 1):
        new = section(cols, traces, 0, i, rule)
        old = section(cols, traces, 0, i, 'always-b')
        assert new == old, 'the A row moved at column %d' % i


def test_unstacking_puts_the_b_row_back_on_the_plain_rule():
    """isFlipped folds in gStacked.  Keying the fix on `t.dir === 'b'` instead
    would break the unstacked view, where B is drawn in its own raw frame and
    the columns run the same way its events do."""
    rule = att_rule_from_source()
    for s, want in _b_sections(rule, flipped=False):
        assert s['att'] == want
    src = _viewer_src()
    body = src[src.index('function section(ti, i)'):][:600]
    assert 'isFlipped(traces[ti])' in body, \
        'the rule must key on isFlipped, not on direction'


# ─── D3: the >=3-trace flat list must speak the displayed frame ───────────

def test_the_flat_event_list_lists_displayed_km_not_raw():
    """With 3+ traces the grid gives way to a flat list, which tagged each row
    with the RAW km while the canvas drew the marker at the mirrored one and
    the sort ran on the raw value — so A and B rows interleaved in two
    different frames.  Live on F71: B event #4 printed 11.2782 km with its
    marker at 54.8116."""
    src = _viewer_src()
    fn = src[src.index('function buildEventRows'):]
    fn = fn[:fn.index('\n}')]
    assert 'km: dispKm(t, e.dist_km)' in fn, 'the flat list is back on raw km'
    assert 'km: e.dist_km' not in fn
    assert 'a.km - b.km' in fn, 'the sort no longer runs on the listed km'
    assert 'flagEvent(e, t)' in fn, \
        'flagEvent must still see the RAW event — its rules are own-frame'


def test_the_flat_list_and_the_grid_use_the_same_transform():
    """Two copies of the mirror is how the frames drifted apart in the first
    place; both call dispKm."""
    src = _viewer_src()
    grid = src[src.index('function renderFastReporterGrid'):][:1200]
    assert 'dispKm(t, e.dist_km)' in grid


# ─── D4: the Average row carries the report's verdict ────────────────────

def test_only_the_average_row_is_gate_highlighted():
    """The Average IS the number the bidirectional report judges, so printing
    it plain hid the verdict on the one cell that carries it.  Live on F71 at
    27.4555 km: A 0.154 plain, B 0.167 red, Average 0.161 plain — and 0.161 is
    the report's flagged figure (team .160, ours .1605).

    Minimum and Maximum stay plain by decision (Robert, 2026-08-19): in the
    ordinary bidirectional view exactly two traces are loaded, so those rows
    restate the A and B rows two rows lower.  Highlighting them would put
    three red cells on screen where one belongs.
    """
    src = _viewer_src()
    fn = src[src.index('const aggRow = (label, fn, gated)'):][:1000]
    assert 'gated ? lossCell(' in fn, 'the Average cell no longer goes through lossCell'

    rows = src[src.index('const aggRows = ['):][:400]
    for label, want in (('Minimum', 'false'), ('Maximum', 'false'), ('Average', 'true')):
        m = re.search(r"aggRow\('" + label + r"'.*?,\s*(true|false)\)", rows, re.S)
        assert m, f'{label} row missing from aggRows'
        assert m.group(1) == want, (
            f'{label} gating changed: expected {want}, found {m.group(1)}'
        )


def test_a_highlighted_aggregate_is_actually_readable():
    """`tr.fr-agg td` sets secondary grey at higher specificity than the
    verdict colours, so a highlighted Average would render grey-on-orange —
    the class would be there and the tech would not see it."""
    src = _viewer_src()
    assert 'table.fr-table tr.fr-agg td.fr-hi' in src, \
        'the aggregate highlight has no colour of its own'
    agg = src.index('table.fr-table tr.fr-agg td {')
    hi = src.index('table.fr-table tr.fr-agg td.fr-hi')
    assert hi > agg, 'the restated colour must come after the rule it beats'


def test_the_aggregate_highlight_uses_the_reports_gate():
    """Not a second threshold — the same clearsGate every other loss cell in
    the table already goes through."""
    src = _viewer_src()
    fn = src[src.index('const lossCell = (v, isBreak)'):][:400]
    assert 'clearsGate(v)' in fn


# ─── real fibers, when the span is on disk ───────────────────────────────

WSC = '/tmp/ws2/Final Testing/Long/Sacramento'
SUI = '/tmp/ws2/Final Testing/Long/Susisun'
needs_span = pytest.mark.skipif(
    not (os.path.isdir(WSC) and os.path.isdir(SUI)),
    reason='WSC<->SUI .sor set not on this machine')


def _real_f71():
    sys.path.insert(0, os.path.join(ROOT, 'viewer'))
    import trace_server as TS
    TS.CONFIG['dir_a'], TS.CONFIG['dir_b'] = WSC, SUI
    launch_a = TS.frame_facts(WSC).get('launch_km') or 0.0
    out = []
    for d, flip in (('a', False), ('b', True)):
        t = TS.load_trace(d, 71)
        out.append({'flipped': flip, 'launch_a_km': launch_a,
                    'far_conn_km': t['far_conn_km'], 'events': t['events']})
    return out


@needs_span
def test_f71_prints_the_numbers_the_file_carries():
    """The live cell from the field report, end to end."""
    traces = _real_f71()
    cols = build_columns(traces)
    rule = att_rule_from_source()
    by_km = {}
    for i in range(len(cols) - 1):
        s = section(cols, traces, 1, i, rule)
        if s:
            by_km[(round(cols[i]['km'], 4), round(cols[i + 1]['km'], 4))] = s

    hot = by_km[(54.8116, 60.4858)]
    assert round(hot['att'], 3) == 0.184 and round(hot['loss'], 3) == 1.045, hot
    assert round(section(cols, traces, 1,
                         [round(c['km'], 4) for c in cols].index(54.8116),
                         'always-b')['att'], 3) == 0.207, 'fixture drifted'

    tail = by_km[(65.0861, 66.0917)]
    assert tail['att'] is not None and round(tail['att'], 3) == 0.187
    assert round(tail['loss'], 3) == 0.188


@needs_span
def test_no_b_section_on_f71_disagrees_with_the_file():
    traces = _real_f71()
    cols = build_columns(traces)
    rule = att_rule_from_source()
    for i in range(len(cols) - 1):
        s = section(cols, traces, 1, i, rule)
        if s is None:
            continue
        a, b = cols[i]['ev'][1], cols[i + 1]['ev'][1]
        assert s['att'] == terminating_slope(a, b), \
            'column %d (%.4f km) disagrees with the file' % (i, cols[i]['km'])
