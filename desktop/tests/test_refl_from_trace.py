"""Trace-measured reflectance + continuous-fiber acquisitions.

5th instance of the stored-table-trust class.  WSC_SUIsh (Sacramento to
Suisun FEC, 24 fibers, 10 ns, 5 km range on a longer cable) came back from
the field with an EMPTY unidirectional workbook while FastReporter showed
F19 carrying a reflective event at 3.8937 km, -75.0 dB.  Four independent
causes, each covered below:

  1. The firmware wrote that event as `0F9999LS`, non-reflective,
     reflectance 0.0.  Both report paths required a negative stored
     reflectance, so the event was dropped before any gate ran.
  2. The acquisition ends in a Continuous Fiber marker (`1O...`), not an
     end-of-fiber, so `is_end` was False on every fiber and the span
     collapsed to 0.00 km.
  3. The 3.0 km front dead zone covered 75% of the 4 km of glass past the
     launch reel.
  4. The span trim discarded the last 22% of the trace, taking the event's
     own right-hand baseline with it.

The boss independently bracketed the reflectance from the other side: in
FastReporter he had to move the summary-box reflectance threshold from the
-72 default to -78 before the event would populate, which puts the true
value between those two.  Our measurement lands at -73.9.
"""
import os
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))
import splicereportmatchexfo as E          # noqa: E402
import sor_reader324802a as SR             # noqa: E402

FIX = os.path.join(HERE, 'fixtures', 'continuous')
F19_KM = 3.8937          # raw (un-normalized) position of the glint
F19_FR_DB = -75.0        # what FastReporter reports after re-analysis


def _recs():
    return [SR.parse_sor_full(os.path.join(FIX, f))
            for f in sorted(os.listdir(FIX)) if f.endswith('.sor')]


def _f19():
    return SR.parse_sor_full(os.path.join(FIX, 'WSC_SUIsh_0019.sor'))


# ── FxdParams: the fields the reflectance math needs ─────────────────────

def test_backscatter_and_pulse_width_parsed():
    d = _f19()
    assert d['backscatter_db'] == pytest.approx(-81.9, abs=0.05)
    assert d['fxd_pulse_ns'] == pytest.approx(10.0)


# ── Trim floor: never cut short of the file's own event table ────────────

def test_trim_keeps_the_whole_event_table():
    """The trim reads FxdParams+20 (data spacing) as the acquisition range,
    which on short-pulse files truncates the trace well before the last
    stored event — here 3.92 km of a 5.00 km fiber, taking F19's right-hand
    baseline with it.  The floor may only ADD samples."""
    d = _f19()
    res = (299_792_458.0 * float(d['exfo_sampling_period']) / 2.0
           / SR._sor_ior_from_events(d, default=1.468))
    covered_km = len(d['trace']) * res / 1000.0
    last_event_km = max(e['dist_km'] for e in d['events'])
    assert covered_km >= last_event_km, (
        f"trace covers {covered_km:.3f} km but events run to {last_event_km:.3f} km")


def test_trim_floor_never_shortens():
    """A guard that could remove samples would be a regression, not a fix."""
    d_trim = SR.parse_sor_full(os.path.join(FIX, 'WSC_SUIsh_0019.sor'), trim=True)
    d_full = SR.parse_sor_full(os.path.join(FIX, 'WSC_SUIsh_0019.sor'), trim=False)
    assert len(d_trim['trace']) <= len(d_full['trace'])


# ── The measurement itself ───────────────────────────────────────────────

def test_f19_glint_is_measurable():
    spike = SR.measure_reflective_spike(_f19(), F19_KM)
    assert spike is not None
    assert spike['height_db'] > 0.5          # real glint, ~0.96 dB
    assert spike['snr'] > 10


def test_bare_glass_measures_nothing():
    """Positions with no event must not produce a spike — the whole gate
    rests on this."""
    d = _f19()
    event_kms = [e['dist_km'] for e in d['events']]
    probes = [km for km in np.arange(1.4, 3.6, 0.05)
              if all(abs(km - e) > 0.2 for e in event_kms)]
    assert len(probes) > 20
    hits = [km for km in probes
            if SR.measure_reflective_spike(d, float(km)) is not None]
    assert not hits, f"phantom spikes at {hits}"


def test_non_reflective_step_is_not_a_glint():
    """F15 carries a real 0.225 dB non-reflective event at 1.0356 km.  A
    loss step is not a reflection and must not measure as one."""
    d = SR.parse_sor_full(os.path.join(FIX, 'WSC_SUIsh_0015.sor'))
    assert SR.measure_reflective_spike(d, 1.0356) is None


def test_absolute_height_floor():
    """SNR alone is not enough.  Eugene (1000 ns, long averaging) has flank
    noise near 2 mdB, where 12-17 mdB of backscatter ripple cleared SNR 7-8
    and produced three phantom reflections."""
    assert SR.REFLM_MIN_HEIGHT_DB >= 0.05
    res, n = 0.32, 20000
    y = 60.0 + 0.00002 * np.arange(n)
    i = int(2500 / res)
    y[i - 1:i + 2] -= 0.015                  # 15 mdB blip, Eugene class
    rec = {'trace': y, 'exfo_sampling_period': 3.125e-09, 'events': [],
           'fxd_pulse_ns': 1000.0, 'backscatter_db': -83.0}
    assert SR.measure_reflective_spike(rec, 2.5) is None


# ── Self-calibration ─────────────────────────────────────────────────────

def test_folder_level_reproduces_stored_reflectance():
    """Solve the backscatter level from the folder's own stored reflective
    events, then use it to re-derive those same reflectances."""
    recs = _recs()
    level = SR.folder_backscatter_level(recs)
    assert level is not None
    errs = []
    for r in recs:
        for e in r['events']:
            if e.get('is_end') or (e.get('reflection') or 0) >= 0:
                continue
            m = SR.measure_reflectance_from_sor(r, e['dist_km'], bs_level=level)
            if m is not None:
                errs.append(m - e['reflection'])
    assert len(errs) >= 5
    assert abs(float(np.median(errs))) < 0.2
    assert max(abs(x) for x in errs) < 1.0


def test_f19_measured_reflectance_matches_fastreporter():
    recs = _recs()
    level = SR.folder_backscatter_level(recs)
    got = SR.measure_reflectance_from_sor(_f19(), F19_KM, bs_level=level)
    assert got is not None
    assert abs(got - F19_FR_DB) < 1.5, f"measured {got:.2f} vs FastReporter {F19_FR_DB}"
    # And inside the bracket the boss established in FastReporter itself:
    # invisible at the -72 threshold, visible at -78.
    assert -78.0 < got < -72.0


def test_too_few_anchors_publishes_no_number():
    """One anchor is not a calibration."""
    assert SR.folder_backscatter_level(_recs()[:1]) is None


def test_no_calibration_means_no_measured_reflectance():
    """Without a level, a measured height stays a height."""
    assert SR.measure_reflectance_from_sor({'trace': None}, 1.0) is None


# ── Window geometry scales with the pulse ────────────────────────────────

def test_windows_scale_with_pulse_width():
    """A glint occupies ~one pulse length of fiber.  Fixed windows only work
    at one pulse width; at 2500 ns a +/-6 m core sits inside the glint."""
    short = SR._reflm_windows({'fxd_pulse_ns': 10.0})
    long_ = SR._reflm_windows({'fxd_pulse_ns': 2500.0})
    assert short == (SR.REFLM_CORE_M, SR.REFLM_FLANK_IN_M, SR.REFLM_FLANK_OUT_M)
    assert long_[0] > 10 * short[0]
    assert long_[1] < long_[2]


# ── Continuous-fiber acquisitions ────────────────────────────────────────

def test_continuous_fiber_detected():
    d = _f19()
    assert E.uni_fiber_eof_strict(d) is None       # no end-of-fiber marker
    assert E.uni_fiber_is_continuous(d) is True
    assert E.uni_fiber_eof(d) == pytest.approx(4.9988, abs=0.01)


def test_continuous_fiber_is_not_a_break():
    """The range ran out, the glass did not.  Calling that a break would
    flag every fiber on a short-range shot."""
    fibers = {i: r for i, r in enumerate(_recs(), start=1)}
    assert E.uni_find_breaks(fibers, [], 5.0) == []


def test_span_survives_a_continuous_acquisition():
    fibers = {i: r for i, r in enumerate(_recs(), start=1)}
    assert E.uni_auto_detect_span(fibers) > 4.0


def test_end_of_fiber_still_wins_when_present():
    """A real end-of-fiber marker must keep taking precedence."""
    rec = {'events': [{'dist_km': 1.0, 'is_end': False, 'type': '1F9999LS'},
                      {'dist_km': 9.0, 'is_end': True, 'type': '1E9999LS'}]}
    assert E.uni_fiber_eof(rec) == 9.0
    assert E.uni_fiber_eof_strict(rec) == 9.0
    assert E.uni_fiber_is_continuous(rec) is False


# ── Front dead zone ──────────────────────────────────────────────────────

def test_front_dead_zone_scales_only_on_short_spans():
    assert E.uni_front_dead_km(True, 62.0) == E.UNI_LAUNCH_FIBER_MAX
    assert E.uni_front_dead_km(True, 4.0) == pytest.approx(1.0)
    assert E.uni_front_dead_km(True, 0.0) == E.UNI_LAUNCH_FIBER_MAX


# ── End to end ───────────────────────────────────────────────────────────

def test_uni_flags_f19_end_to_end(tmp_path):
    import shutil
    src = tmp_path / 'in'
    src.mkdir()
    for f in sorted(os.listdir(FIX)):
        if f.endswith('.sor'):
            shutil.copy2(os.path.join(FIX, f), src)
    res = E.uni_generate(str(src), str(tmp_path / 'o.xlsx'))
    assert res['span_km'] >= 3.9, "continuous acquisition must still yield a span"
    assert res['reflective_columns'], "F19's glint must reach the report"
    cells = [c for c in res['cells'] if c['kind'] == 'reflective']
    assert len(cells) == 1
    assert cells[0]['fiber'] == 19
    assert cells[0]['loss'] == pytest.approx(F19_FR_DB, abs=1.5)


def test_reflective_column_is_labelled_and_explained(tmp_path):
    """A reflective event is not a bend, and its number is a reflectance."""
    import shutil
    src = tmp_path / 'in'
    src.mkdir()
    for f in sorted(os.listdir(FIX)):
        if f.endswith('.sor'):
            shutil.copy2(os.path.join(FIX, f), src)
    res = E.uni_generate(str(src), str(tmp_path / 'o.xlsx'))
    labels = [c['label'] for c in res['grid_columns']]
    assert any(l.startswith('REFL ') for l in labels), labels
    assert not any(l.startswith('Bend/Damage') for l in labels), labels


def test_uni_reflectance_band_on_by_default():
    """The boss ran a uni report and got nothing; the band must not need a
    settings-panel visit to work."""
    assert E.UNI_REFL_FLOOR_DB < 0


# ── Bidirectional parity ─────────────────────────────────────────────────

def test_bidi_dead_zone_scales_like_uni():
    """The Splice Report's mid-span window used a flat 3.0 km blanket at each
    end.  On WSC_SUIsh's 4.00 km that is 3.00 .. 1.00 km — NEGATIVE width, the
    whole cable blanked — so bidi would still have missed F19 after the other
    three fixes.  Long spans keep the 3.0 km rule."""
    eof_short, eof_long = 3.9967, 62.0
    dead_short = min(E.LAUNCH_FIBER_MAX, E.MIDSPAN_DEAD_SPAN_FRAC * eof_short)
    dead_long = min(E.LAUNCH_FIBER_MAX, E.MIDSPAN_DEAD_SPAN_FRAC * eof_long)
    assert dead_long == E.LAUNCH_FIBER_MAX          # 62 km: unchanged
    assert dead_short < eof_short - dead_short      # 4 km: a real window exists
    f19_km = 2.8916                                 # launch-normalized frame
    assert dead_short <= f19_km <= eof_short - dead_short


def test_bidi_and_uni_use_the_same_dead_zone_rule():
    assert E.MIDSPAN_DEAD_SPAN_FRAC == E.UNI_FRONT_DEAD_SPAN_FRAC
