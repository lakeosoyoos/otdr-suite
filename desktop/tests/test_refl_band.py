"""Mid-span reflectance: polarity-robust spike confirm; no ceiling.

the Border job (2026-07-23): real -77.6/-77.9 dB glints (LAMBEY F109 @5.19,
F133 @4.82) measure as -0.13 dB DIPS in accumulated-loss-ascending traces
— 20x noise, at exactly the claimed km — and the positive-only spike
confirm blindly refuted them, so the mid-span reflective detection was
blind on that whole trace-orientation class.  The optional ceiling of
2026-07-23 is gone (Robert 2026-10-02): reflectance is one number.
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))

import splicereportmatchexfo as E  # noqa: E402

SP = 5e-08
M0 = SP * (299792458.0 / 1.468) / 2.0


def _rec(kind, km=5.0, mag=0.7, n=4000, seed=9):
    """Loss-ascending trace with a localized DIP ('dip'), a localized
    SPIKE ('spike'), or nothing ('flat') at km."""
    rng = np.random.RandomState(seed)
    x = np.arange(n)
    tr = 5.0 + 0.19 * (x * M0 / 1000.0) + rng.normal(0, 0.004, n)
    i = int(km * 1000 / M0)
    w = int(15 / M0) or 1
    if kind == 'dip':
        tr[i - w:i + w] -= mag
    elif kind == 'spike':
        tr[i - w:i + w] += mag
    # 50 ns pulse stored in SECONDS — matches the customer L file class and
    # exercises the units normalization; keeps min_run at the short-pulse
    # floor so the narrow synthetic features are width-consistent.
    return {'trace': tr, 'exfo_sampling_period': SP, 'events': [],
            'exfo_calibration': {'NominalPulseWidth': 5e-08}}


def test_spike_confirm_accepts_dip_orientation():
    """Accumulated-loss traces draw the glint as a DIP — must confirm."""
    assert E._reflective_spike_confirms(_rec('dip'), 5.0, -50.0) is True


def test_spike_confirm_accepts_spike_orientation():
    """Power-descending traces draw it UP — must also confirm."""
    assert E._reflective_spike_confirms(_rec('spike'), 5.0, -50.0) is True


def test_spike_confirm_still_refutes_flat_glass():
    """PLACHE F609 class: table claims a reflection, glass is flat in
    BOTH signs — refutation power unchanged."""
    assert E._reflective_spike_confirms(_rec('flat'), 5.0, -50.0) is False


def test_mid_span_reflectance_has_no_ceiling():
    """Robert 2026-10-02: "mid span reflectance can be just one number. we
    don't need the ceiling."  At or above the floor flags, however strong."""
    assert not hasattr(E, 'MIDSPAN_REFL_CEIL_DB')
    assert not hasattr(E, 'UNI_REFL_CEIL_DB')
    src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    assert 'MIDSPAN_REFL_CEIL_DB <' not in src and 'UNI_REFL_CEIL_DB <' not in src
    assert '_passes(dev) or _passes(-dev)' in src        # orientation-symmetric
    assert 'dev = dev - float(np.median(dev))' in src     # offset-artifact centering
    assert 'min_run = max(2, int(0.3 * pulse_m / res))' in src  # width discriminator


def test_the_mid_span_ceiling_row_is_gone():
    import app as hub
    assert 'midspan_refl_ceiling' not in {r[0] for r in hub.OTDR_ROWS}
    assert 'midspan_refl_ceiling' not in hub._OTDR_KEY_TO_ENGINE_GLOBAL
    assert 'midspan_refl_ceiling' not in hub._OTDR_KEY_DISABLE_VALUE
    assert 'midspan_refl_ceiling' not in hub._OTDR_KEY_TO_UNI_GLOBAL
    # a saved setting that still names it (ticked, even) reaches no engine
    s = hub._otdr_settings_from_profile(next(iter(hub.CUSTOMER_PROFILES)))
    s['midspan_refl_ceiling'] = {'apply': True, 'fail': -40.0, 'warning': -40.0}
    assert 'UNI_REFL_CEIL_DB' not in hub._uni_overrides_from_settings(s)


def test_pulse_width_units_normalized():
    """Some firmware writes NominalPulseWidth in SECONDS (5e-08 = 50 ns).
    Treated as ns, the expected-spike floor computed to ~39 dB and refuted
    EVERY mid-span reflective on the file class.  Both unit spellings must
    behave identically."""
    r_sec = _rec('dip', mag=0.13)
    r_ns = _rec('dip', mag=0.13)
    r_ns['exfo_calibration'] = {'NominalPulseWidth': 50}
    assert E._reflective_spike_confirms(r_sec, 5.0, -77.6) is True
    assert E._reflective_spike_confirms(r_ns, 5.0, -77.6) is True


def test_echo_guard_geometry_candidate_scale():
    """Echo position test runs at the CANDIDATE's scale: |cand - n*k| <=
    tol.  A candidate 1.17 km from any parent multiple must NOT be called
    an echo (the old /n form had n*tol slop and ate it)."""
    parents = [(1.006, -45.0)]
    assert E._is_likely_echo(5.19, -77.6, parents) is False
    # true echo geometry still fires: candidate at 2*parent, weaker
    assert E._is_likely_echo(2.012, -77.6, parents) is True


def test_uni_band_on_by_default_no_ceiling():
    """Was off-by-default so the uni workbook stayed byte-stable against the
    ZK format, which has no reflectance category.  Turned ON at the same
    floor the bidirectional report uses after job R short set: the boss ran a uni
    report on a span whose F19 carries a real -74 dB glint and got an empty
    workbook.  Ripple over 10 folders on disk: only job R short set changes."""
    assert E.UNI_REFL_FLOOR_DB == E.MIDSPAN_REFL_WARN_DB == -80.0


def test_uni_band_switchable_off():
    """0 still means off, so a tech can silence the category."""
    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(E, 'UNI_REFL_FLOOR_DB', 0.0)
    try:
        assert E.uni_find_reflective_events({1: {'events': []}}, 10.0) == []
    finally:
        mp.undo()


def test_uni_band_flags_confirmed_glint(monkeypatch):
    monkeypatch.setattr(E, 'UNI_REFL_FLOOR_DB', -80.0)
    monkeypatch.setattr(E, '_reflective_spike_confirms', lambda r, km, refl: True)
    fibers = {7: {'events': [
        {'dist_km': 5.0, 'reflection': -77.6, 'is_reflective': True,
         'is_end': False, 'splice_loss': 0.0},
        {'dist_km': 6.0, 'reflection': -30.0, 'is_reflective': True,   # strong: no ceiling
         'is_end': False, 'splice_loss': 0.0},
        {'dist_km': 7.0, 'reflection': -85.0, 'is_reflective': True,   # below floor
         'is_end': False, 'splice_loss': 0.0},
        {'dist_km': 10.5, 'reflection': -40.0, 'is_reflective': True,
         'is_end': True, 'splice_loss': 0.0},
    ], '_trace_offset_km': 0.0}}
    out = E.uni_find_reflective_events(fibers, 10.5)
    assert [(e['fiber'], e['position_km']) for e in out] == [(7, 5.0), (7, 6.0)]
    cols = E.uni_cluster_reflective(out[:1])
    assert len(cols) == 1 and cols[0]['kind'] == 'reflective'
    assert cols[0]['refl_members'] == {7: -77.6}


def test_uni_band_requires_trace_confirm(monkeypatch):
    monkeypatch.setattr(E, 'UNI_REFL_FLOOR_DB', -80.0)
    monkeypatch.setattr(E, '_reflective_spike_confirms', lambda r, km, refl: False)
    fibers = {7: {'events': [
        {'dist_km': 5.0, 'reflection': -77.6, 'is_reflective': True,
         'is_end': False, 'splice_loss': 0.0}], '_trace_offset_km': 0.0}}
    assert E.uni_find_reflective_events(fibers, 10.5) == []


def test_sharpness_separates_phantom_from_real():
    """The F609/customer L discriminator: a real Fresnel reflection has a SHARP
    edge (peak gradient >> flank noise); a firmware-mislabeled smooth
    backscatter ripple does not.  Amplitude+width alone can't tell them
    apart — sharpness can."""
    n = 4000
    x = np.arange(n)
    base = 5.0 + 0.19 * (x * M0 / 1000.0) + np.random.RandomState(3).normal(0, 0.004, n)
    i = int(5.0 * 1000 / M0)
    cal = {'NominalPulseWidth': 5e-08}
    # SHARP dip (real reflection): abrupt edge
    sharp = base.copy(); sharp[i:i + int(0.05 * 1000 / M0)] -= 0.13
    r_sharp = {'trace': sharp, 'exfo_sampling_period': SP, 'events': [],
               'exfo_calibration': cal}
    assert E._reflective_spike_confirms(r_sharp, 5.0, -77.6) is True
    # SMOOTH dip of the SAME depth (F609 class): gradual, no Fresnel edge
    ramp = np.zeros(n)
    w = int(0.13 * 1000 / M0)                 # 130 m smooth trough
    lo, hi = i - w, i + w
    ramp[lo:i] = np.linspace(0, -0.13, i - lo)
    ramp[i:hi] = np.linspace(-0.13, 0, hi - i)
    smooth = base + ramp
    r_smooth = {'trace': smooth, 'exfo_sampling_period': SP, 'events': [],
                'exfo_calibration': cal}
    assert E._reflective_spike_confirms(r_smooth, 5.0, -66.4) is False


def test_sharp_ratio_constant_present():
    src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    assert 'REFL_SHARP_MIN_RATIO = 5.0' in src
    assert 'core_g / med_g < REFL_SHARP_MIN_RATIO' in src


# ── Panel: the mid-span reflectance row reads as a BAND ──────────────────

def test_no_row_is_a_band():
    """Robert 2026-10-02: "we don't need the band high/low".  The mid-span
    row was a WARN floor .. FAIL band; it is one number now, like the launch
    reflectance row, and the panel draws no band labels."""
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    import ast
    tree = ast.parse(src)
    bands = next(ast.literal_eval(n.value) for n in ast.walk(tree)
                 if isinstance(n, ast.Assign)
                 and any(getattr(t, 'id', '') == '_OTDR_BAND_ROWS' for t in n.targets))
    assert bands == {}
def test_midspan_row_maps_one_number():
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    assert '"midspan_reflectance":  "MIDSPAN_REFL_WARN_DB",' in src
    assert 'MIDSPAN_REFL_FAIL_DB' not in src
    assert '_OTDR_WARN_DEFAULT = {}' in src
    assert '("midspan_reflectance",       "Mid-Span Reflectance",       -80.0,' in src
    # still a member of the profiles that reference it
    assert src.count('"midspan_reflectance", "bend_fold_distance"') >= 2
def test_component_renders_band_labels():
    html = open(os.path.join(ROOT, 'components', 'otdr_settings', 'index.html'),
                encoding='utf-8').read()
    assert 'row.band' in html                 # threshold-mode band hint
    assert 'bandrow' in html
    # and the knobs-mode range rows still exist for the uni panel
    assert "row.kind === \"range\"" in html


def test_no_reflectance_ceiling_rows():
    """Both reflectance rules are one number: no ceiling row of either kind
    (Robert 2026-10-02)."""
    rows = _app_literal('OTDR_ROWS')
    assert not any('ceil' in k for k, *_ in rows), 'a ceiling row is back'
def test_the_launch_reflectance_ceiling_is_gone():
    """Robert 2026-10-02: "we can get rid of reflectance ceiling as well".
    The launch / tailbox reflectance rule is one number: at or above fails."""
    import app as hub
    assert 'reflectance_ceiling' not in {r[0] for r in hub.OTDR_ROWS}
    assert 'reflectance_ceiling' not in hub._OTDR_KEY_TO_ENGINE_GLOBAL
    assert 'reflectance_ceiling' not in hub._OTDR_KEY_DISABLE_VALUE
    assert 'reflectance_ceiling' not in hub.OTDR_DEFAULT_APPLY
    assert not any('reflectance_ceiling' in (p.get('apply') or ())
                   for p in hub.CUSTOMER_PROFILES.values())
    assert 'reflectance' not in hub._OTDR_BAND_ROWS
    assert not hasattr(E, 'LAUNCH_REFL_CEIL_DB')
    src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    assert 'refl_ceil' not in src


# ── Panel: anything the engine does not read is greyed ───────────────────

def _app_literal(name):
    import ast
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    tree = ast.parse(src)
    return next(ast.literal_eval(n.value) for n in ast.walk(tree)
                if isinstance(n, ast.Assign)
                and any(getattr(t, 'id', '') == name for t in n.targets))


def test_greying_is_driven_by_the_real_maps_not_a_hand_flag():
    """A row can never look live while reaching nothing: the panel computes
    `wired` / `warnUsed` from the key->global maps themselves, so wiring a
    new global lights its cell up automatically and un-wiring greys it."""
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    assert "'wired':     key in _OTDR_KEY_TO_ENGINE_GLOBAL" in src
    assert "'warnUsed':  (key in _OTDR_KEY_TO_WARN_GLOBAL" in src
    # ...or the Viewer colours that row's Warning band (the loss rows)
    assert "or key in _OTDR_KEY_TO_VIEWER_WARN)," in src


def test_only_one_row_has_a_live_warning_cell():
    """The Warning column exists for visual fidelity with EXFO's panel, but
    the engine reads it on no row since mid-span reflectance became one number
    (Robert 2026-10-02), and the engine prints no WARN / FAIL split."""
    warn = _app_literal('_OTDR_KEY_TO_WARN_GLOBAL')
    rows = _app_literal('OTDR_ROWS')
    assert warn == {}
    # DERIVED, never hard-coded.  A literal here is a merge-order trap: two
    # PRs can each add one panel row, each stay green alone against the main
    # they were branched from, and turn main red the moment both land.  That
    # is exactly what happened on 2026-08-13 — #56 and #59 each added a row,
    # both CI-green, and their combination broke this assertion.  GitHub's
    # MERGEABLE/CLEAN does not catch it: it checks textual conflicts, not
    # whether an assertion still holds after the merge.
    #
    # What actually matters is the INVARIANT, not the count: no row has a
    # live engine Warning, so every Warning cell the Viewer does not colour
    # with is dead and must render greyed.
    # 13 since Robert 2026-10-02 took out both reflectance ceiling rows
    assert len(rows) >= 13, 'panel rows should not silently disappear'
    eng_src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
                   encoding='utf-8').read()
    assert 'MIDSPAN_REFL_FAIL_DB' not in eng_src and '_sev = ' not in eng_src


def test_unwired_rows_are_exactly_the_unsupported_ones():
    """The hand-kept `supported` flag and the actual engine map must agree,
    or the panel greys the wrong rows."""
    rows = _app_literal('OTDR_ROWS')
    eng = _app_literal('_OTDR_KEY_TO_ENGINE_GLOBAL')
    supported = {k for k, _l, _f, _u, s in rows if s}
    wired = {k for k, *_ in rows if k in eng}
    assert supported == wired, (supported ^ wired)
    # Same merge-order trap as the Warning-cell count above, one step further
    # from tripping: it survives a new WIRED row (rows and wired both rise) but
    # breaks on a new UNWIRED one.  #56 and #59 each happened to add a wired
    # row, so this one held while its sibling did not — luck, not design.
    #
    # The invariant is `supported == wired` on the line before: a row is shown
    # as supported exactly when the engine reads it.  The literal adds nothing
    # that line does not already guarantee, so it only needs to stay sane.
    assert 0 < len(rows) - len(wired) < len(rows), (
        'some rows should be unwired-and-greyed, but not all of them')


def test_component_disables_rather_than_only_dimming():
    """Greyed must mean INERT.  A disabled control that still writes state on
    a synthetic change would reproduce the 2026-06-13 class of bug, where the
    panel sent Python values the tech never saw."""
    html = open(os.path.join(ROOT, 'components', 'otdr_settings', 'index.html'),
                encoding='utf-8').read()
    assert 'cb.disabled = !wired;' in html
    assert 'w.disabled = !enabled || !warnUsed;' in html
    # every threshold-mode handler refuses to write when its control is off
    assert 'if (f.disabled) return;' in html
    assert 'if (w.disabled) return;' in html
    assert 'if (cb.disabled) return;' in html
