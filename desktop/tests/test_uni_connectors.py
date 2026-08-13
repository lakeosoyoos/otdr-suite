"""Unidirectional connector detection — the panel-to-panel span.

Field report (Defuniak Springs Tie Panels, 288 files):

    "loaded to address not detecting broken fibers.  Short shots not
     detecting broken fiber at connector (this is in splice report feature)
     Panel to Panel no splice events."

Two separate defects, both confirmed against the real files:

1.  The uni report had NO connector detection at all.  `detect_launch_issues`
    is called from one place, the bidirectional path.  A panel-to-panel span's
    entire plant is its connectors, so the workbook came back empty.

2.  Launch normalization ERASES exactly the two events such a span is made
    of.  On Defuniak the raw table is

        1F  0.00000 km  loss 0.0     <- the OTDR port
        1F  1.00490 km  loss -0.333  <- entry tie panel
        1F  1.03610 km  loss  0.616  <- far tie panel, 31.2 m later
        1E  2.04150 km  loss 0.0     <- end of the receive reel

    and the normalized table is

        1F  0.00000 km  loss -0.333  <- origin
        1E  0.03120 km  loss 0.0     <- "the end", carrying no loss at all

    The far panel's 0.616 dB is gone and the entry panel is the origin.  No
    threshold, however low, can flag an event that no longer exists.

The tests below pin the recovery of both connectors, the physics that tells
the OTDR's own port apart from a pre-trimmed file's entry connector, and the
trace-measured dark check that answers "did light get through this mate?".
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))

import splicereportmatchexfo as E  # noqa: E402

SP = 5e-08
M0 = SP * (299792458.0 / 1.468) / 2.0        # metres per sample


def _ev(km, loss, refl=-55.0, end=False, tot=None):
    return {'dist_km': km, 'splice_loss': loss, 'reflection': refl,
            'is_end': end, 'is_reflective': True,
            'type': '1E9999LS' if end else '1F9999LS',
            'time_of_travel': (0 if tot is None else tot) if km == 0 else 1}


def _trace(n=30000, dead_from_km=None, noise=0.004, seed=3):
    """Loss-ascending trace.  Past `dead_from_km` it pins to the digitizer
    ceiling, which is what a real fiber end looks like in these files."""
    rng = np.random.RandomState(seed)
    x = np.arange(n)
    tr = 47.0 + 0.19 * (x * M0 / 1000.0) + rng.normal(0, noise, n)
    if dead_from_km is not None:
        tr[int(dead_from_km * 1000 / M0):] = 64.0
    return tr


def _rec(events, trace):
    return {'events': list(events), 'trace': trace,
            'exfo_sampling_period': SP, 'fxd_pulse_ns': 10.0,
            'num_points': len(trace), 'filename': 'f.sor'}


# ── 1. both tie panels come back, and the far one keeps its loss ──────────

def test_both_panels_are_recovered_from_a_normalized_short_shot():
    """The Defuniak shape end to end: port, entry panel, far panel, reel end."""
    raw = [_ev(0.0, 0.0, refl=-86.7, tot=0), _ev(1.0049, -0.333),
           _ev(1.0361, 0.616), _ev(2.0415, 0.0, end=True)]
    r = _rec(raw, _trace(dead_from_km=2.0415))
    fibers = {1: r}
    E.uni_normalize_all(fibers)

    # Precondition: normalization really does destroy both events.
    assert len(r['events']) == 2
    assert r['events'][-1]['is_end'] and r['events'][-1]['splice_loss'] == 0.0

    conns = E.uni_find_connectors(fibers, span_km=0.03)
    at = {round(c['position_km'], 4): c for c in conns}
    assert sorted(at) == [0.0, 0.0312], at

    assert at[0.0]['loss'] == -0.333 and at[0.0]['is_launch']
    assert at[0.0312]['loss'] == 0.616          # the number normalization ate
    assert not at[0.0312]['is_launch']
    # Light reaches the receive reel through both: neither is dark.
    assert not at[0.0]['dark'] and not at[0.0312]['dark']


def test_the_otdr_port_is_not_reported_as_a_connector():
    """The 0-km / 0-loss / backscatter-reflectance event is the instrument,
    not plant.  Reporting it would put a phantom connector on every span."""
    raw = [_ev(0.0, 0.0, refl=-86.7, tot=0), _ev(1.0049, -0.333),
           _ev(1.0361, 0.616), _ev(2.0415, 0.0, end=True)]
    fibers = {1: _rec(raw, _trace(dead_from_km=2.0415))}
    E.uni_normalize_all(fibers)
    assert all(c['loss'] != 0.0 or c['position_km'] > 0
               for c in E.uni_find_connectors(fibers, 0.03))


def test_a_pre_trimmed_file_keeps_its_entry_connector():
    """Dinwiddie ILA-5: the tech exported with the launch reel stripped, so
    the entry connector sits AT 0.0 km — the same slot the port occupies in
    an untrimmed file.  A loss can only be computed where there is glass in
    front of the event, so a non-zero loss at the origin means connector.
    Reading this slot as 'port' loses the entry panel on every trimmed file."""
    raw = [_ev(0.0, -0.215, refl=-54.9, tot=0),
           _ev(0.0311, 0.726, end=True),
           _ev(1.0340, 0.0, refl=-46.6)]        # receive reel, past the end
    fibers = {1: _rec(raw, _trace(dead_from_km=1.034))}
    E.uni_normalize_all(fibers)
    got = {round(c['position_km'], 4): c['loss']
           for c in E.uni_find_connectors(fibers, 0.03)}
    assert got == {0.0: -0.215, 0.0311: 0.726}, got


# ── 2. dark at the connector: broken / unmated / dirty ────────────────────

def test_dark_connector_is_flagged_and_a_mated_one_is_not():
    """The boss's "short shots not detecting broken fiber at connector".

    A dark fiber and a healthy one are IDENTICAL in the normalized event
    table — both read "1E, loss 0.0" — so this can only be settled on the
    glass.  Same events, two traces: one continues past the far panel, one
    dies on it."""
    raw = [_ev(0.0, 0.0, refl=-86.7, tot=0), _ev(1.0049, -0.333),
           _ev(1.0361, 0.616), _ev(2.0415, 0.0, end=True)]

    live = {1: _rec(raw, _trace(dead_from_km=2.0415))}
    dead = {1: _rec(raw, _trace(dead_from_km=1.0361))}
    E.uni_normalize_all(live)
    E.uni_normalize_all(dead)

    far_live = [c for c in E.uni_find_connectors(live, 0.03)
                if c['position_km'] > 0.01][0]
    far_dead = [c for c in E.uni_find_connectors(dead, 0.03)
                if c['position_km'] > 0.01][0]

    assert not far_live['dark']
    assert far_dead['dark']
    # A dark connector flags regardless of its loss number — the fiber is out
    # of service whatever the table says about it.
    assert far_dead['flag']


def test_dark_flags_even_with_the_threshold_turned_off():
    """0 in the settings box disables the LOSS flag.  It must not disable
    'this fiber is dark' — that is a service outage, not a quality grade."""
    raw = [_ev(0.0, 0.0, refl=-86.7, tot=0), _ev(1.0049, -0.333),
           _ev(1.0361, 0.100), _ev(2.0415, 0.0, end=True)]
    fibers = {1: _rec(raw, _trace(dead_from_km=1.0361))}
    E.uni_normalize_all(fibers)
    old = E.UNI_CONN_LOSS_DB
    try:
        E.UNI_CONN_LOSS_DB = 0.0
        far = [c for c in E.uni_find_connectors(fibers, 0.03)
               if c['position_km'] > 0.01][0]
        assert far['dark'] and far['flag']
    finally:
        E.UNI_CONN_LOSS_DB = old


def test_unmeasurable_is_not_dark():
    """No glass in front of the event (entry connector at sample 0) means the
    trace cannot answer.  Silence must not manufacture an outage."""
    raw = [_ev(0.0, -0.215, refl=-54.9, tot=0), _ev(0.0311, 0.726, end=True)]
    fibers = {1: _rec(raw, _trace(dead_from_km=0.5))}
    E.uni_normalize_all(fibers)
    entry = [c for c in E.uni_find_connectors(fibers, 0.03)
             if c['position_km'] < 0.001][0]
    assert E._uni_conn_light_through(fibers[1], 0.0) is None
    assert not entry['dark']


# ── 3. the bare threshold, and the settings box that owns it ─────────────

def test_flag_is_a_bare_threshold_not_a_population_comparison():
    """One fiber at 0.70 among 143 at 0.20 flags; 144 fibers all at 0.70 ALL
    flag.  A population-relative rule would call the second case normal and
    report a uniformly bad panel as clean."""
    def _pop(losses):
        fibers = {}
        for i, L in enumerate(losses, start=1):
            raw = [_ev(0.0, 0.0, refl=-86.7, tot=0), _ev(1.0049, -0.30),
                   _ev(1.0361, L), _ev(2.0415, 0.0, end=True)]
            fibers[i] = _rec(raw, _trace(dead_from_km=2.0415, seed=i))
        E.uni_normalize_all(fibers)
        return [c for c in E.uni_find_connectors(fibers, 0.03)
                if c['position_km'] > 0.01]

    outlier = _pop([0.70] + [0.20] * 143)
    assert sum(1 for c in outlier if c['flag']) == 1

    uniform = _pop([0.70] * 144)
    assert sum(1 for c in uniform if c['flag']) == 144


def test_panel_default_matches_the_engine():
    """The uni settings box sends its value on EVERY run.  A panel row that
    disagrees with the engine constant silently overrides it, which makes any
    engine-side change a no-op."""
    import re
    # encoding is explicit: CI runs on windows-latest, where the default is
    # cp1252 and app.py's non-ASCII (arrows, em dashes) raises
    # UnicodeDecodeError.  Passes on macOS either way, which is precisely why
    # it slipped through locally.  Every other test that reads app.py does this.
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    row = re.search(r"\{'key': 'conn_loss'.*?\n\n", src, re.S).group(0)
    panel = float(re.search(r"'defaults': \{'value': ([0-9.]+)\}", row).group(1))
    assert 'UNI_CONN_LOSS_DB' in row
    assert panel == E.UNI_CONN_LOSS_DB, (panel, E.UNI_CONN_LOSS_DB)


def test_connectors_cluster_tighter_than_off_splice_events():
    """Defuniak's two tie panels are 31.2 m apart.  The off-splice clusterer
    chains at 100 m and merged them into one column, hiding the fact that the
    span has two panels."""
    assert E.UNI_CONN_CLUSTER_M < 31.2 < E.UNI_OFF_SPLICE_CLUSTER_M

    raw = [_ev(0.0, 0.0, refl=-86.7, tot=0), _ev(1.0049, -0.333),
           _ev(1.0361, 0.616), _ev(2.0415, 0.0, end=True)]
    fibers = {1: _rec(raw, _trace(dead_from_km=2.0415))}
    E.uni_normalize_all(fibers)
    cols = E.uni_cluster_connectors(E.uni_find_connectors(fibers, 0.03))
    assert len(cols) == 2, [c['position_km_refined'] for c in cols]
    assert cols[0]['is_launch'] and not cols[1]['is_launch']


def test_readings_are_reported_even_when_nothing_is_flagged():
    """A clean panel must still SHOW as a connector column.  The report's job
    on a panel-to-panel span is to account for the plant, not only to
    complain about it — an empty workbook is what started this."""
    raw = [_ev(0.0, 0.0, refl=-86.7, tot=0), _ev(1.0049, -0.30),
           _ev(1.0361, 0.18), _ev(2.0415, 0.0, end=True)]
    fibers = {1: _rec(raw, _trace(dead_from_km=2.0415))}
    E.uni_normalize_all(fibers)
    cols = E.uni_cluster_connectors(E.uni_find_connectors(fibers, 0.03))
    far = cols[1]
    assert far['conn_members'] == {}          # nothing shaded
    assert far['conn_all'] == {1: 0.18}       # but the reading is on record


# ── 4. a connector is at an END; a mid-span glint is not ─────────────────

def test_mid_span_reflective_is_not_claimed_as_a_connector():
    """Measured over every span on disk — 96 folders, 55,848 stored 1F non-end
    events: 79.2% at the launch, 20.8% within 1.5 km of the fiber's own end,
    0.1% (36 events) anywhere else.  Those 36 read about -70 dB — backscatter
    level, not the -45 to -55 of a real connector.  They are mid-span
    reflective FAULTS and belong to the reflectance band, which names them
    for what they are."""
    raw = [_ev(0.0, 0.0, refl=-86.7, tot=0), _ev(1.0049, -0.30),
           _ev(20.0, 0.21, refl=-70.5),        # mid-span glint
           _ev(60.0, 0.55),                    # receive-reel connector
           _ev(61.0, 0.0, end=True)]
    fibers = {1: _rec(raw, _trace(n=900000, dead_from_km=61.0))}
    E.uni_normalize_all(fibers)
    at = {round(c['position_km'], 2) for c in E.uni_find_connectors(fibers, 60.0)}
    assert 0.0 in at                      # the launch
    assert 58.99 in at or 59.0 in at      # one reel back from the end
    assert not any(18.5 < p < 20.5 for p in at), at


def test_the_same_event_is_never_both_a_connector_and_a_reflective():
    """One predicate decides, so the two categories cannot disagree.  Before
    this, TUCUMCARI F92's receive-reel connector at 96.11 km — 1.06 km before
    its own 97.17 km end, -52.18 dB — appeared in BOTH."""
    end_conn = _ev(59.0, 0.55, refl=-52.2)
    mid = _ev(20.0, 0.21, refl=-70.5)
    # tail_box=True: this shoot ends in a receive reel, so its far end IS a
    # connector.  tail_box=False: the cable ends bare and nothing there is.
    assert E.uni_is_connector_event(end_conn, 59.0, 60.0, True)
    assert not E.uni_is_connector_event(end_conn, 59.0, 60.0, False)
    assert not E.uni_is_connector_event(mid, 20.0, 60.0, True)
    # the launch needs no tail box — the shot is plugged into it
    assert E.uni_is_connector_event(_ev(0.0, -0.3), 0.0, 60.0, False)


def test_end_means_the_fibers_own_end_not_the_span():
    """TUCUMCARI F92's shape: the fiber runs to 97.17 km while the span median
    reads 95.12, and its receive-reel connector sits at 96.11 — PAST the span.

    Both of the pass's end tests key on the fiber's own end.  Were either one
    to use the span instead, this connector would be cut as "past the cable"
    before the end zone ever saw it, and a real -52 dB connector would vanish
    from the report on every fiber longer than the median."""
    raw = [_ev(0.0, 0.0, refl=-86.7, tot=0), _ev(1.0, -0.30),
           _ev(97.11, -0.129, refl=-52.184),
           _ev(98.17, 0.0, end=True)]
    fibers = {1: _rec(raw, _trace(n=1400000, dead_from_km=98.17))}
    E.uni_normalize_all(fibers)
    got = E.uni_find_connectors(fibers, span_km=95.12)   # span SHORTER than
    at = sorted(round(c['position_km'], 2) for c in got)  # this fiber
    assert 96.11 in at, at
    # and it is the fiber's own end that admits it
    assert E.uni_fiber_eof(fibers[1]) > 95.12
