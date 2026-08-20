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


# ─── the COLUMN CLUSTERER, its three decisions also read out of source ────
#
# D2.  The clusterer used a flat 200 m tolerance, matched greedily against the
# LAST column only, on displayed km alone, with no event-type test.  At the
# Suisun end the reflective box connector and the non-reflective ILA splice sit
# 79-90 m apart — well inside 200 m — so when B resolved the ILA splice and A
# did not (the common case: A stores it on 137/1152, B on 329/1152), B's splice
# opened the column at the lower display km and A's far connector joined it,
# leaving B's own connector orphaned.  Measured over all 1,152 WSC<->SUI
# fibers: 259 married a connector to a splice; with the rules below, 0.
#
# All three decisions are parsed out of viewer.html so reverting any of them
# flips this model back and the numbers below fail.

C_KM_PER_S = 299792.458


def tol_rule_from_source(src=None):
    """The column tolerance rule: {'pulses', 'min_km', 'max_km'}, or
    {'flat': x} if it has gone back to a constant."""
    src = src if src is not None else _viewer_src()
    m = re.search(r'const TOL = ([^;]+);', src)
    assert m, 'renderFastReporterGrid no longer assigns `const TOL`'
    expr = ' '.join(m.group(1).split())
    if expr != 'columnTolKm(traces)':
        try:
            return {'flat': float(expr)}
        except ValueError:
            raise AssertionError(
                "the column tolerance changed shape (%r) — this test models it, "
                "so teach the model the new form rather than deleting the check"
                % expr)
    consts = {}
    for name in ('TOL_PULSES', 'TOL_MIN_KM', 'TOL_MAX_KM'):
        c = re.search(r'const ' + name + r'\s*=\s*([0-9.]+)\s*;', src)
        assert c, '%s is gone from viewer.html' % name
        consts[name] = float(c.group(1))
    body = src[src.index('function columnTolKm'):]
    body = ' '.join(body[:body.index('\n}')].split())
    assert 'return Math.min(TOL_MAX_KM, Math.max(TOL_MIN_KM, TOL_PULSES * widest));' in body, (
        'columnTolKm no longer clamps TOL_PULSES x widest — update this model')
    assert 'if (!widest) return TOL_MAX_KM;' in body, \
        'columnTolKm lost its no-pulse-width fallback'
    return {'pulses': consts['TOL_PULSES'], 'min_km': consts['TOL_MIN_KM'],
            'max_km': consts['TOL_MAX_KM']}


def gate_from_source(src=None):
    """'reflectivity' when columnsMayMerge refuses to mix event types."""
    src = src if src is not None else _viewer_src()
    i = src.index('function columnsMayMerge')
    body = src[i:src.index('\n}', i)]
    return 'reflectivity' if 'p.refl !== q.refl' in body else 'none'


def strategy_from_source(src=None):
    """'mnn' | 'greedy-last' — how events are assigned to columns."""
    src = src if src is not None else _viewer_src()
    i = src.index('function renderFastReporterGrid')
    body = src[i:i + 4000]
    if 'clusterColumns(traces, items, TOL)' in body:
        cl = src[src.index('function clusterColumns'):]
        cl = cl[:cl.index('\n}\n')]
        assert 'if (best === null || d < best.d) best = { d, i, j };' in cl, (
            'clusterColumns no longer takes the CLOSEST mergeable pair — that '
            'is the mutual-nearest-neighbour rule this model mirrors')
        return 'mnn'
    if 'cols[cols.length - 1]' in body:
        return 'greedy-last'
    raise AssertionError('the clustering strategy changed shape — model it')


def pulse_len_km(t):
    """c*t/2n — one pulse expressed as a length of fiber."""
    ns = t.get('pulse_ns')
    ior = t.get('ior') or 1.4682
    if not ns or ns <= 0:
        return None
    return C_KM_PER_S * (ns * 1e-9) / 2 / ior


def column_tol_km(traces, rule=None):
    rule = rule if rule is not None else tol_rule_from_source()
    if 'flat' in rule:
        return rule['flat']
    widest = 0.0
    for t in traces:
        p = pulse_len_km(t)
        if p is not None and p > widest:
            widest = p
    if not widest:
        return rule['max_km']
    return min(rule['max_km'], max(rule['min_km'], rule['pulses'] * widest))


def disp_km(t, km):
    """dispKm: a flipped trace mirrors about far connector + A's launch reel."""
    return (t['far_conn_km'] + t['launch_a_km'] - km) if t['flipped'] else km


def _may_merge(p, q, tol, gate):
    if gate == 'reflectivity' and p['refl'] != q['refl']:
        return False
    if any(p['ev'][k] is not None and q['ev'][k] is not None
           for k in range(len(p['ev']))):
        return False
    return abs(p['km'] - q['km']) <= tol


def build_columns(traces, tol=None, strategy=None, gate=None):
    """Python mirror of renderFastReporterGrid's clustering."""
    tol = column_tol_km(traces) if tol is None else tol
    strategy = strategy_from_source() if strategy is None else strategy
    gate = gate_from_source() if gate is None else gate

    if strategy == 'greedy-last':
        items = []
        for ti, t in enumerate(traces):
            for e in t['events']:
                items.append((disp_km(t, e['dist_km']), ti, e))
        items.sort(key=lambda x: x[0])
        cols = []
        for km, ti, e in items:
            last = cols[-1] if cols else None
            if (last and abs(km - last['km']) <= tol and last['ev'][ti] is None
                    and not (gate == 'reflectivity'
                             and last['refl'] != bool(e['is_reflective']))):
                last['ev'][ti] = e
                last['km'] = (last['km'] * last['n'] + km) / (last['n'] + 1)
                last['n'] += 1
            else:
                ev = [None] * len(traces)
                ev[ti] = e
                cols.append({'km': km, 'n': 1, 'ev': ev,
                             'refl': bool(e['is_reflective'])})
        return cols

    cols = []
    for ti, t in enumerate(traces):
        for e in t['events']:
            ev = [None] * len(traces)
            ev[ti] = e
            cols.append({'km': disp_km(t, e['dist_km']), 'n': 1, 'ev': ev,
                         'refl': bool(e['is_reflective'])})
    cols.sort(key=lambda c: c['km'])
    while True:
        best = None
        for i in range(len(cols) - 1):
            for j in range(i + 1, len(cols)):
                d = cols[j]['km'] - cols[i]['km']
                if d > tol:
                    break
                if not _may_merge(cols[i], cols[j], tol, gate):
                    continue
                if best is None or d < best[0]:
                    best = (d, i, j)
        if best is None:
            break
        _d, i, j = best
        a, b = cols[i], cols[j]
        a['km'] = (a['km'] * a['n'] + b['km'] * b['n']) / (a['n'] + b['n'])
        a['n'] += b['n']
        for k in range(len(a['ev'])):
            if a['ev'][k] is None:
                a['ev'][k] = b['ev'][k]
        cols.pop(j)
        cols.sort(key=lambda c: c['km'])
    return cols


def mixed_type_columns(cols):
    """Columns that married a reflective event to a non-reflective one."""
    out = []
    for c in cols:
        present = [e for e in c['ev'] if e]
        if len(present) > 1 and len({bool(e['is_reflective'])
                                     for e in present}) > 1:
            out.append(c)
    return out


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

def ev(km, slope, refl=False, end=False, tot=1, loss=0.1):
    return {'dist_km': km, 'slope': slope, 'is_reflective': refl,
            'is_end': end, 'time_of_travel': tot, 'splice_loss': loss}


LAUNCH_A, CABLE, TAIL_B = 1.0376, 64.05, 1.0376

# The acquisition these fixtures stand in for: WSC<->SUI long, 500 ns at
# IOR 1.47 = 51.0 m of fiber per pulse, so the grid's tolerance is 102.0 m.
PULSE_NS, IOR = 500.0, 1.47

# End-of-fiber events are REFLECTIVE on this span (`1E9999LS`; `is_end` does
# not imply non-reflective) — it matters now that the clusterer refuses to mix
# event types, because a flipped B trace meets A's end event with its own port
# connector.


def a_trace():
    """A: port, launch connector, three splices, far connector, end."""
    return {
        'flipped': False, 'launch_a_km': LAUNCH_A,
        'far_conn_km': LAUNCH_A + CABLE,
        'pulse_ns': PULSE_NS, 'ior': IOR,
        'events': [
            ev(0.0, 0.000, refl=True, tot=0),
            ev(LAUNCH_A, 0.192, refl=True),
            ev(LAUNCH_A + 16.0, 0.181),
            ev(LAUNCH_A + 32.0, 0.202),
            ev(LAUNCH_A + 48.0, 0.174),
            ev(LAUNCH_A + CABLE, 0.199, refl=True),
            ev(LAUNCH_A + CABLE + TAIL_B, 0.190, refl=True, end=True),
        ],
    }


def b_trace(flipped=True):
    """B: the same cable shot the other way, through its own reel.  Its port
    event stores slope 0.000, which is what blanked the last section."""
    return {
        'flipped': flipped, 'launch_a_km': LAUNCH_A,
        'far_conn_km': LAUNCH_A + CABLE,
        'pulse_ns': PULSE_NS, 'ior': IOR,
        'events': [
            ev(0.0, 0.000, refl=True, tot=0),
            ev(LAUNCH_A, 0.187, refl=True),
            ev(LAUNCH_A + 16.0, 0.207),
            ev(LAUNCH_A + 32.0, 0.184),
            ev(LAUNCH_A + 48.0, 0.191),
            ev(LAUNCH_A + CABLE, 0.186, refl=True),
            ev(LAUNCH_A + CABLE + TAIL_B, 0.192, refl=True, end=True),
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


# ─── D2: a connector is never the same event as a splice ─────────────────
#
# The fixtures below are the REAL event tables of WSC<->SUI F2 and F12, copied
# out of the .sor files, so CI exercises the exact geometry that found this
# without needing the span on disk.  `far_conn_km` is each B trace's own, and
# `launch_a_km` is the A folder's population median (1.0363) — the two numbers
# `dispKm` mirrors about.
#
# At this end the Suisun box connector and the ILA splice sit 79-90 m apart.
# F2: B resolves the splice, A does not (A's 65.0229 event is the merged
#     connector+splice).  The old rule opened the column on B's splice and let
#     A's connector join it.
# F12: the mirror case — A resolves the splice, B does not, and B's connector
#     lands between A's two events, so A's connector was the one orphaned.

LAUNCH_A_POP = 1.0363          # frame_facts(Sacramento)['launch_km']


def _evs(rows):
    """(number, km, type, loss, slope) -> the grid's event dicts."""
    out = []
    for num, km, typ, loss, slope in rows:
        out.append({'number': num, 'dist_km': km, 'type': typ,
                    'splice_loss': loss, 'slope': slope,
                    'is_reflective': typ[:1] in ('1', '2'),
                    'is_end': typ[1:2] == 'E',
                    'time_of_travel': 0 if km == 0.0 else 1})
    return out


F2_A = _evs([
    (1, 0.0, '1F9999LS', 0.0, 0.0), (2, 1.0376, '1F9999LS', 0.175, 0.194),
    (3, 6.6359, '0F9999LS', -0.092, 0.184), (4, 12.3413, '0F9999LS', 0.074, 0.184),
    (5, 16.7133, '0F9999LS', 0.129, 0.185), (6, 32.7664, '0F9999LS', 0.042, 0.183),
    (7, 38.018, '0F9999LS', 0.039, 0.182), (8, 43.8483, '0F9999LS', 0.075, 0.19),
    (9, 49.3293, '0F9999LS', 0.065, 0.189), (10, 54.8282, '0F9999LS', -0.071, 0.195),
    (11, 60.5107, '0F9999LS', 0.075, 0.183), (12, 65.0229, '1F9999LS', 0.402, 0.195),
    (13, 66.0962, '1E9999LS', 0.0, 0.176)])
F2_B = _evs([
    (1, 0.0, '1F9999LS', 0.0, 0.0), (2, 1.0044, '1F9999LS', 0.083, 0.191),
    (3, 1.0962, '0F9999LS', 0.286, 1.226), (4, 5.5805, '0F9999LS', -0.05, 0.197),
    (5, 11.2807, '0F9999LS', 0.131, 0.183), (6, 33.2865, '0F9999LS', 0.111, 0.186),
    (7, 38.64, '0F9999LS', 0.084, 0.187), (8, 53.7728, '0F9999LS', -0.057, 0.185),
    (9, 59.4552, '0F9999LS', 0.151, 0.183), (10, 65.0561, '1F9999LS', 0.128, 0.184),
    (11, 66.058, '1E9999LS', 0.0, 0.179)])
F2_B_FAR = 65.0561

F12_A = _evs([
    (1, 0.0, '1F9999LS', 0.0, 0.0), (2, 1.0376, '1F9999LS', 0.252, 0.193),
    (3, 12.3107, '0F9999LS', -0.061, 0.184), (4, 21.9675, '0F9999LS', 0.043, 0.188),
    (5, 27.4613, '0F9999LS', 0.082, 0.183), (6, 32.769, '0F9999LS', 0.05, 0.183),
    (7, 38.0511, '0F9999LS', -0.13, 0.182), (8, 43.8356, '0F9999LS', 0.062, 0.185),
    (9, 54.8231, '0F9999LS', 0.19, 0.189), (10, 60.5056, '0F9999LS', -0.172, 0.181),
    (11, 64.9847, '0F9999LS', 0.176, 0.189), (12, 65.0892, '1F9999LS', 0.175, 0.0),
    (13, 66.0962, '1E9999LS', 0.0, 0.186)])
F12_B = _evs([
    (1, 0.0, '1F9999LS', 0.0, 0.0), (2, 1.0044, '1F9999LS', 0.189, 0.191),
    (3, 5.5856, '0F9999LS', 0.203, 0.19), (4, 11.268, '0F9999LS', -0.095, 0.182),
    (5, 16.7822, '0F9999LS', 0.047, 0.189), (6, 22.2607, '0F9999LS', -0.051, 0.187),
    (7, 28.0527, '0F9999LS', 0.192, 0.187), (8, 38.6375, '0F9999LS', -0.07, 0.181),
    (9, 53.7601, '0F9999LS', 0.076, 0.188), (10, 59.4552, '0F9999LS', 0.098, 0.184),
    (11, 65.0459, '1F9999LS', 0.008, 0.186), (12, 66.058, '1E9999LS', 0.0, 0.183)])
F12_B_FAR = 65.0459


def suisun_pair(a_events, b_events, b_far):
    return [
        {'flipped': False, 'launch_a_km': LAUNCH_A_POP, 'far_conn_km': 0.0,
         'pulse_ns': PULSE_NS, 'ior': IOR, 'events': a_events},
        {'flipped': True, 'launch_a_km': LAUNCH_A_POP, 'far_conn_km': b_far,
         'pulse_ns': PULSE_NS, 'ior': IOR, 'events': b_events},
    ]


OLD_RULE = dict(tol=0.20, strategy='greedy-last', gate='none')


def _cell(cols, ti, want_km):
    """The event this trace contributes to the column nearest `want_km`."""
    c = min(cols, key=lambda c: abs(c['km'] - want_km))
    return c, c['ev'][ti]


def test_f2_connector_no_longer_marries_bs_ila_splice():
    """The worked example.  Old rule: one column at ~65.010 holding A#12
    (65.0229, reflective, loss 0.402) and B#3 (1.0962, non-reflective, loss
    0.286) — a box connector and an ILA splice, 79-90 m apart, in one cell."""
    traces = suisun_pair(F2_A, F2_B, F2_B_FAR)

    before = build_columns(traces, **OLD_RULE)
    bad = mixed_type_columns(before)
    assert len(bad) == 1, 'the fixture no longer reproduces the defect'
    a, b = bad[0]['ev']
    assert (a['number'], b['number']) == (12, 3)
    assert round(bad[0]['km'], 3) == 65.010

    after = build_columns(traces)
    assert mixed_type_columns(after) == [], \
        'a column still mixes a reflective event with a non-reflective one'
    # each gets its own column, and A's connector finds B's connector
    _c, splice = _cell(after, 1, 64.996)
    assert splice['number'] == 3 and _c['ev'][0] is None, \
        "B's ILA splice must stand alone — A never resolved it"
    conn, a_conn = _cell(after, 0, 65.06)
    assert a_conn['number'] == 12 and conn['ev'][1]['number'] == 2, \
        "A's connector must pair with B's connector, not with a splice"


def test_f12_is_the_mirror_case_and_is_fixed_too():
    """A resolves the ILA splice, B does not, and B's connector lands BETWEEN
    A's splice and A's connector — so the old rule orphaned A's connector."""
    traces = suisun_pair(F12_A, F12_B, F12_B_FAR)

    before = build_columns(traces, **OLD_RULE)
    bad = mixed_type_columns(before)
    assert len(bad) == 1
    a, b = bad[0]['ev']
    assert (a['number'], b['number']) == (11, 2), (a['number'], b['number'])
    assert a['is_reflective'] is False and b['is_reflective'] is True
    # and A's own connector was left with no partner
    orphan, _e = _cell(before, 0, 65.0892)
    assert orphan['ev'][1] is None

    after = build_columns(traces)
    assert mixed_type_columns(after) == []
    ila, splice = _cell(after, 0, 64.9847)
    assert splice['number'] == 11 and ila['ev'][1] is None
    conn, a_conn = _cell(after, 0, 65.08)
    assert a_conn['number'] == 12 and conn['ev'][1]['number'] == 2


def test_the_type_test_is_what_separates_them_not_the_tolerance():
    """The connector and the splice are 79-90 m apart — INSIDE the new
    tolerance too.  Distance alone was never going to tell them apart."""
    traces = suisun_pair(F2_A, F2_B, F2_B_FAR)
    tol = column_tol_km(traces)
    gap = abs(disp_km(traces[0], 65.0229) - disp_km(traces[1], 1.0962))
    assert gap < tol, 'the fixture stopped exercising the type test'
    ungated = build_columns(traces, gate='none')
    assert mixed_type_columns(ungated), \
        'without the type test the tolerance alone should still mis-pair'


# ─── D2 part 2: mutual nearest neighbour, not greedy-against-the-last ────

def test_an_event_joins_the_column_it_is_closest_to():
    """Three same-type events, so the type test cannot help: A at 10.000 and
    10.150, B at 10.100.  Greedy-against-the-last hands B to A@10.000 (100 m)
    simply because that column opened first; B's true partner is A@10.150,
    50 m away."""
    A = {'flipped': False, 'launch_a_km': 0.0, 'far_conn_km': 0.0,
         'pulse_ns': PULSE_NS, 'ior': IOR,
         'events': [ev(10.000, 0.185, loss=0.05),
                    ev(10.150, 0.185, loss=0.31)]}
    B = {'flipped': False, 'launch_a_km': 0.0, 'far_conn_km': 0.0,
         'pulse_ns': PULSE_NS, 'ior': IOR,
         'events': [ev(10.100, 0.185, loss=0.29)]}

    old = build_columns([A, B], **OLD_RULE)
    paired = [c for c in old if c['ev'][0] and c['ev'][1]]
    assert len(paired) == 1 and paired[0]['ev'][0]['dist_km'] == 10.000, \
        'the fixture no longer distinguishes the two strategies'

    new = build_columns([A, B])
    paired = [c for c in new if c['ev'][0] and c['ev'][1]]
    assert len(paired) == 1, new
    assert paired[0]['ev'][0]['dist_km'] == 10.150, \
        'the event joined the column that opened first, not the nearest one'


# ─── D2 part 3: the tolerance comes from the pulse width ────────────────

def test_the_tolerance_is_two_pulse_widths():
    """500 ns at IOR 1.47 is 51.0 m of fiber, so the column tolerance is
    102.0 m — not the flat 200 m it replaced.

    Two, because A and B are independent acquisitions and each localizes the
    event to about a pulse.  Measured over all 1,152 WSC<->SUI fibers and
    10,876 A/B correspondences: median 16.5 m, p90 44.7 m, p99 88.0 m,
    max 116.0 m — two pulse widths keeps 99.88% of them."""
    rule = tol_rule_from_source()
    assert 'flat' not in rule, 'the tolerance went back to a constant'
    assert rule['pulses'] == 2
    assert round(pulse_len_km({'pulse_ns': 500.0, 'ior': 1.47}), 4) == 0.0510
    assert round(column_tol_km([a_trace(), b_trace()]), 5) == 0.10197


def test_the_coarsest_loaded_trace_governs():
    """A 500 ns leg paired with a 10 ns one is only as certain as the 500 ns."""
    a, b = a_trace(), b_trace()
    b['pulse_ns'] = 10.0
    assert round(column_tol_km([a, b]), 5) == round(column_tol_km([a, a]), 5)


def test_a_tiny_pulse_stops_at_the_floor():
    """The short shots are 10 ns = 1.0 m per pulse, and 2 m is NOT the A<->B
    position uncertainty: the mirror frame is built from a folder-median
    launch offset, and the launch connector's own position varies 3-5 m
    fiber-to-fiber inside one folder (measured on the WSC<->SUI short set).
    That error does not shrink with the pulse."""
    rule = tol_rule_from_source()
    t = {'pulse_ns': 10.0, 'ior': 1.47}
    assert pulse_len_km(t) < rule['min_km']
    assert column_tol_km([t]) == rule['min_km']


def test_a_very_long_pulse_stops_at_the_old_flat_tolerance():
    """2500 ns is 255 m per pulse.  Uncapped this would MARRY events the old
    rule already kept apart, so the ceiling is the old flat 200 m: this
    change can only ever tighten."""
    rule = tol_rule_from_source()
    assert rule['max_km'] == 0.20, 'the ceiling is meant to be the old rule'
    assert column_tol_km([{'pulse_ns': 2500.0, 'ior': 1.47}]) == 0.20


def test_no_pulse_width_falls_back_instead_of_guessing():
    """A file that carries no pulse width gets the old behaviour, not a
    number the trace cannot support."""
    assert column_tol_km([{'pulse_ns': None, 'ior': 1.47}]) == \
        tol_rule_from_source()['max_km']


def test_the_grid_actually_asks_for_the_pulse_derived_tolerance():
    """Source-level, because a constant left behind would keep this file's
    model honest while the screen still used 0.20."""
    src = _viewer_src()
    fn = src[src.index('function renderFastReporterGrid'):][:400]
    assert 'const TOL = columnTolKm(traces);' in fn
    assert 'const TOL = 0.20' not in src


def test_the_server_ships_the_pulse_width_to_the_page():
    """The tolerance is only pulse-derived if the payload carries a pulse."""
    ts = open(os.path.join(ROOT, 'viewer', 'trace_server.py'),
              encoding='utf-8').read()
    assert "'pulse_ns': pulse_ns," in ts and "'ior': round(float(ior), 5)," in ts
    rd = open(os.path.join(ROOT, 'viewer', 'sor_reader324802a.py'),
              encoding='utf-8').read()
    assert "'fxd_pulse_ns': pulse_ns," in rd, \
        'the viewer reader no longer parses FxdParams pulse width'


# ─── real fibers, when the span is on disk ───────────────────────────────

WSC = '/tmp/ws2/Final Testing/Long/Sacramento'
SUI = '/tmp/ws2/Final Testing/Long/Susisun'
needs_span = pytest.mark.skipif(
    not (os.path.isdir(WSC) and os.path.isdir(SUI)),
    reason='WSC<->SUI .sor set not on this machine')


def _real_pair(fiber):
    sys.path.insert(0, os.path.join(ROOT, 'viewer'))
    import trace_server as TS
    TS.CONFIG['dir_a'], TS.CONFIG['dir_b'] = WSC, SUI
    launch_a = TS.frame_facts(WSC).get('launch_km') or 0.0
    out = []
    for d, flip in (('a', False), ('b', True)):
        t = TS.load_trace(d, fiber)
        out.append({'flipped': flip, 'launch_a_km': launch_a,
                    'far_conn_km': t['far_conn_km'], 'events': t['events'],
                    'pulse_ns': t.get('pulse_ns'), 'ior': t.get('ior')})
    return out


def _real_f71():
    return _real_pair(71)


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


@needs_span
def test_the_span_carries_no_mixed_type_column_any_more():
    """The population check.  Across all 1,152 WSC<->SUI fibers the old rule
    married a connector to a splice on 259 (22.5%) and the new one on 0; this
    walks every 24th fiber so the assertion costs a few seconds rather than
    two minutes.  The old-rule count is asserted too, so a fixture that
    stopped reproducing the defect cannot pass quietly."""
    sampled = list(range(1, 1153, 24))
    before = after = 0
    for f in sampled:
        traces = _real_pair(f)
        before += 1 if mixed_type_columns(build_columns(traces, **OLD_RULE)) else 0
        after += 1 if mixed_type_columns(build_columns(traces)) else 0
    assert after == 0, '%d of %d sampled fibers still mix event types' % (
        after, len(sampled))
    assert before >= len(sampled) // 8, (
        'only %d of %d sampled fibers reproduce the old defect — the sample or '
        'the frame moved' % (before, len(sampled)))


@needs_span
def test_the_real_f2_and_f12_match_the_copied_fixtures():
    """Guards the transcription: the two fixtures above are event tables lifted
    out of the .sor files, and a fixture that has drifted from the file proves
    nothing about the screen."""
    for fiber, a_ev, b_ev, b_far in ((2, F2_A, F2_B, F2_B_FAR),
                                     (12, F12_A, F12_B, F12_B_FAR)):
        traces = _real_pair(fiber)
        assert round(traces[1]['far_conn_km'], 4) == b_far
        for got, want in ((traces[0]['events'], a_ev), (traces[1]['events'], b_ev)):
            assert len(got) == len(want), 'F%d event count moved' % fiber
            for g, w in zip(got, want):
                assert (g['number'], g['dist_km'], g['type']) == \
                       (w['number'], w['dist_km'], w['type']), fiber


@needs_span
def test_a_clean_fiber_still_pairs_a_n_with_b_n_plus_1_minus_n():
    """Do not over-split.  F69 is one of the few WSC<->SUI fibers that resolves
    every event from BOTH ends (only 3 of 1,152 do — most drop an event on one
    side), so its grid is the control: 14 columns, each holding A#n and
    B#(N+1-n), and byte-identical before and after."""
    traces = _real_pair(69)
    before = build_columns(traces, **OLD_RULE)
    after = build_columns(traces)
    assert len(after) == len(before) == 14
    n = len(after)
    for i, c in enumerate(after):
        a, b = c['ev']
        assert a is not None and b is not None, 'column %d lost a side' % i
        assert a['number'] == i + 1
        assert b['number'] == n + 1 - a['number']
    assert [round(c['km'], 4) for c in after] == [round(c['km'], 4) for c in before]


@needs_span
def test_the_tighter_tolerance_does_not_shed_real_pairs():
    """The other half of "do not over-split", as a population.  Over all 1,152
    fibers the old rule made 10,879 two-event columns and the new one 10,863 —
    13 pairs (0.12%) sit between 102 m and 116 m apart and now occupy adjacent
    columns instead of one.  Anything worse than 1% means the tolerance has
    been cut too fine."""
    sampled = list(range(1, 1153, 24))
    before = after = 0
    for f in sampled:
        traces = _real_pair(f)
        before += sum(1 for c in build_columns(traces, **OLD_RULE)
                      if c['ev'][0] and c['ev'][1])
        after += sum(1 for c in build_columns(traces) if c['ev'][0] and c['ev'][1])
    assert before > 0
    assert after >= 0.99 * before, \
        'paired columns fell from %d to %d — the tolerance is too tight' % (
            before, after)
