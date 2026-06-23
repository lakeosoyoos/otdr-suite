"""calibrate — core helix-calibration math.

Given a set of per-trace records (from ``sor_fields.read_trace_record``) and
an anchor table (from ``anchors.load_anchors``), fit

        y_known = m * x_otdr + b        (least squares, over all anchor pairs)

where x_otdr is the OTDR fiber distance to an event (meters) and y_known is
the known cable-sheath distance to the same event (meters).

  * slope  m  = the conversion / helix factor (cable-sheath per OTDR-fiber).
  * EFL%      = (1/m - 1) * 100   — excess fiber length percentage.
  * intercept b = fixed offset (launch / patch-cord / dead-zone).  Reported
                  SEPARATELY; never folded into m.
  * R²        = goodness of fit.
  * residuals = per-anchor (y_known - (m*x + b)).

Bidirectional: when both an A trace (e.g. HOWLAN###) and its B trace
(LANHOW###) exist for the same fiber number, the per-event OTDR distances are
averaged before fitting (B distances are flipped into the A frame using the
B trace's own EOF as the origin).

IOR guardrail: a wrong stored IOR silently corrupts m (0.1% IOR error ≈ the
whole helix effect).  Each trace's stored IOR is compared against the cohort
median and an optional expected fiber-spec value; divergent traces are
flagged and, unless every trace's IOR is confirmed, the combined result is
labeled "combined empirical factor (IOR not independently verified)".

Cross-fiber consistency: all fibers in one cable share one EFL, so we fit a
per-fiber factor and report the spread (std / range) as an error bar, flagging
any fiber that disagrees with the cohort.

AEN142 sanity bands: stranded loose-tube m ≈ 0.97–0.98, central tube
m ≈ 0.99–1.00.  A fit outside the selected band emits a warning rather than
silently returning.

NOTE: this module does not require numpy for the fit (it uses a closed-form
OLS) so it has no new dependency beyond the suite's existing stack.
"""

import math
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from . import anchors as _anchors_mod


# ── AEN142 sanity bands ─────────────────────────────────────────────────
CABLE_TYPE_BANDS = {
    # cable_type key -> (m_low, m_high, label)
    "stranded_loose_tube": (0.970, 0.980, "stranded loose-tube (AEN142)"),
    "central_tube": (0.990, 1.000, "central tube (AEN142)"),
}
DEFAULT_CABLE_TYPE = "stranded_loose_tube"

# IOR guardrail: stored IOR may differ from the cohort by at most this much
# before the trace is flagged.  0.1% of ~1.47 is ~0.0015; the helix effect is
# ~0.3%, so we flag anything past ~0.05% to stay well inside the danger zone.
IOR_COHORT_TOL = 0.0008
# Cross-fiber consistency: flag a fiber whose per-fiber m is more than this
# many cohort std-devs from the cohort mean (and at least this absolute gap).
XFIBER_SIGMA = 2.5
XFIBER_ABS_M = 0.003


# ── Result containers ───────────────────────────────────────────────────
@dataclass
class AnchorFit:
    """One resolved anchor pair that entered the fit."""
    fiber_key: str          # canonical fiber number key, e.g. '001'
    closure_name: str
    anchor_type: str
    x_otdr_m: float         # OTDR fiber distance (meters), bidir-averaged
    y_known_m: float        # known cable-sheath distance (meters)
    direction_used: str     # 'A' | 'B' | 'A+B'
    residual_m: Optional[float] = None  # set after the combined fit
    row_num: int = 0


@dataclass
class FiberFit:
    """Per-fiber least-squares fit (needs >=2 anchors for slope+intercept)."""
    fiber_key: str
    n_anchors: int
    m: Optional[float]
    b: Optional[float]
    efl_pct: Optional[float]
    r2: Optional[float]
    stored_ior: Optional[float]
    ior_flag: bool = False
    ior_note: str = ""
    outlier: bool = False
    outlier_note: str = ""


@dataclass
class CalibrationResult:
    # combined fit over all anchor pairs
    m: Optional[float]
    b_m: Optional[float]            # intercept in meters (reported separately)
    efl_pct: Optional[float]
    r2: Optional[float]
    n_anchors: int
    anchor_fits: list = field(default_factory=list)   # list[AnchorFit]
    fiber_fits: list = field(default_factory=list)    # list[FiberFit]
    # IOR guardrail
    ior_verified: bool = False
    ior_label: str = ""
    cohort_ior: Optional[float] = None
    expected_ior: Optional[float] = None
    ior_flags: list = field(default_factory=list)     # list[str]
    # cross-fiber consistency
    fiber_m_mean: Optional[float] = None
    fiber_m_std: Optional[float] = None
    fiber_m_range: Optional[float] = None
    outlier_fibers: list = field(default_factory=list)
    # AEN142 band
    cable_type: str = DEFAULT_CABLE_TYPE
    band: Optional[tuple] = None
    band_verdict: str = ""
    warnings: list = field(default_factory=list)
    # provenance
    n_traces: int = 0
    wavelength: Optional[float] = None


# ── Fiber-number key extraction (pair A and B traces) ───────────────────
_FIBER_NUM_RE = re.compile(r"(\d{1,4})")


def fiber_key_from_id(fiber_id, filename=None):
    """Canonical fiber-number key used to pair A/B traces and to match
    anchor fiber_id rows.  We key on the trailing digit run of the
    cable_id (e.g. 'HOWLAN001' -> '001', 'LANHOW001' -> '001'), falling
    back to the filename stem."""
    src = fiber_id or ""
    m = list(_FIBER_NUM_RE.finditer(src))
    if not m and filename:
        stem = os.path.splitext(os.path.basename(filename))[0]
        m = list(_FIBER_NUM_RE.finditer(stem))
    if not m:
        return (fiber_id or filename or "?").strip()
    digits = m[-1].group(1)
    return digits.zfill(3)


def direction_from_id(fiber_id, location_a=None, location_b=None, filename=None):
    """Classify a trace as 'A' or 'B'.

    Primary signal is the cable_id prefix: HOWLAN* (originating-side launch)
    is the A direction, LANHOW* is the B direction on this span.  We derive
    the convention generically: A = id starts with the first-4 of location_a,
    B = id starts with the first-4 of location_b.  Falls back to filename.
    """
    src = (fiber_id or "").upper()
    if not src and filename:
        src = os.path.splitext(os.path.basename(filename))[0].upper()
    la = (location_a or "").upper()[:4]
    lb = (location_b or "").upper()[:4]
    if la and src.startswith(la):
        return "A"
    if lb and src.startswith(lb):
        return "B"
    # Heuristic fallback for this span's naming.
    if src.startswith("HOWLAN"):
        return "A"
    if src.startswith("LANHOW"):
        return "B"
    return "A"


# ── Event resolution ────────────────────────────────────────────────────
def _interior_event_distances_m(events, ior=None):
    """Return interior-event OTDR distances in meters (launch & EOF dropped).

    Reuses the suite's interior-event filter for consistency with the rest of
    the suite.  ``ior`` is unused here (dist_km already encodes the trace's
    own IOR) but kept for signature clarity.
    """
    from .sor_fields import _sr  # late import to share the parser instance
    interior = _sr._interior_events(events)
    return [e["dist_km"] * 1000.0 for e in interior]


# Max distance (meters) between an A-frame target and a B-flipped interior
# event for them to be considered the SAME physical closure.  Closures on
# this span are kilometres apart, so a few hundred metres is a safe window
# (and well inside the helix accumulation we are trying to measure).
_BIDIR_ALIGN_TOL_M = 400.0


def _with_target_km(anchor, approx_km):
    """Return a shallow copy of ``anchor`` with ``approx_otdr_km`` set to
    ``approx_km`` — used to hand the A-resolved closure position to the B-frame
    snap so both directions align to the same physical closure."""
    import dataclasses
    return dataclasses.replace(anchor, approx_otdr_km=approx_km)


def _resolve_x_in_a_frame(anchor, interior_m, eof_m, direction):
    """Resolve an anchor to an x-value (OTDR fiber distance, meters) expressed
    in the **A frame**, for one trace direction.

    The anchor's ``event_index`` / ``approx_otdr_km`` always refer to the A
    direction (the originating-end launch).  For the A trace we read that
    event directly.  For the B trace we CANNOT use the same ordinal index —
    B numbers its events from the opposite end and has a different event count
    — so we flip every B interior event into the A frame (``eof_m - x``) and
    snap to the one nearest the anchor's A-frame target position.  This aligns
    the SAME physical closure across opposite distance frames (survey risk
    #7/#8: ordinal alignment across A/B is invalid; align by position).

    Returns float meters in the A frame, or None if it cannot be located /
    aligned within ``_BIDIR_ALIGN_TOL_M``.
    """
    if not interior_m:
        return None

    # First establish the A-frame TARGET position the anchor pins to.
    target = None
    if anchor.anchor_type == "reel":
        idx = anchor.span_end_event
        if idx is not None and 0 <= idx < len(interior_m):
            # reel span indices are defined on the A direction
            target = interior_m[idx] if direction == "A" else None
    elif anchor.event_index is not None:
        idx = anchor.event_index
        if direction == "A":
            if 0 <= idx < len(interior_m):
                target = interior_m[idx]
        # for B we need an A-frame target; derived from approx_otdr_km below
    if target is None and anchor.approx_otdr_km is not None:
        target = anchor.approx_otdr_km * 1000.0

    if direction == "A":
        if target is not None:
            # snap to nearest A interior event (handles approx_otdr_km path)
            nearest = min(interior_m, key=lambda v: abs(v - target))
            return nearest
        return None

    # direction == "B": flip B events into A frame and snap by position.
    if not eof_m:
        return None
    if target is None:
        # Without an A-frame target we cannot align B; the caller's A trace
        # (if present) drives the target via approx_otdr_km. Require one.
        return None
    flipped = [eof_m - x for x in interior_m]
    nearest = min(flipped, key=lambda v: abs(v - target))
    if abs(nearest - target) > _BIDIR_ALIGN_TOL_M:
        return None
    return nearest


# ── Least squares (closed form OLS) ─────────────────────────────────────
def _ols(xs, ys):
    """Ordinary least squares y = m*x + b. Returns (m, b, r2) or
    (None, None, None) when underdetermined."""
    n = len(xs)
    if n < 2:
        return None, None, None
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-12:
        return None, None, None
    m = (n * sxy - sx * sy) / denom
    b = (sy - m * sx) / n
    # R²
    ybar = sy / n
    ss_tot = sum((y - ybar) ** 2 for y in ys)
    ss_res = sum((y - (m * x + b)) ** 2 for x, y in zip(xs, ys))
    if ss_tot < 1e-12:
        r2 = 1.0 if ss_res < 1e-12 else 0.0
    else:
        r2 = 1.0 - ss_res / ss_tot
    return m, b, r2


def _efl_pct(m):
    if m is None or m == 0:
        return None
    return (1.0 / m - 1.0) * 100.0


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _std(vals):
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return 0.0
    mu = sum(vals) / len(vals)
    return math.sqrt(sum((v - mu) ** 2 for v in vals) / (len(vals) - 1))


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return 0.5 * (vals[mid - 1] + vals[mid])


# ── Main entry ──────────────────────────────────────────────────────────
def calibrate(records, anchor_list, cable_type=DEFAULT_CABLE_TYPE,
              expected_ior=None, ior_tol=IOR_COHORT_TOL):
    """Run the full calibration.

    ``records``    : iterable of dicts from sor_fields.read_trace_record.
    ``anchor_list``: list[Anchor] from anchors.load_anchors.
    ``cable_type`` : key into CABLE_TYPE_BANDS (manual; GenParams cable_code
                     is junk on this span so it cannot be auto-detected).
    ``expected_ior``: optional fiber-spec IOR to check stored values against.
    ``ior_tol``    : max allowed deviation from cohort median before flagging.

    Returns a CalibrationResult.
    """
    records = [r for r in records if r]
    result = CalibrationResult(
        m=None, b_m=None, efl_pct=None, r2=None, n_anchors=0,
        cable_type=cable_type, expected_ior=expected_ior,
        n_traces=len(records),
    )
    if not records:
        result.warnings.append("no traces supplied")
        return result

    result.wavelength = records[0].get("wavelength")

    # ── Index traces by fiber key + direction ──
    by_fiber = {}   # key -> {'A': rec, 'B': rec}
    for r in records:
        gp = r.get("genparams") or {}
        fid = gp.get("cable_id") or r.get("filename") or ""
        key = fiber_key_from_id(fid, r.get("filename"))
        d = direction_from_id(fid, gp.get("location_a"), gp.get("location_b"),
                              r.get("filename"))
        by_fiber.setdefault(key, {})[d] = r

    # ── IOR guardrail (cohort) ──
    stored_iors = [r.get("stored_ior") for r in records
                   if r.get("stored_ior") is not None]
    cohort = _median(stored_iors)
    result.cohort_ior = cohort
    all_stored_ok = len(stored_iors) == len(records) and bool(stored_iors)
    for r in records:
        s = r.get("stored_ior")
        fn = r.get("filename")
        if s is None:
            result.ior_flags.append(
                f"{fn}: stored IOR unreadable; using {r.get('ior_source')} "
                f"IOR {r.get('ior'):.5f}")
            all_stored_ok = False
            continue
        if cohort is not None and abs(s - cohort) > ior_tol:
            result.ior_flags.append(
                f"{fn}: stored IOR {s:.5f} differs from cohort "
                f"{cohort:.5f} by {abs(s-cohort):.5f} (> {ior_tol})")
        if expected_ior is not None and abs(s - expected_ior) > ior_tol:
            result.ior_flags.append(
                f"{fn}: stored IOR {s:.5f} differs from expected spec "
                f"{expected_ior:.5f}")

    ior_confirmed = (
        all_stored_ok
        and not result.ior_flags
        and expected_ior is not None
    )
    result.ior_verified = ior_confirmed
    result.ior_label = (
        "IOR independently verified against fiber spec"
        if ior_confirmed else
        "combined empirical factor (IOR not independently verified)"
    )

    # ── Build resolved anchor pairs (bidir-averaged x) ──
    anchor_fits = []
    for anc in anchor_list:
        # Which fibers does this anchor apply to?
        if anc.applies_to_all:
            target_keys = list(by_fiber.keys())
        else:
            target_keys = [fiber_key_from_id(anc.fiber_id)]
        for key in target_keys:
            dirs = by_fiber.get(key)
            if not dirs:
                continue
            xa = xb = None
            recA = dirs.get("A")
            recB = dirs.get("B")
            if anc.direction in ("A", "both") and recA:
                ia = _interior_event_distances_m(recA["events"])
                eofa = recA.get("eof_km")
                xa = _resolve_x_in_a_frame(
                    anc, ia, eofa * 1000.0 if eofa else None, direction="A")
            if anc.direction in ("B", "both") and recB:
                ib = _interior_event_distances_m(recB["events"])
                eofb = recB.get("eof_km")
                # B is aligned by POSITION in the A frame.  When the anchor is
                # located by an A-frame event_index, the resolved A position
                # (xa) is the alignment target; pass it through so B snaps to
                # the same physical closure rather than the same ordinal.
                anc_for_b = anc
                if xa is not None and anc.approx_otdr_km is None:
                    anc_for_b = _with_target_km(anc, xa / 1000.0)
                xb = _resolve_x_in_a_frame(
                    anc_for_b, ib, eofb * 1000.0 if eofb else None,
                    direction="B")
            xs = [v for v in (xa, xb) if v is not None]
            if not xs:
                continue
            x = sum(xs) / len(xs)
            dir_used = "A+B" if (xa is not None and xb is not None) else (
                "A" if xa is not None else "B")
            anchor_fits.append(AnchorFit(
                fiber_key=key,
                closure_name=anc.closure_name or f"row{anc.row_num}",
                anchor_type=anc.anchor_type,
                x_otdr_m=x,
                y_known_m=anc.known_distance_m
                if anc.known_distance_m is not None else anc.segment_length_m,
                direction_used=dir_used,
                row_num=anc.row_num,
            ))

    result.anchor_fits = anchor_fits
    result.n_anchors = len(anchor_fits)
    if not anchor_fits:
        result.warnings.append(
            "no anchors resolved to events — check fiber_id / event_index "
            "matching against the trace cable_id and event ordering")
        return result

    # ── Combined fit over all anchor pairs ──
    xs = [a.x_otdr_m for a in anchor_fits]
    ys = [a.y_known_m for a in anchor_fits]
    m, b, r2 = _ols(xs, ys)
    result.m, result.b_m, result.r2 = m, b, r2
    result.efl_pct = _efl_pct(m)
    if m is not None:
        for a in anchor_fits:
            a.residual_m = a.y_known_m - (m * a.x_otdr_m + b)

    # ── Per-fiber fits (cross-fiber consistency) ──
    fiber_fits = []
    per_key_anchors = {}
    for a in anchor_fits:
        per_key_anchors.setdefault(a.fiber_key, []).append(a)
    for key, alist in sorted(per_key_anchors.items()):
        fx = [a.x_otdr_m for a in alist]
        fy = [a.y_known_m for a in alist]
        fm, fb, fr2 = _ols(fx, fy)
        rec = (by_fiber.get(key, {}).get("A")
               or by_fiber.get(key, {}).get("B") or {})
        fiber_fits.append(FiberFit(
            fiber_key=key, n_anchors=len(alist),
            m=fm, b=fb, efl_pct=_efl_pct(fm), r2=fr2,
            stored_ior=rec.get("stored_ior"),
        ))
    result.fiber_fits = fiber_fits

    # cross-fiber spread + outlier flag (only over fibers with a real m)
    fms = [f.m for f in fiber_fits if f.m is not None]
    if fms:
        mu = _mean(fms)
        sd = _std(fms)
        result.fiber_m_mean = mu
        result.fiber_m_std = sd
        result.fiber_m_range = (max(fms) - min(fms)) if len(fms) > 1 else 0.0
        for f in fiber_fits:
            if f.m is None:
                continue
            gap = abs(f.m - mu)
            if (sd > 0 and gap > XFIBER_SIGMA * sd and gap > XFIBER_ABS_M):
                f.outlier = True
                f.outlier_note = (
                    f"m={f.m:.4f} is {gap/sd:.1f}σ from cohort mean "
                    f"{mu:.4f} (Δ{gap:.4f}) — check IOR / event match")
                result.outlier_fibers.append(f.fiber_key)

    # ── AEN142 band verdict ──
    band = CABLE_TYPE_BANDS.get(cable_type)
    result.band = band
    if band is None:
        result.band_verdict = (
            f"no AEN142 band for cable_type {cable_type!r}; "
            f"band sanity check skipped")
        result.warnings.append(result.band_verdict)
    elif m is None:
        result.band_verdict = "no slope fitted; band check skipped"
    else:
        lo, hi, label = band
        if lo <= m <= hi:
            result.band_verdict = f"PASS: m={m:.4f} within {label} band [{lo}, {hi}]"
        else:
            result.band_verdict = (
                f"WARNING: m={m:.4f} OUTSIDE {label} band [{lo}, {hi}] — "
                f"likely IOR error or mismatched anchor, not a real factor")
            result.warnings.append(result.band_verdict)

    return result
