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

    # σ(M[i] - M[j]) for all pairs via the variance-decomposition identity:
    #     var(A - B) = mean(A²) + mean(B²) - 2·E[A·B] - (E[A] - E[B])²
    m1 = M_raw.mean(axis=1)
    m2 = (M_raw.astype(np.float64) ** 2).mean(axis=1)
    C = (M_raw.astype(np.float64) @ M_raw.astype(np.float64).T) / float(N)
    var_ij = (m2[:, None] + m2[None, :] - 2.0 * C
              - (m1[:, None] - m1[None, :]) ** 2)
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
# 0.30 splits those cleanly.  The rule is only consulted for folders the
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


def _port_split(name):
    """Split a filename stem into (prefix, port).  The prefix doubles as the
    direction/route group so A→B and B→A shots never mix in gap stats."""
    m = _PORT_WL_RE.search(name) or _PORT_TAIL_RE.search(name)
    if not m:
        return name, None
    return name[:m.start(1)], int(m.group(1))


def _neighbor_decay(names, r_matrix,
                    near_gap=_DECAY_NEAR_GAP, far_gap=_DECAY_FAR_GAP,
                    min_pairs=_DECAY_MIN_PAIRS):
    """Neighbor-vs-far raw-r structure for the distance-decay regime test.

    `names` are filename stems aligned with `r_matrix` rows.  Pairs are
    compared only within the same prefix group (same route/direction) and
    only when both files carry a trailing port number.  Returns
    (near_r_median, far_r_median, n_near, n_far) or None when either bucket
    has fewer than `min_pairs` pairs (small folders can't trip this rule).
    """
    K = len(names)
    if K < 2 or r_matrix.shape[0] != K:
        return None
    prefixes, ports = [], []
    for n in names:
        pref, port = _port_split(n)
        prefixes.append(pref)
        ports.append(port)
    port_arr = np.array([p if p is not None else -1 for p in ports],
                        dtype=np.int64)
    has_port = port_arr >= 0
    codes = {p: i for i, p in enumerate(sorted(set(prefixes)))}
    pref_arr = np.array([codes[p] for p in prefixes], dtype=np.int64)

    same_pref = pref_arr[:, None] == pref_arr[None, :]
    both_ports = has_port[:, None] & has_port[None, :]
    gap = np.abs(port_arr[:, None] - port_arr[None, :])
    upper = np.triu(np.ones((K, K), dtype=bool), k=1)
    eligible = upper & same_pref & both_ports
    near_mask = eligible & (gap >= 1) & (gap <= near_gap)
    far_mask = eligible & (gap >= far_gap)
    n_near = int(near_mask.sum())
    n_far = int(far_mask.sum())
    if n_near < min_pairs or n_far < min_pairs:
        return None
    near_r = float(np.median(r_matrix[near_mask]))
    far_r = float(np.median(r_matrix[far_mask]))
    return near_r, far_r, n_near, n_far


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
    if bulk_r >= 0.7 and bulk_sigma < 0.10:
        regime = 'all_dups'
    elif min_L < 200 and len(files) >= 50:
        regime = 'short_panel'
    elif bulk_r >= 0.7 or frac_high_r >= 0.30:
        regime = 'tie_panel'
    else:
        regime = 'production'
        names_raw = [files[i]['name'] for i in valid_idx_raw]
        decay = _neighbor_decay(names_raw, r_raw)
        if decay is not None and (decay[0] - decay[1]) >= _DECAY_MIN_DROP:
            regime = 'tie_panel'
            regime_reason = (f'neighbor-decay: near r {decay[0]:.2f} '
                             f'vs far r {decay[1]:.2f}')
        elif min_L < _SHORT_COMMON_SPAN_M:
            regime = 'tie_panel'
            regime_reason = f'short common span: {min_L:.0f} m'
    _reason_sfx = f', {regime_reason}' if regime_reason else ''
    print(f'Regime: {regime} (bulk σ={bulk_sigma:.4f} dB, '
          f'bulk r={bulk_r:.4f}, frac high-r={frac_high_r:.2f}{_reason_sfx})')
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
        # Standard production ramp — true same-fiber re-shoots in a short
        # panel still produce r ≥ 0.95. With σ-outlier disabled below,
        # the r-tier is the entire detector for this regime.
        R_LO, R_HI = 0.95, 0.99
    else:
        R_LO, R_HI = 0.95, 0.99
    _R_SPAN = R_HI - R_LO
    def _r_to_p(r):
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
    if regime in ('tie_panel', 'all_dups', 'short_panel'):
        p_dup_sigma_eff = np.zeros_like(p_dup_sigma)
    else:
        p_dup_sigma_eff = p_dup_sigma
    # Combined likelihood = max of (possibly confirmed) σ-outlier and r tiers.
    p_dup_raw = np.maximum(p_dup_sigma_eff, p_dup_r)

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
        n_match, n_max, n_min, mean_dloss, max_dloss = _event_match_quality(
            file_events.get(p['a']), file_events.get(p['b']))
        p['events_n_match'] = int(n_match)
        p['events_n_max']   = int(n_max)
        p['events_n_min']   = int(n_min)
        p['events_mean_dloss_db'] = float(mean_dloss)
        p['events_max_dloss_db']  = float(max_dloss)
        if not _events_agree(n_match, n_max, n_min, mean_dloss):
            events_violation[i] = True

    physical_violation = length_violation | events_violation
    p_dup = np.where(physical_violation, np.minimum(p_dup_raw, LEN_CAP), p_dup_raw)

    # Raw-identity short-circuit: a pair whose RAW interior trace is the
    # same data (σ ≤ 0.001 dB, r ≥ 0.98 — see the calibration block above)
    # is a CONFIRMED copy regardless of regime routing.  Applied last so no
    # regime bypass (tie_panel fingerprint subtraction, all_dups σ bypass)
    # or stored-event-table disagreement can hide a literal file copy: the
    # trace itself is the identity proof.  Raises to 1.0 only — never lowers.
    raw_ident_mask = np.array([bool(p.get('raw_identical')) for p in pairs],
                              dtype=bool)
    if raw_ident_mask.any():
        p_dup = np.where(raw_ident_mask, 1.0, p_dup)

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
    best_partner = {}
    for idx, f in enumerate(files):
        best = None
        for p in pairs:
            if f['name'] not in (p['a'], p['b']):
                continue
            if best is None:
                best = p
            elif (p['p_dup'] > best['p_dup']
                  or (p['p_dup'] == best['p_dup'] and p['score'] < best['score'])):
                best = p
        best_partner[f['name']] = best

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
        'bulk_sigma': bulk_sigma,
        'bulk_r': bulk_r,
        'frac_high_r': frac_high_r,
    }


def build_report_sor(folder, title, out_pdf, meta=None):
    analysis = _analyze_sor(folder)
    if meta is not None:
        # Additive side-channel for the runner's manifest (`short_traces`);
        # optional so every existing caller is untouched.
        meta['short_traces'] = analysis.get('short_traces') or []
        if analysis.get('window_guard'):
            meta['window_guard'] = analysis['window_guard']
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
                     f'<td class="center" style="color:{_shape_color(r_val)};font-weight:600">{r_val:.4f}</td>'
                     f'<td class="center">{p["score"]:.4f}</td>'
                     f'<td class="center" style="color:{pd_color};font-weight:600">{pd_val*100:.2f}%</td></tr>')

    # Confirmed-duplicate detail table (p_dup > 0.5)
    file_by_name = {f['name']: f for f in files}
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
        dup_detail_block = (
            '<div class="section-block">'
            '<div class="dir-banner">1. Confirmed duplicate pairs (\u226550% likelihood)</div>'
            '<div style="padding:10px 4px;color:#2d8f48;font-weight:600">'
            'None \u2014 no pairs at \u226550% duplicate likelihood.</div></div>')

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
    <th>Length (km)</th><th>Span loss (dB)</th>
    <th>lowest disagreement</th><th>Duplicate likelihood</th>
    <th>similarity</th><th>Verdict</th></tr>
{file_rows}
</table>
</div>

<div class="section-block">
<div class="dir-banner">4. Top 30 pairs — lowest level of disagreement</div>
<table class="vote-table">
<tr><th>Rank</th><th style="text-align:left">Pair</th>
    <th>level of disagreement</th><th>Duplicate likelihood</th><th>similarity</th></tr>
{top_rows}
</table>
</div>

<div class="section-block">
<div class="dir-banner">5. Top 30 pairs — highest similarity</div>
<table class="vote-table">
<tr><th>Rank</th><th style="text-align:left">Pair</th>
    <th>similarity</th><th>level of disagreement</th><th>Duplicate likelihood</th></tr>
{sim_rows}
</table>
</div>
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
    with tempfile.TemporaryDirectory() as td:
        tmp_pdf = os.path.join(td, 'report.pdf')
        build_report_sor(folder, title, tmp_pdf, meta=meta)
        with open(tmp_pdf, 'rb') as fh:
            pdf_bytes = fh.read()
    n_files = len(glob.glob(os.path.join(folder, '*.sor')))
    n_pairs = n_files * (n_files - 1) // 2
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

    # ---------- Top 30 — lowest disagreement ----------
    ws = wb.create_sheet('Top 30 lowest disagreement')
    headers = ['Rank', 'Pair A', 'Pair B', 'Level of disagreement',
               'Duplicate likelihood (%)', 'Similarity']
    rows_data = []
    for rank, k in enumerate(order[:30], 1):
        p = pairs[k]
        rows_data.append([
            rank, p['a'], p['b'], p['score'],
            p['p_dup'] * 100.0, p.get('shape_r'),
        ])
    _write_table(ws, headers, rows_data,
                 col_widths=[6, 18, 18, 22, 22, 12])

    # ---------- Top 30 — highest similarity ----------
    ws = wb.create_sheet('Top 30 highest similarity')
    headers = ['Rank', 'Pair A', 'Pair B', 'Similarity',
               'Level of disagreement', 'Duplicate likelihood (%)']
    sim_sorted = sorted([(i, p) for i, p in enumerate(pairs)
                         if p.get('shape_r') is not None],
                        key=lambda x: -x[1]['shape_r'])[:30]
    rows_data = []
    for rank, (_, p) in enumerate(sim_sorted, 1):
        rows_data.append([
            rank, p['a'], p['b'], p['shape_r'],
            p['score'], p['p_dup'] * 100.0,
        ])
    _write_table(ws, headers, rows_data,
                 col_widths=[6, 18, 18, 12, 22, 22])

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
    wb.save(out_xlsx)
    print(f'XLSX: {out_xlsx}')
    return out_xlsx


def run_sor_xlsx_bytes(folder, title, meta=None):
    """Run SOR mode and return (xlsx_bytes, n_files, n_pairs).  Pass a dict
    as `meta` to receive additive analysis facts (currently
    `short_traces`) without changing the return contract."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, 'report.xlsx')
        build_xlsx_sor(folder, title, tmp, meta=meta)
        with open(tmp, 'rb') as fh:
            xlsx_bytes = fh.read()
    n_files = len(glob.glob(os.path.join(folder, '*.sor')))
    n_pairs = n_files * (n_files - 1) // 2
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
