"""Trace-frame alignment for the reflective re-measure gate (HOWLAN panels).

An event's stored km and its sample position in the DataPts block are not
always the same frame.  HOWLAN was shot over a ~1.02 km launch reel that the
OTDR compensated OUT of the event table while the trace kept it, so the gate
was re-measuring 1.02 km upstream of every claim — flat backscatter, so it
refuted BOTH of HOWLAN's real panel-class reflectives (F192 -56.2 dB @17.93 km,
F194 -42.9 dB @71.95 km, the one the bidirectional report calls DIRTY
CONNECTOR) and the uni report showed no reflective column at all.

Both events are REAL: each is corroborated by the opposite-direction shot at
the same physical position (LANHOW192 @99.41 km = 117.32-99.41 = 17.91 km;
LANHOW194 @45.39 km = 71.94 km), and once the frame is aligned the raw trace
shows the Fresnel plateau and the loss step the table claims.

The alignment is MEASURED against the fibre's own end-of-fibre event (nothing
in the SOR records the compensation — FxdParams' acquisition offset and front
panel offset are 0 on every EXFO file we have), and it only ever applies a
launch-reel-sized forward shift, so a misread falls back to the old behaviour.
CHEPLA0609's F609 firmware phantom stays refuted in BOTH frames.
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))
import numpy as np
import splicereportmatchexfo as E
import sor_reader324802a as SR

FIX = os.path.join(HERE, 'fixtures', 'refl')

# (file, stored km, stored reflectance) — own-frame, straight off KeyEvents.
HOWLAN_F192 = ('HOWLAN192_1550.sor', 17.9268, -56.189)
HOWLAN_F194 = ('HOWLAN194_1550.sor', 71.9469, -42.933)
CHEPLA_F609 = ('CHEPLA0609_1550.sor', 25.6818, -66.44)


def _load(name):
    return SR.parse_sor_full(os.path.join(FIX, name))


def _res(d):
    ior = SR._sor_ior_from_events(d, default=1.468)
    return 299792458.0 * float(d.get('exfo_sampling_period') or 5e-08) / 2.0 / ior


def _shift(d):
    return E._trace_frame_shift_km(d, np.asarray(d['trace'], float), _res(d))


def test_launch_compensated_file_reports_the_reel_length():
    """HOWLAN's table origin sits ~1.02 km into its own trace — the length of
    the launch reel the OTDR compensated out.  Measured, not assumed."""
    for name, _, _ in (HOWLAN_F192, HOWLAN_F194):
        s = _shift(_load(name))
        assert 0.95 < s < 1.10, '%s: shift %.4f km' % (name, s)


def test_uncompensated_file_reports_no_shift():
    """CHEPLA's own table origin IS its trace origin (its launch connector is
    still IN the event list at 1.0095 km) — nothing to correct."""
    assert _shift(_load(CHEPLA_F609[0])) == 0.0


def test_howlan_panels_confirm():
    """The two panel-class reflectives the uni report was hiding.  Both carry
    the glint their table claims once the window lands on it."""
    for name, km, refl in (HOWLAN_F192, HOWLAN_F194):
        d = _load(name)
        assert E._reflective_spike_confirms(d, km, refl) is True, name


def test_howlan_panel_is_a_sharp_fresnel_not_a_ripple():
    """Why they pass: F194 is a saturating 3.5 dB Fresnel plateau one pulse
    width wide with a 1.2 dB loss step behind it — the opposite of F609's
    smooth 26 mdB wiggle.  Guards against a future 'fix' that confirms them
    by loosening the sharpness gate instead of aiming the window."""
    d = _load(HOWLAN_F194[0])
    y = np.asarray(d['trace'], float)
    res = _res(d)
    k = int((HOWLAN_F194[1] + _shift(d)) * 1000.0 / res)
    core = float(np.max(np.abs(np.diff(y[k - 12:k + 12]))))
    flank = np.abs(np.diff(y[k - 80:k - 12]))
    assert core / max(float(np.median(flank)), 1e-6) > 50.0


def test_f609_phantom_refuted_in_both_frames():
    """The phantom stays dead measured at its own stored km AND at the
    launch-normalized km the bidir/uni runners actually hand the gate."""
    d = _load(CHEPLA_F609[0])
    assert E._reflective_spike_confirms(d, CHEPLA_F609[1], CHEPLA_F609[2]) is False
    n = _load(CHEPLA_F609[0])
    off = E._untrimmed_launch_offset_km(n['events'])
    assert off > 0.9                       # CHEPLA really is untrimmed
    n['events'] = E._normalize_untrimmed_events(n['events'])
    assert E._reflective_spike_confirms(n, CHEPLA_F609[1] - off,
                                        CHEPLA_F609[2]) is False


def test_shift_is_bounded_and_fail_safe():
    """A shift is only ever a launch-reel-sized forward move; anything else
    (including an unreadable trace) leaves the gate where it was."""
    src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    assert 'REFL_FRAME_MIN_SHIFT_KM <= s <= REFL_FRAME_MAX_SHIFT_KM' in src
    assert E.REFL_FRAME_MAX_SHIFT_KM <= E.LAUNCH_FIBER_MAX
    junk = {'trace': list(np.linspace(30.0, 40.0, 5000)), 'events': []}
    assert E._trace_frame_shift_km(junk, np.asarray(junk['trace'], float), 5.1) == 0.0


def test_uni_caller_hands_over_its_own_frame():
    """The uni path used to pre-add _trace_offset_km; the gate resolves the
    frame itself now, so adding it too would double-count the reel."""
    src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    i = src.index('def uni_find_reflective_events')
    body = src[i:i + 1800]
    assert '_reflective_spike_confirms(r, km, refl)' in body
    assert '_trace_offset_km' not in body
