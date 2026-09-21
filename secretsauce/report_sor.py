"""
report_sor.py — SOR-file variant of the clean report.

Takes a folder of .sor files, runs the same classification logic (single
wavelength), and produces the clean HTML + PDF output with likelihood column.
"""
import os, re, sys, glob, base64, subprocess, argparse
from datetime import datetime
from itertools import combinations
from io import BytesIO
import numpy as np
from scipy.stats import norm

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from sor_reader324802a import parse_sor_full

from report import (  # reuse helpers — all neutral
    _BASE_CSS, _embed_logo, _find_chrome, _outlier_probability,
    html_to_pdf_bytes, _fmt_time_gap, _detrend, _shape_color,
    _COLOR_HIGH, _COLOR_MID, _COLOR_LOW,
    _event_match_quality, _events_agree,
)

_IOR = 1.4682
_LAUNCH_SKIP_M = 500
_END_BUFFER_M  = 200


def load_sor_file(path):
    r = parse_sor_full(path, trim=False)
    if r is None:
        raise ValueError(f'unparseable: {path}')
    trace = r['trace']
    sp = r.get('exfo_sampling_period')
    if not sp or sp <= 0:
        raise ValueError(f'bad sampling period: {path}')
    dz_m = 2.998e8 * sp / (2.0 * _IOR)
    pos = np.arange(len(trace)) * dz_m
    length_m = r.get('exfo_spans_length') or (pos[-1] if len(pos) else 0.0)
    events = r.get('events') or []
    # Pulse width expressed in SAMPLES.  The speckle high-pass has to cut at
    # this scale, and it varies 15x across the acquisitions on disk, so the
    # filter width cannot be a fixed sample count (see _SPECKLE_HP_WIDTH).
    _cal = r.get('exfo_calibration') or {}
    _cpw = _cal.get('CalibratedPulseWidth') or _cal.get('NominalPulseWidth')
    pulse_samples = (float(_cpw) / sp) if (_cpw and sp and sp > 0) else None
    # Max splice loss from event table (firmware-reported, interior events only)
    splice_vals = [e.get('splice_loss') for e in events
                   if e.get('splice_loss') is not None
                   and not e.get('is_end')
                   and (e.get('dist_km') or 0) > 0.01]
    max_splice = max((abs(v) for v in splice_vals), default=None) if splice_vals else None
    # Pull OTDR serial number from GenParams/SupParams so we can flag pairs
    # acquired by different OTDRs in the confirmed-duplicate detail table.
    from sor_reader324802a import parse_gen_params
    gp = parse_gen_params(path) or {}
    serial = (gp.get('serial_number') or '').strip() or None
    return {
        'name':     os.path.splitext(os.path.basename(path))[0],
        'filepath': path,
        'trace':    trace.astype(np.float32),
        'pos':      pos,
        'length':   float(length_m),
        'loss':     r.get('exfo_spans_loss'),
        'max_splice_dB': max_splice,
        'timestamp': r.get('date_time'),
        'wavelength': r.get('exfo_wavelength_nm') or r.get('wavelength'),
        'serial_number': serial,
        'events':   events,
        'pulse_samples': pulse_samples,
    }


def _pair_score(a, b, interior_start, interior_end):
    pa, pb = a['pos'], b['pos']
    ta, tb = a['trace'], b['trace']
    n = min(len(ta), len(tb))
    mask = (pa[:n] > interior_start) & (pa[:n] < interior_end)
    if mask.sum() < 50:
        return None
    return float(np.std(ta[:n][mask] - tb[:n][mask]))


def _compute_pair_metrics_batch(files, interior_start, interior_end, min_samples=50,
                                  tie_panel_mode=False):
    """Vectorized pair-metric computation. For N files this scales as O(N²·S)
    via two matmuls instead of O(N²) Python loops, so 864-file runs go from
    hours to seconds.

    Returns (sigma_matrix, r_matrix, valid_file_indices) where the matrices
    are indexed by position within `valid_file_indices` (NOT the original
    `files` list). σ is computed on raw traces; r on detrended traces.
    """
    interior = []
    valid_idx = []
    for i, f in enumerate(files):
        ta, pa = f['trace'], f['pos']
        n = len(ta)
        mask = (pa[:n] > interior_start) & (pa[:n] < interior_end)
        if mask.sum() < min_samples:
            continue
        interior.append((ta[mask].astype(np.float32),
                         pa[mask].astype(np.float32)))
        valid_idx.append(i)
    if len(interior) < 2:
        return None

    N = min(len(d[0]) for d in interior)
    K = len(interior)
    M_raw = np.empty((K, N), dtype=np.float32)
    M_det = np.empty((K, N), dtype=np.float32)
    for k, (ts, ps) in enumerate(interior):
        ts = ts[:N]; ps = ps[:N]
        M_raw[k] = ts
        # Detrend per-row: subtract best-fit linear (slope·pos + intercept).
        # Closed-form: slope = cov(p, t) / var(p), intercept = mean(t) - slope·mean(p).
        pm = ps.mean(); tm = ts.mean()
        denom = ((ps - pm) ** 2).sum()
        slope = float(((ps - pm) * (ts - tm)).sum() / denom) if denom > 0 else 0.0
        intercept = float(tm - slope * pm)
        M_det[k] = ts - (slope * ps + intercept)

    # σ(M[i] - M[j]) for all pairs via the variance-decomposition identity
    # on MEAN-CENTERED rows:
    #     var(A - B) = var(A') + var(B') - 2·E[A'·B']   with A' = A - E[A]
    # Centering first is load-bearing, not cosmetic: on raw ~46 dB trace
    # levels the uncentered identity (m2a + m2b - 2C - Δmean²) subtracts
    # ~2000-magnitude terms to extract a variance of ~1e-4, and the float32
    # trace quantization (~5.5e-6 dB/sample at 46 dB) alone puts ~2.6e-4 of
    # error into the cross term — catastrophic cancellation.  On a pristine
    # span (true pair σ ~0.01) that error DOMINATES: σ collapsed to 0.0000
    # for high-injection-offset pairs and the σ-outlier tier confirmed 67
    # numerical artifacts as duplicates (Lumen Border LAM/BEY, 2026-07-23).
    # Centered values span ~±1 dB, so the same identity is exact to ~1e-8.
    M64 = M_raw.astype(np.float64)
    M0 = M64 - M64.mean(axis=1, keepdims=True)
    v = (M0 ** 2).mean(axis=1)
    C0 = (M0 @ M0.T) / float(N)
    var_ij = v[:, None] + v[None, :] - 2.0 * C0
    sigma_matrix = np.sqrt(np.maximum(var_ij, 0.0))

    # Pearson r on detrended traces, after FINGERPRINT EXTRACTION:
    # subtract the per-position MEDIAN trace across files so the launch
    # reflection, attenuation slope, and shared connector signatures that
    # every fiber sees through the same launch box get cancelled. What
    # remains is each fiber's unique Rayleigh-scatterer fingerprint +
    # shot noise — the actual basis for "same fiber" calls.
    #
    # Why median (not mean): in datasets where duplicates make up a large
    # fraction of the files (e.g. TEST DUPE has 12 of 18 fibers in
    # duplicate pairs), the mean is biased toward the duplicate signal and
    # subtracting it weakens the same-fiber agreement. The median is
    # robust to that — it represents the typical "non-duplicate" trace
    # even when ~half the dataset is duplicates of the other half.
    #
    # Without this step, tie panels (short fibers with no splice events)
    # show inflated r because the shared launch+connector features
    # dominate the trace. With it, two truly-different short fibers
    # uncorrelate to near zero.
    M_det64 = M_det.astype(np.float64)
    if tie_panel_mode:
        # Subtract the median trace across all files: removes the shared
        # launch + connector signal so the per-fiber Rayleigh fingerprint
        # is what r actually measures. Median (not mean) is robust to the
        # presence of real duplicates in the dataset.
        group_ref = np.median(M_det64, axis=0, keepdims=True)
        M_fingerprint = M_det64 - group_ref
    else:
        # Production mode: skip fingerprint extraction. Real same-fiber
        # duplicates with naturally-low r (0.85-0.94) on long fibers
        # shouldn't be demoted by an aggressive shared-signal subtraction.
        M_fingerprint = M_det64
    # Re-center each row's residual fingerprint (should already be near zero).
    Mc = M_fingerprint - M_fingerprint.mean(axis=1, keepdims=True)
    std = np.sqrt((Mc ** 2).mean(axis=1))
    std_outer = np.outer(std, std)
    np.maximum(std_outer, 1e-12, out=std_outer)
    r_matrix = (Mc @ Mc.T) / (float(N) * std_outer)
    np.clip(r_matrix, -1.0, 1.0, out=r_matrix)
    return sigma_matrix, r_matrix, valid_idx


def _pair_shape_r(a, b, interior_start, interior_end):
    """Detrended Pearson r in the interior window. r ≈ 1 → same fiber."""
    pa = a['pos']
    ta, tb = a['trace'], b['trace']
    n = min(len(ta), len(tb))
    mask = (pa[:n] > interior_start) & (pa[:n] < interior_end)
    if mask.sum() < 50:
        return None
    pp = pa[:n][mask].astype(np.float64)
    da = _detrend(ta[:n][mask].astype(np.float64), pp)
    db = _detrend(tb[:n][mask].astype(np.float64), pp)
    sa, sb = np.std(da), np.std(db)
    if sa == 0 or sb == 0:
        return None
    return float(np.dot(da - da.mean(), db - db.mean()) / (sa * sb * len(da)))


def _distribution_chart(scores, p_dup, stats, shape_rs=None):
    """2x2 grid of panels (4-mode) or stacked 2 (2-mode):
        top-left:    level-of-disagreement distribution (histogram + cluster fit)
        top-right:   similarity score distribution (histogram + same-fiber tiers)
        bottom-left: per-pair likelihood vs level of disagreement
        bottom-right: per-pair likelihood vs similarity score
    When `shape_rs` is None, reverts to a 2-panel column (top-left + bottom-left)."""
    if shape_rs is not None:
        # 13x6 keeps the chart compact enough that section 1 banner + the 2x2
        # grid fit on the same landscape page as the title/cards header.
        fig, axes = plt.subplots(2, 2, figsize=(13, 6))
        ax1, axR  = axes[0, 0], axes[0, 1]
        ax2, axRS = axes[1, 0], axes[1, 1]
    else:
        fig, axes = plt.subplots(2, 1, figsize=(13, 5.5))
        ax1, ax2 = axes
        axR = axRS = None
    legend_kw = dict(loc='upper center', bbox_to_anchor=(0.5, -0.30),
                     ncol=2, fontsize=7.5, frameon=False)

    log_s = np.log10(np.maximum(scores, 1e-9))
    counts, bin_edges, _ = ax1.hist(log_s, bins=50, color='#4A90D9',
                                    alpha=0.75, edgecolor='white')
    bin_width = bin_edges[1] - bin_edges[0]
    # Scale the Gaussian PDF to raw-count units so it overlays the histogram.
    x = np.linspace(log_s.min() - 0.2, log_s.max() + 0.2, 400)
    ax1.plot(x, norm.pdf(x, stats['center_log'], stats['spread_log']) * len(log_s) * bin_width,
             color='#b97000', linewidth=2, label='cluster fit')
    ax1.axvline(stats['center_log'], linestyle='--', color='#b97000', alpha=0.7)
    for z_line in (-3, -5, -10):
        ax1.axvline(stats['center_log'] + z_line * stats['spread_log'],
                    linestyle=':', color='#888', alpha=0.5)
    ax1.set_xticklabels([])
    ax1.set_xlabel('level of disagreement (log scale)')
    ax1.set_ylabel('Number of pairs')
    ax1.set_title('Pair level-of-disagreement distribution with cluster fit', fontweight='bold')
    ax1.legend(**legend_kw)
    ax1.grid(alpha=0.3)

    if axR is not None:
        rs = np.asarray([r if r is not None else np.nan for r in shape_rs],
                        dtype=np.float64)
        rs_valid = rs[~np.isnan(rs)]
        # Always show out to similarity = 1.0 with the 0.95/0.99 thresholds
        # visible, so the reference lines anchor the reader's eye.
        lo = min(0.4, float(rs_valid.min()) - 0.02) if rs_valid.size else 0.4
        hi = 1.005
        if rs_valid.size:
            bins = np.linspace(lo, hi, 60)
            axR.hist(rs_valid, bins=bins, color='#4A90D9', alpha=0.75,
                     edgecolor='white')
            # Tier markers: green ≥ 0.99, orange 0.95–0.99, grey < 0.95.
            axR.axvspan(0.99, hi, color=_COLOR_HIGH, alpha=0.10)
            axR.axvspan(0.95, 0.99, color=_COLOR_MID, alpha=0.10)
            axR.axvline(0.99, linestyle='--', color=_COLOR_HIGH, linewidth=1.3,
                        label='≥ 0.99 (same fiber)')
            axR.axvline(0.95, linestyle=':', color=_COLOR_MID, linewidth=1.2,
                        label='= 0.95 (borderline floor)')
        axR.set_xlim(lo, hi)
        axR.set_xlabel('similarity score per pair')
        axR.set_ylabel('Number of pairs')
        ttl = ('Similarity score distribution — duplicates concentrate near 1.0'
               if rs_valid.size else 'Similarity score unavailable')
        axR.set_title(ttl, fontweight='bold')
        axR.legend(**legend_kw)
        axR.grid(axis='y', alpha=0.3)

    # Tier masks: high ≥ 0.9, mid 0.5–0.9, low ≤ 0.5. Colors match the tables.
    p = np.asarray(p_dup)
    m_hi = p > 0.9
    m_md = (p > 0.5) & (~m_hi)
    m_lo = ~(m_hi | m_md)
    if m_lo.any():
        ax2.scatter(log_s[m_lo], p[m_lo], s=45, alpha=0.6, color=_COLOR_LOW,
                    edgecolor='white', linewidth=0.5,
                    label=f'Non-duplicate (n={int(m_lo.sum())})')
    if m_md.any():
        ax2.scatter(log_s[m_md], p[m_md], s=120, alpha=0.95,
                    color=_COLOR_MID, edgecolor='black', linewidth=1, zorder=4,
                    label=f'Borderline 50–90% (n={int(m_md.sum())})')
    if m_hi.any():
        ax2.scatter(log_s[m_hi], p[m_hi], s=140, alpha=0.95,
                    color=_COLOR_HIGH, edgecolor='black', linewidth=1, zorder=5,
                    label=f'Duplicate ≥90% (n={int(m_hi.sum())})')
    ax2.axhline(0.9, color=_COLOR_HIGH, linestyle=':', alpha=0.4, linewidth=1)
    ax2.axhline(0.5, color=_COLOR_MID, linestyle='--', alpha=0.5, linewidth=1)
    ax2.set_xticklabels([])
    ax2.set_xlabel('level of disagreement (log scale)')
    ax2.set_ylabel('duplicate likelihood')
    ax2.set_title('Per-pair likelihood vs level of disagreement', fontweight='bold')
    ax2.legend(**legend_kw)
    ax2.grid(alpha=0.3)

    if axRS is not None:
        # Per-pair likelihood vs similarity score (Pearson r). Same tier-color
        # masks as the disagreement scatter, so high/mid/low pairs render
        # consistently between panels.
        rs_full = np.asarray([r if r is not None else np.nan for r in shape_rs],
                             dtype=np.float64)
        valid = ~np.isnan(rs_full)
        m_hi_v = m_hi & valid
        m_md_v = m_md & valid
        m_lo_v = m_lo & valid
        if m_lo_v.any():
            axRS.scatter(rs_full[m_lo_v], p[m_lo_v], s=45, alpha=0.6,
                         color=_COLOR_LOW, edgecolor='white', linewidth=0.5,
                         label=f'Non-duplicate (n={int(m_lo_v.sum())})')
        if m_md_v.any():
            axRS.scatter(rs_full[m_md_v], p[m_md_v], s=120, alpha=0.95,
                         color=_COLOR_MID, edgecolor='black', linewidth=1, zorder=4,
                         label=f'Borderline 50–90% (n={int(m_md_v.sum())})')
        if m_hi_v.any():
            axRS.scatter(rs_full[m_hi_v], p[m_hi_v], s=140, alpha=0.95,
                         color=_COLOR_HIGH, edgecolor='black', linewidth=1, zorder=5,
                         label=f'Duplicate ≥90% (n={int(m_hi_v.sum())})')
        axRS.axhline(0.9, color=_COLOR_HIGH, linestyle=':', alpha=0.4, linewidth=1)
        axRS.axhline(0.5, color=_COLOR_MID, linestyle='--', alpha=0.5, linewidth=1)
        axRS.axvline(0.99, color=_COLOR_HIGH, linestyle=':', alpha=0.4, linewidth=1)
        axRS.axvline(0.95, color=_COLOR_MID, linestyle='--', alpha=0.5, linewidth=1)
        # Lock x-axis so the 0.95 / 0.99 reference lines always show.
        rs_valid_pts = rs_full[valid]
        rs_lo = min(0.4, float(rs_valid_pts.min()) - 0.02) if rs_valid_pts.size else 0.4
        axRS.set_xlim(rs_lo, 1.005)
        axRS.set_xlabel('similarity score per pair')
        axRS.set_ylabel('duplicate likelihood')
        axRS.set_title('Per-pair likelihood vs similarity score', fontweight='bold')
        axRS.legend(**legend_kw)
        axRS.grid(alpha=0.3)

    plt.tight_layout()
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close()
    buf.seek(0)
    return base64.b64encode(buf.read()).decode('ascii')


# ── Raw-identity short-circuit ────────────────────────────────────────────
# A pair whose RAW interior metrics are essentially identical is a copy of
# the same acquisition data — no regime routing may hide it.  Applied to the
# PRE-fingerprint metrics (σ on raw traces, r on detrended-only traces), so
# tie_panel median-subtraction can't cancel the shared signal first: a
# byte-identical file pair in a tie-panel folder previously came back
# "no duplicates" because both residuals against the group median were
# equal (or exactly zero when the copies ARE the median) and σ-outlier is
# bypassed in that regime.
#
# Thresholds (calibrated 2026-07-14 on real spans):
#   byte-identical pair (measured through this float32 pipeline)
#                              →  raw σ = 3.8e-6 dB, raw r = 1.000000
#   closest NON-copy pair seen →  SANDUR 107↔146: σ 0.00495 dB, r 0.9982
#                                 (864-file span; NOT md5-identical, 5 m
#                                  length delta — near-identical re-shoots
#                                  that current main does NOT flag >0.5)
#   A-F West 145-288 tie panel →  min raw σ 0.0290 dB, max raw r 0.810
#   SEANOR / ELMMIL            →  min raw σ 0.0162 / 0.0079 dB
# The σ floor 0.001 dB sits ~260× above a true copy and ~5× below the
# closest real-world non-copy, so only a literal copy / re-export of the
# same acquisition data can trip it.  raw r ≥ 0.98 is a co-gate (copies
# read exactly 1.0).  The short-circuit only ever RAISES p_dup (to 1.0)
# — it can't demote, so no existing detection is weakened.
_RAW_IDENT_R = 0.98
_RAW_IDENT_SIGMA_DB = 0.001

# ── Distance-decay (shared-glass) tie-panel routing ───────────────────────
# Tie panels whose files share physical glass (jumper feed + ribbon) show
# raw-r that DECAYS with port distance: neighbors correlate strongly, far
# ports don't.  Real duplicate files don't care about port distance, so
# decay is a folder-level signature of shared path — route those folders to
# tie_panel (fingerprint extraction) even when bulk_r / frac_high_r stay
# low because the correlation tops out among neighbors only.
# Calibration (2026-07-14):
#   A-F West 145-288 (the FP flood): near r 0.580 vs far r 0.096 → decay 0.48
#   SEANOR 432-file production span: near r 0.574 vs far r 0.472 → decay 0.10
#   SANDUR (if it were consulted):   near r 0.928 vs far r 0.895 → decay 0.03
# 0.30 splits those cleanly.
#
# ── AUDITED 2026-08-29: RETIREMENT CONSIDERED AND REJECTED ────────────────
# After the same-instrument fix (`serials`), the drop measured on every
# folder on disk is:
#   A-F West 0.0344 | ELMMIL 0.0353 | SANDUR 0.0655 | EMVSUI Long 0.0745
#   SEANOR 0.1024 | NIL->MEC 0.1466 | SUIEMV Long 0.2065 | MEC->NIL 0.2286
# ZERO of the 13 folders is routed by this rule, and deleting it changes no
# folder's regime.  Its own calibration case is also gone: the A-F West
# figure above was taken on a 305 m collapsed window, and the later
# window-restoration fix took that folder to 2037 m where it measures 0.0344
# and routes tie_panel on bulk_r 0.9485 / frac_high_r 0.4829 instead.  (It
# was never solely dependent on decay anyway — its raw min_L of 1005 m also
# trips _SHORT_COMMON_SPAN_M, and both constants landed in commit 777c5fb.)
#
# RETIRING IT ANYWAY WAS REJECTED, on measurement.  Across the corpus the
# rule's effect is: zero duplicates suppressed, some false positives
# suppressed.  The one folder that still crosses the trigger is SUIEMV Long
# restricted to serial 989584 (547 files, decay 0.3255); deleting the route
# there takes the report from 0 pairs to 10, and all 10 are false positives
# on a folder with no duplicates — 8 of which already print in the
# as-delivered run.  EMVSUI Long's worst single-instrument half measures
# 0.0826, a 3.6x margin against the trigger, so the folder carrying the four
# known duplicates is not close to being re-broken.
#
# KNOWN, UNFIXED: what the near/far drop measures is confounded with
# ELAPSED ACQUISITION TIME, not just port distance — near pairs are shot
# minutes apart and far pairs days apart.  Time-matched on SUIEMV/989584
# (both buckets dt < 1 h) the drop is -0.008.  Ordinary long cables do carry
# a real port gradient too (0.13-0.25 after time-matching), so the premise
# "decay implies shared launch glass" is not safe in either direction.  The
# rule is left in place because it currently costs nothing and removing it
# has no measured benefit; the near/far medians are now printed on EVERY run
# so the first folder it does misroute is visible in the log.  The rule is only consulted for folders the
# existing rules would have called 'production' (additive routing), so
# all_dups / short_panel / tie_panel folders can never be re-routed by it.
_DECAY_NEAR_GAP = 3        # |port Δ| ≤ 3 → "neighbor" pair
_DECAY_FAR_GAP = 30        # |port Δ| ≥ 30 → "far" pair
_DECAY_MIN_PAIRS = 10      # need this many near AND far pairs to judge
_DECAY_MIN_DROP = 0.30     # near_r − far_r ≥ this → shared-path structure
# Very short common spans are launch+connector dominated — the interior
# window is too short for the Rayleigh fingerprint to separate fibers, so
# σ-outlier cascades on shared structure (A-F West: 1 005 m common span,
# 305 m interior → 1 997 false positives in production mode).
_SHORT_COMMON_SPAN_M = 2000.0
_ALLDUPS_MIN_SPAN_M = 15000.0   # all_dups needs >= this much common window

# all_dups SELF-REFUTATION check (2026-07-31).  The all_dups claim is
# "every file in this folder IS the same physical fiber".  If that were
# true MOST pairs would be near-identical by definition, so frac_high_r
# (the fraction of pairs at raw r >= 0.95) would be near 1.  A folder that
# claims all_dups while frac_high_r reads 0.00 is refuting itself: it has
# NO near-identical pairs at all, and the only thing that put it in the
# regime is a bulk median sitting a hair inside the gate.
#   BKF->DEL 80 km span, boss's 2026-07-30 run (all 48 pairs adjudicated
#   FALSE POSITIVE):  A dir bulk σ 0.0979 (< 0.10 by 2 mdB), bulk r 0.8014
#   (>= 0.7), span 80 km — routed all_dups with frac_high_r = 0.00, which
#   skips the σ-outlier tier, skips the twin gate, and widens the r-ramp
#   to 0.85-0.95 so an ordinary r ~0.91 walks to "Likely duplicate".
#   Under production routing the SAME folder reports ZERO pairs (measured).
# 0.50 is deliberately generous: a genuine all-duplicates folder sits at
# ~1.0.  Measured across the whole 2026-07-31 ripple corpus (55 folder
# runs, 21 spans), 12 folders routed all_dups and EVERY ONE of them read
# frac_high_r <= 0.03 — bkfdel A/B (0.00), tulbar B/AB (0.02/0.01) and the
# eight span3 Indio-Mecca runs (0.00-0.03).  Not one folder on disk is
# genuinely all-duplicates, so in practice this check turns the regime OFF
# for the corpus we have; that is the finding, not a side effect.  A folder
# that really is one fiber shot 400 times would clear 0.50 by a mile.
# The signal was already computed and PRINTED on the Regime: line — it just
# was never consulted.
_ALLDUPS_MIN_HIGHR_FRAC = 0.5
# NOT applied to the tie_panel route.  tie_panel is the CONSERVATIVE
# destination (fingerprint extraction + the 0.999-0.9999 ramp + σ-outlier
# bypassed), so demanding a high frac_high_r there would push folders the
# other way — into production, where σ-outlier is live.  Measured
# counter-example: the BKF+DEL combined 864-file folder routes tie_panel on
# bulk r 0.7256 with frac_high_r 0.00 and reports ZERO pairs; a
# frac_high_r sanity on that route would have sent it to production
# instead.  Leave tie_panel routing untouched.

# Uniqueness (twin) gate — ALL regimes.  A true duplicate is
# UNIQUELY close to its twin: its pair σ sits far below its σ to every
# other file.  Ribbon-family members (same tube position in adjacent
# ribbons: Δ24/Δ48 ladders like Lumen Border 146-194-218-242) share helix
# micro-structure on pristine cables and land at σ ~0.009 / r ~0.99
# against SEVERAL partners at once — no unique twin, so they are cable
# geometry, not duplication.  A flagged pair must have pair-σ ≤ this
# fraction of the smaller member's next-best σ, or it caps at borderline.
#
# 2026-07-31: the gate used to run in the production regime ONLY, on the
# reasoning that the other regimes "have their own machinery".  They don't
# — all_dups / tie_panel / short_panel bypass σ-outlier entirely, so a
# regime misroute took the twin gate off the board at exactly the moment
# it was needed.  On the BKF↔DEL all_dups misroute every one of the 47
# flagged pairs had a twin ratio of 1.00-1.71 (its σ was no closer to its
# "twin" than to the rest of the folder) and the gate would have capped
# all 47.  It now runs in every regime; the semantics are unchanged, and
# it reads the RAW σ matrix in all regimes (tie_panel_mode only changes r).
_UNIQ_TWIN_RATIO = 0.5
                                # (ELMMIL long shots: 69.5 km — comfortably in;
                                # Span 7 short shots: 5 km — routed tie_panel)

# ── Rayleigh-speckle confirmation gate ────────────────────────────────────
# The class-closer for "similar-looking but different fiber".  Below the
# splice/attenuation structure that σ and detrended-r measure sits the
# Rayleigh backscatter speckle: the frozen-in, sub-pulse-width interference
# pattern of each individual fiber's scattering centres.  It is
# DETERMINISTIC per fiber — the same fiber re-shot on the same instrument
# reproduces it — and independent between fibers, even fibers in the same
# ribbon of the same cable that share every macroscopic feature.
#
# Method: subtract a moving average of _SPECKLE_HP_WIDTH samples (the
# low-pass that carries splice/attenuation structure), keep the residual,
# and Pearson-correlate the two residuals inside each analysis window.
# Windows are FRACTIONS of the interior, taken from the launch-side (high
# SNR) 60% — the far end runs into the noise floor where every pair
# decorrelates.  Score = MAX over windows (the most permissive combiner:
# one window agreeing is enough to confirm).
#
# NO FIXED CONFIRM THRESHOLD — that was the first cut and it is WRONG.
# A same-fiber pair's speckle correlation falls off with how much
# acquisition noise separates the two shots: s²/(s²+σ²/2) for band
# amplitude s and pair disagreement σ.  Measured band amplitudes are ~7.4
# mdB (BKF, dz 2.55 m) and ~3.6 mdB (DEL), so a genuine re-shoot at σ =
# 20 mdB reads only ~0.35, at σ = 40 mdB only ~0.08.  Any fixed threshold
# high enough to reject the BKF false positives (r_hp <= 0.078) would also
# reject real duplicates at those σ — measured directly by injecting white
# noise into a real file:
#     BKF  σ 0.0149 -> same-fiber control 0.320   (fixed 0.25 keeps it)
#     BKF  σ 0.0396 -> same-fiber control 0.077   (fixed 0.25 KILLS it)
#     CHEPLA σ 0.0098 -> control 0.091            (fixed 0.25 KILLS it)
#     NILMEC σ 0.0125 -> control 0.217            (fixed 0.25 KILLS it)
# So the gate is scored against the pair's OWN same-fiber floor instead:
#
#   r_floor = s²/(s²+σ²/2)   (_speckle_same_fiber_floor — the LOWEST value
#             the same-fiber hypothesis can produce at this pair's σ; any
#             low-frequency part of the difference only raises the truth)
#   null_q  = the 99th percentile of r_hp over a sample of KNOWN-different
#             pairs in this same folder (what chance looks like here)
#
#   VETO iff   r_floor >= null_q                (the statistic can tell
#                                                same from different at
#                                                this pair's σ, on this
#                                                folder — otherwise abstain)
#        and   r_hp <= r_floor / _SPECKLE_FLOOR_MARGIN
#                                               (the pair is far below
#                                                even the worst same-fiber
#                                                case — not one fiber)
#
# Worked (2026-07-31).  Across the whole 62-run ripple corpus the gate
# vetoed exactly THREE pairs — it is a narrow, high-confidence instrument,
# NOT the thing that fixes BKF.  All three were then corroborated by an
# INDEPENDENT check the gate never sees: the same two fiber numbers shot
# from the OTHER end.  Same glass reads the same from both ends.
#   PLACHE0665↔0666  A-dir σ 0.0057 r 0.998 (looks like a copy)
#                    r_hp 0.0008  floor 0.206  null_q 0.137  -> VETO
#                    B-dir CHEPLA0665↔0666: σ 0.0993 r 0.336 = different
#                    fibers.  Veto correct.
#   WNHNIL413↔414    A-dir σ 0.0070 r 0.992, events 11/12 @ 1.1 mdB —
#                    the classic true-duplicate signature, and the only
#                    gate that catches it.  r_hp 0.0586 floor 0.445
#                    null_q 0.110 -> VETO.  B-dir NILWNH413↔414: σ 0.0537
#                    r 0.747 = different fibers.  Veto correct.
#   NILMEC418↔424    σ 0.0125 r_hp 0.0395 floor 0.209 null_q 0.201 ->
#                    VETO, but the competence margin is THIN (1.04x).
#                    B-dir MECNIL418↔424 reads r_hp -0.047 against floor
#                    0.308 — both ends independently say "no shared
#                    speckle".  Veto correct, margin noted.
# Kept (gate declined to act):
#   TUCROM453↔454    σ 0.0040 r_hp 0.184 floor 0.250 — within 1.4x of the
#                    floor, not far enough below: kept.
#   CHEPLA0167↔0168  σ 0.0098 floor 0.091 < null_q 0.154: ABSTAIN, kept.
#   BKF's 47 + DELBKF138↔162 (σ 0.021-0.12): floor at/below null_q, so the
#                    gate abstains; the router / twin / events gates are
#                    what clear those.
#   byte-identical copy: σ 0 -> floor 1.0, r_hp 1.0 -> never vetoed.
#   BKF synthetic re-shoots (+10/+15/+20 mdB white noise on a real file):
#                    r_hp 0.594/0.443/0.349 vs floor 0.44/0.32/0.25 —
#                    all comfortably above floor/3, all kept.
#
# DEMOTE-ONLY and FAIL-SAFE, matching the splicereport re-measure gates:
# the gate can only cap a pair some other tier already pushed over the
# print threshold, it never promotes, and anything UNMEASURABLE
# (mismatched sample spacing, too few samples, a flat/saturated window,
# too few files to build a null) confirms by default.
# HIGH-PASS WIDTH IS PER-FOLDER (2026-08-29).  21 samples was tuned on
# 500 ns / 25 ns acquisitions, where it happens to equal one pulse width.
# The pulse expressed in SAMPLES varies 15x over the acquisitions on disk
# (275ns/25ns = 11, 500ns/25ns = 20, 2500ns/50ns = 50, 10ns/3.125ns = 3.2,
# 5ns/0.78ns = 6.4), so a fixed sample count is a different filter on every
# span.  Run WIDER than the pulse and splice steps survive the moving
# average and land in the residual as a bipolar spike of the same sign in
# every fiber, which is common-mode and inflates the folder null.
#
# MEASURED on Mecca<->Niland (275 ns / 25 ns, pulse = 11 samples, so the
# shipped 21 is 1.9 pulse widths): 10.2% of the null sample's residual
# energy sits beyond 3 MAD, concentrated exactly on the splice set
# (5.45-5.71 km, 10.46-10.83 km, 21.4 km — events the table places in
# 150-454 of the 576 files).  Folder null p99 reads 0.201.  Narrowed to the
# pulse it reads 0.072, and NILMEC498<->504 goes from r_hp +0.068 (gate
# ABSTAINS, floor 0.124 < null 0.201, pair prints at p_dup 1.0) to -0.132
# against floor 0.095 (VETO).  The B direction agrees independently:
# MECNIL498<->504 +0.147 -> -0.030, null 0.092 -> 0.067, keep -> VETO.
#
# WHETHER THAT DEMOTION IS CORRECT IS UNKNOWN, and this comment previously
# claimed otherwise.  It said the field had ruled the pair out.  It had not:
# the question asked was whether 498/504 could be a tie-panel or short-shot
# artifact, which is uncertainty, not a verdict.  Recording it as ground
# truth was this file's error, not the field's.
#
# The width change stands on its own measurement — 21 samples is 1.9 pulse
# widths on this acquisition and demonstrably passes splice structure into
# the residual — but it should not be read as CONFIRMED by the 498/504
# outcome, because that outcome has no known answer to be confirmed against.
# What the evidence actually says, both ways:
#   FOR the pair being real: sigma 0.0171 / 0.0235, detrended r 0.9955 /
#     0.9851, EOF identical to 0.1 m in BOTH directions, residual flat at
#     ~0.00 dB across all 61 km (per-bin std 0.006-0.012), and event tables
#     matching 9 of 10 with median |dloss| 0.0020 dB — TIGHTER than
#     confirmed duplicate 563/564 at 0.0030.
#   AGAINST: at the pulse-matched width the Rayleigh fingerprint sits below
#     the folder's own different-fiber null in both directions.  That is
#     NEGATIVE evidence — no co-located shared backscatter where some should
#     be visible — not proof of different glass.
# Everything except the fingerprint points at a duplicate.  The gate now
# demotes it; if that is wrong, this is where a real duplicate is being
# suppressed.  Re-shooting 498 and 504 from either end would settle it: a
# genuine re-shoot reads 0.5-0.9 at lag 0, the way 359/360 and all four
# EMVSUI duplicates do.
#
# THIS IS AN EMPIRICAL RULE, NOT A DERIVED ONE.  "Match the filter to the
# pulse" is NOT sufficient on its own: at matched width the null reads 0.072
# (NILMEC), 0.086 (EMVSUI L), 0.363 (SEANOR), 0.493 (SANDUR), 0.955
# (A-F West), 0.976 (EMVSUI Short) — a 13x spread, so w/pulse is not the
# controlling variable.  The cap at 21 is what keeps the long-pulse folders
# (SEANOR/SANDUR, pulse 50 samples) on the width they were calibrated with.
# The rule therefore only ever NARROWS, and only on acquisitions whose pulse
# is shorter than the calibrated 21.
#
# CONTROL SET, stated honestly: across the 13 folders on disk the speckle
# gate evaluates FIVE pairs in total, all of them in the two Mecca<->Niland
# directions.  Every other folder is a no-op because the gate never runs
# there, not because the width was shown to be safe there.  Full-engine A/B
# runs: NIL->MEC 1 of 165,600 pairs changed, MEC->NIL 1 of 165,600, and ZERO
# of 1.39 million across A-F West, A-F East, LAMBEY, EMVSUI Long, EMVSUI
# Short (at w=5), SUIEMV Long and SUIEMV Short.  EMVSUI's four confirmed
# duplicates keep their verdicts (79/80 0.467, 511/512 0.909, 563/564 0.615,
# 296/308 0.478 — unchanged, the folder resolves to 21).  The veto is not
# knife-edged: 498/504 vetoes at w = 7, 9, 11, 13 and 15.
_SPECKLE_HP_WIDTH = 21          # moving-average width in SAMPLES (odd); CAP
_SPECKLE_HP_WIDTH_MIN = 5       # never narrow below this, however short the pulse
# ONE union window, not three combined with MAX.
#
# MAX across sub-windows is the WORST combiner available.  It lifts the null
# far more than it lifts the true minimum, because the null gets to take the
# best of k draws while a true pair only needs one to agree.  Measured on
# EMVSUI Long against 54,285 known-different pairs,
# margin = (true minimum) - (null maximum):
#
#     k=1  single window                                      +0.242   4/4
#     k=2  MAX +0.075   MEAN +0.101   MEDIAN +0.101           all 4/4
#     k=3  MAX +0.007   MEAN +0.116   MEDIAN -0.035        MEDIAN 3/4
#     k=5  MAX -0.090   MEAN +0.020   MEDIAN -0.080           MAX 1/4
#
# At the shipped k=3 the MAX combiner had spent almost the whole margin.
#
# The union deliberately stops at 0.60.  Widening to the entire interior
# pulls in the far end where the SNR is gone: null p50 jumps to +0.5686,
# null max to +0.9714, and the harness collapses to 1/4 at zero false
# positives with 564 of them.  The 2-60% placement is doing real work.
#
# MEASURED RIPPLE on the two folders on disk that carry long-span
# candidates - every verdict identical, folder null nearly halved:
#
#     Mecca 576f    2 candidates, 2 demoted, 498/504 at 0.5
#                   null p99  0.067 -> 0.036
#     Niland 576f   3 candidates, 2 demoted, 1 inconclusive,
#                   359/360 at 0.8505
#                   null p99  0.072 -> 0.037
#
# The halved null is margin the gate did not have before, spent on nothing
# yet.  Sub-sample alignment, which is what turns that margin into changed
# verdicts, is deliberately NOT part of this change.
_SPECKLE_WINDOWS = ((0.02, 0.60),)
_SPECKLE_MIN_SAMPLES = 500      # per window, after high-pass edge trim
_SPECKLE_DZ_TOL = 1e-6          # relative sample-spacing match required
_SPECKLE_FLOOR_MARGIN = 3.0     # r_hp must be this far below r_floor to veto
_SPECKLE_NULL_FILES = 60        # evenly-spaced folder sample for the null
_SPECKLE_NULL_PCT = 99.0        # percentile of that null a veto must clear
_SPECKLE_NULL_MIN_PAIRS = 100   # fewer than this -> no null -> no vetoes
# ── Where the band starts: span-relative, then past the launch hardware ─────
# _SPECKLE_WINDOWS places the band at 2%-60% of the interior window, which is
# span-relative; the interior itself starts at a FIXED _LAUNCH_SKIP_M = 500 m,
# which is not.  On a long span the 2% offset clears a launch reel by itself
# (2% of 64 km is 1.3 km).  On a short span it does not: Goodland->Monument is
# 4,993 m shot over a 1,005 m reel, so the band ran 586-3,076 m and swallowed
# the reel's panel connector.  That connector is the SAME hardware in every
# shot of the session and its residual is 30x the glass (2.93 dB peak against
# 0.11 dB), so it dominates the normalised residual and every pair of
# DIFFERENT fibres reads r 0.92.  The bar is 3x the null p99, so it went to
# 2.79 - above the 1.0 a correlation can reach - and the folder was reported
# as "confirmation impossible in this acquisition class" when the real fault
# was the window.  Measured on that folder, band right edge held at 3,076 m:
#
#     band start   null p50   null p99    bar     ratio
#       586 m       +0.000     0.929     2.787   0.001   (what ships today)
#     1,006 m       +0.174     0.935     2.804   0.066   (1 m past the event)
#     1,010 m       +0.001     0.855     2.564   0.018   (5 m past)
#     1,015 m       +0.001     0.043     0.129   0.371   (10 m past)
#     1,060 m       +0.001     0.044     0.131   0.364
#     1,505 m       +0.001     0.049     0.148   0.324
#
# Ten metres past the event is enough at 10 ns, i.e. about ten pulse widths,
# so the guard scales with the pulse: a wider pulse smears the event further.
# The SEARCH for that reflector is span-relative - only an event inside the
# first _SPECKLE_LAUNCH_ZONE_FRAC of the interior can be launch hardware - so
# a reflective splice at 20 km of a 64 km span is never mistaken for a reel,
# and a folder whose band already clears its launch is left byte-identical.
_SPECKLE_LAUNCH_ZONE_FRAC = 0.25     # past this it is plant, not launch gear
_SPECKLE_EVENT_GUARD_PULSES = 20.0   # dead-zone guard past a shared reflector
_SPECKLE_EVENT_GUARD_MIN_M = 25.0    # floor for that guard
_SPECKLE_SHARED_EVENT_FRAC = 0.5     # in this share of files to count as shared
# Multiple of the folder null a pair must clear for its fingerprint to
# REFUTE the twin gate's sigma-ratio proxy (see the twin-gate block).  The
# null is already the 99th percentile of known-different pairs, so this is
# a deliberately high bar.  Measured on EMVSUI0 Long Shots: over 4,005
# known-different pairs sampled from 90 fibers across the cable the
# statistic reads p50 0.024, p99 0.084, p99.9 0.103 and MAXIMUM 0.111,
# while the four confirmed same-fiber pairs read 0.467, 0.478, 0.615 and
# 0.909.  3x p99 (0.258 there) sits in the empty band between them.
_SPECKLE_CONFIRM_NULL_MULT = 3.0
# COMPETENCE CEILING for that bar.  _SPECKLE_CONFIRM_NULL_MULT was calibrated
# on the one corpus where the folder null happens to be small, and the product
# `mult x null_p99` is NOT scale-free.  Measured across every span class on
# disk (2026-08-31), with each folder's own engine-selected high-pass width:
#
#     folder                 span      null p50   null p99   3x bar
#     EMVSUI Long           78.5 km     +0.024     +0.086     0.257   usable
#     BETA tray               62 m      +0.250     +0.573     1.718
#     LSC1->LSC6              31 m      +0.244     +0.600     1.800
#     Reubensville ILA5       31 m      +0.225     +0.688     2.064
#     Dinwiddie             2.07 km     +0.911     +0.961     2.883
#     EMVSUI Short          3.99 km     +0.031     +0.976     2.927
#     ELMMIL sh             4.99 km     +0.971     +0.976     2.929
#
# A Pearson r cannot exceed 1.0, so on four of the five span classes the bar
# is not a conservative gate — it is a DISABLED one that reads in the run log
# exactly like a gate that ran and found nothing.  "0 confirmed by
# fingerprint" is abstention, not refutation, and a tech cannot tell the two
# apart.  Swept at hp = 5/7/11/21/41/101: the bar stays above 1.0 at every
# width for every class except 78 km, so it is not a filter-width artifact.
#
# This constant does NOT change any verdict.  It decides only whether the run
# log is allowed to imply the gate did work.  The multiplier itself is left
# alone deliberately: lowering it would arm the gate on folders whose
# extreme-sigma candidate sets run 132 to 6,081 pairs (see the rescue block
# below), and that is a measurement to be made before, not during, a repair.
_SPECKLE_BAR_MAX = 0.90

# ── Detector competence, from physics rather than a sample count ─────────────
# Whether THIS folder's traces can carry a duplicate verdict at all is set by
# two numbers the folder itself provides, plus one property of glass:
#
#   _FINGERPRINT_DB   the fibre's own Rayleigh fingerprint amplitude in the
#                     speckle band.  A property of the glass, not the shot:
#                     measured flat at 0.00344 dB (+/-8%) while received power
#                     fell 12 dB along one 78 km trace and the noise rose 9.9 dB
#                     (EMVSUI Long, eight 9 km segments, 2026-08-31).
#   sigma_band        the folder's speckle-band residual amplitude, measured per
#                     file.  At 500 ns it is ~0.004 dB (fingerprint-dominated);
#                     at 5 ns and 10 ns it is 0.016-0.050 dB (noise-dominated).
#   the folder null   what known-different pairs read here (p50, p99).
#
# A genuine re-shoot reads   null p50 + REPRO x (fingerprint / sigma_band)^2
# and must clear the confirm bar (_SPECKLE_CONFIRM_NULL_MULT x null p99).
# Calibrated on every folder with known truth (2026-09-02, calib.py):
#
#     folder            pulse  span   sigma  predicted  bar    ratio  truth
#     EMVSUI Long       500ns  78 km  .0042    0.479    0.128   3.7   4/4 found
#     MILTOP            500ns  62 km  .0044    0.424    0.217   2.0   (production)
#     ELMMIL short       10ns   5 km  .0492    0.920    2.781   0.33  none found
#     Dinwiddie A x B     5ns   2 km  .0381    0.654    2.776   0.24  0/48 found
#     BETA tray           5ns   62 m  .0108    0.198    1.136   0.17  none found
#     Cle Elum W288       5ns  104 m  .0087    0.149    0.974   0.15  none found
#     retruetest + LSC    5ns   31 m  .0158    0.116    1.341   0.09  0/66 found
#     EMVSUI Short       10ns   4 km  .0497    0.005    2.810   0.00  0/4 found
#
# The measured same-fibre r on EMVSUI Long is 0.39-0.73 against a predicted
# 0.68 with REPRO = 1, i.e. about 70% of a fingerprint survives a re-shoot
# (launch mating, polarisation, temperature); REPRO carries that.  Dinwiddie
# and EMVSUI Short same-fibre pairs READ 0.89, but so do time-adjacent pairs
# of different fibres there: that is the instrument, and the ratio ignores it
# by construction (it compares the fingerprint TERM to the folder's spread).
_FINGERPRINT_DB = 0.00344
_FINGERPRINT_REPRO = 0.70
_COMPETENCE_OK_RATIO = 1.5        # predicted same-fibre r / bar at or above: measurable
_COMPETENCE_MARGINAL_RATIO = 1.0  # between this and OK: marginal; below: NOT MEASURED
_COMPETENCE_NULL_FILES = 60       # evenly spaced diagnostic sample (mirrors _SPECKLE_NULL_FILES)
_COMPETENCE_MIN_CELLS = 25        # independent speckle cells a span must hold at a pulse width
_M_PER_NS = 0.10209               # one-way metres per ns of pulse at IOR 1.4682

# ── Mating likelihood: a calibrated ranking for folders the fingerprint cannot judge ──
# The field duplicate on a short panel is a tech who shoots the next "fibre"
# without moving the jumper.  What that leaves in the file is not the glass
# (unmeasurable at 5 ns, see _FINGERPRINT_DB) but the CONNECTOR MATINGS: the
# launch connector's loss and reflectance and the panel-port connector's
# loss and reflectance (see _mating_port_event) are unchanged between the two shots, while two
# different ports differ by the spread of two different connector pairs.
# Measured on retruetest (one jumper shot 12x, 5 ns, 31 m: 30 never-unplugged
# + 36 re-plugged pairs) against LSC1->LSC6 (288 different fibres, same
# instrument; the honest null is CONSECUTIVE ports shot seconds apart, which
# share instrument state), 2026-09-03, scratchpad lik_field/ + lik_build/:
#
#     feature                        AUC vs consecutive-port null
#     |d launch-connector loss|      0.90   (0.93 never unplugged)
#     |d first-connector refl|       0.84
#     |d first-connector loss|       0.75   (0.89 never unplugged, 0.64 re-plugged)
#     |d end reflectance|            0.74
#     |d launch reflectance|         0.66
#     instrument-state scalars only  0.51   (temperatures, noise, averages: NOT
#                                            what is being learned)
#     combined, out-of-fold          0.988; top 10 pairs: 8 true; label-shuffle 0.49
#
# Each feature's likelihood ratio is P(|d| | same port) / P(|d| | this folder's
# own pairs): the same-port density is a half-normal with the scale measured
# on the true pairs below, the folder density is the folder's own quantile
# bins, so the ratio self-calibrates to whatever instrument and connector
# population the folder carries.  Ratios multiply with exponent
# _MATING_ALPHA (features are not independent: two per connector); at 0.9 the
# top decile predicts 1.46% against 1.59% observed with the true prior.
# The percentage printed assumes _MATING_PRIOR_DUPS duplicate pairs per
# folder, and says so on the sheet; the ratio is printed beside it so the
# prior is never hidden.  This is a RANKING for a tech to check against the
# port log.  It never routes a pair and never touches p_dup.
_MATING_TRUE_SCALES = {'dl0': 0.0177,   # launch-connector loss, dB
                       'dl1': 0.0316,   # first interior connector loss, dB
                       'dr1': 0.1734,   # first interior connector reflectance, dB
                       'dr0': 0.6981,   # launch reflectance, dB
                       'drE': 0.1175}   # end-event reflectance, dB
_MATING_FEATURES = ('dl0', 'dl1', 'dr1', 'dr0', 'drE')
_MATING_ALPHA = 0.9
_MATING_NULL_BINS = 24
_MATING_PRIOR_DUPS = 1.0          # expected duplicate pairs per folder (stated prior)
_MATING_MIN_PAIRS = 30            # fewer pairs than this: no folder density, no ranking
# Feature gates, from the boss's RDR4RDR5 tray (2026-09-08: 18 ports, 13-18
# re-shoots of 1-6 ten minutes later, 5 ns through a 1 km reel + 31 m jumper,
# far end at 2.06 km).  Two features were WRONG there, not merely weak:
#   launch: the launch event read -80 dB, the noise floor behind the reel,
#           not a mating, so its "agreement" was random (true-pair |d| 0.39 dB
#           vs 0.26 dB for any pair).  Used only when the folder's launch
#           reflectance is a measured mating (median above the gate).
#   far end: at 2 km on 5 ns the far-end reflectance disagreed MORE on true
#           pairs (0.40 dB) than on random pairs (0.25 dB).  Used only when
#           the far end is within _MATING_END_MAX_M (retruetest: 1.04 km).
# With both gates: each re-shoot's original ranks 1st of 12 on five of the
# six pairs (2nd on the sixth); ungated, 1,1,3,5,1,5.  Calibration set
# (retruetest 12 x LSC 58): both gates stay open, AUC 0.999 unchanged.
_MATING_LAUNCH_MIN_REFL_DB = -70.0
_MATING_END_MAX_M = 1500.0
# A pair must be at least this sigma-likely before a sigma-bypassed regime
# will even look at its fingerprint.  0.99 is deliberately extreme: on the
# whole corpus it selects TWO pairs, both on MILTOP, and nothing at all on
# the folders whose cascades the bypass exists to prevent.
_SIGMA_RESCUE_MIN = 0.99

# ── Robust common span + suspected-break reporting ────────────────────────
# The common analysis span used to be the raw MINIMUM EOF over all files,
# so ONE broken fiber collapsed the whole folder's window.  A-F West
# 145-288 (266 files, median EOF 2 037 m) has port 198 physically broken
# ~1 km out — A-side EOF 1 005 m, B-side 1 036 m, and the two EOFs sum to
# the span — and that single strand shrank the interior to 305 m, hiding
# the folder-wide similarity and misrouting the regime (the 1 997-false-
# positive flood).  Two rules here:
#   1. Files whose EOF is far below the folder median are SUSPECTED BREAKS
#      — a finding in its own right, always reported (manifest key
#      `short_traces` + a "Suspected broken / short fibers" report section).
#   2. Suspected breaks are ALWAYS excluded from the common-span
#      computation and pair metrics — they physically lack the glass being
#      compared (their post-break samples are noise floor, not
#      backscatter) — provided ≥2 healthy files remain and the folder
#      passes the consistency guard below.
#
# LONG-SPAN WINDOW RESTORATION (2026-07-15): the first cut of rule 2 only
# excluded when the raw-min window had collapsed below the 2 km
# launch+connector floor (_SHORT_COMMON_SPAN_M), which preserved ELMMIL's
# and SANDUR's historical pair tables but left ELMMIL_1550 analyzing
# 22 288 m of a 69 554 m span (ELMMIL0231's break) and SANDUR 20 144 m of
# ~100 925 m (SANDUR841).  Robert approved changing those baselines
# (better detection is worth the numbers moving), so the 2 km-collapse
# precondition is gone: the window is always rebuilt from the healthy
# population.  _SHORT_COMMON_SPAN_M keeps its separate role in regime
# routing (short-common-span → tie_panel).
#
# SANITY GUARD (_INCONSISTENT_FOLDER_FRAC): if MORE than 20 % of the
# folder's files sit below 75 % of the median, that isn't "a few broken
# strands" — it's an inconsistent folder (mixed spans, wrong files) where
# the median itself is untrustworthy.  Exclude NOTHING, keep the raw-min
# window, and warn ("folder trace lengths are inconsistent …").
#
# Calibration (2026-07-15, per-file EOFs on real folders — the guard must
# fire on none of these):
#   A-F West 145-288: 2 of 266 (0.8 %) below cut → excluded, window
#                     restored to the healthy min 2 036.8 m (unchanged
#                     from the first cut — its raw min was < 2 km).
#   A-F East 1-144:   0 outliers (min 2 036.8 m ≈ median) → byte-identical.
#   SEANOR (432):     0 outliers (min 108 818 m = 99 % of median)
#                     → byte-identical.
#   ELMMIL (1152):    1 of 1152 (0.09 %): ELMMIL0231 ends 22 288 m (32 %
#                     of the 69 554 m median) → NOW excluded, window
#                     restored 22 288 → 69 549 m (baseline change,
#                     approved).
#   SANDUR (864):     2 of 864 (0.23 %): SANDUR841 @ 20 144 m (20 %) and
#                     SANDUR229 @ 59 219 m (59 %) → NOW excluded, window
#                     restored 20 144 → ~100 856 m (baseline change,
#                     approved).
_BREAK_FRAC_OF_MEDIAN = 0.75   # EOF below this × median ⇒ suspected break
_BREAK_AB_SUM_TOL = 0.10       # |EOF_A + EOF_B − median| ≤ this × median
_INCONSISTENT_FOLDER_FRAC = 0.20   # > this fraction short ⇒ guard, no exclusion

# Port extraction mirrors run_secretsauce._extract_fiber_num (fiber number
# before a wavelength suffix first, else trailing digits) so the two agree
# on which digits are the port:  ELMMIL0001_1550 → ('ELMMIL', 1),
# BCK1BCK60145 → ('BCK1BCK6', 145).
_PORT_WL_RE = re.compile(r'(\d{3,4})_\d{3,4}\b')
_PORT_TAIL_RE = re.compile(r'(\d{3,4})$')
# A trailing run of NON-digits hides the port from _PORT_TAIL_RE, and the
# whole filename then becomes its own one-member prefix group.  Measured on
# disk: 'DNW5DNW10271withstartstop' and friends, 347 files across 9 folders,
# 331 of them in folders where EVERY file collapses this way.  Split the
# suffix off, parse the head, then put the suffix BACK on the prefix so a
# folder mixing 'X0001' and 'X0001withstartstop' still gets two groups.
_PORT_SUFFIX_RE = re.compile(r'(\d)([^\d]+)$')
# Ports under 100 are 1-2 digits ('RCHDNW-A-66').  Only reachable once both
# rules above have failed, which guarantees the trailing digit run is 1 or 2
# long — a longer run already matched _PORT_TAIL_RE.  The separator
# lookbehind stops a route code ('...BCK6') or an ordinary word ending in a
# digit ('panel1') from donating its last digit as a port.
_PORT_SHORT_TAIL_RE = re.compile(r'(?<=[^0-9A-Za-z])(\d{1,2})$')


def _port_split(name):
    """Split a filename stem into (prefix, port).  The prefix doubles as the
    direction/route group so A→B and B→A shots never mix in gap stats.

    STRICTLY ADDITIVE (2026-08-29): the two original patterns are tried
    first and unchanged, so any name that parses today parses identically.
    The two fallbacks below only ever turn `port None` into a parsed port.
    Swept over 41,268 distinct stems / 57,628 files on disk: 330 stems
    change, every one of them None -> parsed, and ZERO where the old and new
    parsers both return a port and disagree.
    """
    m = _PORT_WL_RE.search(name) or _PORT_TAIL_RE.search(name)
    if m:
        return name[:m.start(1)], int(m.group(1))
    suf = _PORT_SUFFIX_RE.search(name)
    if suf:
        pref, port = _port_split(name[:suf.end(1)])
        if port is not None:
            return pref + suf.group(2), port
    short = _PORT_SHORT_TAIL_RE.search(name)
    if short:
        return name[:short.start(1)], int(short.group(1))
    return name, None


def _merge_zero_pad_prefixes(prefixes):
    """Fold '<P>0' into '<P>' when BOTH forms occur in the same folder.

    The greedy 4-digit tail eats a padding zero when the zero-padding is
    inconsistent: 'PTL5PTL1sh0232' -> ('PTL5PTL1sh', 232) but
    'PTL5PTL1sh00309' -> ('PTL5PTL1sh0', 309), orphaning that one file into
    its own group.  Data-driven and folder-scoped — it only fires when both
    the padded and unpadded form are present in the same call, so a prefix
    that legitimately ends in '0' with no shorter sibling is never touched.
    Measured: fires on 1 folder / 1 file across all 230 folders on disk.
    """
    uniq = set(prefixes)
    canon = {}
    for p in uniq:
        q = p
        while q.endswith('0') and q[:-1] in uniq:
            q = q[:-1]
        canon[p] = q
    return [canon[p] for p in prefixes]


def _neighbor_decay(names, r_matrix, serials=None,
                    near_gap=_DECAY_NEAR_GAP, far_gap=_DECAY_FAR_GAP,
                    min_pairs=_DECAY_MIN_PAIRS):
    """Neighbor-vs-far raw-r structure for the distance-decay regime test.

    `names` are filename stems aligned with `r_matrix` rows.  Pairs are
    compared only within the same prefix group (same route/direction) and
    only when both files carry a trailing port number.  Returns
    (near_r_median, far_r_median, n_near, n_far) or None when either bucket
    has fewer than `min_pairs` pairs (small folders can't trip this rule).

    SAME INSTRUMENT ONLY (`serials`, 2026-08-29).  The rule reads a drop in
    r between near and far port pairs as evidence of shared launch glass.
    That inference only holds when the two buckets differ in port distance
    and NOTHING ELSE.  On a folder shot by more than one OTDR the far
    bucket fills up with cross-instrument pairs, which decorrelate for
    reasons that have nothing to do with port distance, and the rule fires
    on a folder that has no tie panel in it.

    Measured on EMVSUI0 Long Shots (1152 fibers, 78.5 km, serials 1723356
    and 1876271 interleaved across the port range over 5 days):

        near (gap <= 3,  same OTDR)  r 0.746
        far  (gap >= 30, same OTDR)  r 0.677   <- port distance costs 0.069
        far  (gap >= 30, DIFF OTDR)  r 0.205   <- instrument costs 0.472

    The pooled far median landed at 0.37, the folder tripped the 0.30 drop,
    routed tie_panel, and tie_panel bypasses sigma-outlier — so the single
    tightest pair of all 662,976 (sigma 0.0095 dB against a bulk of 0.1238,
    z = -9.1) scored 0.0 and the report flagged nothing at all.  Restricted
    to one instrument the drop is 0.069 and the rule correctly stays quiet.

    Fails OPEN per pair when either serial is missing, matching the
    different-OTDR verdict gate: an unknown instrument is not evidence.
    """
    K = len(names)
    if K < 2 or r_matrix.shape[0] != K:
        return None
    prefixes, ports = [], []
    for n in names:
        pref, port = _port_split(n)
        prefixes.append(pref)
        ports.append(port)
    prefixes = _merge_zero_pad_prefixes(prefixes)
    port_arr = np.array([p if p is not None else -1 for p in ports],
                        dtype=np.int64)
    has_port = port_arr >= 0
    codes = {p: i for i, p in enumerate(sorted(set(prefixes)))}
    pref_arr = np.array([codes[p] for p in prefixes], dtype=np.int64)

    same_pref = pref_arr[:, None] == pref_arr[None, :]
    both_ports = has_port[:, None] & has_port[None, :]
    gap = np.abs(port_arr[:, None] - port_arr[None, :])
    upper = np.triu(np.ones((K, K), dtype=bool), k=1)
    # Same-instrument mask.  Missing serial fails open (pair stays eligible),
    # so a folder with no serials at all behaves exactly as it did before.
    if serials is None:
        same_ser = np.ones((K, K), dtype=bool)
    else:
        codes_s = {v: i for i, v in enumerate(sorted({x for x in serials if x}))}
        ser_arr = np.array([codes_s.get(x, -1) if x else -1 for x in serials],
                           dtype=np.int64)
        known = ser_arr >= 0
        same_ser = ((~known[:, None]) | (~known[None, :])
                    | (ser_arr[:, None] == ser_arr[None, :]))
    eligible = upper & same_pref & both_ports & same_ser
    near_mask = eligible & (gap >= 1) & (gap <= near_gap)
    far_mask = eligible & (gap >= far_gap)
    n_near = int(near_mask.sum())
    n_far = int(far_mask.sum())
    if n_near < min_pairs or n_far < min_pairs:
        return None
    near_r = float(np.median(r_matrix[near_mask]))
    far_r = float(np.median(r_matrix[far_mask]))
    return near_r, far_r, n_near, n_far


def _speckle_hp_width(files):
    """Moving-average width in samples for THIS folder's acquisition.

    Returns _SPECKLE_HP_WIDTH (the calibrated cap) unless every file agrees
    on a pulse shorter than that, in which case it narrows to the pulse.

    ACQUISITION-UNIFORMITY GUARD.  The width is taken from the MEDIAN over
    all files and only applied when the folder is uniform, because Secret
    Sauce runs one report over whatever folder is uploaded.  A mixed folder
    is reachable and would otherwise have its filter width decided by which
    file sorted first: 275 ns and 500 ns EXFO acquisitions share a
    bit-identical sample spacing (dz = 2.552445171 m), so _SPECKLE_DZ_TOL
    considers their traces comparable and the existing grid guard does not
    catch the mismatch.  When the folder is not uniform we decline to
    narrow and keep the calibrated width, which is the fail-safe direction:
    a wider filter only ever inflates the null, and a higher null makes the
    gate abstain rather than veto.
    """
    vals = [f.get('pulse_samples') for f in files]
    vals = [float(v) for v in vals if v and np.isfinite(v) and v > 0]
    if len(vals) < len(files) or not vals:
        return _SPECKLE_HP_WIDTH        # a file could not report its pulse
    lo, hi = min(vals), max(vals)
    if hi - lo > 0.01 * hi:
        return _SPECKLE_HP_WIDTH        # mixed acquisition: do not narrow
    w = int(np.floor(float(np.median(vals))))
    if w % 2 == 0:
        w += 1                          # the kernel must be odd
    return max(_SPECKLE_HP_WIDTH_MIN, min(_SPECKLE_HP_WIDTH, w))


def _band_i0(band_start_m, dz):
    """First sample index a band starting at `band_start_m` may use.  0 when
    no floor applies, so every caller's `max(...)` is a no-op then and the
    three window builders keep their byte-identical index arithmetic."""
    if band_start_m is None or not np.isfinite(band_start_m) or band_start_m <= 0:
        return 0
    return int(np.floor(float(band_start_m) / dz)) + 1


def _speckle_shared_reflector(files, band_lo, zone_hi):
    """(median position, guard) of the LAST reflective event shared by the
    folder between `band_lo` and `zone_hi`, or None.

    Shared hardware is the same physical mating in every file of a session, so
    its residual correlates at r ~ 1.0 between DIFFERENT fibres and swamps the
    glass — see the calibration block above _SPECKLE_LAUNCH_ZONE_FRAC.  The
    guard scales with the pulse because a wider pulse smears the event further.
    """
    if not np.isfinite(band_lo) or not np.isfinite(zone_hi) or zone_hi <= band_lo:
        return None
    last, pulse_m, n_seen = [], [], 0
    for f in files or ():
        ev = (f or {}).get('events') or []
        if not ev:
            continue
        n_seen += 1
        here = [float(e['dist_km']) * 1000.0 for e in ev
                if e.get('dist_km') is not None
                and (e.get('is_reflective')
                     or e.get('reflection') not in (None, 0, 0.0))
                and band_lo < float(e['dist_km']) * 1000.0 < zone_hi]
        if here:
            last.append(max(here))
        pos, ps = (f or {}).get('pos'), (f or {}).get('pulse_samples')
        if pos is not None and len(pos) > 1 and ps:
            pulse_m.append(float(pos[1] - pos[0]) * float(ps))
    if not n_seen or len(last) < _SPECKLE_SHARED_EVENT_FRAC * n_seen:
        return None
    guard = _SPECKLE_EVENT_GUARD_MIN_M
    if pulse_m:
        guard = max(guard, _SPECKLE_EVENT_GUARD_PULSES * float(np.median(pulse_m)))
    return float(np.median(last)), float(guard)


def _speckle_band_floor(files, interior_start, interior_end):
    """(band start in metres or None, note or None) for this folder.

    The band starts past the launch hardware: the far side of the last shared
    reflective event inside the LAUNCH ZONE, which is span-relative, so a
    reflective splice at 20 km of a 64 km span is never mistaken for a reel and
    a folder whose 2% offset already clears its launch stays byte-identical.

    A shared reflector PAST the launch zone is reported, not acted on.  It
    inflates the baseline the same way, but clearing it would cost more glass
    than it keeps, and on the one trusted set where that geometry occurs
    (RDR4RDR5, port at 1,036 m of a 2,064 m span, six known re-shoot pairs)
    clearing it does not recover the fingerprint: the six true pairs read
    0.12, 0.05, 0.03, 0.01, -0.01 and -0.02 against a 0.24 bar, i.e. nothing.
    At 5 ns over 1 km of trunk there is no fingerprint to uncover, so making
    that gate live would arm it on folders where it separates nothing.
    """
    span = interior_end - interior_start
    if span <= 0:
        return None, None
    f0, f1 = _SPECKLE_WINDOWS[0]
    band_lo = interior_start + span * f0
    band_hi = interior_start + span * f1
    zone_hi = min(band_hi, interior_start + span * _SPECKLE_LAUNCH_ZONE_FRAC)
    hit = _speckle_shared_reflector(files, band_lo, zone_hi)
    if hit is not None:
        pos, guard = hit
        return pos + guard, None
    rest = _speckle_shared_reflector(files, max(band_lo, zone_hi), band_hi)
    if rest is not None:
        pos, _ = rest
        return None, (f'a shared reflective event at {pos:,.0f} m sits inside '
                      f'the fingerprint band ({band_lo:,.0f}-{band_hi:,.0f} m) '
                      f'but past the launch zone, so the band keeps it; every '
                      f'pair reads alike there and the confirm bar is inflated')
    return None, None


def _speckle_window_census(files, interior_start, interior_end, hp_width=None,
                           band_start_m=None):
    """(interior samples, smallest per-window sample count) for this folder.

    Diagnostic only — nothing routes on it.  It exists so a run that could
    not measure anything can SAY what it was short of, instead of printing a
    zero that reads like "no duplicates found".  Mirrors the index arithmetic
    in _speckle_windows exactly, so the number it reports is the number that
    was actually compared against _SPECKLE_MIN_SAMPLES.
    """
    w = _SPECKLE_HP_WIDTH if hp_width is None else int(hp_width)
    span = interior_end - interior_start
    best = None
    for f in files or ():
        pos = (f or {}).get('pos')
        if pos is None or len(pos) < 2:
            continue
        dz = float(pos[1] - pos[0])
        if not np.isfinite(dz) or dz <= 0 or span <= 0:
            continue
        n = len(pos)
        n_int = int(np.ceil(interior_end / dz)) - int(np.floor(interior_start / dz))
        n_win = None
        for f0, f1 in _SPECKLE_WINDOWS:
            i0 = int(np.floor((interior_start + span * f0) / dz)) + 1
            i1 = int(np.ceil((interior_start + span * f1) / dz))
            i0 = max(i0, 0, _band_i0(band_start_m, dz))
            i1 = min(i1, n)
            k = max(0, i1 - i0 - 2 * w)
            n_win = k if n_win is None else min(n_win, k)
        cand = (n_int, n_win if n_win is not None else 0)
        if best is None or cand < best:
            best = cand
    return best if best is not None else (0, 0)


def _speckle_band_residuals(files, interior_start, interior_end, hp_width=None,
                            band_start_m=None):
    """Per-file (name, unit residual, sigma_band dB, n) over the union window
    with NO sample floor.  DIAGNOSTIC ONLY: nothing routes on it.  Mirrors the
    index arithmetic of _speckle_windows so the band it measures is the band
    the gate would have used, on folders too short for the gate to run."""
    w = _SPECKLE_HP_WIDTH if hp_width is None else int(hp_width)
    kern = np.ones(w) / w
    span = interior_end - interior_start
    out = []
    if span <= 0:
        return out
    f0, f1 = _SPECKLE_WINDOWS[0]
    for f in files or ():
        trace, pos = (f or {}).get('trace'), (f or {}).get('pos')
        if trace is None or pos is None or len(trace) < 4 or len(pos) < 2:
            continue
        dz = float(pos[1] - pos[0])
        if not np.isfinite(dz) or dz <= 0:
            continue
        n = len(trace)
        i0 = max(int(np.floor((interior_start + span * f0) / dz)) + 1, 0,
                 _band_i0(band_start_m, dz))
        i1 = min(int(np.ceil((interior_start + span * f1) / dz)), n)
        if i1 - i0 < 3 * w:
            continue
        x = np.asarray(trace[i0:i1], dtype=np.float64)
        h = (x - np.convolve(x, kern, mode='same'))[w:-w]
        h = h - h.mean()
        nrm = float(np.sqrt(np.dot(h, h)))
        if not np.isfinite(nrm) or nrm <= 0:
            continue
        out.append((f.get('name'), h / nrm, float(nrm / np.sqrt(len(h))),
                    int(len(h)), dz, f.get('pulse_samples'), f.get('wavelength')))
    return out


def _speckle_competence(files, interior_start, interior_end, hp_width=None,
                        span_m=None, band_start_m=None):
    """Can this folder carry a duplicate verdict?  Pure function of the folder.

    Returns None when fewer than 3 files can be measured, else a dict:
      status          'OK' | 'MARGINAL' | 'NOT MEASURED'
      ratio           predicted same-fibre r / confirm bar
      pred_same_r     null p50 + _FINGERPRINT_REPRO x (fingerprint/sigma_band)^2
      bar, null_p50, null_p99, null_sd, null_max, n_null_pairs, n_files
      sigma_band_db, fingerprint_term, pulse_ns, span_m, cells
      remedy          one of 'none needed' | 'span' | 'pulse' | 'impossible'
      min_span_m      span that would make this pulse width measurable (or None)
      pulse_need_ns   pulse width that would make this span measurable (or None)
      message         one plain-English sentence group for the tech
      what_it_takes   the remedy in words

    The diagnostic null takes an evenly spaced sample of files at the folder's
    dominant wavelength and treats every pair in it as different fibres, the
    same assumption _spk_null makes.  See the calibration block above
    _FINGERPRINT_DB for the measured table this rule reproduces.
    """
    files = [f for f in (files or ()) if f is not None]
    n_folder = len(files)
    # dominant wavelength first, so a mixed-lambda folder does not pool a
    # bimodal null (see _spk_null); then an evenly spaced sample, so a
    # 1,152-file folder costs the same as a 60-file one
    wls = [f.get('wavelength') for f in files if f.get('wavelength') is not None]
    if wls:
        vals, counts = np.unique(np.round(np.asarray(wls, dtype=float), 1),
                                 return_counts=True)
        dom = float(vals[int(np.argmax(counts))])
        files = [f for f in files if f.get('wavelength') is None
                 or abs(float(f.get('wavelength')) - dom) < 0.05]
    step = max(1, len(files) // _COMPETENCE_NULL_FILES)
    res = _speckle_band_residuals(files[::step][:_COMPETENCE_NULL_FILES],
                                  interior_start, interior_end, hp_width,
                                  band_start_m=band_start_m)
    if len(res) < 3:
        return None
    sample = res
    null = np.array([float(np.dot(sample[i][1], sample[j][1]))
                     for i in range(len(sample)) for j in range(i + 1, len(sample))])
    if len(null) < 3:
        return None
    sigma = float(np.median([r[2] for r in res]))
    n_samp = int(np.median([r[3] for r in res]))
    dz = float(np.median([r[4] for r in res]))
    pulses = [r[5] for r in res if r[5]]
    pulse_ns = (float(np.median(pulses)) * dz / _M_PER_NS) if pulses else None
    if span_m is None:
        span_m = float(interior_end - interior_start)
    p50, p99 = float(np.median(null)), float(np.percentile(null, 99))
    sd, mx = float(null.std()), float(null.max())
    bar = _SPECKLE_CONFIRM_NULL_MULT * p99
    fp = (_FINGERPRINT_DB / sigma) ** 2 if sigma > 0 else float('inf')
    # the fingerprint term is a correlation contribution: it saturates at 1
    # (fingerprint/sigma_band)^2 == f^2/(f^2 + noise^2) since sigma_band^2 = f^2 + noise^2
    fp_r = min(fp, 1.0)
    pred = p50 + _FINGERPRINT_REPRO * fp_r
    ratio = (pred / bar) if bar > 0 else float('inf')
    if ratio >= _COMPETENCE_OK_RATIO:
        status = 'OK'
    elif ratio >= _COMPETENCE_MARGINAL_RATIO:
        status = 'MARGINAL'
    else:
        status = 'NOT MEASURED'
    cells = (span_m / (_M_PER_NS * pulse_ns)) if pulse_ns else None
    pulse_txt = f'{pulse_ns:,.0f} ns' if pulse_ns else 'an unknown pulse width'
    # ── what it would take ─────────────────────────────────────────────────
    remedy, min_span, pulse_need, takes = 'none needed', None, None, ''
    if status != 'OK':
        need = _COMPETENCE_OK_RATIO * bar          # same-fibre r a re-shoot must reach
        if bar >= 1.0 or p50 >= 0.33:
            # No Pearson r clears a bar above 1.0, and a null median this high
            # means the shared instrument structure, not noise, sets the
            # spread; more pulse energy raises it as fast as the signal
            # (retruetest averaging sweep, 2026-09-01).
            remedy = 'impossible'
            takes = ('Confirmation is impossible in this acquisition class: '
                     f'the folder\'s own spread puts the confirm bar at {bar:.2f} '
                     '(a correlation cannot exceed 1.00), and that spread is '
                     'shared instrument structure that more pulse energy or '
                     'averaging raises rather than lowers.')
        else:
            need_fp = (need - p50) / _FINGERPRINT_REPRO
            if need_fp >= 1.0:
                remedy = 'impossible'
                takes = ('Confirmation is impossible here: even a perfectly '
                         'reproduced fingerprint would not clear the confirm bar.')
            else:
                # 1. longer span at this pulse (white null shrinks as 1/sqrt N)
                white = 1.0 / np.sqrt(max(n_samp / max(hp_width or _SPECKLE_HP_WIDTH, 1) * 3.0, 1.0))
                if abs(p50) < 3.0 * white:
                    min_span = float(span_m * (need / max(pred, 1e-9)) ** 2)
                # 2. wider pulse at this span (noise-floor regime: sigma ~ 1/pulse)
                sig_need = _FINGERPRINT_DB * np.sqrt((1.0 - need_fp) / need_fp)
                if pulse_ns and sigma > sig_need:
                    cand = pulse_ns * sigma / sig_need
                    cand_cells = span_m / (_M_PER_NS * cand)
                    fits = (3.0 * _M_PER_NS * cand) <= span_m
                    if fits and cand_cells >= _COMPETENCE_MIN_CELLS:
                        pulse_need = float(cand)
                if pulse_need is not None:
                    remedy = 'pulse'
                    takes = (f'To measure duplicates on this {span_m:,.0f} m span, '
                             f'shoot at about {pulse_need:,.0f} ns or wider '
                             f'(this folder was shot at {pulse_txt}).')
                    if min_span is not None:
                        takes += (f' Alternatively, at {pulse_txt} the span '
                                  f'would need to be at least {min_span/1000:,.1f} km.')
                elif min_span is not None:
                    remedy = 'span'
                    takes = (f'At {pulse_txt} the span would need to be at '
                             f'least {min_span/1000:,.1f} km; no pulse width that '
                             f'fits a {span_m:,.0f} m span carries enough energy.')
                else:
                    remedy = 'impossible'
                    takes = (f'No pulse width that fits a {span_m:,.0f} m span '
                             'carries enough energy to measure duplicates, and the '
                             'folder\'s spread is not set by noise alone, so a '
                             'longer span at this pulse would not help either.')
    if status == 'OK':
        message = (f'Duplicate detector competent: shot at {pulse_txt} over '
                   f'{span_m:,.0f} m, a genuine re-shoot would read about '
                   f'{pred:.2f} against a confirm bar of {bar:.2f} (ratio {ratio:.1f}).')
    elif status == 'MARGINAL':
        message = (f'Duplicate detector MARGINAL: shot at {pulse_txt} over '
                   f'{span_m:,.0f} m, a genuine re-shoot would read about '
                   f'{pred:.2f} against a confirm bar of {bar:.2f} (ratio '
                   f'{ratio:.1f}; {_COMPETENCE_OK_RATIO:.1f} is comfortable). '
                   'Weak duplicates can be missed here.')
    else:
        message = (f'Duplicates NOT MEASURED. Shot at {pulse_txt} over '
                   f'{span_m:,.0f} m: the fibre fingerprint is {fp_r:.3f} of the '
                   f'trace noise band and this folder\'s own spread reaches '
                   f'{p99:.2f}, so a genuine re-shoot would read about {pred:.2f} '
                   f'against a confirm bar of {bar:.2f} (ratio {ratio:.2f}; needs '
                   f'{_COMPETENCE_OK_RATIO:.1f}). A zero here means the detector '
                   'could not run, not that there are no duplicates.')
    return {'status': status, 'ratio': float(ratio), 'pred_same_r': float(pred),
            'bar': float(bar), 'null_p50': p50, 'null_p99': p99, 'null_sd': sd,
            'null_max': mx, 'n_null_pairs': int(len(null)),
            'n_files': int(n_folder), 'n_sampled': int(len(res)),
            'sigma_band_db': sigma,
            'fingerprint_term': float(fp_r), 'pulse_ns': pulse_ns,
            'span_m': float(span_m), 'cells': cells, 'remedy': remedy,
            'band_start_m': (None if band_start_m is None else float(band_start_m)),
            'min_span_m': min_span, 'pulse_need_ns': pulse_need,
            'message': message, 'what_it_takes': takes}


def _speckle_windows(f, interior_start, interior_end, hp_width=None,
                     band_start_m=None):
    """Unit-normalized speckle-band residual of one trace per analysis
    window (see the _SPECKLE_* calibration block).

    The residual is the trace minus an `hp_width`-sample moving
    average (see _speckle_hp_width; defaults to the calibrated cap) — the low-pass carries splice steps and attenuation slope, the
    residual carries each fiber's own frozen-in Rayleigh interference
    pattern.  Window bounds are computed from the sample spacing (not from
    a boolean position mask) so two files on the same grid always get
    byte-identical index ranges.

    Returns {'dz': sample spacing,
             'win': [ (i0, i1, unit_residual, residual_std_dB) | None ]}
    or None when the file can't be measured at all (fail-safe: unmeasurable
    never vetoes).  residual_std_dB is the fiber's own speckle amplitude in
    that window — the scale that sets how much acquisition noise the
    statistic can survive (see _speckle_same_fiber_floor).
    """
    if f is None:
        return None
    trace, pos = f.get('trace'), f.get('pos')
    if trace is None or pos is None or len(trace) < 4 or len(pos) < 2:
        return None
    dz = float(pos[1] - pos[0])
    if not np.isfinite(dz) or dz <= 0:
        return None
    w = _SPECKLE_HP_WIDTH if hp_width is None else int(hp_width)
    kern = np.ones(w) / w
    n = len(trace)
    span = interior_end - interior_start
    if span <= 0:
        return None
    out = []
    for f0, f1 in _SPECKLE_WINDOWS:
        i0 = int(np.floor((interior_start + span * f0) / dz)) + 1
        i1 = int(np.ceil((interior_start + span * f1) / dz))
        i0 = max(i0, 0, _band_i0(band_start_m, dz))
        i1 = min(i1, n)
        # Need the window plus the convolution edge trim on both sides.
        if i1 - i0 < _SPECKLE_MIN_SAMPLES + 2 * w:
            out.append(None)
            continue
        x = trace[i0:i1].astype(np.float64)
        h = (x - np.convolve(x, kern, mode='same'))[w:-w]
        h = h - h.mean()
        nrm = float(np.sqrt(np.dot(h, h)))
        if not np.isfinite(nrm) or nrm <= 0:
            # Flat / saturated / all-NaN window — nothing to fingerprint.
            out.append(None)
            continue
        out.append((i0, i1, h / nrm, float(nrm / np.sqrt(len(h)))))
    if all(v is None for v in out):
        return None
    return {'dz': dz, 'win': out}


def _speckle_comparable(ra, rb):
    """Windows the two files can actually be compared in, or None."""
    if ra is None or rb is None:
        return None
    dz_ref = max(ra['dz'], rb['dz'])
    if abs(ra['dz'] - rb['dz']) > _SPECKLE_DZ_TOL * dz_ref:
        return None                      # different acquisition grid
    out = []
    for wa, wb in zip(ra['win'], rb['win']):
        if wa is None or wb is None:
            continue
        if wa[0] != wb[0] or wa[1] != wb[1] or len(wa[2]) != len(wb[2]):
            continue
        out.append((wa, wb))
    return out or None


def _speckle_pair_r(ra, rb):
    """MAX speckle-band Pearson r across the analysis windows, or None when
    the pair is UNMEASURABLE (different sample spacing, no window long
    enough on both sides, flat residuals).  MAX is the most permissive
    combiner — one agreeing window is enough to confirm a pair."""
    cw = _speckle_comparable(ra, rb)
    if cw is None:
        return None
    return max(float(np.dot(wa[2], wb[2])) for wa, wb in cw)


def _speckle_same_fiber_floor(ra, rb, sigma_pair):
    """LOWEST speckle r the same-fiber hypothesis can produce for a pair
    that disagrees by `sigma_pair` dB — i.e. the number this pair would
    still have to beat if it really were one fiber shot twice.

    Two shots of one fiber share the speckle exactly; everything that makes
    them differ is acquisition noise.  Put ALL of that difference into the
    speckle band (the worst case — real re-shoot differences are launch
    level and thermal drift, which the high-pass removes) and split it
    evenly between the two shots.  With band amplitude s and per-shot noise
    σ/√2 the correlation is s² / (s² + σ²/2).  Verified against
    white-noise-injected controls on real files: predicted 0.331 vs
    measured 0.320 (BKF, σ 0.0149); predicted 0.056 vs measured 0.056
    (DEL, σ 0.0210); predicted 0.065 vs measured 0.077 (BKF, σ 0.0396).

    Any low-frequency component in the real difference only pushes the true
    value ABOVE this, so it is a genuine lower bound.  Uses the SMALLER of
    the two files' band amplitudes (conservative) and the best window.
    Returns None when the pair is unmeasurable.
    """
    cw = _speckle_comparable(ra, rb)
    if cw is None:
        return None
    sig2 = max(float(sigma_pair), 0.0) ** 2 / 2.0
    best = None
    for wa, wb in cw:
        s2 = min(wa[3], wb[3]) ** 2
        if s2 <= 0:
            continue
        v = s2 / (s2 + sig2)
        if best is None or v > best:
            best = v
    return best


def _robust_common_span(lengths):
    """Robust common analysis span over per-file EOFs (meters).

    Returns (span_m, median_m, outlier_idx, excluded_idx, guard_note):
      span_m       — the common span the interior window is built from
      median_m     — folder median EOF
      outlier_idx  — indices whose EOF < _BREAK_FRAC_OF_MEDIAN × median
                     (suspected breaks — ALWAYS reported, never silent)
      excluded_idx — indices to drop from pair metrics.  Equal to
                     outlier_idx whenever suspected breaks exist, ≥2
                     healthy files remain, and the consistency guard
                     does not fire; else empty.
      guard_note   — None normally; the warning string when the
                     inconsistent-folder guard fired (no exclusion, span
                     stays the raw min).
    Homogeneous folders (no extreme outliers) return exactly min(lengths),
    so their windows — and pair tables — are byte-identical to the
    historical raw-min behavior.  See the calibration block above
    _BREAK_FRAC_OF_MEDIAN for the long-span window restoration + guard.
    """
    arr = np.asarray(list(lengths), dtype=np.float64)
    raw_min = float(arr.min())
    med = float(np.median(arr))
    if med <= 0:
        return raw_min, med, [], [], None
    cut = _BREAK_FRAC_OF_MEDIAN * med
    outlier_idx = [int(i) for i in np.flatnonzero(arr < cut)]
    span, excluded_idx, guard_note = raw_min, [], None
    n = len(arr)
    n_healthy = n - len(outlier_idx)
    if outlier_idx:
        if len(outlier_idx) > _INCONSISTENT_FOLDER_FRAC * n:
            guard_note = (
                f'folder trace lengths are inconsistent ({len(outlier_idx)} '
                f'of {n} below 75% of median) — window not restored; '
                f'check folder contents')
        elif n_healthy >= 2:
            healthy_min = float(arr[arr >= cut].min())
            if healthy_min > raw_min:
                span = healthy_min
                excluded_idx = outlier_idx
    return span, med, outlier_idx, excluded_idx, guard_note


def _ab_break_notes(entries, median_m):
    """Cross-direction A+B consistency for suspected breaks (in place).

    `entries` are short-trace dicts carrying 'file' and 'eof_m'.  When the
    folder holds BOTH directions of the same port (same trailing port
    number, different prefix — the same direction/prefix grouping
    _neighbor_decay uses) and the two EOFs sum to the folder median span
    within ±_BREAK_AB_SUM_TOL, the two shots are the two sides of ONE
    physical break: each entry gains a 'break_note' anchored at its own
    launch end, e.g. "A+B lengths are consistent with a break ~1005 m from
    the BCK1BCK6 end"."""
    if not median_m or median_m <= 0:
        return
    by_port = {}
    for e in entries:
        pref, port = _port_split(e['file'])
        if port is None:
            continue
        by_port.setdefault(port, []).append((pref, e))
    for port, lst in by_port.items():
        if len(lst) != 2:
            continue
        (pref_a, ea), (pref_b, eb) = lst
        if pref_a == pref_b:
            continue
        total = ea['eof_m'] + eb['eof_m']
        if abs(total - median_m) <= _BREAK_AB_SUM_TOL * median_m:
            for pref, e in lst:
                e['break_note'] = (
                    f'A+B lengths are consistent with a break '
                    f'~{e["eof_m"]:.0f} m from the {pref} end')


def _competence_section_html(detail):
    """PDF/HTML notice when the detector could not measure this folder.
    Returns '' when the verdict is OK (or absent), so unaffected reports
    stay byte-stable.  The same sentence the hub shows and the workbook
    carries, so a tech sees one story wherever they look."""
    if not detail or detail.get('status') in (None, 'OK'):
        return ''
    from html import escape as _esc
    takes = detail.get('what_it_takes') or ''
    return (f'<div class="verdict-box verdict-dispute">'
            f'<b>Duplicate detection {_esc(str(detail.get("status")))}.</b> '
            f'{_esc(str(detail.get("message", "")))}'
            + (f'<br><i>{_esc(takes)}</i>' if takes else '')
            + '</div>')


def _event_fallback_chart(fb):
    """Where the reviewed pairs sit in the folder's own spread.  '' when there
    is nothing to draw.

    The printed margin could not tell a useful ranking from a worthless one
    (1.03x on a direction that agreed with an outside auditor 43 times out of
    52, 1.09x on one that agreed once out of 22).  A picture can: the reader
    sees directly whether the marked pairs are a separated group or the left
    edge of one continuous cloud, which is the question the number was being
    asked to answer.

    Left panel is the reflectance distribution against its limit - a gap means
    the marked pairs are a different population, no gap means the cut runs
    through a continuum.  Right panel is the joint view, since a pair is only
    marked when BOTH readings are inside; the corner rectangle is the gate and
    a cluster tucked into it looks different from a cloud clipped by it.

    Blue for the folder, amber for the reviewed pairs - the pairing already
    used by the distribution chart above, and separated in both hue and
    lightness so it survives colour-blind readers and greyscale printing.
    """
    rows = (fb or {}).get('rows') or []
    if len(rows) < 2:
        return ''
    refl = np.array([r['max_drefl_db'] for r in rows], dtype=np.float64)
    loss = np.array([r['max_dloss_db'] for r in rows], dtype=np.float64) * 1000.0
    keep = np.array([bool(r['within_gate']) for r in rows])
    rg = float(fb.get('refl_gate_db') or _EVT_FB_REFL_DB)
    lg = float(fb.get('loss_gate_db') or _EVT_FB_LOSS_DB) * 1000.0
    BULK, MARK = '#4A90D9', '#b97000'
    # LOG AXES, and the reason is not taste.  A folder's reflectance spread
    # runs from a few hundredths of a dB to ~10 dB while the limit sits at
    # 0.20, so on a linear axis the whole reviewed group is a two-pixel sliver
    # at the left edge and the panel answers nothing.  Same for splice loss:
    # 25 mdB against a spread reaching 300.  Floors keep exact zeros (two
    # readings that quantised to the same value) on the plot.
    R_FLOOR, L_FLOOR = 1e-3, 0.5
    refl_p = np.maximum(refl, R_FLOOR)
    loss_p = np.maximum(loss, L_FLOOR)

    fig, (axS, axH) = plt.subplots(1, 2, figsize=(13, 4.6),
                                   gridspec_kw={'width_ratios': [1.45, 1.0]})

    lo, hi = float(refl_p.min()), float(refl_p.max())
    bins = np.logspace(np.log10(max(lo, R_FLOOR)), np.log10(max(hi, rg * 4)), 60)
    axH.hist(refl_p, bins=bins, color=BULK, alpha=0.8, edgecolor='white',
             linewidth=0.4, label=f'all {len(rows):,} comparable pairs')
    axH.axvline(rg, color=MARK, linestyle='--', linewidth=2,
                label=f'reflectance limit {rg:.2f} dB')
    axH.axvspan(bins[0], rg, color=MARK, alpha=0.10)
    axH.set_xscale('log')
    axH.set_xlabel('max reflectance difference over matched events (dB, log scale)')
    axH.set_ylabel('Number of pairs')
    axH.set_title('Reflectance spread across the folder',
                  fontweight='bold', fontsize=10)
    axH.legend(loc='upper left', fontsize=7.5, frameon=False)
    axH.grid(alpha=0.3, which='both')

    idx = np.flatnonzero(~keep)
    if idx.size > 20000:
        idx = np.random.RandomState(0).choice(idx, 20000, replace=False)
    axS.scatter(refl_p[idx], loss_p[idx], s=4, c=BULK, alpha=0.18,
                linewidths=0, label='not reviewed')
    axS.scatter(refl_p[keep], loss_p[keep], s=34, c=MARK, edgecolors='white',
                linewidths=0.6, zorder=3,
                label=f'review for duplicate ({int(keep.sum())})')
    axS.add_patch(Rectangle((bins[0], L_FLOOR), rg - bins[0], lg - L_FLOOR,
                            fill=False, edgecolor=MARK, linestyle='--',
                            linewidth=1.8, zorder=2))
    axS.set_xscale('log')
    axS.set_yscale('log')
    axS.set_xlim(bins[0], bins[-1])
    axS.set_ylim(L_FLOOR, max(float(loss_p.max()), lg * 4))
    axS.set_xlabel('max reflectance difference (dB, log scale)')
    axS.set_ylabel('max splice loss difference (mdB, log scale)')
    axS.set_title('Both readings together; the box is the review limit',
                  fontweight='bold', fontsize=12)
    axS.legend(loc='upper left', fontsize=7.5, frameon=False)
    axS.grid(alpha=0.3, which='both')

    plt.tight_layout()
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode('ascii')


def _event_fallback_section_html(fb):
    """The ranked event-table list for folders whose fingerprint could not
    answer.  '' when the fingerprint answered for itself, so reports that were
    competent stay byte-stable.

    Prints the evidence beside every row (reflectance, splice loss, position,
    time gap) and marks which rows sit inside the gate.  Nothing here is a
    verdict: no row moved p_dup or routed a pair.
    """
    if not fb:
        return ''
    from html import escape as _esc
    if fb.get('note'):
        return ('<div class="verdict-box verdict-dispute">'
                '<b>Event table checked as well.</b> '
                + _esc(fb['note']) + '</div>')
    gate = ('reflectance agreeing within %.2f dB and splice loss within %.0f mdB '
            'at every matched event'
            % (fb['refl_gate_db'], fb['loss_gate_db'] * 1000))
    margin, margin_txt = fb.get('margin'), ''
    if margin and margin >= 1.5:
        margin_txt = (' The tightest pair outside the gate reads %.1fx looser on '
                      'reflectance than the loosest pair inside it, so the '
                      'boundary falls across a gap.' % margin)
    elif margin:
        margin_txt = (' The tightest pair outside the gate is only %.1fx looser '
                      'on reflectance than the loosest pair inside it, so this '
                      'ranking runs through a continuum and the gate is a '
                      'marker, not a wall.' % margin)
    _c = _event_fallback_chart(fb)
    _chart = (f'<img src="data:image/png;base64,{_c}" class="chart-img" />'
              if _c else '')
    shown = fb['rows'][:_EVT_FB_PDF_ROWS]
    over = len(fb['rows']) - len(shown)
    out = []
    for r in shown:
        gap = (_fmt_time_gap(r['gap_s']) if r.get('gap_s') is not None
               else '&mdash;')
        mark = ('<span style="color:#b97000;font-weight:700">Yes</span>'
                if r['within_gate'] else '')
        out.append('<tr><td class="center">%d</td>'
                   '<td class="pair-cell">%s &harr; %s</td>'
                   '<td class="center">%d</td>'
                   '<td class="center">%.3f</td>'
                   '<td class="center">%.0f</td>'
                   '<td class="center">%.2f</td>'
                   '<td class="center">%s</td>'
                   '<td class="center">%s</td></tr>'
                   % (r['rank'], _esc(r['a']), _esc(r['b']), r['n_events'],
                      r['max_drefl_db'], r['max_dloss_db'] * 1000,
                      r['max_dpos_m'], gap, mark))
    tail = ''
    if over > 0:
        tail = ('<div style="padding:8px 4px;color:#b97000;font-weight:600">'
                'and %s more ranked pairs, of which the Excel report carries '
                'the next %s.</div>'
                % (format(over, ','),
                   format(min(over, _EVT_FB_XLSX_ROWS - len(shown)), ',')))
    return (
        '<div class="section-block">'
        '<div class="dir-banner">1b. Event-table ranking (the trace could not answer)</div>'
        '<p style="font-size:11px;margin:4px 0 6px 0">The trace fingerprint could not '
        'measure this folder, so these pairs were ranked on the stored event table '
        'instead: every event appearing in both files at the same distance, compared '
        'on reflectance and splice loss. Most alike first, with the rows to review '
        'marked. '
        + ('%s of %s comparable pairs have %s.'
           % (format(fb['n_within'], ','), format(fb['n_compared'], ','), gate))
        + margin_txt
        + ' This is a <b>ranking for a tech to check against the port log</b>, not a '
        'verdict: no row here is a confirmed duplicate, none of them changed any '
        'likelihood on this report, and a pair low in the list is not cleared. '
'Across the three panels an outside auditor '
        'has scored, this ranking marked 97 of their 107 flagged fibres in the '
        'direction the duplication was committed. Going the other way it marks far '
        'fewer, which may simply mean there is nothing there to find. Either way, '
        'if the port log clears the top rows, read that as <b>no answer here</b> '
        'rather than as no duplicates.</p>'
        + _chart
        + '<table class="vote-table">'
        '<tr><th>Rank</th><th style="text-align:left">Pair</th><th>Events matched</th>'
        + ('<th>max &Delta; reflectance (dB)<br>'
           '<span style="font-weight:400">limit %.2f</span></th>'
           '<th>max &Delta; splice loss (mdB)<br>'
           '<span style="font-weight:400">limit %.0f</span></th>'
           % (fb['refl_gate_db'], fb['loss_gate_db'] * 1000))
        + '<th>max &Delta; position (m)</th><th>Time gap</th>'
          '<th>Review for duplicate</th></tr>'
        + ''.join(out) + '</table>' + tail + '</div>')


def _short_trace_section_html(short_traces, window_guard=None):
    """PDF section for suspected broken / short fibers.  Returns '' when
    there are none, so unaffected reports stay byte-stable.  When the
    inconsistent-folder guard fired, a warning banner renders above the
    table (the guard can only fire when suspected breaks exist)."""
    if not short_traces:
        return ''
    guard_html = ''
    if window_guard:
        guard_html = (f'<div class="verdict-box verdict-dispute">'
                      f'<b>Warning:</b> {window_guard}</div>')
    rows = ''
    for e in short_traces:
        finding = 'suspected break'
        if e.get('excluded'):
            finding += ' — excluded from pair comparison'
        if e.get('break_note'):
            finding += f'. {e["break_note"]}'
        rows += (f'<tr><td class="pair-cell">{e["file"]}</td>'
                 f'<td class="center">{e["eof_m"]:.0f}</td>'
                 f'<td class="center">{e["median_eof_m"]:.0f}</td>'
                 f'<td>{finding}</td></tr>')
    return f'''
<div class="section-block">
<div class="dir-banner">Suspected broken / short fibers</div>{guard_html}
<table class="vote-table">
<tr><th style="text-align:left">File</th><th>Ends at (m)</th>
    <th>Folder median (m)</th><th style="text-align:left">Finding</th></tr>
{rows}
</table>
</div>
'''


_ALLDUPS_SIGMA_CLIFF = 0.10     # the all_dups trigger's sigma cutoff
_ALLDUPS_SIGMA_MARGIN = 0.03    # warn when a folder sits this close to it


def _regime_margin_note(bulk_r, bulk_sigma):
    """One sentence when a folder is near the all_dups sigma cliff, else None.

    `bulk_r >= 0.7 and bulk_sigma < 0.10` is a hard step: on one side the
    folder is ordinary, on the other EVERY pair is judged by the widened
    0.85-0.95 ramp and a 1,152-fiber span can report the large majority of
    its pairs as duplicates.  Nothing warns when a folder approaches it.

    Measured 2026-09-01, the two closest real spans on disk:

        NEWELM json  1152f  26,092 m  bulk_r 0.9861  sigma 0.1256
        ELMNEW json  1152f   5,460 m  bulk_r 0.9867  sigma 0.1234

    Both clear the bulk_r half of the trigger outright and are held out of
    all_dups by 0.0256 and 0.0234 of sigma respectively - and ELMNEW is
    additionally blocked by the 15 km span floor while NEWELM, at 26 km, is
    not.  Neither is flooding today; both are one small sigma shift away.

    This REPORTS ONLY.  It does not move the cliff, reroute anything, or
    change a verdict - deliberately, because the router decides everything
    downstream on every folder and the case for moving it is a folder that
    has actually tipped, which does not exist yet.  What this buys is that
    the next folder to approach says so, instead of being discovered as a
    flood.
    """
    if bulk_r is None or bulk_sigma is None:
        return None
    if bulk_r < 0.7:
        return None                      # the other half of the trigger is far away
    d = bulk_sigma - _ALLDUPS_SIGMA_CLIFF
    if abs(d) > _ALLDUPS_SIGMA_MARGIN:
        return None
    if d >= 0:
        return (f'NEAR the all_dups cliff: bulk sigma {bulk_sigma:.4f} is only '
                f'{d:.4f} above the {_ALLDUPS_SIGMA_CLIFF:.2f} cutoff and bulk r '
                f'{bulk_r:.4f} already clears 0.70. A small sigma shift would '
                f'route this folder all_dups and judge every pair on the '
                f'widened 0.85-0.95 ramp.')
    return (f'INSIDE the all_dups cliff by only {-d:.4f}: bulk sigma '
            f'{bulk_sigma:.4f} against the {_ALLDUPS_SIGMA_CLIFF:.2f} cutoff. '
            f'This folder is routed all_dups on a narrow margin.')


# ── Near splice: the splice behind the panel ─────────────────────────────
# Every FEC span on disk carries ONE splice a few tens of metres behind the
# panel (the pigtail spliced to the cable) and nothing else in its 5 km
# window.  A folder-wide step scan finds nothing above 0.004 dB anywhere else:
#
#     span                splice past port   loss median / sd   fibres > 5 noise
#     Goodland->Monument        55 m          +0.094 / 0.095 dB        49%
#     Monument->Goodland        43 m          +0.072 / 0.074           30%
#     Ancho->Duran              87 m          +0.129 / 0.109           56%
#     Duran->Ancho              84 m          +0.052 / 0.078           27%
#
# That splice is glass, so an unplug and re-plug cannot change it, which makes
# it the one per-fibre measurement on a splice-free short span that survives
# the duplicate that actually happens.  The firmware reports it on only about
# 20% of files, so it is measured from the trace.  Noise comes from the SAME
# window geometry at splice-free spots further down each fibre: 0.013, 0.019,
# 0.012 and 0.011 dB per shot on the four spans, with tails heavier than a
# normal curve (Goodland: 0.19% beyond 4 sd against 0.006%), so every rate
# this prints is read off that empirical distribution.
#
# Checked on the only labelled repeats there are: Goodland 0012 and 0841, each
# shot twice about 7 h apart with the connector redone (0012 on a different
# meter).  Splice loss +0.2617 -> +0.2715 and -0.1208 -> -0.1018 dB, 0.5 and
# 1.0 sd, while their connector losses moved 0.47 and 0.41 dB.  The first
# noise estimate used a 233 m reference window that also straddled the splice
# and understated the noise; the reference has to match the geometry.
#
# It cannot CONFIRM a pair: 35-60% of different-fibre pairs also agree within
# 3 sd.  So it never touches p_dup.  It prints evidence against a pair.
_NEAR_SPLICE_SEARCH_M = 250.0          # how far past the panel port to look
_NEAR_SPLICE_PORT_GUARD_M = 15.0       # connector dead zone, floor
_NEAR_SPLICE_PORT_GUARD_PULSES = 10.0
_NEAR_SPLICE_GUARD_M = 5.0             # either side of the splice, floor
_NEAR_SPLICE_GUARD_PULSES = 3.0
_NEAR_SPLICE_SCAN_GAP_M = 3.0          # localisation scan: gap and window
_NEAR_SPLICE_SCAN_WIN_M = 10.0
_NEAR_SPLICE_MIN_BEFORE_M = 10.0       # fibre needed before the splice
_NEAR_SPLICE_AFTER_M = 400.0
_NEAR_SPLICE_SLOPE_M = 2000.0          # attenuation fit, from port + 150 m
_NEAR_SPLICE_SPOTS_M = (300.0, 700.0, 1100.0, 1500.0,
                        1900.0, 2300.0, 2700.0, 3100.0)
_NEAR_SPLICE_MIN_SPOTS = 3
_NEAR_SPLICE_MIN_FILES = 6
_NEAR_SPLICE_MIN_FRAC = 0.20           # fibres whose splice clears 5 x noise
_NEAR_SPLICE_STEP_NOISE = 5.0
_NEAR_SPLICE_EVENT_SHARE = 0.05        # reflective event at the step: a connector


def _near_splice_prep(f):
    """Flattened trace and panel-port anchor for one file, or None."""
    ev = (f or {}).get('events') or []
    if len(ev) < 3:
        return None
    port_ev = _mating_port_event(ev)
    if port_ev is None or port_ev.get('dist_km') is None:
        return None
    if not (port_ev.get('is_reflective')
            or port_ev.get('reflection') not in (None, 0, 0.0)):
        return None
    tr = np.asarray(f.get('trace'), dtype=float)
    pos = np.asarray(f.get('pos'), dtype=float)
    k = min(len(tr), len(pos))
    if k < 200:
        return None
    tr, pos = tr[:k], pos[:k]
    end = float(f.get('length') or pos[-1])
    port = float(port_ev['dist_km']) * 1000.0
    if port > 0.5 * end:
        return None
    m = (pos > port + 150.0) & (pos < min(port + 150.0 + _NEAR_SPLICE_SLOPE_M,
                                          end - 20.0))
    if m.sum() < 50:
        return None
    flat = tr - np.polyfit(pos[m], tr[m], 1)[0] * pos
    ps = f.get('pulse_samples')
    return {'pos': pos, 'cs': np.concatenate(([0.0], np.cumsum(flat))),
            'port': port, 'end': end,
            'pulse_m': float(ps) * float(pos[1] - pos[0]) if ps else None,
            'events': ev}


def _near_splice_means(P, lo, hi):
    """Mean of the flattened trace strictly inside (lo, hi); NaN under 5 samples."""
    lo = np.atleast_1d(np.asarray(lo, dtype=float))
    hi = np.atleast_1d(np.asarray(hi, dtype=float))
    i0 = np.searchsorted(P['pos'], lo, side='right')
    i1 = np.searchsorted(P['pos'], hi, side='left')
    n = i1 - i0
    out = np.full(lo.shape, np.nan)
    ok = n >= 5
    out[ok] = (P['cs'][i1[ok]] - P['cs'][i0[ok]]) / n[ok]
    return out


def _near_splice_same_pct(ns, d):
    """Percent of same-fibre noise differences at least as large as d."""
    nd = (ns or {}).get('null_diffs')
    if nd is None or not len(nd):
        return None
    return 100.0 * (len(nd) - int(np.searchsorted(nd, d, side='left'))) / len(nd)


def _near_splice(files, pairs):
    """Measure the splice behind the panel on every fibre, attach each pair's
    difference ('splice_diff_db', 'splice_diff_sd'), and return a summary.
    Abstains out loud.  Never touches p_dup."""
    def _no(reason):
        return {'usable': False,
                'note': 'Near splice: NOT USABLE - ' + reason + '.'}
    P = {}
    for f in files or ():
        pp = _near_splice_prep(f)
        if pp is not None:
            P[f.get('name')] = pp
    if len(P) < _NEAR_SPLICE_MIN_FILES:
        return _no('%d fibre(s) with a panel port and a trace to measure, fewer '
                   'than %d' % (len(P), _NEAR_SPLICE_MIN_FILES))
    pulses = [p['pulse_m'] for p in P.values() if p['pulse_m']]
    pulse_m = float(np.median(pulses)) if pulses else 0.0
    pg = max(_NEAR_SPLICE_PORT_GUARD_M, _NEAR_SPLICE_PORT_GUARD_PULSES * pulse_m)
    g = max(_NEAR_SPLICE_GUARD_M, _NEAR_SPLICE_GUARD_PULSES * pulse_m)
    gs = max(_NEAR_SPLICE_SCAN_GAP_M, 2.0 * pulse_m)
    ws = max(_NEAR_SPLICE_SCAN_WIN_M, 5.0 * pulse_m)
    offsets = np.arange(pg + ws + gs, _NEAR_SPLICE_SEARCH_M + 1e-9, 1.0)
    if offsets.size == 0 or pg + _NEAR_SPLICE_MIN_BEFORE_M + g >= _NEAR_SPLICE_SEARCH_M:
        return _no('a %.1f m pulse leaves no fibre to measure within %.0f m of '
                   'the panel port' % (pulse_m, _NEAR_SPLICE_SEARCH_M))
    names = list(P)
    S = np.array([
        _near_splice_means(P[n], P[n]['port'] + offsets + gs,
                           P[n]['port'] + offsets + gs + ws)
        - _near_splice_means(P[n], P[n]['port'] + offsets - gs - ws,
                             P[n]['port'] + offsets - gs)
        for n in names])
    with np.errstate(all='ignore'):
        med = np.nanmedian(S, axis=0)
    if not np.isfinite(med).any():
        return _no('no fibre reaches past the search window')
    # The scan's median is a plateau as wide as its gap either side of the
    # splice.  Take the middle of it, not the first maximum, so a fibre whose
    # splice sits a metre or two off the folder median stays clear of both
    # measurement windows.
    absmed = np.abs(med)
    jmax = int(np.nanargmax(absmed))
    top = float(absmed[jmax])
    lo_j = hi_j = jmax
    while lo_j - 1 >= 0 and absmed[lo_j - 1] >= 0.9 * top:
        lo_j -= 1
    while hi_j + 1 < len(absmed) and absmed[hi_j + 1] >= 0.9 * top:
        hi_j += 1
    o = float(0.5 * (offsets[lo_j] + offsets[hi_j]))
    # A reflective event at the step is a connector, and a re-plug changes a
    # connector: measuring it would veto the very duplicates this is for.
    n_refl = 0
    for n in names:
        at = P[n]['port'] + o
        for e in P[n]['events'][1:-1]:
            dk = e.get('dist_km')
            if (dk is not None and abs(float(dk) * 1000.0 - at) <= g + 2.0
                    and (e.get('is_reflective')
                         or e.get('reflection') not in (None, 0, 0.0))):
                n_refl += 1
                break
    if n_refl > _NEAR_SPLICE_EVENT_SHARE * len(names):
        return _no('the step %.0f m past the panel port is a reflective event on '
                   '%d fibre(s): a connector, not a splice' % (o, n_refl))
    blen = o - g - pg
    if blen < _NEAR_SPLICE_MIN_BEFORE_M:
        return _no('the step %.0f m past the panel port leaves only %.0f m of '
                   'fibre before it outside the connector dead zone' % (o, blen))
    spots_m = np.array(_NEAR_SPLICE_SPOTS_M, dtype=float)
    loss, rows = {}, []
    for n in names:
        p = P[n]
        s = p['port'] + o
        if s + g + _NEAR_SPLICE_AFTER_M >= p['end'] - 20.0:
            loss[n] = float('nan')
            rows.append(np.full(spots_m.size, np.nan))
            continue
        loss[n] = float(_near_splice_means(p, s + g, s + g + _NEAR_SPLICE_AFTER_M)[0]
                        - _near_splice_means(p, p['port'] + pg, s - g)[0])
        c = s + spots_m
        v = (_near_splice_means(p, c + g, c + g + _NEAR_SPLICE_AFTER_M)
             - _near_splice_means(p, c - g - blen, c - g))
        v[c + g + _NEAR_SPLICE_AFTER_M >= p['end'] - 20.0] = np.nan
        rows.append(v)
    SP = np.array(rows, dtype=float)
    # A reference spot is only noise if no event sits in its windows.
    for j, d in enumerate(spots_m):
        lo_rel = o + d - g - blen - 20.0
        hi_rel = o + d + g + _NEAR_SPLICE_AFTER_M + 20.0
        hits = 0
        for n in names:
            pt = P[n]['port']
            for e in P[n]['events'][1:-1]:
                dk = e.get('dist_km')
                if dk is not None and lo_rel <= float(dk) * 1000.0 - pt <= hi_rel:
                    hits += 1
                    break
        if hits > 0.02 * len(names):
            SP[:, j] = np.nan
    good = [j for j in range(SP.shape[1])
            if np.isfinite(SP[:, j]).sum() >= _NEAR_SPLICE_MIN_FILES]
    if len(good) < _NEAR_SPLICE_MIN_SPOTS:
        return _no('only %d splice-free stretch(es) to measure the noise on, %d '
                   'needed' % (len(good), _NEAR_SPLICE_MIN_SPOTS))
    SP = SP[:, good]
    SP = SP - np.nanmedian(SP, axis=0)
    sig = float(1.4826 * np.nanmedian(np.abs(SP)))
    L = np.array([loss[n] for n in names], dtype=float)
    fin = np.isfinite(L)
    if fin.sum() < _NEAR_SPLICE_MIN_FILES or not sig > 0:
        return _no('too few fibres reach far enough past the splice')
    frac = float(np.mean(np.abs(L[fin]) > _NEAR_SPLICE_STEP_NOISE * sig))
    med_l = float(np.median(L[fin]))
    if frac < _NEAR_SPLICE_MIN_FRAC:
        return _no('no shared splice within %.0f m of the panel port (the strongest '
                   'step, %.0f m past it, clears %gx the %.3f dB noise on %.0f%% of '
                   'fibres; %.0f%% needed)'
                   % (_NEAR_SPLICE_SEARCH_M, o, _NEAR_SPLICE_STEP_NOISE, sig,
                      100 * frac, 100 * _NEAR_SPLICE_MIN_FRAC))
    diffs = []
    for i in range(SP.shape[1]):
        for j in range(i + 1, SP.shape[1]):
            d = SP[:, i] - SP[:, j]
            diffs.append(np.abs(d[np.isfinite(d)]))
    nd = np.sort(np.concatenate(diffs))
    sd2 = sig * np.sqrt(2.0)
    for p in pairs or ():
        la, lb = loss.get(p.get('a')), loss.get(p.get('b'))
        if la is None or lb is None or not (np.isfinite(la) and np.isfinite(lb)):
            continue
        d = abs(la - lb)
        p['splice_diff_db'] = float(d)
        p['splice_diff_sd'] = float(d / sd2)
    out = {'usable': True, 'offset_m': o, 'noise_db': sig, 'sd_pair_db': float(sd2),
           'frac': frac, 'median_db': med_l, 'n_fibres': int(fin.sum()),
           'n_spots': len(good), 'loss': loss, 'null_diffs': nd,
           'before_m': (pg, o - g), 'after_m': (o + g, o + g + _NEAR_SPLICE_AFTER_M)}
    out['beyond_4sd_pct'] = _near_splice_same_pct(out, 4.0 * sd2)
    out['summary'] = ('Splice %.0f m past the panel port, measurable on %.0f%% of '
                      'fibres. Two shots of one fibre agree within %.3f dB (1 sd). '
                      'See the Near splice sheet.' % (o, 100 * frac, sd2))
    out['note'] = ('Near splice: usable - a splice %.0f m past the panel port carries '
                   'a measurable loss on %.0f%% of %d fibres (median %+.3f dB). One '
                   'shot measures it to %.3f dB, so two shots of one fibre agree '
                   'within %.3f dB (1 sd) and differ by more than 4 sd in %.2f%% of '
                   'cases. It is glass, so a re-plug cannot change it. Evidence '
                   'against a pair, never for one.'
                   % (o, 100 * frac, int(fin.sum()), med_l, sig, sd2,
                      out['beyond_4sd_pct']))
    return out


# ── Shot out of order ────────────────────────────────────────────────────
# A fibre skipped in the run and shot later: shot more than _FILL_IN_FAR_S
# after BOTH neighbouring fibre numbers, while those neighbours were shot
# within _FILL_IN_BRIDGE_S of each other (the tech jumped over it).  Only runs
# shot LATER count: a fibre shot at its normal time between two fill-ins
# looks the same in reverse and is not one.  Whole ribbons shot out of order
# fail the bridge test, which is what keeps them off the list.  Measured:
# Goodland originals 14 runs, and the Finals add exactly 0012 and 0841, the
# two known re-shoots; Monument 7; Duran->Ancho 10 (two of them next-day);
# Ancho->Duran 0 (its day boundary between fibres 432 and 433 is not listed);
# the RDR4RDR5 tray, LSC and both Dinwiddie panels 0.  It says where the port
# had to be found again.  It is not evidence of a duplicate.
_FILL_IN_FAR_S = 1800.0
_FILL_IN_BRIDGE_S = 900.0
_FILL_IN_MAX_RUN = 8


def _fill_ins(files):
    """[{'names': [...], 'first': n, 'last': n, 'shot_at', 'before', 'after',
    'before_at', 'after_at', 'minutes_later'}] sorted by shot time."""
    groups = {}
    for f in files or ():
        t = (f or {}).get('timestamp')
        try:
            pref, n = _port_split(f.get('name') or '')
        except Exception:
            continue
        if not t or n is None:
            continue
        groups.setdefault(pref, {}).setdefault(int(n), []).append(
            (float(t), f.get('name')))
    runs = []
    for pref, g in groups.items():
        if any(len(v) > 1 for v in g.values()):
            continue            # the same fibre number twice: panels mixed
        t = {n: v[0][0] for n, v in g.items()}
        nm = {n: v[0][1] for n, v in g.items()}
        taken = set()
        for a in sorted(t):
            if a - 1 not in t or a in taken:
                continue
            for length in range(1, _FILL_IN_MAX_RUN + 1):
                b = a + length - 1
                if b + 1 not in t or any(k not in t for k in range(a, b + 1)):
                    break
                lo, hi = t[a - 1], t[b + 1]
                if abs(hi - lo) > _FILL_IN_BRIDGE_S:
                    continue
                if all(t[k] - max(lo, hi) > _FILL_IN_FAR_S for k in range(a, b + 1)):
                    runs.append({'names': [nm[k] for k in range(a, b + 1)],
                                 'first': a, 'last': b, 'shot_at': t[a],
                                 'before': nm[a - 1], 'after': nm[b + 1],
                                 'before_at': lo, 'after_at': hi,
                                 'minutes_later': (t[a] - max(lo, hi)) / 60.0})
                    taken.update(range(a, b + 1))
                    break
    runs.sort(key=lambda r: r['shot_at'])
    return runs


def _mating_port_event(ev):
    """The panel-PORT mating: the last reflective interior event that carries a
    loss (falls back to the last interior event with a loss).

    The mating ranking was calibrated on jumper-straight-into-the-port shots
    (retruetest / LSC: launch -> port at 31.5 m -> trunk -> end), where the
    port is the FIRST interior event.  A tray shot through a launch reel
    (Dinwiddie: launch -> 1004 m reel -> reel-to-jumper mating -> 62 m jumper
    -> port at 1066 m -> trunk -> end) puts the reel-to-jumper mating first,
    and that mating is the SAME physical pairing for every port in the
    session, so reading it made the ranking blind to the port.  Taking the
    last reflective interior mating returns the port on both geometries.
    """
    inner = [e for e in ev[1:-1] if e.get('splice_loss') is not None]
    if not inner:
        return None
    refl = [e for e in inner
            if e.get('is_reflective') or (e.get('reflection') not in (None, 0, 0.0))]
    return (refl or inner)[-1]


def _mating_file_features(f):
    """(launch loss, port-mating loss, port-mating refl, launch refl,
    end refl) from the firmware event table, or None where absent.
    The port mating is chosen by _mating_port_event."""
    ev = (f or {}).get('events') or []
    if not ev:
        return {k: None for k in ('l0', 'l1', 'r1', 'r0', 'rE')}
    launch = ev[0]
    port = _mating_port_event(ev) if len(ev) > 2 else None
    if port is None and len(ev) == 2 and ev[1].get('splice_loss') is not None:
        port = ev[1]          # two-event table: the only interior mating
    last = ev[-1] if len(ev) > 1 else None
    return {'l0': launch.get('splice_loss'), 'r0': launch.get('reflection'),
            'l1': port.get('splice_loss') if port else None,
            'r1': port.get('reflection') if port else None,
            'rE': last.get('reflection') if last else None}


def _mating_top(analysis, n=20):
    """The top-n pairs by mating likelihood ratio, for the runner's manifest so
    the hub can show the ranking in-page in EVERY output mode.  Empty when the
    folder abstained (fewer than _MATING_MIN_PAIRS pairs), so the manifest key
    stays absent and every unaffected manifest is byte-stable (additive)."""
    if not (analysis or {}).get('mating'):
        return []
    ranked = [p for p in analysis.get('pairs') or [] if p.get('mating_lr') is not None]
    # The likelihood ratio is quantised: it reads bin indices, so at most
    # _MATING_NULL_BINS ** len(features) distinct values exist and the top one
    # is shared.  On Goodland->Monument 36 pairs sat on the ceiling and the
    # displayed top 20 was simply the first 20 alphabetically.  Raising the bin
    # count breaks the ties but costs discrimination (measured on retruetest:
    # 10 of 66 true pairs in the top 10 at 24 bins, 7 at 400), so the ratio is
    # left exactly as calibrated and ties are ordered by how alike the pair is
    # inside its own folder.  Display only; no likelihood value moves.
    ranked.sort(key=lambda p: (-p['mating_lr'], -(p.get('mating_tie') or 0.0)))
    # Each instrument's ratio is calibrated against that instrument's own
    # density, so the two are not on one scale and a straight merge lets the
    # louder one fill the whole list.  Take from each in turn instead.
    by_inst = {}
    for pr in ranked:
        by_inst.setdefault(pr.get('mating_inst'), []).append(pr)
    if len(by_inst) > 1:
        queues = [iter(v) for v in by_inst.values()]
        ranked, done = [], False
        while not done and len(ranked) < n:
            done = True
            for q in queues:
                nxt = next(q, None)
                if nxt is not None:
                    ranked.append(nxt)
                    done = False
    out = []
    for p in ranked[:n]:
        rec = {'a': p['a'], 'b': p['b'],
               'mating_lr': round(float(p['mating_lr']), 1),
               'mating_p': round(float(p['mating_p']), 4)}
        if p.get('mating_inst') is not None and len(by_inst) > 1:
            rec['instrument'] = str(p['mating_inst'])
        out.append(rec)
    return out


def _confidence_band(detail):
    """Detector confidence for the folder, from the competence ratio."""
    if not detail or detail.get('ratio') is None:
        return {'band': 'Unknown', 'ratio': None,
                'note': 'Detector confidence unknown: too few measurable files.'}
    r = float(detail['ratio'])
    band = ('High' if r >= _COMPETENCE_OK_RATIO
            else 'Medium' if r >= _COMPETENCE_MARGINAL_RATIO else 'Low')
    note = {'High': 'the fingerprint detector is competent here; the duplicate '
                    'likelihood tiers are the verdict.',
            'Medium': 'the fingerprint detector is marginal here; weak duplicates '
                      'can be missed. Use the mating likelihood as a second look.',
            'Low': 'the fingerprint detector cannot measure this folder; the '
                   'duplicate likelihood tiers carry no information here. The '
                   'mating likelihood is a ranking to check against the port log, '
                   'not a verdict.'}[band]
    return {'band': band, 'ratio': r,
            'note': f'Detector confidence {band} (ratio {r:.2f}): {note}'}


def _mating_gates(files, feats):
    """Which mating features this folder may use (see _MATING_LAUNCH_MIN_REFL_DB
    and _MATING_END_MAX_M).  Returns {'dropped': [...], 'launch_refl_db': x,
    'end_m': y, 'notes': [...]} so the sheet can print why."""
    r0 = [v['r0'] for v in feats.values() if v.get('r0') is not None]
    ends = []
    for f in files:
        ev = f.get('events') or []
        if len(ev) > 1 and ev[-1].get('dist_km') is not None:
            ends.append(float(ev[-1]['dist_km']) * 1000.0)   # the end event, engine-derived
        elif f.get('length'):
            ends.append(float(f['length']))
    launch = float(np.median(r0)) if r0 else None
    end_m = float(np.median(ends)) if ends else None
    dropped, notes = [], []
    if launch is None or launch <= _MATING_LAUNCH_MIN_REFL_DB:
        dropped += ['dl0', 'dr0']
        notes.append('launch connector not used: its reflectance reads '
                     + ('%.1f dB' % launch if launch is not None else 'nothing')
                     + ', below %.0f dB, a noise reading rather than a mating' % _MATING_LAUNCH_MIN_REFL_DB)
    if end_m is None or end_m >= _MATING_END_MAX_M:
        dropped += ['drE']
        notes.append('far-end reflectance not used: the far end is at '
                     + ('%.0f m' % end_m if end_m is not None else 'an unknown distance')
                     + ', beyond %.0f m, where its shot-to-shot repeat exceeds the port-to-port spread' % _MATING_END_MAX_M)
    return {'dropped': dropped, 'launch_refl_db': launch, 'end_m': end_m, 'notes': notes}


# ── Event-table fallback: what to say when the fingerprint cannot answer ─────
# When `_speckle_competence` returns MARGINAL or NOT MEASURED the trace can no
# longer decide, and the report used to print a bare zero beside a box
# explaining that the zero means nothing.  A tech reading that has no next
# step.  The stored event table is still there, so this asks a narrower
# question of it: do these two files describe the same matings, at the same
# places, with the same losses?
#
# RANKED, NOT BINARY.  Every comparable pair keeps a place in the order; the
# gates below are a SORT KEY and a marker, not a filter.  The boundary is
# soft - on Cle Elum 144f East the tightest pair below the gate is only 1.2x
# looser on reflectance than the loosest pair above it - so deleting the tail
# would throw away a pair that sits one notch outside.
#
# It is a TRIAGE LIST, never a verdict.  Nothing here routes a pair and p_dup
# is untouched, exactly as for the mating ranking below.
#
# AGAINST AN OUTSIDE AUDITOR (Yupana), all three of their Reubensville
# reports, every .sor on disk, 2026-09-21.  They publish flagged FIBRES at
# "95% or higher as possibly being fraudulent" - a likelihood, like this
# ranking, not a verdict.  Marked = fibres this gate puts inside its limits:
#
#     their report              direction     pairs   marked   agree    extra
#     ILA1-6 Panel A (52)       PTL1->PTL6   41,328      73   46/52       27
#     ILA1-6 Panel B (22)       PTL1->PTL6   41,328      73   20/22       53
#     ILA1-5 Panel A (33)       PLT1->PLT5   41,041      54   31/33       23
#     ILA1-6 Panel A            PTL6->PTL1   41,328      41   14/52       27
#     ILA1-6 Panel B            PTL6->PTL1   41,328      24    5/22       19
#     ILA1-5 Panel A            PLT5->PLT1   41,328      11    2/33        9
#
# 97 of 107 flagged fibres in the forward direction.  CAUTION ON THE REVERSE
# ROWS: they are weak, but that is not established as a failure.  If the
# duplication was committed in one direction's shots then the other direction
# has nothing to find, and Cle Elum shows the same shape (A->B found all three
# known pairs, B->A none).  Do not read the reverse rows as a miss rate
# without knowing which direction the auditor scored.
#
# A PRIOR VERSION OF THIS COMMENT, and the sentence the report printed, said
# one direction matched 43 of 52 while "the opposite direction of the same
# span matched 1 of 22".  That was wrong: the 22-fibre list is Panel B, a
# different acquisition, not Panel A's reverse direction.  Scoring a panel
# against another panel's answer key is meaningless.  Keys are per PANEL.
#
# WHICH ORDERING.  Measured by where the known duplicates land in the full
# 10,296-pair list (2026-09-21, every .sor on disk, Yupana's adjudication as
# truth; Cle Elum 144f East A->B, 3 true pairs):
#
#     ordering                              true pairs at rank
#     time gap alone                        530, 1108, 2065
#     max |d splice loss| alone              11,   63,  405
#     max |d reflectance| alone              19,   41,   43
#     normalised sum of the two               1,    2,    8
#     THIS: d reflectance, loss-gate          1,    2,    3
#           passers first
#
# On Cle Elum 288f West A-F B->A the same ordering puts the known 18<->48 at
# rank 1 of 10,296 (reflectance alone 5, splice loss alone 187, gap 2,885).
#
# WHY BOTH FEATURES.  As a yes/no gate each one alone is useless and the pair
# of them is not (same tray, same truth):
#
#     rule                                   flags   caught   false
#     max |d splice| <= 10 mdB                  94      2         92
#     max |d splice| <= 25 mdB                 579      3        576
#     time gap <= 20 min                     2,709      3      2,706
#     max |d reflectance| <= 0.20 dB            82      3         79
#     both gates together                        5      3          2
#
# Splice loss alone floods because the values are stored to the mdB and with
# 10,296 pairs exact ties are arithmetic, not physics.  Time gap is PRINTED
# and breaks exact ties but never orders anything else: the shortest gap in
# that tray is 91 s and belongs to a non-duplicate.
#
# THE REFLECTANCE GATE is Yupana's (an outside auditor whose short-span
# duplicate calls we reverse-engineered on Reubensville: 17 TP / 1 FP).  It
# discriminates because a re-shoot that never moved the jumper re-reads the
# SAME matings, while two different ports are two different connector pairs.
#
# WHERE THE TWO LIMITS COME FROM (swept 2026-09-21 against both adjudicated
# sets: Cle Elum 144f East A->B, 3 true pairs in 10,296; Reubensville PTL1 to
# PTL6 Panel A, an outside auditor's 52 flagged fibres in 41,328 pairs).
#
#   refl  loss |  Cle Elum marked / caught  |  Reubensville marked / agree / extra
#   0.20    25 |        5 / 3 of 3          |      60 /  43 of 52 / 17
#   0.25    25 |        9 / 3 of 3          |      73 /  46 of 52 / 27   <- shipped
#   0.30    25 |       13 / 3 of 3          |      87 /  48 of 52 / 39
#   0.40    25 |       23 / 3 of 3          |     110 /  50 of 52 / 60
#   0.50    25 |       32 / 3 of 3          |     133 /  50 of 52 / 83
#   0.20    40 |       15 / 3 of 3          |      66 /  44 of 52 / 22
#   0.20    60 |       27 / 3 of 3          |      78 /  44 of 52 / 34
#
# THE LOSS CUT IS NOT THE KNOB.  25 -> 40 -> 60 mdB buys at most one extra
# agreement (43 -> 44 -> 44) while the extras run 17 -> 22 -> 34, and on Cle
# Elum it triples the list to 15 rows and catches nothing new.  That is what
# 25 was calibrated on in the first place: the four known true pairs read 3,
# 8, 17 and 21 mdB, so real duplicates sit well inside it.
#
# REFLECTANCE IS THE KNOB, and it pays until about 0.30.  Cost per extra
# fibre caught: 3.3 more rows going to 0.25, 6 to 0.30, 10.5 to 0.40, and
# infinite past that (0.50 catches nothing 0.40 did not).  0.25 is the knee.
#
# Raising it is cheap here for a reason particular to this section: the
# reflectance limit is NOT part of the sort key, so it changes only how far
# down the rows are marked, never the order.  A tech works down the ranking
# and stops when the port log stops matching.  Revisit when more adjudicated
# folders land.
#
# WHAT IT DOES NOT DO.  On the Dinwiddie tie-panel reshoots (240 files, 28,680
# pairs, 48 genuine same-fibre re-shoots) it marks 0 of 48 - the class proven
# unrecoverable in every earlier sweep.  What matters there is that it stays
# QUIET rather than guessing: 4 marked pairs in 28,680.  A fallback that
# cannot answer should say little.
_EVT_FB_POS_TOL_M   = 2.0     # two events are the same event within this
_EVT_FB_REFL_DB     = 0.25    # max |d reflectance| over matched events
_EVT_FB_LOSS_DB     = 0.025   # max |d splice loss| over matched events
_EVT_FB_MIN_EVENTS  = 2       # fewer matched events than this: no opinion
_EVT_FB_LOSS_WEIGHT = 0.5     # splice loss counts half of reflectance in the score
_EVT_FB_PDF_ROWS    = 20      # the printed list is triage; past this it is not
_EVT_FB_XLSX_ROWS   = 500     # the workbook carries the deeper tail


def _evt_fb_events(f):
    """Events carrying a measured reflectance, as (position_m, loss_db, refl_db).

    Deliberately NOT `_event_match_quality`'s interior selection, which drops
    the end-of-fibre event and everything inside 10 m.  On the short panel
    spans this fallback exists for, those two ARE the event table: a Cle Elum
    fibre has three events (0 m, 69 m, 1,074 m) and the interior rule would
    leave one, below `_EVT_FB_MIN_EVENTS`.  The matings are the signal here,
    so the launch and the far end are kept.

    Events beyond the trace are dropped.  Stored tables carry entries the
    instrument never measured: a Reubensville file whose trace runs 0-5,000 m
    lists an event at 87,593,938 m, 17,000x past the far end, carrying a
    reflectance (-45.6 dB) and a loss (-0.274 dB) that would otherwise enter
    the max comparisons and let firmware junk manufacture agreement between
    two fibres.  The trace's own extent is the exact bound: there is no
    measured event past where the measurement stopped.
    """
    out = []
    pos = (f or {}).get('pos')
    try:
        far_m = float(np.max(pos)) if pos is not None and len(pos) else None
    except (TypeError, ValueError):
        far_m = None
    for e in (f or {}).get('events') or []:
        r = e.get('reflection')
        if r in (None, 0, 0.0) or (isinstance(r, float) and np.isnan(r)):
            continue
        sl = e.get('splice_loss')
        if sl is None or (isinstance(sl, float) and np.isnan(sl)):
            continue
        d_m = float(e.get('dist_km') or 0.0) * 1000.0
        if not np.isfinite(d_m) or d_m < 0.0:
            continue
        if far_m is not None and d_m > far_m + _EVT_FB_POS_TOL_M:
            continue
        out.append((d_m, float(sl), float(r)))
    return sorted(out)


def _evt_fb_compare(a_ev, b_ev):
    """Greedy nearest-position match; (n, max dpos, max dloss, max drefl).

    None when fewer than `_EVT_FB_MIN_EVENTS` events line up, which is the
    honest answer for a pair whose tables describe different structures.
    """
    if not a_ev or not b_ev:
        return None
    used = [False] * len(b_ev)
    dpos = dloss = drefl = 0.0
    n = 0
    for pa, la, ra in a_ev:
        best_j, best_d = -1, _EVT_FB_POS_TOL_M + 1.0
        for j, (pb, _, _) in enumerate(b_ev):
            if used[j]:
                continue
            d = abs(pa - pb)
            if d < best_d:
                best_d, best_j = d, j
        if best_j < 0:
            continue
        used[best_j] = True
        n += 1
        dpos  = max(dpos,  best_d)
        dloss = max(dloss, abs(la - b_ev[best_j][1]))
        drefl = max(drefl, abs(ra - b_ev[best_j][2]))
    if n < _EVT_FB_MIN_EVENTS:
        return None
    return n, dpos, dloss, drefl


def _event_table_fallback(files, pairs, competence_detail):
    """Rank pairs on their stored event tables, for folders whose fingerprint
    came back MARGINAL or NOT MEASURED.

    Returns a summary dict for the sheet, or None when the fingerprint
    answered for itself (status OK) and the fallback is not wanted.  Attaches
    'evt_fb_*' keys to every pair it could compare.  Display only: p_dup and
    every routing decision are untouched.
    """
    status = (competence_detail or {}).get('status')
    if status == 'OK':
        return None
    ev = {f['name']: _evt_fb_events(f) for f in files}
    by_name = {f['name']: f for f in files}
    rows = []
    for p in pairs:
        cmp_ = _evt_fb_compare(ev.get(p['a']), ev.get(p['b']))
        if cmp_ is None:
            continue
        n, dpos, dloss, drefl = cmp_
        p['evt_fb_n_events'] = int(n)
        p['evt_fb_max_dpos_m']   = float(dpos)
        p['evt_fb_max_dloss_db'] = float(dloss)
        p['evt_fb_max_drefl_db'] = float(drefl)
        within = (drefl <= _EVT_FB_REFL_DB) and (dloss <= _EVT_FB_LOSS_DB)
        p['evt_fb_within_gate'] = bool(within)
        ta = (by_name.get(p['a']) or {}).get('timestamp')
        tb = (by_name.get(p['b']) or {}).get('timestamp')
        gap = abs(ta - tb) if (ta and tb) else None
        p['evt_fb_gap_s'] = (None if gap is None else float(gap))
        rows.append({'a': p['a'], 'b': p['b'], 'n_events': int(n),
                     'max_dpos_m': float(dpos), 'max_dloss_db': float(dloss),
                     'max_drefl_db': float(drefl), 'within_gate': bool(within),
                     'gap_s': (None if gap is None else float(gap))})
    if not rows:
        return {'status': status, 'n_compared': 0, 'n_within': 0, 'rows': [],
                'margin': None,
                'refl_gate_db': _EVT_FB_REFL_DB, 'loss_gate_db': _EVT_FB_LOSS_DB,
                'note': ('No ranking from the event table either: no pair had '
                         f'{_EVT_FB_MIN_EVENTS} events at the same positions to '
                         'compare. The stored tables describe different '
                         'structures, or carry no reflectance.')}
    # One continuous score, each reading in its own limit's units, with
    # reflectance carrying twice the weight of splice loss.  See the
    # _EVT_FB_LOSS_WEIGHT block above for the measurements behind that weight
    # and for every feature tried and rejected.  Time gap breaks exact ties
    # and orders nothing else.
    for r in rows:
        r['score'] = (r['max_drefl_db'] / _EVT_FB_REFL_DB
                      + _EVT_FB_LOSS_WEIGHT * r['max_dloss_db'] / _EVT_FB_LOSS_DB)
    rows.sort(key=lambda r: (r['score'],
                             r['gap_s'] if r['gap_s'] is not None
                             else float('inf')))
    for i, r in enumerate(rows, 1):
        r['rank'] = i
    within = [r for r in rows if r['within_gate']]
    # Daylight under the gate, on the axis that discriminates: the tightest
    # reflectance among pairs that PASS the loss gate but fail the
    # reflectance one, against the loosest reflectance inside the gate.  Near
    # 1.0 means the cut fell inside a continuum and the marking is soft, which
    # the section says out loud.  Pairs rejected on LOSS are excluded: one
    # sitting at 0.03 dB is not "the next pair up" on reflectance, and
    # counting it drives the ratio below 1 and inverts the sentence.
    rejected = [r['max_drefl_db'] for r in rows
                if not r['within_gate'] and r['max_dloss_db'] <= _EVT_FB_LOSS_DB]
    margin = None
    if within and rejected and within[-1]['max_drefl_db'] > 0:
        margin = float(min(rejected) / within[-1]['max_drefl_db'])
    return {'status': status, 'n_compared': len(rows), 'n_within': len(within),
            'rows': rows, 'margin': margin,
            'refl_gate_db': _EVT_FB_REFL_DB, 'loss_gate_db': _EVT_FB_LOSS_DB,
            'note': ''}


def _mating_likelihood(files, pairs):
    """Attach 'mating_lr' and 'mating_p' to every pair (in place).

    Returns a summary dict for the sheet, or None when the folder has too few
    pairs for its own density.  Diagnostic and display only: nothing routes
    on these keys and p_dup is untouched.  See the calibration block above
    _MATING_TRUE_SCALES.
    """
    from scipy.stats import halfnorm
    n = len(pairs)
    if n < _MATING_MIN_PAIRS:
        for p in pairs:
            p['mating_lr'] = None
            p['mating_p'] = None
        return None
    feats = {f['name']: _mating_file_features(f) for f in files}
    gates = _mating_gates(files, feats)
    active = [k for k in _MATING_FEATURES if k not in gates['dropped']]
    cols = {k: np.full(n, np.nan) for k in active}
    keymap = {'dl0': 'l0', 'dl1': 'l1', 'dr1': 'r1', 'dr0': 'r0', 'drE': 'rE'}
    for i, p in enumerate(pairs):
        fa, fb = feats.get(p['a']), feats.get(p['b'])
        if not fa or not fb:
            continue
        for k in active:
            va, vb = fa[keymap[k]], fb[keymap[k]]
            if va is not None and vb is not None:
                cols[k][i] = abs(float(va) - float(vb))
    # ── One instrument at a time ────────────────────────────────────────────
    # A folder can carry two OTDRs.  Goodland->Monument is 1,152 files shot by
    # an FTBx-730D (serial 1882155, fibres 1-576) and an FTBx-730C (1723374,
    # 577-1152) in parallel, both named _1550 though their lasers sit 8 nm
    # apart.  Pooled, the ranking stops being about connectors: 99.9% of the
    # pairs above 10x were same-instrument against a 50% baseline, i.e. it was
    # reading which OTDR took the file.  So each instrument gets its own
    # density and its own ranking, and pairs that span two instruments carry
    # no mating likelihood at all.  A single-instrument folder has one group
    # and is byte-identical to before.
    serial = {f.get('name'): (f.get('serial_number') or None) for f in files}
    sa = [serial.get(p['a']) for p in pairs]
    sb = [serial.get(p['b']) for p in pairs]
    same_inst = np.array([(x is None or y is None or x == y)
                          for x, y in zip(sa, sb)], dtype=bool)
    inst = np.array([(x if (x is not None and x == y) else None)
                     for x, y in zip(sa, sb)], dtype=object)
    seen = sorted({v for v in inst if v is not None})
    groups = [(g, np.array([v == g for v in inst], dtype=bool)) for g in seen] \
        if seen else [(None, np.ones(n, dtype=bool))]
    if seen and not same_inst.all():
        groups = [(g, m) for g, m in groups if m.sum() >= _MATING_MIN_PAIRS]
    lr = np.ones(n)
    tie = np.zeros(n)          # continuous, for ordering inside a tied bin only
    used = []
    for k in active:
        x = cols[k]
        good = ~np.isnan(x)
        if good.sum() < _MATING_MIN_PAIRS:
            continue
        f_lr = np.ones(n)
        hit = False
        for _g, gm in groups:
            sel = gm & good
            if sel.sum() < _MATING_MIN_PAIRS:
                continue
            hit = True
            q = np.quantile(x[sel], np.linspace(0.0, 1.0, _MATING_NULL_BINS + 1))
            q[0], q[-1] = 0.0, np.inf
            pt = np.diff(halfnorm.cdf(q, scale=_MATING_TRUE_SCALES[k]))
            pt = np.maximum(pt, 1e-4)
            pt /= pt.sum()
            pn = np.full(_MATING_NULL_BINS, 1.0 / _MATING_NULL_BINS)
            b = np.clip(np.searchsorted(q, x[sel], side='right') - 1,
                        0, _MATING_NULL_BINS - 1)
            f_lr[sel] = pt[b] / pn[b]
            # where this pair's |d| falls inside its own group, 0 = most alike
            r = np.empty(int(sel.sum()))
            r[np.argsort(x[sel], kind='mergesort')] = (
                np.arange(int(sel.sum())) + 1.0) / int(sel.sum())
            tie[sel] += 1.0 - r
        if not hit:
            continue
        lr *= f_lr
        used.append(k)
        for i, p in enumerate(pairs):
            p['mating_' + k] = None if np.isnan(x[i]) else float(x[i])
    if not used:
        for p in pairs:
            p['mating_lr'] = None
            p['mating_p'] = None
        return None
    lr = lr ** _MATING_ALPHA
    scored = np.ones(n, dtype=bool)
    if len(groups) > 1 or not same_inst.all():
        scored = np.zeros(n, dtype=bool)
        for _g, gm in groups:
            scored |= gm
    prior = _MATING_PRIOR_DUPS / max(int(scored.sum()), 1)
    post = prior * lr / (prior * lr + 1.0 - prior)
    for i, p in enumerate(pairs):
        if not scored[i]:
            # two different OTDRs: the features compare hardware, not matings
            p['mating_lr'] = None
            p['mating_p'] = None
            p['mating_tie'] = None
            continue
        p['mating_lr'] = float(lr[i])
        p['mating_p'] = float(post[i])
        p['mating_tie'] = float(tie[i])
        p['mating_inst'] = inst[i]
    lr = np.where(scored, lr, -np.inf)
    top = int(np.argmax(lr))
    return {'n_pairs': int(scored.sum()), 'features': used, 'prior': prior,
            'gates': gates, 'instruments': [g for g, _ in groups if g],
            'n_cross_instrument': int(n - scored.sum()),
            'prior_note': (f'{_MATING_PRIOR_DUPS:g} duplicate pair expected per '
                           f'folder, i.e. 1 in {n:,} pairs; the likelihood ratio '
                           f'column is prior-free'),
            'top_lr': float(lr[top]), 'top_p': float(post[top]),
            'n_lr_ge_100': int((lr >= 100).sum()),
            'n_lr_ge_10': int((lr >= 10).sum())}


def _best_partners(files, pairs):
    """For each file, the pair giving the HIGHEST duplicate likelihood,
    tie-broken by the smallest disagreement; None for a file with no pair.

    One pass over the pairs.  This used to scan every pair once per file,
    which is files x pairs steps: about 760 million on a 1,152-file folder,
    roughly a minute, and slower still as each pair dict grows.  The choice
    is unchanged: pairs are still visited in list order and the comparison
    is strict, so an exact tie keeps the earlier pair, as the old scan did."""
    best_partner = {f['name']: None for f in files}
    for p in pairs:
        for name in (p['a'], p['b']):
            if name not in best_partner:
                continue
            best = best_partner[name]
            if best is None or (p['p_dup'] > best['p_dup']
                                or (p['p_dup'] == best['p_dup']
                                    and p['score'] < best['score'])):
                best_partner[name] = p
    return best_partner


def _analyze_sor(folder):
    """Shared SOR analysis: load files, compute pair metrics, apply
    physical-reality filters, pick best partners. Returns a dict the
    PDF and XLSX renderers can both consume.
    """
    paths = sorted(glob.glob(os.path.join(folder, '*.sor')))
    files = []
    for p in paths:
        try:
            files.append(load_sor_file(p))
        except Exception as e:
            print(f'  skip {os.path.basename(p)}: {e}')
    if len(files) < 2:
        raise RuntimeError(f'Not enough usable .sor files in {folder}')
    print(f'Loaded {len(files)} .sor files from {folder}')

    # Robust common span: rebuilt from the healthy population whenever
    # suspected breaks exist — see the calibration block above
    # _BREAK_FRAC_OF_MEDIAN.  Suspected breaks (EOF far below the folder
    # median) are always surfaced via `short_traces` AND excluded from pair
    # metrics (they physically lack the glass being compared), unless the
    # inconsistent-folder guard fired (window_guard below).
    sized = [f for f in files if f['length'] > 0]
    min_L, median_L, out_idx, excl_idx, window_guard = _robust_common_span(
        [f['length'] for f in sized])
    excluded_names = {sized[i]['name'] for i in excl_idx}
    short_traces = []
    for i in out_idx:
        f = sized[i]
        short_traces.append({
            'file': f['name'],
            'eof_m': round(float(f['length']), 1),
            'median_eof_m': round(median_L, 1),
            'excluded': f['name'] in excluded_names,
            'note': (f'ends at {f["length"]:.0f} m (folder median '
                     f'{median_L:.0f} m) — suspected break'),
        })
    short_traces.sort(key=lambda e: (e['eof_m'], e['file']))
    _ab_break_notes(short_traces, median_L)
    for e in short_traces:
        state = 'excluded from pair metrics' if e['excluded'] else 'kept'
        line = f'  Suspected break: {e["file"]} {e["note"]} [{state}]'
        if e.get('break_note'):
            line += f' — {e["break_note"]}'
        print(line)
    if window_guard:
        print(f'  WARNING: {window_guard}')
    if excluded_names:
        files = [f for f in files if f['name'] not in excluded_names]
        print(f'Common span restored to {min_L:.0f} m '
              f'({len(excluded_names)} suspected-broken trace(s) excluded, '
              f'{len(files)} files remain)')
    interior_start = _LAUNCH_SKIP_M
    interior_end = min_L - _END_BUFFER_M
    if interior_end - interior_start < 100:
        interior_start = max(2.0, min_L * 0.05)
        interior_end = max(interior_start + 2.0, min_L * 0.95)
    print(f'Interior window: {interior_start:.0f}–{interior_end:.0f} m  '
          f'(common span {min_L:.0f} m)')

    print(f'Computing pair metrics for {len(files)} files '
          f'({len(files) * (len(files) - 1) // 2} pairs)...')
    # Three-regime classifier (replaces the old file-count-floor heuristic):
    #
    #   PRODUCTION — typical case. Bulk pair-r low (~0.3), σ-outlier detector
    #                works because non-duplicate pairs define a clear bulk.
    #   TIE-PANEL  — many fibers sharing a launch+connector signal. Bulk r
    #                high (~0.95) AND bulk σ moderate (~0.15 dB) — the
    #                shared signal pulls r up but the fibers are physically
    #                different so σ doesn't collapse. Needs fingerprint
    #                extraction + tightened r-ramp + r-confirmation gate.
    #   ALL-DUPS   — every file is the same physical fiber. Bulk r high
    #                (~0.95) AND bulk σ at shot-noise floor (~0.06 dB).
    #                σ-outlier detector breaks (no non-duplicate bulk), so
    #                bypass it and use a widened r-ramp.
    #
    # First pass: compute pair metrics WITHOUT fingerprint extraction so
    # the classifier can see the raw σ/r distributions.
    batch_raw = _compute_pair_metrics_batch(files, interior_start, interior_end,
                                            tie_panel_mode=False)
    if batch_raw is None:
        raise RuntimeError('No comparable pairs after interior masking')
    sigma_raw, r_raw, valid_idx_raw = batch_raw
    iu_raw = np.triu_indices(sigma_raw.shape[0], k=1)
    bulk_sigma = float(np.median(sigma_raw[iu_raw])) if len(iu_raw[0]) else 0.0
    bulk_r = float(np.median(r_raw[iu_raw])) if len(iu_raw[0]) else 0.0
    # Fraction of pairs with elevated raw r. Catches tie panels whose MEDIAN
    # r is low (most ports mutually uncorrelated) but a large minority of
    # pairs share cable structure at r ~ 1.0. Example: 2 km tie panels
    # (CLQTILA) where median r ~ 0.18 yet ~48% of pairs sit at r >= 0.95
    # because they run the same route. Median alone misses these and they
    # cascade into 20k+ false positives in production mode — which also
    # OOM-kills the renderer (a 20k-row confirmed-duplicate table).
    frac_high_r = float((r_raw[iu_raw] >= 0.95).mean()) if len(iu_raw[0]) else 0.0
    # Four-regime classifier:
    #   all_dups    — every file IS the same fiber. High r, low σ.
    #   short_panel — many short fibers (< 200 m interior) in a panel where
    #                 the interior trace is too featureless for σ-outlier
    #                 to discriminate. Without this gate σ-outlier cascades
    #                 into thousands of false positives (BETA Raywood/
    #                 Sorrento etc.). Bulk r stays LOW on short panels
    #                 because the shared launch+connector signal doesn't
    #                 dominate a featureless interior, so the tie_panel
    #                 trigger never fires for these.
    #   tie_panel   — many fibers with shared structure. Triggered by EITHER
    #                 high median r (>=0.7, classic short-launch tie panels
    #                 like Deming) OR a high FRACTION of elevated-r pairs
    #                 (>=30% at r>=0.95, long tie panels like CLQTILA whose
    #                 median is low). Either way fingerprint extraction +
    #                 the tight ramp sort true re-shoots from shared cable
    #                 structure, so over-classifying here is self-correcting.
    #   production  — typical case.
    # Order matters: all_dups checked first so a hypothetical all-duplicates
    # short-fiber dataset doesn't get misrouted to short_panel.
    #
    # Two ADDITIVE tie_panel routes run after the existing rules (they can
    # only re-route folders that would otherwise land in 'production'):
    #   neighbor-decay — raw r falls off with port distance (shared glass:
    #                    jumper feed + ribbon). Copies don't care about port
    #                    distance, so decay ⇒ shared path, not duplication.
    #                    A-F West: 1 997 σ-outlier false positives at ≥99%
    #                    in production mode; its bulk_r stayed 0.05 because
    #                    shared-glass r tops out ~0.8 among NEIGHBORS only,
    #                    which the bulk_r / frac_high_r triggers can't see.
    #   short common span — < 2 km of shared window is launch+connector
    #                    dominated; too little Rayleigh fingerprint for the
    #                    σ-outlier bulk to mean anything.
    regime_reason = None
    # all_dups additionally requires a LONG-ENOUGH common window for bulk_r
    # to mean anything.  Short-shot folders of UNIQUE fibers (Span 7
    # Tularosa-Orogrande: 864 fibers, ~5 km common span) correlate broadband
    # over the short shared window — bulk r lands INSIDE the all_dups
    # 0.85-0.95 ramp and the ordinary bulk walks over 50% (62,014 false
    # pairs, issue #9), with the self-refuting signature frac_high_r = 0.00
    # and ZERO pairs at >=99%.  Same physics as the < 2 km tie_panel rule,
    # applied to the gate that claims folders FIRST.  A short folder with
    # high bulk r falls through to the bulk_r >= 0.7 tie_panel route
    # (fingerprint extraction + the 0.999 ramp) — true re-shoots still land
    # at r >= 0.999 there, and byte-copies are caught regime-independently
    # by the raw-identity short-circuit.
    #
    # all_dups ALSO has to survive its own self-refutation check: a folder
    # where every file is the same fiber has most pairs near-identical, so
    # frac_high_r must be high.  frac_high_r = 0.00 with an all_dups claim
    # is self-refuting (BKF↔DEL 80 km — see _ALLDUPS_MIN_HIGHR_FRAC), and
    # such folders route to production, where the σ-outlier bulk, the twin
    # gate, and the 0.95-0.99 ramp all apply.
    # sigma_ratio (the standalone Secret Sauce's noise-relative all_dups
    # gate, bulk_sigma / (sqrt(2) * noise_floor) <= 3.0) was evaluated for
    # porting here on 2026-08-29 and CLOSED AS SUPERSEDED.  Measured: the
    # 3.0 threshold is unreachable — real same-fiber duplicate pairs read
    # 5.53 / 6.24 / 10.59 / 14.37 and the loosest folder on disk (LAMBEY)
    # reads 15.3, so it would not fire even on a genuine all-duplicates
    # folder.  Its noise_floor is also quantization-limited rather than a
    # noise measurement: the 2nd-difference MAD lands on integer multiples
    # of the 0.000999 dB Bellcore storage quantum (SEANOR pegged at exactly
    # 1.000, EMVSUI Long 2.004, EMVSUI Short 9.008), so it takes about six
    # values corpus-wide.  And DURANC, the folder it was written for, is
    # already blocked twice here by _robust_common_span (5 broken traces
    # excluded, min_L 6985 -> 89902 m, bulk_r 0.9873 -> 0.5073) and by
    # _ALLDUPS_MIN_SPAN_M.  NOT by _ALLDUPS_MIN_HIGHR_FRAC — DURANC's
    # frac_high_r is 0.7758, which clears 0.5.  The three guards are
    # complementary, not interchangeable.
    alldups_refuted = (bulk_r >= 0.7 and bulk_sigma < 0.10
                       and min_L >= _ALLDUPS_MIN_SPAN_M
                       and frac_high_r < _ALLDUPS_MIN_HIGHR_FRAC)
    if (bulk_r >= 0.7 and bulk_sigma < 0.10
            and min_L >= _ALLDUPS_MIN_SPAN_M
            and frac_high_r >= _ALLDUPS_MIN_HIGHR_FRAC):
        regime = 'all_dups'
    elif alldups_refuted:
        # Self-refuted all_dups claim.  Route PRODUCTION, not tie_panel:
        # bulk_r >= 0.7 would otherwise hand the folder straight to the
        # tie_panel route below, which bypasses σ-outlier — and σ-outlier
        # against a real non-duplicate bulk is exactly the detector a
        # 432-unique-fiber long span needs.
        regime = 'production'
        regime_reason = (f'all_dups refuted: frac high-r {frac_high_r:.2f} '
                         f'< {_ALLDUPS_MIN_HIGHR_FRAC:.2f}')
    elif min_L < 200 and len(files) >= 50:
        regime = 'short_panel'
    elif bulk_r >= 0.7 or frac_high_r >= 0.30:
        regime = 'tie_panel'
    else:
        regime = 'production'
    # Measured on EVERY run, routed on only in the 'production' branch below.
    # The rule fires on no folder on disk (see the _DECAY_* audit note), so
    # this line is how the first folder it does misroute becomes visible.
    names_raw = [files[i]['name'] for i in valid_idx_raw]
    serials_raw = [files[i].get('serial_number') for i in valid_idx_raw]
    decay = _neighbor_decay(names_raw, r_raw, serials_raw)
    if regime == 'production':
        # Additive tie_panel re-routes: only ever applied to folders that
        # landed on 'production' (including via the all_dups refutation).
        _extra = None
        if decay is not None and (decay[0] - decay[1]) >= _DECAY_MIN_DROP:
            regime = 'tie_panel'
            _extra = (f'neighbor-decay: near r {decay[0]:.2f} '
                      f'vs far r {decay[1]:.2f}')
        elif min_L < _SHORT_COMMON_SPAN_M:
            regime = 'tie_panel'
            _extra = f'short common span: {min_L:.0f} m'
        if _extra:
            regime_reason = (f'{regime_reason}; {_extra}' if regime_reason
                             else _extra)
    _reason_sfx = f', {regime_reason}' if regime_reason else ''
    print(f'Regime: {regime} (bulk σ={bulk_sigma:.4f} dB, '
          f'bulk r={bulk_r:.4f}, frac high-r={frac_high_r:.2f}{_reason_sfx})')
    regime_margin = _regime_margin_note(bulk_r, bulk_sigma)
    if regime_margin:
        print(f'Regime margin: {regime_margin}')
    # Diagnostic, always logged, never routed on outside the branch above.
    if decay is not None:
        print(f'Port-distance decay: near r {decay[0]:.4f} vs far r '
              f'{decay[1]:.4f} (drop {decay[0] - decay[1]:.4f}, trigger '
              f'{_DECAY_MIN_DROP:.2f}; {decay[2]} near / {decay[3]} far pairs, '
              f'same instrument)')
    else:
        print('Port-distance decay: not measurable on this folder')
    tie_panel_mode = (regime == 'tie_panel')
    if regime == 'tie_panel':
        # Re-compute with fingerprint extraction (median-trace subtraction)
        # so the r-tier sees per-fiber residuals instead of shared signal.
        batch = _compute_pair_metrics_batch(files, interior_start, interior_end,
                                              tie_panel_mode=True)
    else:
        batch = batch_raw
    sigma_matrix, r_matrix, valid_idx = batch
    # Raw-identity short-circuit inputs: σ is computed on raw traces in BOTH
    # batch passes (tie_panel_mode only changes r), so sigma_matrix is already
    # the raw σ.  Raw r comes from the first (pre-fingerprint) pass.  valid_idx
    # selection is deterministic on (files, window) so the two passes align;
    # guard anyway — a mismatch disables the short-circuit rather than
    # mis-indexing a matrix.
    r_raw_aligned = r_raw if list(valid_idx) == list(valid_idx_raw) else None
    pairs = []
    K = len(valid_idx)
    for ki in range(K):
        i = valid_idx[ki]
        name_i = files[i]['name']
        len_i = files[i].get('length')
        for kj in range(ki + 1, K):
            j = valid_idx[kj]
            len_j = files[j].get('length')
            len_delta = (abs(len_i - len_j) if (len_i and len_j) else None)
            sigma_ij = float(sigma_matrix[ki, kj])
            raw_r_ij = (float(r_raw_aligned[ki, kj])
                        if r_raw_aligned is not None else None)
            pairs.append({
                'a': name_i,
                'b': files[j]['name'],
                'score': sigma_ij,
                'shape_r': float(r_matrix[ki, kj]),
                'shape_r_raw': raw_r_ij,
                'raw_identical': bool(raw_r_ij is not None
                                      and raw_r_ij >= _RAW_IDENT_R
                                      and sigma_ij <= _RAW_IDENT_SIGMA_DB),
                'length_delta_m': len_delta,
            })
    if not pairs:
        raise RuntimeError('No comparable pairs after interior masking')
    print(f'Pair metrics ready: {len(pairs)} pairs')

    scores = np.array([p['score'] for p in pairs], dtype=np.float64)
    p_dup_sigma, stats = _outlier_probability(scores)

    # Pearson-shape contribution. Each regime uses its own r-ramp:
    #   production: (0.95 → 0.99)     standard
    #   tie-panel:  (0.999 → 0.9999)  tightened — fingerprint extraction
    #               on tie panels leaves residual r up to ~0.998 between
    #               physically-different fibers (shared 2-km-scale bend
    #               structure the median can't fully capture). True same-
    #               fiber re-shoots in a tie panel land at r ≥ 0.9999.
    #   all-dups:   (0.85 → 0.95)     widened — every pair is genuinely
    #               a same-fiber re-shoot, so even pairs with r as low as
    #               0.85 (short-fiber shot-noise spread) are real duplicates.
    if regime == 'tie_panel':
        R_LO, R_HI = 0.999, 0.9999
    elif regime == 'all_dups':
        R_LO, R_HI = 0.85, 0.95
    elif regime == 'short_panel':
        # ABSTAIN.  This branch used to say "true same-fiber re-shoots in a
        # short panel still produce r >= 0.95", and use the production ramp
        # as the entire detector for the regime.  Measured 2026-08-31, that
        # sentence is false, and the ramp separates nothing in EITHER
        # direction.
        #
        # Two 31 m folders, 12 files each, SAME instrument (FTBx-730C-SM2-
        # OPM-EA sn 870995), same 1552.9 nm, same 5 ns pulse, 38 minutes
        # apart.  Raw r, which is what this ramp reads:
        #
        #     retruetest   ONE fiber x12, 66 REAL dups   p50 0.9642
        #                                                min 0.9423
        #                                                max 0.9874
        #     LSC1->LSC6   288 DIFFERENT fibers, 0 dups  p50 0.9618
        #                                                min 0.9177
        #                                                max 0.9926
        #
        # The different-fiber MAXIMUM (0.9926) is ABOVE the true-fiber
        # maximum (0.9874), and 16,376 different-fiber pairs sit above the
        # true-pair median.  There is no threshold on this axis that keeps
        # duplicates on one side.
        #
        # What it yields today, on every short panel on disk:
        #
        #     folder                     >=0.99   >=0.50   >=0.10   truth
        #     LSC1->LSC6      31 m            0    7,588   32,732   0 dups
        #     REUB PTL5 A     31 m            0       34    3,357   none known
        #     BETA LFY E DW   62 m            0        0        0   none known
        #     BETA ORN W SW   62 m            0        0        0   none known
        #     Cle Elum E 144f 68 m            0        0        0   Yupana list
        #     Cle Elum W 144f 68 m            0        0        0   Yupana list
        #
        # Zero true positives anywhere, including on the two trays that
        # carry a known duplicate list, against 7,588 cells at >=0.50 on a
        # folder proven to contain no duplicates at all.  It is a
        # false-positive generator with no measured yield, so it goes.
        #
        # Abstention rather than a different ramp: at these spans the
        # same-fiber and different-fiber distributions are NESTED, not
        # shifted (0/66 at 0 FP in ten configurations against a matched
        # same-instrument null, and 0/48 on a zero-confound control).  A
        # tuned replacement would be fitting noise.  With sigma-outlier
        # already bypassed for this regime, the folder now produces no
        # duplicate claim at all - and `Speckle competence` in the run log
        # is what tells the tech that is abstention, not absence.
        R_LO, R_HI = None, None
    else:
        R_LO, R_HI = 0.95, 0.99
    _R_SPAN = None if R_LO is None else R_HI - R_LO
    def _r_to_p(r):
        if R_LO is None:
            # Abstaining regime: the r axis carries no information here.
            return 0.0
        if r is None:
            return 0.0
        if r >= R_HI:
            return 1.0
        if r <= R_LO:
            return 0.0
        return float((r - R_LO) / _R_SPAN)

    p_dup_r = np.array([_r_to_p(p.get('shape_r')) for p in pairs],
                       dtype=np.float64)

    # σ-outlier handling: ONLY production mode trusts it. Every other regime
    # bypasses σ-outlier and lets the regime-specific r-ramp drive the verdict.
    #   production  — standard max(σ-outlier, r-tier) combiner.
    #   tie_panel   — bypass σ. The fingerprint-extracted tight r-ramp
    #                 (0.999-0.9999) is the detector. σ-outlier would cascade
    #                 on shared cable structure: on a 2 km tie panel (CLQTILA)
    #                 ~48% of pairs share enough route structure that σ looks
    #                 like an outlier AND post-fingerprint r still sits above
    #                 0.9, so the old r≥0.9 confirmation gate let 20k false
    #                 positives through. True re-shoots survive (post-FP r→1.0).
    #   all_dups    — no non-duplicate bulk to define an "outlier".
    #   short_panel — short featureless fibers give a narrow σ bulk that
    #                 cascades.
    # ── Shared Rayleigh-speckle context, built at most once per run ───────
    # Both the twin-gate refutation below and the speckle confirmation gate
    # further down read the same folder null and the same per-file windows.
    _by_name = {f['name']: f for f in files}
    # One filter width for the whole folder, from its own acquisition.
    _hp_w = _speckle_hp_width(files)
    if _hp_w != _SPECKLE_HP_WIDTH:
        print(f'Speckle high-pass: {_hp_w} samples '
              f'(pulse-matched; calibrated cap is {_SPECKLE_HP_WIDTH})')
    # The band's left edge, once the launch hardware is out of it.  None on
    # every folder whose 2% offset already clears its launch, which keeps
    # those folders byte-identical (see the _SPECKLE_LAUNCH_ZONE_FRAC block).
    _band_start, _band_note = _speckle_band_floor(files, interior_start,
                                                  interior_end)
    _f0, _f1 = _SPECKLE_WINDOWS[0]
    _bspan = interior_end - interior_start
    if _band_start is not None:
        print(f'Speckle band: starts {_band_start:,.0f} m, past the shared '
              f'reflective event in the launch zone (would have been '
              f'{interior_start + _bspan * _f0:,.0f} m; band ends '
              f'{interior_start + _bspan * _f1:,.0f} m)')
    elif _band_note:
        print(f'Speckle band: {_band_note}')
    _spk = {'cache': {}, 'null_q': {}}

    def _spk_wl(name_or_file):
        """The acquisition wavelength this trace was shot at, rounded to
        0.1 nm, or None when the file does not report one."""
        f = (name_or_file if isinstance(name_or_file, dict)
             else _by_name.get(name_or_file))
        wl = (f or {}).get('wavelength')
        try:
            return None if wl is None else round(float(wl), 1)
        except (TypeError, ValueError):
            return None

    def _spk_null(wl=None):
        """Folder null: what the statistic reads between files KNOWN to be
        different fibers here.  Evenly-spaced sample (no RNG — the run has
        to be reproducible).

        GROUPED BY WAVELENGTH.  Rayleigh speckle is a wavelength-dependent
        interference pattern: the same glass shot at a different lambda
        gives an unrelated fingerprint.  Pooling wavelengths therefore makes
        the null BIMODAL — same-lambda pairs carry the real correlation
        while cross-lambda pairs sit near zero — and a percentile of a
        bimodal distribution describes neither mode.

        56 folders on disk carry more than one acquisition wavelength,
        including production spans: MILTOP and TOPMIL (1152 files, 1548.0 +
        1539.8), MILELMsh (1539.8 + 1554.8), LONGS (864 files, 1550.4 +
        1554.8), Niland and Mecca (576 each), and Winterhaven, which carries
        FOUR.  Measured on LONGS, 1,770 known-different pairs:

            pooled          p50 +0.0230   p99 +0.0780   3x bar 0.234
            wl 1550.4       p50 +0.0332   p99 +0.0992   3x bar 0.298
            wl 1554.8       p50 +0.0221   p99 +0.0702   3x bar 0.211

        The pooled null is not merely inflated, it is wrong in BOTH
        directions: too lax for 1550.4 and too strict for 1554.8.  Neither
        wavelength is judged against what its own fibers actually do.

        A pair whose two files were shot at DIFFERENT wavelengths has no
        null of its own and cannot be compared anyway, so it is
        unmeasurable rather than scored — see _spk_null_for_pair.
        """
        key = wl
        if key not in _spk['null_q']:
            _spk['null_q'][key] = None
            step = max(1, len(files) // _SPECKLE_NULL_FILES)
            sample = [f for f in files[::step][:_SPECKLE_NULL_FILES]
                      if key is None or _spk_wl(f) == key]
            null_res = [(_speckle_windows(f, interior_start, interior_end,
                                          hp_width=_hp_w,
                                          band_start_m=_band_start))
                        for f in sample]
            null_res = [r for r in null_res if r is not None]
            null_vals = [v for a_i in range(len(null_res))
                         for b_i in range(a_i + 1, len(null_res))
                         for v in (_speckle_pair_r(null_res[a_i], null_res[b_i]),)
                         if v is not None]
            if len(null_vals) >= _SPECKLE_NULL_MIN_PAIRS:
                _spk['null_q'][key] = float(np.percentile(null_vals,
                                                          _SPECKLE_NULL_PCT))
        return _spk['null_q'][key]

    def _spk_null_for_pair(a_name, b_name):
        """(null, comparable).  `comparable` is False when the two files were
        shot at different wavelengths — their speckle patterns are unrelated
        by physics, so the statistic means nothing for that pair and it must
        be treated as UNMEASURABLE (kept), never as a refutation."""
        wa, wb = _spk_wl(a_name), _spk_wl(b_name)
        if wa is None or wb is None:
            # A file that does not report its wavelength keeps the old
            # pooled behaviour: fail-safe, and no folder on disk hits it.
            return _spk_null(None), True
        if wa != wb:
            return None, False
        nq = _spk_null(wa)
        # Too few same-wavelength files to build that lambda's own null:
        # fall back to the pooled one rather than losing the gate entirely.
        return (nq if nq is not None else _spk_null(None)), True

    def _spk_win(name):
        if name not in _spk['cache']:
            _spk['cache'][name] = _speckle_windows(_by_name.get(name),
                                                   interior_start, interior_end,
                                                   hp_width=_hp_w,
                                                   band_start_m=_band_start)
        return _spk['cache'][name]

    if regime in ('tie_panel', 'all_dups', 'short_panel'):
        p_dup_sigma_eff = np.zeros_like(p_dup_sigma)
    else:
        p_dup_sigma_eff = p_dup_sigma
    # Combined likelihood = max of (possibly confirmed) σ-outlier and r tiers.
    p_dup_raw = np.maximum(p_dup_sigma_eff, p_dup_r)

    # ── Fingerprint rescue from a sigma-bypassed regime ───────────────────
    # tie_panel / all_dups / short_panel throw the sigma-outlier result away
    # wholesale, because on a genuine shared-glass folder it cascades.  That
    # is right for the bulk and wrong for the tail: a pair that is an EXTREME
    # sigma outlier and ALSO carries the fiber's own Rayleigh fingerprint is
    # not shared structure, and zeroing it is the exact failure PR #122
    # repaired on EMVSUI by a different route.
    #
    # MEASURED on MILTOP (Miller->Topeka, 1146 files after break exclusion).
    # It clears the tie_panel trigger by 0.0214 - bulk_r 0.7214 against 0.70 -
    # and the bypass then discards:
    #     MILTOPls0329/0330  sigma 0.00985  p_sigma 0.9991  r 0.9965 -> 0.0000
    #     MILTOPls0830/0831  sigma 0.00941  p_sigma 0.9995  r 0.9964 -> 0.0000
    # Fingerprinted against a 1,770-pair known-different null on that folder
    # (p50 0.0310, p99 0.1066, MAX 0.2470): 329/330 reads 0.8243, which is
    # 3.3x the maximum any different-fiber pair there reaches, with identical
    # EOF.  830/831 reads 0.1225 against a same-fiber floor of 0.3055 and is
    # NOT rescued - the bar is doing real work, not waving both through.
    #
    # WHY THIS CANNOT RE-OPEN THE CASCADES THE REGIME EXISTS TO STOP: the
    # candidate set is empty on every other LONG sigma-bypassed folder on
    # disk.  Measured p_dup_sigma > _SIGMA_RESCUE_MIN: A-F West 0 (the
    # 1,997-FP panel), A-F East 0, BKF<->DEL 0 (the 47-FP set), LAMBEY 0
    # (the 67-FP set), TULORO 0 (the 62k flood), ELMMIL short 0.  MILTOP's 2
    # are the only candidates among them.  A LONG folder whose sigma bulk is
    # genuinely cascading has no extreme outliers to rescue, by construction.
    #
    # THE WORD "LONG" IS LOAD-BEARING.  Short panels are NOT empty: the 20
    # on disk carry 132 to 6,081 candidates each (2,953 on BETA LFY East
    # 144f DW Tray A-F, which is the historic flood number).  Nothing is
    # rescued there today only because the confirm bar is
    # _SPECKLE_CONFIRM_NULL_MULT x the folder's own null p99, and on every
    # span class below 78 km that product EXCEEDS 1.0 - a value a Pearson r
    # cannot take.  Measured bars: EMVSUI Long 78.5 km 0.257 (usable), BETA
    # 62 m 1.718, LSC 31 m 1.800, Reubensville 31 m 2.064, Dinwiddie 2.07 km
    # 2.883, EMVSUI Short 3.99 km 2.927, ELMMIL sh 4.99 km 2.929.
    #
    # So on short panels this rescue is safe by ARITHMETIC, not by the
    # emptiness argument above.  Anyone repairing that bar must re-measure
    # the short-panel candidate sets BEFORE lowering it, or this path
    # inherits the flood.  (Measured 2026-08-31.)
    #
    # Rescued pairs re-enter at their sigma likelihood and then face EVERY
    # downstream gate - length, events, twin, serial and the speckle veto -
    # exactly as a production-regime pair does.
    n_sig_rescued = 0
    if regime in ('tie_panel', 'all_dups', 'short_panel'):
        _cands = [i for i in range(len(pairs))
                  if p_dup_sigma[i] > _SIGMA_RESCUE_MIN]
        if _cands:
            for i in _cands:
                pr = pairs[i]
                # Each pair against ITS OWN wavelength's null; a
                # cross-wavelength pair has no comparable null at all.
                _nq, _cmp = _spk_null_for_pair(pr['a'], pr['b'])
                if _nq is None or not _cmp:
                    continue
                _bar = _nq * _SPECKLE_CONFIRM_NULL_MULT
                ra, rb = _spk_win(pr['a']), _spk_win(pr['b'])
                r_hp = _speckle_pair_r(ra, rb)
                r_floor = _speckle_same_fiber_floor(ra, rb, pr['score'])
                if r_hp is None or r_floor is None:
                    continue
                # Both: clearly above what different fibers do here, AND
                # at least what the same-fiber hypothesis predicts at this
                # pair's own sigma.
                if r_hp < _bar or r_hp < r_floor:
                    continue
                p_dup_raw[i] = max(p_dup_raw[i], float(p_dup_sigma[i]))
                pr['sigma_rescued'] = True
                pr['speckle_r'] = round(float(r_hp), 4)
                n_sig_rescued += 1
            print(f'Sigma rescue: {len(_cands)} extreme outlier(s) in a '
                  f'{regime} folder, {n_sig_rescued} confirmed by fingerprint')

    # Physical-reality filter: same fiber must produce the same end-of-fiber
    # length to within launch-connector + IOR + sample-resolution variation.
    # Tolerance scales with fiber length but is bounded:
    #   - floor 0.5 m  (launch-mating + OTDR sample resolution dominate at small spans)
    #   - 0.01 % of length above 5 km
    #   - cap 2 m      (avoid being too permissive on 100 km+ spans)
    # When a pair's length delta exceeds tol, cap likelihood at 0.5 (borderline) —
    # different physical fibers can't be the same fiber regardless of how similar
    # their splice profiles look. Pairs with no length info pass through.
    LEN_CAP = 0.5
    def _len_tol_m(length_m):
        # Tolerance accommodates launch-cable-swap systematic offsets (~5 m
        # observed in real re-shoots) but still catches physically-different-
        # fiber routing differences (typically tens to hundreds of meters
        # when paths diverge at closures). The event filter does the
        # fine-grained discrimination — length is just a coarse pre-filter.
        if length_m is None or length_m <= 0:
            return 10.0
        return max(10.0, length_m * 5e-4)
    length_deltas = np.array([(p.get('length_delta_m') or 0.0) for p in pairs], dtype=np.float64)
    has_lengths = np.array([p.get('length_delta_m') is not None for p in pairs])
    # Use the LONGER of the two fibers in the pair to set tolerance.
    name_to_length = {f['name']: (f.get('length') or 0) for f in files}
    pair_max_len = np.array([
        max(name_to_length.get(p['a'], 0), name_to_length.get(p['b'], 0))
        for p in pairs
    ], dtype=np.float64)
    tols = np.array([_len_tol_m(L) for L in pair_max_len], dtype=np.float64)
    length_violation = has_lengths & (length_deltas > tols)

    # Event-table consistency gate: same physical fiber → splice events match
    # in count, position, and loss. Different fibers can share σ/r and even
    # length (paths diverge then reconverge) but their event tables disagree.
    # Only evaluate pairs that survived the σ/r screen, since pairs already
    # at p_dup_raw < 0.1 won't be flagged regardless.
    file_events = {f['name']: f.get('events') for f in files}
    events_violation = np.zeros(len(pairs), dtype=bool)
    EVENT_CHECK_THRESHOLD = 0.10
    for i, p in enumerate(pairs):
        if p_dup_raw[i] < EVENT_CHECK_THRESHOLD:
            continue
        (n_match, n_max, n_min, mean_dloss, max_dloss,
         median_dloss, n_max_sig) = _event_match_quality(
            file_events.get(p['a']), file_events.get(p['b']))
        p['events_n_match'] = int(n_match)
        p['events_n_max']   = int(n_max)
        p['events_n_min']   = int(n_min)
        p['events_mean_dloss_db'] = float(mean_dloss)
        p['events_max_dloss_db']  = float(max_dloss)
        p['events_median_dloss_db'] = float(median_dloss)
        p['events_n_max_significant'] = int(n_max_sig)
        if not _events_agree(n_match, n_max, n_min, mean_dloss,
                             median_dloss_db=median_dloss,
                             n_max_significant=n_max_sig):
            events_violation[i] = True
            # Distinguish "the tables disagree" from "the table is present
            # but too thin to check" in the internals (both cap
            # identically).  See _events_agree: BKFDEL028/040 are the only
            # 2 of 432 files on that span with <= 2 interior events, and
            # every one of the 47 false positives contained one of them.
            if n_min < 3:
                p['events_unverifiable'] = True

    # ── Event-gate refutation by fingerprint ─────────────────────────
    # The loss leg of the event gate asks "do these two stored tables report
    # the same splice losses?" and answers it with a fixed 10 mdB cut on the
    # median |Δloss|.  That cut is a PROXY for fiber identity.  The Rayleigh
    # speckle answers the identity question by direct measurement, so where
    # the two disagree the measurement decides - the same treatment the twin
    # gate's σ-ratio proxy already gets below.
    #
    # WHY THE PROXY NEEDS ONE.  The cut sits far below the spread real
    # fibers show.  Measured on Romero->Tucu (40 files, 97 km, production,
    # 780 different-fiber pairs with >= 3 matched events), median |Δsplice
    # loss| between DIFFERENT fibers reads p25 0.0474 / p50 0.0675 / p75
    # 0.0930 dB.  The 0.010 cut is 4-6x below that, so there is a wide band
    # of entirely plausible same-fiber table differences that trips the gate
    # while the glass still says one fiber.  Demonstrated by nudging each of
    # one file's 14 interior splice losses in BOTH the samples and the
    # stored table, which keeps every event where it is and every length
    # intact (harness9.py in the counsel-response measurements):
    #
    #     nudge   pair σ    r        median|Δl|  speckle r   verdict was
    #     0.005   0.0036   0.9994   0.0027      0.9944      1.00
    #     0.010   0.0073   0.9975   0.0055      0.9783      1.00
    #     0.020   0.0145   0.9896   0.0110      0.9216      0.50  <- capped
    #     0.030   0.0218   0.9759   0.0165      0.8467      0.50  <- capped
    #
    # against a folder null whose p99 is 0.0794 and whose MAXIMUM over those
    # 780 known-different pairs is 0.1102.  A pair reading 0.92 there is not
    # a borderline call.
    #
    # WHAT MAY BE REFUTED - the loss magnitude, and nothing else.  The pair
    # is re-asked of _events_agree with the loss cut lifted and every other
    # threshold untouched; only a pair that passes THAT is eligible, so the
    # scope can never drift from the calibrated function.  Two consequences,
    # both deliberate:
    #
    #   * The EVENT-POOR leg is not refutable at any fingerprint.  A pair
    #     whose table is present but thinner than `min_count`
    #     (events_unverifiable) is skipped outright and keeps its cap. That
    #     is the BKF<->DEL 80 km case the gate was built for: BKFDEL028 and
    #     BKFDEL040 are the only 2 of 432 files with <= 2 interior events,
    #     and all 47 false positives on that span contained one of them.
    #     Those pairs never reach the fingerprint test, and the folder is
    #     measured unchanged (see the branch's before/after corpus table).
    #   * The COUNT leg is not refutable either.  A genuine re-shoot detects
    #     the same splices in both shots - the calibration measured 100%
    #     match rate and equal counts on true same-fiber pairs - so an
    #     asymmetric table is real evidence about the pair and keeps its cap
    #     even when the fingerprint is strong.
    #
    # MEASURED ON THE WHOLE CORPUS (2026-09-18, before/after on the pinned
    # engine).  10 folders, 5,904 files, 2,606,868 pairs - the historic
    # false-positive floods named in the calibration comments above plus the
    # trusted duplicate sets and the one folder with known same-fibre truth:
    #
    #     folder                  files    pairs   ev-capped  eligible  refuted
    #     A-F West 145-288          264   34,716          0         0        0
    #     A-F East 1-144            288   41,328          0         0        0
    #     LAMBEY (Lumen border)     432   93,096          0         0        0
    #     BKF<->DEL (LONGS)         864  372,816          0         0        0
    #     TULORO                    864  372,816          0         0        0
    #     MILTOP                  1,146  656,085          0         0        0
    #     Romero->Tucu              864  372,816          4         1        0
    #     EMVSUI0 Long Shots      1,152  662,976         32         1        0
    #     $ RDR4RDR5 (boss tray)     18      153          0         0        0
    #     retruetest                 12       66          0         0        0
    #
    # NOT ONE verdict moves anywhere in that corpus, and the reason is not
    # that the block never ran.  It ran on both eligible pairs, measured
    # both, and DECLINED both because their fingerprints read at the folder
    # null - the measurement CORROBORATED the event gate rather than
    # refuting it:
    #
    #     ROMTUC303/436   speckle r -0.0461   bar 0.2130   declined
    #     EMVSUI016/160   speckle r +0.0129   bar 0.1210   declined
    #
    # The other 34 event-capped pairs never became eligible: 30 sit below
    # LEN_CAP already (capping them changes no verdict) and 4 fail the count
    # leg, which is not refutable.  EMVSUI's four confirmed duplicates are
    # not event-capped at all, so they are untouched, and the twin gate's
    # own 2 refutations there are unchanged.
    #
    # WHAT COUNTS AS A POSITIVE MEASUREMENT.  All five must hold, and any
    # unmeasurable input leaves the cap in place rather than lifting it:
    #   1. the two files share a wavelength, so their patterns are
    #      comparable at all (_spk_null_for_pair);
    #   2. the folder's confirm bar is itself reachable - bar <=
    #      _SPECKLE_BAR_MAX, i.e. this run is not one whose own competence
    #      line reads NOT MEASURED.  Without this the refutation could fire
    #      on a statistic the same log declares unusable;
    #   3. the pair clears the folder confirm bar,
    #      _SPECKLE_CONFIRM_NULL_MULT x the null p99; and
    #   4. the pair clears its own same-fiber floor, so it reads at or above
    #      the lowest value the same-fiber hypothesis predicts at its σ -
    #      a pair reading BELOW that is too far apart to be one fiber and is
    #      not allowed to refute anything.
    #
    # NOT imported from the speckle VETO gate: its `r_floor < null -> abstain`
    # test.  That test is direction-specific and belongs where it is.  A floor
    # under the null means a same-fiber pair COULD read as low as chance here,
    # so a LOW reading proves nothing - which is exactly why the veto must
    # abstain.  It says nothing against a HIGH reading, and importing it here
    # would abstain on the very pairs this block exists for: on Romero->Tucu
    # the band amplitude is 0.0013 dB, so a σ 0.0145 pair has a floor of
    # 0.0157 against a null p99 of 0.0794 - the test would have silently
    # discarded a pair measuring 0.9216, eight times the folder's confirm bar
    # and eight times its known-different MAXIMUM.  Competence in the confirm
    # direction is what conditions 2 and 3 measure: whether a reading that
    # high can be produced by chance in this folder at all.
    #
    # Conditions 2 and 3 are what keep this off the short-span classes by
    # construction: a 5/10 ns tray or panel folder puts the bar above 1.0,
    # which no Pearson r can reach, so no pair there is ever refuted.
    # Literal copies are unaffected either way - the raw-identity
    # short-circuit already overrides every physical filter.
    n_ev_refuted = 0
    ev_loss_only = []
    for i, p in enumerate(pairs):
        if not events_violation[i] or p_dup_raw[i] <= LEN_CAP:
            continue          # capping this pair changes no verdict
        if p.get('events_unverifiable'):
            continue          # event-poor: never refutable, see above
        # Re-ask the calibrated function with the loss cut lifted.  True
        # means the loss magnitude was the ONLY objection.
        if _events_agree(p['events_n_match'], p['events_n_max'],
                         p['events_n_min'], p['events_mean_dloss_db'],
                         loss_thresh_db=float('inf'),
                         median_dloss_db=p['events_median_dloss_db'],
                         n_max_significant=p['events_n_max_significant']):
            ev_loss_only.append(i)
    for i in ev_loss_only:
        p = pairs[i]
        nq, cmpb = _spk_null_for_pair(p['a'], p['b'])
        if nq is None or not cmpb:
            continue
        bar = nq * _SPECKLE_CONFIRM_NULL_MULT
        if bar > _SPECKLE_BAR_MAX:
            continue          # bar out of reach: the statistic is not usable here
        ra, rb = _spk_win(p['a']), _spk_win(p['b'])
        r_pair = _speckle_pair_r(ra, rb)
        r_floor = _speckle_same_fiber_floor(ra, rb, p['score'])
        if r_pair is None or r_floor is None:
            continue          # unmeasurable pair: keep the cap
        # Recorded BEFORE the decision, so a pair that was measured and
        # declined is distinguishable in the internals from one that was
        # never measured.  A silent decline and a silent pass look the same
        # on a sheet, and the declines are the ones worth being able to
        # check: measured on Romero->Tucu (864 files), the one eligible
        # pair read -0.0461 against a 0.2130 bar - the fingerprint
        # CORROBORATED the event gate there rather than refuting it.
        p['events_speckle_r'] = round(r_pair, 4)
        p['events_speckle_floor'] = round(r_floor, 4)
        p['events_speckle_bar'] = round(bar, 4)
        if r_pair < bar or r_pair < r_floor:
            continue
        events_violation[i] = False
        p['events_refuted_by_speckle'] = True
        n_ev_refuted += 1
    # Logged whenever any pair was eligible, so an abstention (eligible but
    # 0 refuted) is visible in the run log and not silence.
    if ev_loss_only:
        print(f'Event gate: {n_ev_refuted} of {len(ev_loss_only)} '
              f'loss-magnitude objection(s) refuted by the fingerprint')

    # Uniqueness (twin) gate — ALL regimes (2026-07-31; was production-only,
    # which took it off the board on exactly the misroutes it guards
    # against).  For each pair that would flag, ask whether the two files
    # are each other's UNIQUE twin: pair σ must be ≤ _UNIQ_TWIN_RATIO x the
    # smaller of the two members' next-best σ against anyone else.
    # Family/ladder members (several equally-close partners) fail; a
    # genuine re-shoot or copy passes even on a pristine featureless span
    # (its twin is still several x closer than the field).  σ is the RAW
    # pair σ in every regime, so the comparison means the same thing
    # everywhere.  Verdict-level guard only — flag or don't, no review
    # tier; and the raw-identity short-circuit still overrides it, so a
    # literal copy can never be capped by this.
    uniq_violation = np.zeros(len(pairs), dtype=bool)
    Ksz = sigma_matrix.shape[0]
    sig_self_inf = sigma_matrix + np.diag(np.full(Ksz, np.inf))
    sig_sorted = np.sort(sig_self_inf, axis=1)
    best1, best2 = sig_sorted[:, 0], sig_sorted[:, 1]
    arg_sorted = np.argsort(sig_self_inf, axis=1)
    arg1, arg2 = arg_sorted[:, 0], arg_sorted[:, 1]
    uniq_rival = {}                       # pair index -> (row, rival row)
    pidx = 0
    for ki in range(Ksz):
        for kj in range(ki + 1, Ksz):
            if p_dup_raw[pidx] > 0.5:
                s = float(sigma_matrix[ki, kj])
                take_i = s <= best1[ki]
                take_j = s <= best1[kj]
                nb_i = float(best2[ki] if take_i else best1[ki])
                nb_j = float(best2[kj] if take_j else best1[kj])
                if s > _UNIQ_TWIN_RATIO * min(nb_i, nb_j):
                    uniq_violation[pidx] = True
                    pairs[pidx]['uniq_next_best_db'] = round(min(nb_i, nb_j), 4)
                    # Remember the file that raised the objection so the
                    # fingerprint can be asked about that specific rival.
                    if nb_i <= nb_j:
                        owner = ki
                        rival = int(arg2[ki] if take_i else arg1[ki])
                    else:
                        owner = kj
                        rival = int(arg2[kj] if take_j else arg1[kj])
                    uniq_rival[pidx] = (owner, rival)
            pidx += 1

    # Different-OTDR gate: duplication (a copied file, or the same fiber
    # re-shot and presented as another) is a SINGLE-instrument phenomenon.
    # A pair acquired by two different physical OTDRs is two independent
    # acquisitions — it can only be "the same data" at raw-identity grade,
    # and the raw-identity short-circuit below fires regardless of this
    # cap.  (Lumen Border LAMBEY170/241: serials 1876272 vs 1978245, 84
    # min apart, 3.4 dB injection delta, r 0.992 on a pristine span —
    # and the SAME two fibers from the OPPOSITE end read σ 0.05, five
    # times the flag level.  Different boxes -> different fibers.)
    # Fails open when either serial is missing.
    name_to_serial = {f['name']: f.get('serial_number') for f in files}
    serial_violation = np.zeros(len(pairs), dtype=bool)
    for i, p in enumerate(pairs):
        if p_dup_raw[i] <= 0.5:
            continue
        sa, sb_ = name_to_serial.get(p['a']), name_to_serial.get(p['b'])
        if sa and sb_ and sa != sb_ and not p.get('raw_identical'):
            serial_violation[i] = True
            p['serial_mismatch'] = f'{sa} != {sb_}'

    # ── Twin-gate refutation by fingerprint ───────────────────────────────
    # The twin gate asks "is this pair's partner UNIQUE?" and answers it
    # with a sigma ratio, which is a proxy.  The Rayleigh speckle answers
    # the same question by direct measurement, so where the two disagree
    # the measurement decides.  A pair is restored only when BOTH hold:
    # the pair itself fingerprints far above what different fibers produce
    # in this folder, AND the specific rival that raised the objection
    # fingerprints at the null, i.e. is demonstrably NOT a second twin.
    #
    # Measured on EMVSUI0 Long Shots (folder null p99 = 0.086):
    #   563/564 sigma 0.0182  fingerprint 0.6150   <- confirmed duplicate
    #     rival 564/566 sigma 0.0331  fingerprint 0.0170  <- different fiber
    #   296/308 sigma 0.0246  fingerprint 0.4780   <- confirmed duplicate
    #     rival 308/336 sigma 0.0350  fingerprint 0.0739  <- different fiber
    # Both were capped at 0.5 because their twin was "only" 1.8x and 1.4x
    # closer than a rival that shares no fingerprint with them at all.
    #
    # This CANNOT re-open a ribbon-ladder false positive (the case the twin
    # gate was built for): a ladder pair fails the first condition — it has
    # no shared fingerprint either — so it never reaches the rival test.
    # Applies only to pairs whose ONLY objection is the twin gate; length,
    # events and serial violations are untouched.
    n_uniq_refuted = 0
    twin_only = [i for i in range(len(pairs))
                 if uniq_violation[i] and not (length_violation[i]
                                               or events_violation[i]
                                               or serial_violation[i])]
    if twin_only:
        for i in twin_only:
            p = pairs[i]
            # Per-wavelength null; a cross-wavelength pair is not
            # comparable and must not refute anything.
            nq, cmpb = _spk_null_for_pair(p['a'], p['b'])
            if nq is None or not cmpb:
                continue
            bar = nq * _SPECKLE_CONFIRM_NULL_MULT
            r_pair = _speckle_pair_r(_spk_win(p['a']), _spk_win(p['b']))
            if r_pair is None or r_pair < bar:
                continue
            owner, rival = uniq_rival.get(i, (None, None))
            if owner is None:
                continue
            o_name = files[valid_idx[owner]]['name']
            v_name = files[valid_idx[rival]]['name']
            # The rival comparison needs the same footing, or a
            # cross-wavelength rival reads ~0 and refutes for free.
            _, rcmp = _spk_null_for_pair(o_name, v_name)
            if not rcmp:
                continue
            r_rival = _speckle_pair_r(_spk_win(o_name), _spk_win(v_name))
            if r_rival is None or r_rival >= nq:
                continue
            uniq_violation[i] = False
            p['uniq_refuted_by_speckle'] = True
            p['uniq_rival_speckle_r'] = round(r_rival, 4)
            n_uniq_refuted += 1
    if n_uniq_refuted:
        print(f'Twin gate: {n_uniq_refuted} of {len(twin_only)} sigma-ratio '
              f'objection(s) refuted by the fingerprint')

    physical_violation = (length_violation | events_violation
                          | uniq_violation | serial_violation)
    p_dup = np.where(physical_violation, np.minimum(p_dup_raw, LEN_CAP), p_dup_raw)

    # Rayleigh-speckle confirmation gate (see the _SPECKLE_* calibration
    # block).  LAST gate before the raw-identity short-circuit: a pair still
    # standing above the print threshold is demoted when its sub-pulse-width
    # backscatter fingerprint sits far below anything the same-fiber
    # hypothesis could produce at that pair's own σ — and only when the
    # statistic has been shown to separate same from different on THIS
    # folder at THAT σ.  Otherwise the gate abstains.  Only the survivors
    # are measured, so the cost is O(files in candidate pairs) + the one
    # folder-null sample, not O(pairs).
    speckle_violation = np.zeros(len(pairs), dtype=bool)
    cand = [i for i in range(len(pairs)) if p_dup[i] > 0.5]
    n_unmeas = n_abstain = 0
    null_q = None
    if cand:
        # Same windows and same folder null the twin-gate refutation used;
        # built once, on first demand, either here or up there.
        null_q = _spk_null(None)     # pooled value, for the run-log line only
        for i in cand:
            p = pairs[i]
            # Each pair is judged against its OWN wavelength's null.  A
            # cross-wavelength pair is unmeasurable by physics — the two
            # speckle patterns are unrelated — so it is KEPT, never vetoed.
            p_nq, p_cmp = _spk_null_for_pair(p['a'], p['b'])
            ra, rb = _spk_win(p['a']), _spk_win(p['b'])
            r_hp = _speckle_pair_r(ra, rb)
            r_floor = _speckle_same_fiber_floor(ra, rb, p['score'])
            if r_hp is None or r_floor is None or p_nq is None or not p_cmp:
                p['speckle_unmeasurable'] = True     # fail-safe: no veto
                n_unmeas += 1
                continue
            p['speckle_r'] = round(r_hp, 4)
            p['speckle_floor'] = round(r_floor, 4)
            if r_floor < p_nq:
                # At this pair's σ the statistic cannot separate a
                # same-fiber re-shoot from two random fibers here.  Abstain.
                p['speckle_abstain'] = True
                n_abstain += 1
                continue
            if r_hp <= r_floor / _SPECKLE_FLOOR_MARGIN:
                speckle_violation[i] = True
                p['speckle_capped'] = True
        p_dup = np.where(speckle_violation, np.minimum(p_dup, LEN_CAP), p_dup)
    # Always logged (even at 0 candidates) so the gate is auditable from any
    # run log — silence would be indistinguishable from the gate not running.
    print(f'Speckle gate: {len(cand)} candidate pair(s), '
          f'{int(speckle_violation.sum())} demoted, '
          f'{n_abstain} inconclusive (kept), '
          f'{n_unmeas} unmeasurable (kept)'
          + (f', folder null p{_SPECKLE_NULL_PCT:.0f}={null_q:.3f}'
             if null_q is not None else ''))
    # ── Competence, said out loud ─────────────────────────────────────────
    # Everything above can read as a clean, confident zero on a folder where
    # the gate could not have fired whatever the traces held.  Two ways that
    # happens, and neither was visible before:
    #
    #   1. No null.  Windows shorter than _SPECKLE_MIN_SAMPLES make every
    #      file unmeasurable, or the folder has too few files to reach
    #      _SPECKLE_NULL_MIN_PAIRS, so the statistic is never computed.
    #   2. A null that exists but puts the confirm bar out of reach — see
    #      _SPECKLE_BAR_MAX.  A bar above 1.0 cannot be cleared by any
    #      Pearson r, so no pair can ever be confirmed against it.
    #
    # This prints; it does not decide.  No verdict on any folder moves.
    _spk_bar = None if null_q is None else null_q * _SPECKLE_CONFIRM_NULL_MULT
    competence = None
    # The physics verdict comes first and is computed on EVERY folder, from
    # the folder's own noise and its own spread, with no sample floor: so a
    # folder where no candidate ever reached the gate (Dinwiddie: the r-ramp
    # produced the zero, the null was never requested) is judged on what its
    # traces can carry, not on which code path happened to run.
    competence_detail = _speckle_competence(files, interior_start, interior_end,
                                            hp_width=_hp_w, span_m=min_L,
                                            band_start_m=_band_start)
    if competence_detail is not None:
        _cd = competence_detail
        print(f"Speckle competence: {_cd['status']} - ratio {_cd['ratio']:.2f} "
              f"(predicted same-fibre r {_cd['pred_same_r']:.3f} vs bar "
              f"{_cd['bar']:.3f}; sigma_band {_cd['sigma_band_db']:.4f} dB, "
              f"fingerprint term {_cd['fingerprint_term']:.4f}, diagnostic null "
              f"p50 {_cd['null_p50']:+.3f} p99 {_cd['null_p99']:+.3f} over "
              f"{_cd['n_null_pairs']} pairs"
              + (f", pulse {_cd['pulse_ns']:.0f} ns" if _cd['pulse_ns'] else '')
              + f", span {_cd['span_m']:.0f} m)")
        if _cd['status'] != 'OK':
            print(f"Speckle competence: {_cd['message']}")
            if _cd['what_it_takes']:
                print(f"Speckle competence: {_cd['what_it_takes']}")
            competence = _cd['message']
    if null_q is None:
        _n_int, _n_win = _speckle_window_census(files, interior_start,
                                                interior_end, _hp_w,
                                                band_start_m=_band_start)
        print(f'Speckle competence: UNMEASURABLE — no folder null. '
              f'{len(files)} file(s), interior {_n_int} sample(s), '
              f'smallest window {_n_win} vs floor {_SPECKLE_MIN_SAMPLES}, '
              f'null needs {_SPECKLE_NULL_MIN_PAIRS} pair(s). '
              f'Zero flags here means NOT MEASURED, not "no duplicates".')
        if competence is None and competence_detail is None:
            competence = (f'NOT MEASURED - no folder null. Interior {_n_int} '
                          f'sample(s), smallest window {_n_win} vs floor '
                          f'{_SPECKLE_MIN_SAMPLES}. A zero here means the '
                          f'detector could not run, not "no duplicates".')
    elif _spk_bar > _SPECKLE_BAR_MAX:
        print(f'Speckle competence: UNMEASURABLE — confirm bar '
              f'{_spk_bar:.3f} = {_SPECKLE_CONFIRM_NULL_MULT:g} x null '
              f'p{_SPECKLE_NULL_PCT:.0f} {null_q:.3f}, above the '
              f'{_SPECKLE_BAR_MAX:.2f} ceiling'
              + (' and above 1.0, which no Pearson r can reach'
                 if _spk_bar > 1.0 else '')
              + '. Zero confirmations here means NOT MEASURED.')
        if competence is None:
            competence = (f'NOT MEASURED - confirm bar {_spk_bar:.3f} exceeds the '
                          f'{_SPECKLE_BAR_MAX:.2f} ceiling'
                          + (' and 1.0, which no Pearson r can reach'
                             if _spk_bar > 1.0 else '')
                          + '. A zero here means the detector could not run.')
    else:
        print(f'Speckle competence: OK — confirm bar {_spk_bar:.3f}, '
              f'null p{_SPECKLE_NULL_PCT:.0f} {null_q:.3f}')

    # ── Mating likelihood + confidence band (display only) ─────────────────
    confidence = _confidence_band(competence_detail)
    mating = _mating_likelihood(files, pairs)
    # Event-table ranking: only speaks when the fingerprint could not.
    event_fallback = _event_table_fallback(files, pairs, competence_detail)
    if event_fallback is not None:
        print(f"Event-table ranking ({event_fallback['status']}): "
              f"{event_fallback['n_compared']:,} comparable pairs, "
              f"{event_fallback['n_within']} within the gate"
              + (f", reflectance margin {event_fallback['margin']:.1f}x"
                 if event_fallback.get('margin') else ''))
    print(f"Detector confidence: {confidence['band']}"
          + (f" (ratio {confidence['ratio']:.2f})" if confidence['ratio'] is not None else ''))
    if mating:
        print(f"Mating likelihood: {mating['n_pairs']} pairs, features "
              f"{','.join(mating['features'])}, top ratio {mating['top_lr']:.0f}x "
              f"(p {mating['top_p']*100:.1f}% at the stated prior), "
              f"{mating['n_lr_ge_100']} pair(s) >= 100x, {mating['n_lr_ge_10']} >= 10x")
    # Raw-identity short-circuit: a pair whose RAW interior trace is the
    # same data (σ ≤ 0.001 dB, r ≥ 0.98 — see the calibration block above)
    # is a CONFIRMED copy regardless of regime routing.  Applied last so no
    # regime bypass (tie_panel fingerprint subtraction, all_dups σ bypass),
    # stored-event-table disagreement, or gate above it (twin, speckle) can
    # hide a literal file copy: the trace itself is the identity proof.
    # Raises to 1.0 only — never lowers.  (Byte-identical copies also pass
    # the speckle gate trivially: identical traces give r_hp = 1.0.)
    raw_ident_mask = np.array([bool(p.get('raw_identical')) for p in pairs],
                              dtype=bool)
    if raw_ident_mask.any():
        p_dup = np.where(raw_ident_mask, 1.0, p_dup)

    # ── Near splice + shot out of order (display only) ───────────────────
    # Neither touches p_dup.  The splice is evidence AGAINST a pair and cannot
    # confirm one, and a fill-in says where to look, not what was found.
    near_splice = _near_splice(files, pairs)
    print(near_splice['note'])
    fill_ins = _fill_ins(files)
    if fill_ins:
        print('Shot out of order: %d fibre(s) in %d run(s) shot more than %.0f min '
              'after both neighbouring fibres.'
              % (sum(len(r['names']) for r in fill_ins), len(fill_ins),
                 _FILL_IN_FAR_S / 60.0))

    for i, p in enumerate(pairs):
        p['p_dup_sigma']   = float(p_dup_sigma[i])
        p['p_dup_r']       = float(p_dup_r[i])
        p['p_dup_raw']     = float(p_dup_raw[i])
        p['p_dup']         = float(p_dup[i])
        p['length_capped'] = bool(length_violation[i])
        p['events_capped'] = bool(events_violation[i])
        p['z']             = float(stats['z'][i])

    order = np.argsort(scores)
    n99 = int((p_dup > 0.99).sum())
    n50 = int((p_dup > 0.5).sum())
    n10 = int((p_dup > 0.1).sum())
    print(f'Likelihood >99%: {n99}   >50%: {n50}   >10%: {n10}')

    # For each file, pick the partner that gives the HIGHEST duplicate
    # likelihood (tie-broken by smallest disagreement). This ensures the
    # per-file table is symmetric: if pair (A,B) is the most-likely
    # duplicate for both A and B, both rows point at each other. Earlier
    # logic picked by smallest σ alone, which could leave a confirmed-
    # duplicate flag on one row while the partner's row pointed elsewhere.
    best_partner = _best_partners(files, pairs)

    return {
        'files': files,
        'pairs': pairs,
        'scores': scores,
        'stats': stats,
        'p_dup': p_dup,
        'best_partner': best_partner,
        'n99': n99, 'n50': n50, 'n10': n10,
        'interior_start': interior_start, 'interior_end': interior_end,
        'min_L': min_L,
        'short_traces': short_traces,
        'window_guard': window_guard,
        'order_by_score': order,
        'regime': regime,
        'regime_reason': regime_reason,
        'regime_margin': regime_margin,
        'bulk_sigma': bulk_sigma,
        'bulk_r': bulk_r,
        'frac_high_r': frac_high_r,
        'competence': competence,
        'competence_detail': competence_detail,
        'confidence': confidence,
        'mating': mating,
        'event_fallback': event_fallback,
        'near_splice': near_splice,
        'fill_ins': fill_ins,
    }


def build_report_sor(folder, title, out_pdf, meta=None):
    analysis = _analyze_sor(folder)
    if meta is not None:
        # Additive side-channel for the runner's manifest (`short_traces`);
        # optional so every existing caller is untouched.
        meta['short_traces'] = analysis.get('short_traces') or []
        if analysis.get('window_guard'):
            meta['window_guard'] = analysis['window_guard']
        _cd = analysis.get('competence_detail')
        if _cd and _cd.get('status') != 'OK':
            meta['competence'] = _cd
        if analysis.get('confidence'):
            meta['confidence'] = analysis['confidence']
        _efb_m = analysis.get('event_fallback')
        if _efb_m:
            meta['event_fallback'] = {
                'status': _efb_m['status'],
                'n_compared': _efb_m['n_compared'],
                'n_within': _efb_m['n_within'],
                'margin': _efb_m.get('margin'),
                'rows': _efb_m['rows'][:_EVT_FB_PDF_ROWS],
            }
        _mt = _mating_top(analysis)
        if _mt:
            meta['mating_top'] = _mt
        # The counts the REPORT prints, so the caller can stop recomputing
        # its own (see run_sor_bytes).
        meta['n_files'] = len(analysis['files'])
        meta['n_pairs'] = len(analysis['pairs'])
    files = analysis['files']
    pairs = analysis['pairs']
    scores = analysis['scores']
    stats = analysis['stats']
    p_dup = analysis['p_dup']
    best_partner = analysis['best_partner']
    n99, n50, n10 = analysis['n99'], analysis['n50'], analysis['n10']
    order = analysis['order_by_score']

    verdict_block = (f'<div class="verdict-box verdict-confirm">'
                     f'<b>{n50} duplicate pair(s) identified</b> at ≥50% likelihood; '
                     f'{n99} at ≥99% likelihood across {len(pairs)} pairs.</div>'
                     if n50 else
                     '<div class="verdict-box verdict-dispute">'
                     '<b>No duplicate pairs identified</b> at ≥50% likelihood.</div>')

    shape_rs = [p.get('shape_r') for p in pairs]
    dist_chart = _distribution_chart(scores, p_dup, stats, shape_rs=shape_rs)

    # '' when the folder has no suspected breaks — unaffected reports stay
    # byte-stable (no empty section, no renumbering).
    short_block = _short_trace_section_html(
        analysis.get('short_traces'),
        window_guard=analysis.get('window_guard'))
    competence_block = _competence_section_html(analysis.get('competence_detail'))
    event_fb = analysis.get('event_fallback')
    event_fb_block = _event_fallback_section_html(event_fb)

    file_by_name = {f['name']: f for f in files}

    def _gap_str(name_a, name_b):
        _fa, _fb = file_by_name.get(name_a), file_by_name.get(name_b)
        _ta = _fa.get('timestamp') if _fa else None
        _tb = _fb.get('timestamp') if _fb else None
        return _fmt_time_gap(abs(_ta - _tb)) if _ta and _tb else '—'

    file_rows = ''
    for f in sorted(files, key=lambda x: x['name']):
        bp = best_partner.get(f['name'])
        if bp is None:
            continue
        partner = bp['b'] if bp['a'] == f['name'] else bp['a']
        pd_val = bp['p_dup']
        pd_color = '#2d8f48' if pd_val > 0.9 else ('#b97000' if pd_val > 0.1 else '#888')
        verdict_cell = (f'<span class="dup">DUPLICATE of {partner}</span>'
                        if pd_val > 0.5 else
                        f'<span class="na">unique (closest: {partner})</span>')
        loss_cell = f'{f["loss"]:.3f}' if f['loss'] is not None else '—'
        r_val = bp.get('shape_r')
        r_cell = ('<td class="center na">—</td>' if r_val is None else
                  f'<td class="center" style="color:{_shape_color(r_val)};font-weight:600">{r_val:.4f}</td>')
        file_rows += (f'<tr><td class="pair-cell">{f["name"]}</td>'
                      f'<td class="center">{f["length"]/1000:.3f}</td>'
                      f'<td class="center">{_gap_str(f["name"], partner)}</td>'
                      f'<td class="center">{loss_cell}</td>'
                      f'<td class="center">{bp["score"]:.4f}</td>'
                      f'<td class="center" style="color:{pd_color};font-weight:600">{pd_val*100:.2f}%</td>'
                      f'{r_cell}'
                      f'<td class="center">{verdict_cell}</td></tr>')

    top_rows = ''
    for rank, k in enumerate(order[:30], 1):
        p = pairs[k]
        pd_val = p['p_dup']
        pd_color = '#2d8f48' if pd_val > 0.9 else ('#b97000' if pd_val > 0.1 else '#888')
        r_val = p.get('shape_r')
        r_cell = ('<td class="center na">—</td>' if r_val is None else
                  f'<td class="center" style="color:{_shape_color(r_val)};font-weight:600">{r_val:.4f}</td>')
        top_rows += (f'<tr><td class="center">{rank}</td>'
                     f'<td class="pair-cell">{p["a"]} ↔ {p["b"]}</td>'
                     f'<td class="center">{_gap_str(p["a"], p["b"])}</td>'
                     f'<td class="center">{p["score"]:.4f}</td>'
                     f'<td class="center" style="color:{pd_color};font-weight:600">{pd_val*100:.2f}%</td>'
                     f'{r_cell}</tr>')

    # Top 30 by similarity (highest first). Skip pairs where similarity is None.
    sim_pairs = [(i, p) for i, p in enumerate(pairs) if p.get('shape_r') is not None]
    sim_order = sorted(sim_pairs, key=lambda x: -x[1]['shape_r'])[:30]
    sim_rows = ''
    for rank, (k, p) in enumerate(sim_order, 1):
        pd_val = p['p_dup']
        pd_color = '#2d8f48' if pd_val > 0.9 else ('#b97000' if pd_val > 0.1 else '#888')
        r_val = p['shape_r']
        sim_rows += (f'<tr><td class="center">{rank}</td>'
                     f'<td class="pair-cell">{p["a"]} ↔ {p["b"]}</td>'
                     f'<td class="center">{_gap_str(p["a"], p["b"])}</td>'
                     f'<td class="center" style="color:{_shape_color(r_val)};font-weight:600">{r_val:.4f}</td>'
                     f'<td class="center">{p["score"]:.4f}</td>'
                     f'<td class="center" style="color:{pd_color};font-weight:600">{pd_val*100:.2f}%</td></tr>')

    # Mating likelihood table (top 20 by likelihood ratio) + confidence line
    _conf = analysis.get('confidence') or {}
    mating_rows = ''
    if analysis.get('mating'):
        m_order = sorted([p for p in pairs if p.get('mating_lr') is not None],
                         key=lambda q: -q['mating_lr'])[:20]
        def _mdb(p, k):
            v = p.get(k)
            return '' if v is None else f'{v * 1000:.0f}'

        def _mdbr(p, k):
            v = p.get(k)
            return '' if v is None else f'{v:.2f}'

        for rank, p in enumerate(m_order, 1):
            mating_rows += (f'<tr><td class="center">{rank}</td>'
                            f'<td class="pair-cell">{p["a"]} ↔ {p["b"]}</td>'
                            f'<td class="center">{_gap_str(p["a"], p["b"])}</td>'
                            f'<td class="center" style="font-weight:600">{p["mating_p"] * 100:.1f}%</td>'
                            f'<td class="center">{p["mating_lr"]:.0f}x</td>'
                            f'<td class="center">{_mdb(p, "mating_dl0")}</td>'
                            f'<td class="center">{_mdb(p, "mating_dl1")}</td>'
                            f'<td class="center">{_mdbr(p, "mating_dr1")}</td>'
                            f'<td class="center">{p["p_dup"] * 100:.1f}%</td></tr>')
    from html import escape as _esc_c
    confidence_block = (f'<div class="verdict-box"><b>{_esc_c(_conf.get("note", ""))}</b></div>'
                        if _conf.get('note') else '')
    mating_block = ('' if not mating_rows else f'''
<div class="section-block">
<div class="dir-banner">6. Mating likelihood — top 20 (a ranking, not a verdict)</div>
<p style="font-size:11px;margin:4px 0 6px 0">Pairs ranked by how alike their connector matings are:
launch and panel-port connector loss and reflectance, end reflectance. Percentage assumes
{_esc_c(analysis["mating"]["prior_note"])}. Check the top pairs against the port log.</p>
<table class="vote-table">
<tr><th>Rank</th><th style="text-align:left">Pair</th><th>Time gap</th><th>Mating likelihood</th>
    <th>Likelihood ratio</th><th>Δ launch loss (mdB)</th><th>Δ first-conn loss (mdB)</th>
    <th>Δ first-conn refl (dB)</th><th>Duplicate likelihood</th></tr>
{mating_rows}
</table>
</div>''')

    # Confirmed-duplicate detail table (p_dup > 0.5)
    dup_pairs_sorted = sorted([p for p in pairs if p['p_dup'] > 0.5],
                              key=lambda q: -q['p_dup'])
    # PDF cap (Zach 2026-07-21): an all_dups folder produced 62,014 pairs
    # >=50% — the unbounded table blew Chrome's print budget and crashed the
    # run.  The PDF renders the top PDF_DUP_ROWS_CAP by likelihood with an
    # overflow note; the Excel report always carries the complete list.
    from report import _capped_rows, PDF_DUP_ROWS_CAP
    dup_pairs_render, dup_overflow = _capped_rows(dup_pairs_sorted,
                                                  PDF_DUP_ROWS_CAP)
    dup_detail_rows = ''
    for p in dup_pairs_render:
        fa = file_by_name.get(p['a']); fb = file_by_name.get(p['b'])
        if fa is None or fb is None:
            continue
        ta, tb = fa.get('timestamp'), fb.get('timestamp')
        gap_str = _fmt_time_gap(abs(ta - tb)) if ta and tb else '—'
        a_sl, b_sl = fa.get('loss'), fb.get('loss')
        # Max splice Δ at MATCHED events (For-Romeo style): for each splice
        # closure that exists in both fibers, |Δloss|, then max across closures.
        # Falls back to '—' when no events were matched.
        max_dloss = p.get('events_max_dloss_db')
        n_match_pair = p.get('events_n_match', 0)
        ms_cell = (f'<td class="center">{max_dloss*1000:.0f}</td>'
                   if max_dloss is not None and n_match_pair >= 1
                   else '<td class="center na">—</td>')
        sl_cell = (f'<td class="center">{abs(a_sl - b_sl)*1000:.0f}</td>'
                   if a_sl is not None and b_sl is not None
                   else '<td class="center na">—</td>')
        # Same OTDR serial → both shots came from the same instrument.
        sn_a, sn_b = fa.get('serial_number'), fb.get('serial_number')
        if sn_a and sn_b:
            same_sn = (sn_a == sn_b)
            sn_cell = (f'<td class="center" style="color:#2d8f48;font-weight:700">Yes</td>'
                       if same_sn else
                       f'<td class="center" style="color:#c0392b;font-weight:700">No</td>')
        else:
            sn_cell = '<td class="center na">—</td>'
        pd_val = p['p_dup']
        pd_color = '#2d8f48' if pd_val > 0.9 else '#b97000'
        r_val = p.get('shape_r')
        r_cell = ('<td class="center na">—</td>' if r_val is None else
                  f'<td class="center" style="color:{_shape_color(r_val)};font-weight:600">{r_val:.4f}</td>')
        dup_detail_rows += (f'<tr><td class="pair-cell">{p["a"]} ↔ {p["b"]}</td>'
                            f'<td class="center">{gap_str}</td>'
                            f'{ms_cell}{sl_cell}{r_cell}{sn_cell}'
                            f'<td class="center" style="color:{pd_color};font-weight:600">{pd_val*100:.2f}%</td></tr>')
    # Boss request (2026-07-15): duplicates lead the report — this block is
    # section 1 on page one, with an explicit "none" line when the folder is
    # clean so the verdict is visible at a glance.
    if dup_detail_rows:
        wl_hdr = f'{int(files[0].get("wavelength") or 0)} nm' if files else ''
        dup_detail_block = f'''
<div class="section-block">
<div class="dir-banner">1. Confirmed duplicate pairs (≥50% likelihood) — detail ({wl_hdr})</div>
<table class="vote-table">
<tr><th style="text-align:left">Pair</th><th>Time gap</th>
  <th>max splice Δ (mdB)</th><th>span loss Δ (mdB)</th>
  <th>similarity</th><th>Same OTDR</th><th>Duplicate likelihood</th></tr>
{dup_detail_rows}
</table>
{('<div style="padding:8px 4px;color:#b97000;font-weight:600">… and '
  f'{dup_overflow:,} more pairs at ≥50% likelihood — the complete list is '
  'in the Excel report.</div>') if dup_overflow else ''}
</div>
'''
    else:
        # A zero the detector could not measure is not a clean folder.
        # Green says "clean" to anyone skimming, so it is spent only when the
        # trace actually answered; otherwise the line names the detector that
        # produced the zero and points at the ranking below.
        _fp_ran = (analysis.get('competence_detail') or {}).get('status') in (None, 'OK')
        if _fp_ran:
            _none_line = ('<div style="padding:10px 4px;color:#2d8f48;font-weight:600">'
                          'None \u2014 no pairs at \u226550% duplicate likelihood.</div>')
        else:
            _n_gate = (event_fb or {}).get('n_within') or 0
            _none_line = (
                '<div style="padding:10px 4px;color:#b97000;font-weight:600">'
                'None at \u226550% likelihood, but the trace fingerprint could not '
                'measure this folder, so that zero is not a clean bill of health.'
                + ((' %d pair(s) sit inside the event-table gate; the full ranking '
                    'is below.') % _n_gate if _n_gate else
                   ' The event table was ranked as well and put nothing inside its gate.')
                + '</div>')
        dup_detail_block = (
            '<div class="section-block">'
            '<div class="dir-banner">1. Confirmed duplicate pairs (\u226550% likelihood)</div>'
            + _none_line + '</div>')

    generated = datetime.now().strftime('%Y-%m-%d %H:%M')
    html = f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{title}</title>
<style>{_BASE_CSS}</style></head><body>
{_embed_logo()}
<h1>{title}</h1>
<div class="subtitle">{len(files)} files &bull; {len(pairs)} pairs &bull; generated {generated}</div>

{verdict_block}

{dup_detail_block}
{event_fb_block}
{competence_block}
{short_block}
<div class="section-block">
<div class="dir-banner">2. Distribution</div>
<img src="data:image/png;base64,{dist_chart}" class="chart-img" />
</div>

<div class="cards">
  <div class="card"><div class="card-label">Files</div><div class="card-value">{len(files)}</div></div>
  <div class="card"><div class="card-label">Pairs</div><div class="card-value">{len(pairs)}</div></div>
  <div class="card"><div class="card-label">Likelihood &gt; 99%</div>
    <div class="card-value good">{n99}</div></div>
  <div class="card"><div class="card-label">Likelihood &gt; 50%</div>
    <div class="card-value">{n50}</div></div>
  <div class="card"><div class="card-label">Likelihood &gt; 10%</div>
    <div class="card-value">{n10}</div></div>
</div>

<div class="section-block">
<div class="dir-banner">3. Per-file verdict</div>
<table class="vote-table">
<tr><th style="text-align:left">File</th>
    <th>Length (km)</th><th>Time gap (closest)</th><th>Span loss (dB)</th>
    <th>lowest disagreement</th><th>Duplicate likelihood</th>
    <th>similarity</th><th>Verdict</th></tr>
{file_rows}
</table>
</div>

<div class="section-block">
<div class="dir-banner">4. Top 30 pairs — lowest level of disagreement</div>
<table class="vote-table">
<tr><th>Rank</th><th style="text-align:left">Pair</th><th>Time gap</th>
    <th>level of disagreement</th><th>Duplicate likelihood</th><th>similarity</th></tr>
{top_rows}
</table>
</div>

<div class="section-block">
<div class="dir-banner">5. Top 30 pairs — highest similarity</div>
<table class="vote-table">
<tr><th>Rank</th><th style="text-align:left">Pair</th><th>Time gap</th>
    <th>similarity</th><th>level of disagreement</th><th>Duplicate likelihood</th></tr>
{sim_rows}
</table>
</div>
{confidence_block}
{mating_block}
</body></html>'''

    pdf_bytes = html_to_pdf_bytes(html, base_url=folder)
    with open(out_pdf, 'wb') as fh:
        fh.write(pdf_bytes)
    print(f'PDF:  {out_pdf}')
    return out_pdf


def run_sor_bytes(folder, title, meta=None):
    """Run SOR mode and return (pdf_bytes, n_files, n_pairs).  Pass a dict
    as `meta` to receive additive analysis facts (currently
    `short_traces`) without changing the return contract."""
    import tempfile
    _meta = meta if meta is not None else {}
    with tempfile.TemporaryDirectory() as td:
        tmp_pdf = os.path.join(td, 'report.pdf')
        build_report_sor(folder, title, tmp_pdf, meta=_meta)
        with open(tmp_pdf, 'rb') as fh:
            pdf_bytes = fh.read()
    # Report what was ANALYSED, not what was globbed.  These used to
    # recount the staged folder after rendering, which disagreed with the
    # report's own header on any span where a trace is excluded:
    #
    #   ELMDALE TO MILER   glob 1152 / 662,976   analysed 1151 / 661,825
    #                      (ELMMIL0231_1550 ends at 22,288 m against a
    #                       69,567 m median — a real break)
    #   DURANC 1-144       glob  144 /  10,296   analysed  141 /   9,870
    #
    # The glob number reached the download-button label and the green
    # "N SOR files processed" line while the workbook's own Summary sheet
    # printed the smaller one.  Same folder, two numbers, no explanation.
    #
    # The glob was also case-sensitive on a path that is not: _inventory
    # matches on a lowercased name, so a file saved as .SOR was inventoried
    # and staged but missed here.  On POSIX that made the count too LOW and
    # the trace was silently dropped from the analysis; on Windows
    # ntpath.normcase lowercases, so the same folder behaved differently in
    # the field than on the dev machine.  Reading the analysis removes the
    # second parser entirely rather than teaching it the same rules.
    n_files = _meta.get('n_files', 0)
    n_pairs = _meta.get('n_pairs', 0)
    return pdf_bytes, n_files, n_pairs


def build_xlsx_sor(folder, title, out_xlsx, meta=None):
    """SOR-mode Excel renderer. Same analysis as build_report_sor, but
    output is an .xlsx workbook with one sheet per table (no rendered
    charts — Excel users typically filter / sort the raw numbers).

    Sheets:
      Summary                — header counts and verdict
      Suspected short fibers — only when suspected breaks exist
      Per-file verdict
      Confirmed duplicates   — pairs at ≥50% likelihood, with detail columns
      Top 30 — lowest disagreement
      Top 30 — highest similarity
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from openpyxl.drawing.image import Image as XlsxImage

    analysis = _analyze_sor(folder)
    if meta is not None:
        meta['short_traces'] = analysis.get('short_traces') or []
        if analysis.get('window_guard'):
            meta['window_guard'] = analysis['window_guard']
        _cd = analysis.get('competence_detail')
        if _cd and _cd.get('status') != 'OK':
            meta['competence'] = _cd
        if analysis.get('confidence'):
            meta['confidence'] = analysis['confidence']
        _efb_m = analysis.get('event_fallback')
        if _efb_m:
            meta['event_fallback'] = {
                'status': _efb_m['status'],
                'n_compared': _efb_m['n_compared'],
                'n_within': _efb_m['n_within'],
                'margin': _efb_m.get('margin'),
                'rows': _efb_m['rows'][:_EVT_FB_PDF_ROWS],
            }
        _mt = _mating_top(analysis)
        if _mt:
            meta['mating_top'] = _mt
        meta['n_files'] = len(analysis['files'])
        meta['n_pairs'] = len(analysis['pairs'])
    files = analysis['files']
    pairs = analysis['pairs']
    best_partner = analysis['best_partner']
    n99, n50, n10 = analysis['n99'], analysis['n50'], analysis['n10']
    order = analysis['order_by_score']

    wb = Workbook()

    # Unified font: Calibri 12 everywhere. Bold variant for headers and
    # labels keeps the same size/family for visual consistency.
    BASE = Font(name='Calibri', size=12)
    BASE_BOLD = Font(name='Calibri', size=12, bold=True)
    TITLE_FONT = Font(name='Calibri', size=14, bold=True)
    HDR_FONT = Font(name='Calibri', size=12, bold=True, color='FFFFFF')
    hdr_fill = PatternFill('solid', fgColor='2C3E50')

    # ---------- Summary ----------
    ws = wb.active
    ws.title = 'Summary'

    ws['A1'] = title
    ws['A1'].font = TITLE_FONT
    ws['A2'] = f'Generated {datetime.now().strftime("%Y-%m-%d %H:%M")}'
    ws['A2'].font = BASE

    rows = [
        ('Files', len(files)),
        ('Pairs', len(pairs)),
        ('Regime', analysis.get('regime', 'production')),
    ]
    # Only present when one of the additive tie_panel routes fired, so
    # reports from unaffected folders keep their exact row layout.
    if analysis.get('regime_reason'):
        rows.append(('Regime reason', analysis['regime_reason']))
    # Competence, in the workbook and not only in the run log.  A tech reads
    # this sheet, not stdout, and `Likelihood >= 99%: 0` on a folder where the
    # detector could not run is indistinguishable from `no duplicates` without
    # it.  Conditional, so folders where the gate DID run keep their exact
    # row layout.
    if analysis.get('competence'):
        rows.append(('Detector competence', analysis['competence']))
    _efb_sum = analysis.get('event_fallback')
    if _efb_sum:
        rows.append((
            'Event-table ranking',
            (f"{_efb_sum['n_compared']:,} comparable pairs ranked on the stored "
             f"event table; {_efb_sum['n_within']} sit inside the gate "
             f"(reflectance within {_efb_sum.get('refl_gate_db', 0):.2f} dB and "
             f"splice loss within "
             f"{(_efb_sum.get('loss_gate_db') or 0) * 1000:.0f} mdB at every "
             "matched event). A ranking to check against the port log, not a "
             "verdict.")
            if _efb_sum.get('n_compared') else
            (_efb_sum.get('note') or 'No ranking from the event table either.')))
    _cdet = analysis.get('competence_detail') or {}
    if _cdet.get('status') != 'OK' and _cdet.get('what_it_takes'):
        rows.append(('What it would take', _cdet['what_it_takes']))
    _conf = analysis.get('confidence') or {}
    if _conf.get('note'):
        rows.append(('Detector confidence', _conf['note']))
    _mat = analysis.get('mating') or {}
    if _mat:
        rows.append(('Mating likelihood prior', _mat['prior_note']))
        for _g in (_mat.get('gates') or {}).get('notes') or []:
            rows.append(('Mating feature not used', _g))
    # Near the all_dups sigma cliff.  Conditional, like every other optional
    # row here, so an ordinary folder keeps its exact layout.
    if analysis.get('regime_margin'):
        rows.append(('Regime margin', analysis['regime_margin']))
    rows += [
        ('Bulk pair-σ (dB)', f'{analysis.get("bulk_sigma", 0.0):.4f}'),
        ('Bulk pair-r',      f'{analysis.get("bulk_r", 0.0):.4f}'),
        ('Frac pairs r≥0.95', f'{analysis.get("frac_high_r", 0.0):.2f}'),
        ('Likelihood ≥ 99%', n99),
        ('Likelihood ≥ 50%', n50),
        ('Likelihood ≥ 10%', n10),
        ('Common span (m)', f'{analysis["min_L"]:.1f}'),
        ('Interior window (m)',
         f'{analysis["interior_start"]:.0f}–{analysis["interior_end"]:.0f}'),
    ]
    # Only when suspected breaks exist — unaffected Summary layouts stay
    # byte-stable (same pattern as the Regime-reason row above).
    short_traces = analysis.get('short_traces') or []
    if short_traces:
        rows.append(('Suspected short fibers', len(short_traces)))
    if analysis.get('window_guard'):
        rows.append(('Window warning', analysis['window_guard']))
    _ns = analysis.get('near_splice') or {}
    if _ns.get('usable'):
        rows.append(('Near splice', _ns['summary']))
    _fi = analysis.get('fill_ins') or []
    if _fi:
        rows.append(('Shot out of order',
                     '%d fibre(s) shot after both neighbouring fibres. See the '
                     'Shot out of order sheet.' % sum(len(r['names']) for r in _fi)))
    for i, (k, v) in enumerate(rows, start=4):
        c1 = ws.cell(row=i, column=1, value=k); c1.font = BASE_BOLD
        c2 = ws.cell(row=i, column=2, value=v); c2.font = BASE
    ws.column_dimensions['A'].width = 22
    ws.column_dimensions['B'].width = 24

    def _write_table(ws, headers, rows_data, col_widths=None):
        for c, h in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=c, value=h)
            cell.fill = hdr_fill
            cell.font = HDR_FONT
            cell.alignment = Alignment(horizontal='center')
        for r, row in enumerate(rows_data, start=2):
            for c, v in enumerate(row, start=1):
                cell = ws.cell(row=r, column=c, value=v)
                cell.font = BASE
        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = (f'A1:{get_column_letter(len(headers))}'
                              f'{1 + len(rows_data)}')
        if col_widths:
            for c, w in enumerate(col_widths, start=1):
                ws.column_dimensions[get_column_letter(c)].width = w

    # ---------- Suspected short fibers (only when present) ----------
    if short_traces:
        ws = wb.create_sheet('Suspected short fibers', 1)   # after Summary
        headers = ['File', 'Ends at (m)', 'Folder median (m)',
                   'Excluded from pairs', 'Finding']
        rows_data = []
        for e in short_traces:
            finding = 'suspected break'
            if e.get('break_note'):
                finding += f' — {e["break_note"]}'
            rows_data.append([e['file'], e['eof_m'], e['median_eof_m'],
                              'Yes' if e.get('excluded') else 'No', finding])
        _write_table(ws, headers, rows_data,
                     col_widths=[18, 13, 17, 18, 72])

    # ---------- Per-file verdict ----------
    ws = wb.create_sheet('Per-file verdict')
    headers = ['File', 'Length (km)', 'Span loss (dB)',
               'Lowest disagreement', 'Duplicate likelihood (%)',
               'Similarity', 'Best partner', 'Verdict']
    rows_data = []
    for f in sorted(files, key=lambda x: x['name']):
        bp = best_partner.get(f['name'])
        if bp is None:
            rows_data.append([f['name'], None, None, None, None, None, None, '—'])
            continue
        partner = bp['b'] if bp['a'] == f['name'] else bp['a']
        verdict = (f'DUPLICATE of {partner}' if bp['p_dup'] > 0.5
                   else f'unique (closest: {partner})')
        rows_data.append([
            f['name'],
            (f['length'] / 1000.0) if f.get('length') else None,
            f.get('loss'),
            bp['score'],
            bp['p_dup'] * 100.0,
            bp.get('shape_r'),
            partner,
            verdict,
        ])
    _write_table(ws, headers, rows_data,
                 col_widths=[18, 12, 14, 18, 22, 12, 20, 32])

    # ---------- Confirmed duplicates (≥50% likelihood) ----------
    ws = wb.create_sheet('Confirmed duplicates')
    headers = ['Pair A', 'Pair B', 'Time gap (s)',
               'Max splice Δ at matched events (mdB)',
               'Span loss Δ (mdB)', 'Similarity', 'Same OTDR',
               'Duplicate likelihood (%)']
    file_by_name = {f['name']: f for f in files}
    dup_sorted = sorted([p for p in pairs if p['p_dup'] > 0.5],
                        key=lambda q: -q['p_dup'])
    rows_data = []
    for p in dup_sorted:
        fa = file_by_name.get(p['a'])
        fb = file_by_name.get(p['b'])
        ta, tb = (fa.get('timestamp') if fa else None,
                  fb.get('timestamp') if fb else None)
        gap = abs(ta - tb) if ta and tb else None
        a_sl = fa.get('loss') if fa else None
        b_sl = fb.get('loss') if fb else None
        sl_d = abs(a_sl - b_sl) * 1000 if a_sl is not None and b_sl is not None else None
        max_d = p.get('events_max_dloss_db')
        ms_d = max_d * 1000 if (max_d is not None and p.get('events_n_match', 0) >= 1) else None
        sn_a = fa.get('serial_number') if fa else None
        sn_b = fb.get('serial_number') if fb else None
        if sn_a and sn_b:
            same_sn = 'Yes' if sn_a == sn_b else 'No'
        else:
            same_sn = '—'
        rows_data.append([
            p['a'], p['b'], gap, ms_d, sl_d,
            p.get('shape_r'), same_sn, p['p_dup'] * 100.0,
        ])
    _write_table(ws, headers, rows_data,
                 col_widths=[18, 18, 13, 32, 18, 12, 11, 22])

    # ---------- Event-table ranking ----------
    # Only when the trace fingerprint could not answer.  Carries the ranking
    # well past what the PDF prints; these are triage rows for a tech to check
    # against the port log, not verdicts, and nothing here moved a likelihood
    # on any other sheet.
    _efb = analysis.get('event_fallback')
    if _efb and _efb.get('rows'):
        ws = wb.create_sheet('Event-table ranking')
        headers = ['Rank', 'Pair A', 'Pair B', 'Events matched',
                   f'Max Δ reflectance (dB) - limit {_EVT_FB_REFL_DB:.2f}',
                   f'Max Δ splice loss (mdB) - limit {_EVT_FB_LOSS_DB * 1000:.0f}',
                   'Max Δ position (m)', 'Time gap (s)',
                   'Review for duplicate']
        rows_data = [[r['rank'], r['a'], r['b'], r['n_events'],
                      round(r['max_drefl_db'], 4),
                      round(r['max_dloss_db'] * 1000, 1),
                      round(r['max_dpos_m'], 3), r['gap_s'],
                      'Yes' if r['within_gate'] else '']
                     for r in _efb['rows'][:_EVT_FB_XLSX_ROWS]]
        _write_table(ws, headers, rows_data,
                     col_widths=[8, 16, 16, 16, 22, 24, 20, 14, 12])

    # ---------- Top 30 — lowest disagreement ----------
    def _gap_s(name_a, name_b):
        _fa, _fb = file_by_name.get(name_a), file_by_name.get(name_b)
        _ta = _fa.get('timestamp') if _fa else None
        _tb = _fb.get('timestamp') if _fb else None
        return abs(_ta - _tb) if _ta and _tb else None

    ws = wb.create_sheet('Top 30 lowest disagreement')
    headers = ['Rank', 'Pair A', 'Pair B', 'Time gap (s)',
               'Level of disagreement',
               'Duplicate likelihood (%)', 'Similarity']
    rows_data = []
    for rank, k in enumerate(order[:30], 1):
        p = pairs[k]
        rows_data.append([
            rank, p['a'], p['b'], _gap_s(p['a'], p['b']), p['score'],
            p['p_dup'] * 100.0, p.get('shape_r'),
        ])
    _write_table(ws, headers, rows_data,
                 col_widths=[6, 18, 18, 13, 22, 22, 12])

    # ---------- Top 30 — highest similarity ----------
    ws = wb.create_sheet('Top 30 highest similarity')
    headers = ['Rank', 'Pair A', 'Pair B', 'Time gap (s)', 'Similarity',
               'Level of disagreement', 'Duplicate likelihood (%)']
    sim_sorted = sorted([(i, p) for i, p in enumerate(pairs)
                         if p.get('shape_r') is not None],
                        key=lambda x: -x[1]['shape_r'])[:30]
    rows_data = []
    for rank, (_, p) in enumerate(sim_sorted, 1):
        rows_data.append([
            rank, p['a'], p['b'], _gap_s(p['a'], p['b']), p['shape_r'],
            p['score'], p['p_dup'] * 100.0,
        ])
    _write_table(ws, headers, rows_data,
                 col_widths=[6, 18, 18, 13, 12, 22, 22])

    # ---------- Charts ----------
    # Generate the same 2x2 distribution chart used in the PDF and embed
    # it on its own sheet so Excel users have the visual context too.
    try:
        shape_rs = [p.get('shape_r') for p in pairs]
        chart_b64 = _distribution_chart(
            analysis['scores'], analysis['p_dup'], analysis['stats'],
            shape_rs=shape_rs)
        png_bytes = base64.b64decode(chart_b64)
        img_buf = BytesIO(png_bytes)
        img = XlsxImage(img_buf)
        # Matplotlib rendered at figsize (13, 6) at 150 dpi → ~1950×900 px
        # native. Keep aspect ratio while scaling to a sensible Excel width.
        orig_w, orig_h = img.width, img.height
        target_w = 1400  # matches the PDF body's max content width
        img.width = target_w
        img.height = int(target_w * orig_h / orig_w) if orig_w else target_w // 2
        ws = wb.create_sheet('Charts')
        ws['A1'] = 'Distribution charts'
        ws['A1'].font = TITLE_FONT
        ws.add_image(img, 'A3')
    except Exception as exc:
        # Charts are nice-to-have — never fail the whole report on a render error.
        print(f'  warn: skipped Charts sheet ({exc})')

    # Boss request: duplicates up front — first sheet after Summary.
    if 'Confirmed duplicates' in wb.sheetnames:
        wb.move_sheet('Confirmed duplicates',
                      offset=1 - wb.sheetnames.index('Confirmed duplicates'))
    # ---------- Mating likelihood (appended LAST so sheet indices are stable) ----------
    if analysis.get('mating'):
        ws = wb.create_sheet('Mating likelihood')
        headers = ['Rank', 'Pair A', 'Pair B', 'Time gap (s)',
                   'Mating likelihood (%)', 'Likelihood ratio (x)',
                   'Δ launch loss (mdB)', 'Δ first-connector loss (mdB)',
                   'Δ first-connector refl (dB)', 'Δ launch refl (dB)',
                   'Δ end refl (dB)', 'Duplicate likelihood (%)']
        m_sorted = sorted([p for p in pairs if p.get('mating_lr') is not None],
                          key=lambda q: -q['mating_lr'])[:50]
        rows_data = []
        for rank, p in enumerate(m_sorted, 1):
            def _md(k):
                v = p.get('mating_' + k)
                return None if v is None else v * 1000.0
            rows_data.append([
                rank, p['a'], p['b'], _gap_s(p['a'], p['b']),
                p['mating_p'] * 100.0, p['mating_lr'],
                _md('dl0'), _md('dl1'), p.get('mating_dr1'), p.get('mating_dr0'),
                p.get('mating_drE'), p['p_dup'] * 100.0,
            ])
        _write_table(ws, headers, rows_data,
                     col_widths=[6, 18, 18, 13, 20, 18, 18, 24, 24, 18, 16, 22])
        _conf = analysis.get('confidence') or {}
        ws.cell(row=len(rows_data) + 3, column=1,
                value=_conf.get('note', ''))
        ws.cell(row=len(rows_data) + 4, column=1,
                value=('Mating likelihood ranks pairs by how alike their connector '
                       'matings are (launch and first connector loss and reflectance, '
                       'end reflectance). It is a ranking to check against the port '
                       'log, not a duplicate verdict. Prior: '
                       + analysis['mating']['prior_note'] + '.'
                       + ''.join(' ' + g[0].upper() + g[1:] + '.' for g in
                                 (analysis['mating'].get('gates') or {}).get('notes') or [])))
    # ---------- Near splice + Shot out of order (appended LAST, only when present) ----------
    from datetime import datetime as _dt_ns, timezone as _tz_ns

    def _shot_at(t):
        return (_dt_ns.fromtimestamp(float(t), _tz_ns.utc).strftime('%Y-%m-%d %H:%M:%S')
                if t else None)

    def _put_row(ws, r, values, font, fill=None):
        for c, v in enumerate(values, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.font = font
            if fill is not None:
                cell.fill = fill
    _ns = analysis.get('near_splice') or {}
    if _ns.get('usable'):
        ws = wb.create_sheet('Near splice')
        loss, sd2 = _ns['loss'], _ns['sd_pair_db']

        def _diff_cells(la, lb):
            if la is None or lb is None or not (np.isfinite(la) and np.isfinite(lb)):
                return [None, None, None]
            d = abs(la - lb)
            pct = _near_splice_same_pct(_ns, d)
            return [round(d, 4), round(d / sd2, 2), None if pct is None else round(pct, 3)]

        def _l(name):
            v = loss.get(name)
            return None if v is None or not np.isfinite(v) else round(v, 4)
        _put_row(ws, 1, [_ns['summary']], BASE_BOLD)
        _put_row(ws, 2, ['Measured from the trace, not the event table: the level %.0f to %.0f m '
                         'past the panel port minus the level %.0f to %.0f m past it, attenuation '
                         'removed. Noise %.4f dB per shot, from the same windows at %d splice-free '
                         'stretches. Two shots of one fibre differ by more than 4 sd in %.2f%% of '
                         'cases. The splice is glass, so a re-plug cannot change it. A large '
                         'difference is evidence that two files are different fibres; a small '
                         'one proves nothing.'
                         % (_ns['after_m'][0], _ns['after_m'][1], _ns['before_m'][0],
                            _ns['before_m'][1], _ns['noise_db'], _ns['n_spots'],
                            _ns['beyond_4sd_pct'])], BASE)
        r = 4
        listed = []
        seen = set()
        # Exactly the Confirmed duplicates criterion (> 0.5).  Pairs demoted by
        # a physical or speckle violation sit AT 0.5 (LEN_CAP), and listing them
        # here would show the tech 57 'duplicates' on a folder that has none.
        for p in pairs:
            if p.get('p_dup', 0.0) > 0.5:
                listed.append((p, 'confirmed duplicate'))
                seen.add((p['a'], p['b']))
        for p in sorted([q for q in pairs if q.get('mating_lr') is not None],
                        key=lambda q: -q['mating_lr'])[:20]:
            if (p['a'], p['b']) not in seen:
                listed.append((p, 'mating likelihood top 20'))
        diff_hdr = ['Difference (dB)', 'Difference (sd)',
                    'Shots of one fibre that differ this much (%)']
        if listed:
            _put_row(ws, r, ['Pairs listed elsewhere in this report'], BASE_BOLD)
            r += 1
            _put_row(ws, r, ['Pair A', 'Pair B', 'Listed because', 'Time gap (s)',
                             'Splice loss A (dB)', 'Splice loss B (dB)'] + diff_hdr,
                     HDR_FONT, hdr_fill)
            for p, why in listed:
                r += 1
                _put_row(ws, r, [p['a'], p['b'], why, _gap_s(p['a'], p['b']),
                                 _l(p['a']), _l(p['b'])]
                         + _diff_cells(loss.get(p['a']), loss.get(p['b'])), BASE)
            r += 2
        _put_row(ws, r, ['Every fibre against the next fibre number'], BASE_BOLD)
        r += 1
        _put_row(ws, r, ['File', 'Meter', 'Shot at', 'Splice loss (dB)', 'Next fibre',
                         'Time gap (s)'] + diff_hdr, HDR_FONT, hdr_fill)
        by_group = {}
        for f in files:
            try:
                pref, num = _port_split(f['name'])
            except Exception:
                pref, num = f['name'], None
            by_group.setdefault(pref, {})[num] = f
        for pref in sorted(by_group, key=str):
            grp = by_group[pref]
            for num in sorted(grp, key=lambda x: (x is None, x or 0)):
                f = grp[num]
                nxt = grp.get(num + 1) if num is not None else None
                r += 1
                _put_row(ws, r, [f['name'], f.get('serial_number'), _shot_at(f.get('timestamp')),
                                 _l(f['name']), nxt['name'] if nxt else None,
                                 _gap_s(f['name'], nxt['name']) if nxt else None]
                         + (_diff_cells(loss.get(f['name']), loss.get(nxt['name']))
                            if nxt else [None, None, None]), BASE)
        for col, w in zip('ABCDEFGHI', (22, 22, 30, 16, 20, 20, 16, 15, 24)):
            ws.column_dimensions[col].width = w
    _fi = analysis.get('fill_ins') or []
    if _fi:
        ws = wb.create_sheet('Shot out of order')
        rows_data = [[', '.join(x['names']), _shot_at(x['shot_at']),
                      x['before'], _shot_at(x['before_at']),
                      x['after'], _shot_at(x['after_at']),
                      round(x['minutes_later'], 1)] for x in _fi]
        _write_table(ws, ['Fibre(s)', 'Shot at', 'Fibre before', 'Shot at',
                          'Fibre after', 'Shot at', 'Minutes after both neighbours'],
                     rows_data, col_widths=[36, 20, 22, 20, 22, 20, 18])
        ws.cell(row=len(rows_data) + 3, column=1,
                value=('Each of these was shot more than %.0f minutes after both '
                       'neighbouring fibres, while those neighbours were shot back to '
                       'back. The fibre was skipped and shot later, so its port had to '
                       'be found again. This is where to check the port log. It is not '
                       'a duplicate finding.' % (_FILL_IN_FAR_S / 60.0)))
    wb.save(out_xlsx)
    print(f'XLSX: {out_xlsx}')
    return out_xlsx


def run_sor_xlsx_bytes(folder, title, meta=None):
    """Run SOR mode and return (xlsx_bytes, n_files, n_pairs).  Pass a dict
    as `meta` to receive additive analysis facts (currently
    `short_traces`) without changing the return contract."""
    import tempfile
    _meta = meta if meta is not None else {}
    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, 'report.xlsx')
        build_xlsx_sor(folder, title, tmp, meta=_meta)
        with open(tmp, 'rb') as fh:
            xlsx_bytes = fh.read()
    # Report what was ANALYSED, not what was globbed.  These used to
    # recount the staged folder after rendering, which disagreed with the
    # report's own header on any span where a trace is excluded:
    #
    #   ELMDALE TO MILER   glob 1152 / 662,976   analysed 1151 / 661,825
    #                      (ELMMIL0231_1550 ends at 22,288 m against a
    #                       69,567 m median — a real break)
    #   DURANC 1-144       glob  144 /  10,296   analysed  141 /   9,870
    #
    # The glob number reached the download-button label and the green
    # "N SOR files processed" line while the workbook's own Summary sheet
    # printed the smaller one.  Same folder, two numbers, no explanation.
    #
    # The glob was also case-sensitive on a path that is not: _inventory
    # matches on a lowercased name, so a file saved as .SOR was inventoried
    # and staged but missed here.  On POSIX that made the count too LOW and
    # the trace was silently dropped from the analysis; on Windows
    # ntpath.normcase lowercases, so the same folder behaved differently in
    # the field than on the dev machine.  Reading the analysis removes the
    # second parser entirely rather than teaching it the same rules.
    n_files = _meta.get('n_files', 0)
    n_pairs = _meta.get('n_pairs', 0)
    return xlsx_bytes, n_files, n_pairs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sor-dir', required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--out-pdf', help='Path for PDF output')
    parser.add_argument('--out-xlsx', help='Path for XLSX output')
    args = parser.parse_args()
    if args.out_pdf:
        build_report_sor(args.sor_dir, args.title, args.out_pdf)
    if args.out_xlsx:
        build_xlsx_sor(args.sor_dir, args.title, args.out_xlsx)
    if not args.out_pdf and not args.out_xlsx:
        parser.error('Specify at least one of --out-pdf or --out-xlsx')


if __name__ == '__main__':
    main()
