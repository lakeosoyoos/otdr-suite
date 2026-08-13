"""Mid-span reflectance: polarity-robust spike confirm + band ceiling.

Lumen Border (2026-07-23): real -77.6/-77.9 dB glints (LAMBEY F109 @5.19,
F133 @4.82) measure as -0.13 dB DIPS in accumulated-loss-ascending traces
— 20x noise, at exactly the claimed km — and the positive-only spike
confirm blindly refuted them, so the mid-span reflective detection was
blind on that whole trace-orientation class.  Plus Robert's band ask:
an optional ceiling so the pass can flag ONLY [warn floor, ceiling]
(e.g. -80..-40 isolates faint fusion glints).
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
    # 50 ns pulse stored in SECONDS — matches the Lumen file class and
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


def test_band_ceiling_default_off_and_gate_wired():
    assert E.MIDSPAN_REFL_CEIL_DB == 0.0          # shipped behavior
    src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    assert src.count('if MIDSPAN_REFL_CEIL_DB < 0 and refl > MIDSPAN_REFL_CEIL_DB:') == 1
    assert '_passes(dev) or _passes(-dev)' in src        # orientation-symmetric
    assert 'dev = dev - float(np.median(dev))' in src     # offset-artifact centering
    assert 'min_run = max(2, int(0.3 * pulse_m / res))' in src  # width discriminator


def test_panel_row_and_maps():
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    assert '"midspan_refl_ceiling"' in src or "'midspan_refl_ceiling'" in src
    assert '"midspan_refl_ceiling": "MIDSPAN_REFL_CEIL_DB"' in src
    assert '"midspan_refl_ceiling": 0.0' in src        # unticked = no ceiling
    # NOT pre-applied: shipped default keeps no ceiling
    apply_block = src.split('OTDR_DEFAULT_APPLY = ', 1)[1].split('}', 1)[0]
    assert 'midspan_refl_ceiling' not in apply_block


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
    floor the bidirectional report uses after WSC_SUIsh: the boss ran a uni
    report on a span whose F19 carries a real -74 dB glint and got an empty
    workbook.  Ripple over 10 folders on disk: only WSC_SUIsh changes."""
    assert E.UNI_REFL_FLOOR_DB == E.MIDSPAN_REFL_WARN_DB == -80.0
    assert E.UNI_REFL_CEIL_DB == 0.0          # no ceiling unless the tech sets one


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
    monkeypatch.setattr(E, 'UNI_REFL_CEIL_DB', -40.0)
    monkeypatch.setattr(E, '_reflective_spike_confirms', lambda r, km, refl: True)
    fibers = {7: {'events': [
        {'dist_km': 5.0, 'reflection': -77.6, 'is_reflective': True,
         'is_end': False, 'splice_loss': 0.0},
        {'dist_km': 6.0, 'reflection': -30.0, 'is_reflective': True,   # above ceiling
         'is_end': False, 'splice_loss': 0.0},
        {'dist_km': 7.0, 'reflection': -85.0, 'is_reflective': True,   # below floor
         'is_end': False, 'splice_loss': 0.0},
        {'dist_km': 10.5, 'reflection': -40.0, 'is_reflective': True,
         'is_end': True, 'splice_loss': 0.0},
    ], '_trace_offset_km': 0.0}}
    out = E.uni_find_reflective_events(fibers, 10.5)
    assert [(e['fiber'], e['position_km']) for e in out] == [(7, 5.0)]
    cols = E.uni_cluster_reflective(out)
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
    """The F609/Lumen discriminator: a real Fresnel reflection has a SHARP
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

def test_midspan_row_is_declared_a_band():
    """Robert: 'splice report needs a high and low band like uni has.'  The
    row already WAS one — engine-side it drives two globals, MIDSPAN_REFL_
    FAIL_DB at the strong end and MIDSPAN_REFL_WARN_DB at the weak end — the
    panel just rendered it as an ordinary fail/warning pair."""
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    import ast
    tree = ast.parse(src)
    bands = next(ast.literal_eval(n.value) for n in ast.walk(tree)
                 if isinstance(n, ast.Assign)
                 and any(getattr(t, 'id', '') == '_OTDR_BAND_ROWS' for t in n.targets))
    assert 'midspan_reflectance' in bands
    low, high = bands['midspan_reflectance']
    assert 'low' in low.lower() and 'high' in high.lower()


def test_band_is_rendering_only_no_data_model_change():
    """The whole point of the minimal design: the row keeps the {apply,
    fail, warning} shape, so CUSTOMER_PROFILES, the key->global maps and
    _overrides_from_settings are all untouched.  If someone later moves the
    band onto its own slots, these must be revisited together."""
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    assert '"midspan_reflectance":  "MIDSPAN_REFL_FAIL_DB"' in src   # strong end
    assert '"midspan_reflectance":  "MIDSPAN_REFL_WARN_DB"' in src   # weak end
    assert '_OTDR_WARN_DEFAULT = {"midspan_reflectance": -80.0}' in src
    # still a member of the profiles that reference it
    assert src.count('"midspan_reflectance", "bend_fold_distance"') >= 2


def test_component_renders_band_labels():
    html = open(os.path.join(ROOT, 'components', 'otdr_settings', 'index.html'),
                encoding='utf-8').read()
    assert 'row.band' in html                 # threshold-mode band hint
    assert 'bandrow' in html
    # and the knobs-mode range rows still exist for the uni panel
    assert "row.kind === \"range\"" in html


def test_only_the_midspan_row_is_a_band():
    """A stray band flag on a plain threshold row would mislabel a real
    fail/warning pair as a range."""
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    import ast
    tree = ast.parse(src)
    bands = next(ast.literal_eval(n.value) for n in ast.walk(tree)
                 if isinstance(n, ast.Assign)
                 and any(getattr(t, 'id', '') == '_OTDR_BAND_ROWS' for t in n.targets))
    assert set(bands) == {'midspan_reflectance'}


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
    assert "'warnUsed':  key in _OTDR_KEY_TO_WARN_GLOBAL" in src


def test_only_one_row_has_a_live_warning_cell():
    """The Warning column exists for visual fidelity with EXFO's panel, but
    the engine reads it on exactly one row — and "FAIL"/"WARN" appears once
    in the whole engine, at the mid-span reflectance severity split."""
    warn = _app_literal('_OTDR_KEY_TO_WARN_GLOBAL')
    rows = _app_literal('OTDR_ROWS')
    assert set(warn) == {'midspan_reflectance'}
    # DERIVED, never hard-coded.  A literal here is a merge-order trap: two
    # PRs can each add one panel row, each stay green alone against the main
    # they were branched from, and turn main red the moment both land.  That
    # is exactly what happened on 2026-08-13 — #56 and #59 each added a row,
    # both CI-green, and their combination broke this assertion.  GitHub's
    # MERGEABLE/CLEAN does not catch it: it checks textual conflicts, not
    # whether an assertion still holds after the merge.
    #
    # What actually matters is the INVARIANT, not the count: exactly one row
    # has a live Warning, so every other row's Warning cell is dead and must
    # render greyed.
    assert len(warn) == 1
    assert len(rows) - len(warn) == len(rows) - 1
    assert len(rows) >= 14, 'panel rows should not silently disappear'
    eng_src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
                   encoding='utf-8').read()
    assert eng_src.count('"FAIL" if refl >= MIDSPAN_REFL_FAIL_DB else "WARN"') == 1


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
