"""A confirmed fingerprint refutes the event gate's LOSS leg - and nothing else.

The event-table gate caps a pair at 0.5 when the median |delta splice loss|
over matched events exceeds 10 mdB.  That cut is a proxy for fibre identity,
and it sits far below the spread real fibres show.  Measured on Romero->Tucu
(40 files, 97 km, production, 780 different-fibre pairs with >= 3 matched
events), median |delta splice loss| between DIFFERENT fibres there reads p25
0.0474 / p50 0.0675 / p75 0.0930 dB - four to six times the cut.  So there is
a wide band of entirely plausible same-fibre table differences that trips the
gate while the glass still says one fibre.

Demonstrated by nudging each of one real file's 14 interior splice losses in
BOTH the samples and the stored table, which keeps every event where it is and
every length intact (harness9.py in the counsel-response measurements):

    nudge   pair sigma   r        median|dl|  speckle r   verdict
    0.005   0.0036       0.9994   0.0027      0.9944      1.00
    0.010   0.0073       0.9975   0.0055      0.9783      1.00
    0.020   0.0145       0.9896   0.0110      0.9216      0.50  <- capped
    0.030   0.0218       0.9759   0.0165      0.8467      0.50  <- capped

against a folder null whose p99 is 0.0794 and whose MAXIMUM over those 780
known-different pairs is 0.1102.  A pair reading 0.92 there is not a
borderline call.

WHAT THIS TEST IS GUARDING.  The event gate exists because two event-poor
files on the BKF<->DEL 80 km span were the common factor in 47 false
positives: BKFDEL028 and BKFDEL040 are the only 2 of 432 files whose stored
table holds <= 2 interior events, and the old fail-OPEN on thin tables let
every pair containing one of them skip the gate.  The refutation must not
re-open that, so it is scoped to the LOSS MAGNITUDE only:

  * event-POOR (events_unverifiable, n_min < min_count) is skipped outright
    and keeps its cap at any fingerprint;
  * an ASYMMETRIC COUNT keeps its cap at any fingerprint - a genuine re-shoot
    detects the same splices in both shots (the calibration measured 100%
    match and equal counts on true same-fibre pairs);
  * eligibility is decided by re-asking _events_agree with the loss cut
    LIFTED and every other threshold untouched, so the scope cannot drift
    from the calibrated function.

And it requires a POSITIVE measurement, never the absence of one: an
unmeasurable pair, a cross-wavelength pair, or a folder whose confirm bar is
out of reach all leave the cap in place.

MEASURED BEFORE AND AFTER on 10 folders, 5,904 files, 2,606,868 pairs - the
historic flood folders plus the trusted duplicate sets plus the one folder
with known same-fibre truth.  Not one verdict moves.  The block is not merely
empty there: it ran on both eligible pairs, measured both and DECLINED both,
because their fingerprints read at the folder null (ROMTUC303/436 at -0.0461
against a 0.2130 bar; EMVSUI016/160 at +0.0129 against 0.1210).  The
measurement corroborated the event gate rather than refuting it.

Namespace isolation rule: the engine is only exercised through subprocesses.
"""
from __future__ import annotations

import json
import subprocess
import sys

from conftest import SECRETSAUCE_DIR


def _run(script: str, *args):
    p = subprocess.run([sys.executable, "-c", script, str(SECRETSAUCE_DIR), *args],
                       capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, p.stderr[-3000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


# ── a synthetic 78 km / 500 ns production folder with one injected pair ──────
# 40 background fibres, each with its own Rayleigh fingerprint and its own
# splice losses, plus two shots of one further fibre.  The splice steps are
# smeared over the pulse the way a real one is, so the engine's own high-pass
# removes them; an instantaneous step survives the filter, plants shared
# structure at identical positions in every fibre and inflates the folder null
# until the confirm bar exceeds 1.0 (which would disable the very block under
# test).  Folder reads: production, competence OK, confirm bar 0.216.
_SYNTH = r"""
import sys, json, copy, io, contextlib
sys.path.insert(0, sys.argv[1])
import numpy as np
import report_sor as RS

L, DZ, SIG_N, PULSE, WL, N_EV = 78000.0, 2.55, 0.0025, 20, 1550.0, 14


def _fibre(rng, n):
    return (rng.standard_normal(n) * RS._FINGERPRINT_DB,
            np.abs(rng.normal(0.08, 0.07, N_EV)))


def _build(name, g, losses, ev_pos, pos, rng, keep=None, drop_first=0):
    n = len(pos)
    stair = np.zeros(n)
    evs = []
    for d, sl in zip(ev_pos, losses):
        stair[int(np.searchsorted(pos, d)):] -= sl
        evs.append({'dist_km': d / 1000.0, 'splice_loss': float(sl),
                    'is_end': False, 'reflectance': None})
    stair = np.convolve(stair, np.ones(PULSE) / PULSE, mode='same')
    tr = 50.0 - 0.00022 * pos + stair + g + rng.standard_normal(n) * SIG_N
    end = {'dist_km': L / 1000.0, 'splice_loss': None, 'is_end': True,
           'reflectance': -40.0}
    evs = (evs[:keep] if keep is not None else evs[drop_first:]) + [end]
    return {'name': name, 'filepath': '/synth/' + name + '.sor',
            'trace': tr.astype(np.float32), 'pos': pos, 'length': float(L),
            'loss': None, 'max_splice_dB': float(max(losses)), 'timestamp': None,
            'wavelength': WL, 'serial_number': None, 'events': evs,
            'pulse_samples': PULSE}


def folder(mode, nudge=0.03, n_bg=40, seed=7):
    rng = np.random.default_rng(seed)
    n = int(L / DZ) + 400
    pos = np.arange(n) * DZ
    ev_pos = np.linspace(0.05 * L, 0.92 * L, N_EV)
    files = [_build(f'BG{k:04d}', *_fibre(rng, n), ev_pos, pos, rng)
             for k in range(n_bg)]
    g, losses = _fibre(rng, n)
    # alternating +/- nudge makes median |d loss| exactly `nudge`
    sign = np.where(np.arange(N_EV) % 2 == 0, 1.0, -1.0)
    if mode == 'poor':
        files.append(_build('DUPA', g, losses, ev_pos, pos, rng, keep=2))
        files.append(_build('DUPB', g, losses, ev_pos, pos, rng, keep=2))
        return files
    files.append(_build('DUPA', g, losses, ev_pos, pos, rng))
    if mode == 'loss':
        files.append(_build('DUPB', g, losses + nudge * sign, ev_pos, pos, rng))
    elif mode == 'count':
        files.append(_build('DUPB', g, losses, ev_pos, pos, rng, drop_first=5))
    elif mode == 'table':
        f = _build('DUPB', g, losses, ev_pos, pos, rng)      # samples untouched
        for e, sl, sg in zip([e for e in f['events'] if not e['is_end']],
                             losses, sign):
            e['splice_loss'] = float(sl + nudge * sg)        # table edited
        files.append(f)
    elif mode == 'clean':
        files.append(_build('DUPB', g, losses, ev_pos, pos, rng))
    return files


def run(mode, **kw):
    files = folder(mode, **kw)
    by = {f['filepath']: f for f in files}
    RS.load_sor_file = lambda p: copy.deepcopy(by[p])
    RS.glob.glob = lambda pat, **k: sorted(by)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        a = RS._analyze_sor('/synth')
    pr = [p for p in a['pairs'] if {p['a'], p['b']} == {'DUPA', 'DUPB'}][0]
    out = {k: pr.get(k) for k in (
        'score', 'p_dup_raw', 'p_dup', 'events_capped', 'events_n_match',
        'events_n_max', 'events_n_min', 'events_median_dloss_db',
        'events_unverifiable', 'events_refuted_by_speckle', 'events_speckle_r',
        'events_speckle_floor', 'events_speckle_bar')}
    # the fingerprint measured directly, whatever the gates decided
    bn = {f['name']: f for f in a['files']}
    hp = RS._speckle_hp_width(a['files'])
    wa = RS._speckle_windows(bn['DUPA'], a['interior_start'], a['interior_end'],
                             hp_width=hp)
    wb = RS._speckle_windows(bn['DUPB'], a['interior_start'], a['interior_end'],
                             hp_width=hp)
    out['measured_r'] = RS._speckle_pair_r(wa, wb)
    out['regime'] = a.get('regime')
    out['log'] = buf.getvalue().splitlines()
    return out
"""


def test_the_folder_the_scenarios_run_in_is_actually_competent():
    """If the synthetic folder's confirm bar drifted above 1.0 every scenario
    below would pass for the wrong reason - nothing can be refuted there."""
    out = _run(_SYNTH + r"""
print(json.dumps(run('clean')))
""")
    assert out["regime"] == "production", out["regime"]
    line = [l for l in out["log"] if l.startswith("Speckle competence: OK,")]
    assert line, [l for l in out["log"] if "competence" in l]
    assert out["measured_r"] > 0.5, out["measured_r"]


def test_an_agreeing_same_fibre_pair_is_untouched():
    """The control: no event objection, so nothing to refute either way."""
    out = _run(_SYNTH + "print(json.dumps(run('clean')))")
    assert out["events_capped"] is False
    assert out["events_refuted_by_speckle"] is None
    assert out["p_dup"] == out["p_dup_raw"] > 0.99


def test_a_loss_only_disagreement_with_a_confirmed_fingerprint_is_refuted():
    """THE REGRESSION.  Median |d loss| 0.030 dB - three times the cut and
    still well under the 0.047 dB lower quartile of what different fibres in
    a real folder differ by - on a pair whose fingerprint reads ~0.68 against
    a 0.216 confirm bar.  Before the fix this capped at 0.5."""
    out = _run(_SYNTH + "print(json.dumps(run('loss')))")
    assert out["events_n_match"] == out["events_n_max"] == 14, out
    assert out["events_median_dloss_db"] > 0.010, out["events_median_dloss_db"]
    assert out["events_unverifiable"] is None
    assert out["events_refuted_by_speckle"] is True, out
    assert out["events_capped"] is False
    # the cap is gone: the verdict is the pair's own likelihood again
    assert out["p_dup"] == out["p_dup_raw"], out
    assert out["p_dup"] > 0.99, out
    # and the measurement that carried it is on the record
    assert out["events_speckle_r"] >= out["events_speckle_bar"]
    assert out["events_speckle_r"] >= out["events_speckle_floor"]


def test_an_event_poor_pair_is_never_refuted_however_strong_the_fingerprint():
    """The BKF<->DEL guard.  Two interior events is the shape of BKFDEL028 and
    BKFDEL040, whose fail-open generated all 47 false positives on that span.
    This pair carries the SAME fingerprint as the refuted one above (~0.68),
    so the cap survives on the leg rule, not on a weak measurement."""
    out = _run(_SYNTH + "print(json.dumps(run('poor')))")
    assert out["events_n_min"] < 3, out
    assert out["events_unverifiable"] is True
    assert out["measured_r"] > 0.5, "the fingerprint must be strong here"
    assert out["events_refuted_by_speckle"] is None, out
    assert out["events_capped"] is True
    assert out["p_dup"] == 0.5, out
    # never even measured - eligibility is refused before the fingerprint
    assert out["events_speckle_r"] is None


def test_an_asymmetric_event_count_is_never_refuted():
    """A real re-shoot detects the same splices twice, so a missing-events
    disagreement is evidence about the pair and keeps its cap.  Same strong
    fingerprint as the refuted case."""
    out = _run(_SYNTH + "print(json.dumps(run('count')))")
    assert out["events_n_match"] < out["events_n_max"], out
    assert out["events_unverifiable"] is None
    assert out["measured_r"] > 0.5, "the fingerprint must be strong here"
    assert out["events_refuted_by_speckle"] is None, out
    assert out["events_capped"] is True
    assert out["p_dup"] == 0.5, out
    assert out["events_speckle_r"] is None


def test_a_table_only_edit_is_measured_and_declined_not_waved_through():
    """The relabel countermeasure: samples left alone, stored losses edited.
    It IS eligible and IS measured, and the same-fibre floor declines it - at
    sigma 0.0035 the floor is 0.747 while the pair reads 0.677.  The floor is
    a worst-case bound (it puts the pair's whole disagreement in the speckle
    band), so it is deliberately conservative: the cap stays.  This is
    recorded because it is the one case where the refutation could look as if
    it should have fired and did not."""
    out = _run(_SYNTH + "print(json.dumps(run('table')))")
    assert out["events_median_dloss_db"] > 0.010
    assert out["events_refuted_by_speckle"] is None, out
    assert out["p_dup"] == 0.5, out
    # measured, and the numbers behind the decline are on the pair
    assert out["events_speckle_r"] is not None
    assert out["events_speckle_bar"] is not None
    assert out["events_speckle_r"] >= out["events_speckle_bar"], out
    assert out["events_speckle_r"] < out["events_speckle_floor"], out


# ── the rules that make the scope hold, checked in the source ───────────────
def _block() -> str:
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("# ── Event-gate refutation by fingerprint")
    return src[i:src.index("# Uniqueness (twin) gate", i)]


def test_only_the_loss_leg_is_eligible():
    """Eligibility is the calibrated function re-asked with the loss cut
    lifted.  Re-implementing the other legs here is how the scope drifts."""
    b = _block()
    assert "loss_thresh_db=float('inf')" in b
    assert "if p.get('events_unverifiable'):" in b
    assert "_events_agree(" in b


def test_the_refutation_needs_a_positive_measurement():
    """Every failure mode must leave the cap in place.  A fail-open here is
    the BKF<->DEL mistake in a new place."""
    b = _block()
    assert "if nq is None or not cmpb:" in b
    assert "if r_pair is None or r_floor is None:" in b
    assert "if r_pair < bar or r_pair < r_floor:" in b
    assert "bar = nq * _SPECKLE_CONFIRM_NULL_MULT" in b
    # nothing may clear the violation except the measured path
    assert b.count("events_violation[i] = False") == 1


def test_short_span_folders_are_safe_by_arithmetic():
    """A 5/10 ns tray or panel folder puts the confirm bar above 1.0, which no
    Pearson r can reach, and the competence ceiling is asserted explicitly so
    the block cannot fire on a run whose own log says NOT MEASURED."""
    b = _block()
    assert "if bar > _SPECKLE_BAR_MAX:" in b


def test_the_pair_only_matters_above_the_cap():
    """Pairs already below LEN_CAP are skipped: capping them changes no
    verdict, and measuring them would cost the speckle windows for nothing."""
    assert "if not events_violation[i] or p_dup_raw[i] <= LEN_CAP:" in _block()


def test_the_veto_gates_abstain_test_is_not_imported():
    """`r_floor < null -> abstain` belongs to the speckle VETO and is wrong in
    the confirm direction: a floor under the null means a LOW reading proves
    nothing, which says nothing against a HIGH one.  Importing it would have
    discarded the pair this block exists for - on Romero->Tucu the band
    amplitude is 0.0013 dB, so a sigma 0.0145 pair has a floor of 0.0157
    against a null p99 of 0.0794, and the pair measured 0.9216."""
    b = _block()
    assert "if r_floor < nq:" not in b, "the veto's abstain test is back"
    assert "NOT imported from the speckle VETO gate" in b


def test_it_reports_itself():
    """An eligible-but-declined pair must be visible in the run log, or an
    abstention is indistinguishable from the block not running."""
    b = _block()
    assert "if ev_loss_only:" in b
    assert "refuted by the fingerprint" in b
    # and the measurement is recorded before the decision, so a decline is
    # auditable on the pair itself
    i_rec = b.index("p['events_speckle_r'] = round(r_pair, 4)")
    i_dec = b.index("if r_pair < bar or r_pair < r_floor:")
    assert i_rec < i_dec, "record the measurement before deciding on it"


def test_the_corpus_measurement_is_recorded():
    """The safety argument is a measurement, not a claim.  These numbers are
    what tells whoever widens the scope later what they are spending."""
    b = _block()
    for marker in ("2,606,868", "BKF<->DEL (LONGS)", "A-F West 145-288",
                   "LAMBEY", "TULORO", "MILTOP", "EMVSUI0 Long Shots",
                   "retruetest", "ROMTUC303/436", "EMVSUI016/160",
                   "0.0474", "0.0675", "0.0930"):
        assert marker in b, f"missing calibration evidence: {marker}"


_ORDER = r"""
import sys, json, inspect
sys.path.insert(0, sys.argv[1])
import report_sor as R
src = inspect.getsource(R._analyze_sor)
print(json.dumps({
  'after_events': src.index('events_violation = np.zeros') < src.index('n_ev_refuted = 0'),
  'before_twin_refute': src.index('n_ev_refuted = 0') < src.index('n_uniq_refuted = 0'),
  'before_physical': src.index('n_ev_refuted = 0') < src.index('physical_violation = (length_violation'),
  'speckle_ctx_first': src.index('def _spk_null(') < src.index('n_ev_refuted = 0'),
}))
"""


def test_the_ordering_holds_at_runtime():
    """Source-slice asserts can pass on a file that no longer composes.  The
    block must sit after the event loop that fills the counters it reads,
    before the twin-gate refutation (so a pair whose event objection is gone
    can be judged on its twin objection alone, as that block requires), and
    before physical_violation consumes the array."""
    out = _run(_ORDER)
    assert out["after_events"] is True
    assert out["before_twin_refute"] is True
    assert out["before_physical"] is True
    assert out["speckle_ctx_first"] is True, (
        "the speckle helpers must be defined above the block or _spk_null is undefined")
