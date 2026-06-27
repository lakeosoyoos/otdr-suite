"""anchors — load + validate the anchor table for helix calibration.

An anchor maps a KNOWN cable-sheath distance (the tech's as-built / reel
put-up footage) to an OTDR event (fiber distance) so the calibration can fit
``y_known = m * x_otdr + b``.

Table format (CSV or .xlsx, one row per known cable-sheath measurement).
Recognized columns (case-insensitive, snake or spaced):

    fiber_id        str   — matches GenParams cable_id / filename stem
                            (e.g. HOWLAN001). Blank or '*' => applies to all
                            fibers in the cable (one shared EFL).
    anchor_type     enum  — 'closure' (cumulative as-built footage to a named
                            closure) or 'reel' (put-up length of one reel
                            between two reel-splice events).
    closure_name    str   — optional human label (e.g. 'Splice 3').
    event_index     int   — ordinal in the trace's interior events (0-based)
                            that this anchor pins to. One of event_index /
                            approx_otdr_km is required for 'closure' rows.
    approx_otdr_km  float — approximate fiber-km; tool snaps to nearest
                            interior event.
    known_distance  float — KNOWN cable-sheath distance to that event
                            (cumulative-from-origin for 'closure').
    units           enum  — 'ft' | 'm' (REQUIRED per row; drives
                            normalization). Default 'ft' if a header-level
                            default is supplied.
    direction       enum  — 'A' | 'B' | 'both' (optional). Lets bidirectional
                            per-event distances be averaged before fitting.
    notes           str   — free text.

For 'reel' rows the put-up segment is described by:
    span_start_event int  — interior event index at the reel's near end.
    span_end_event   int  — interior event index at the reel's far end.
    segment_length   float— the reel's put-up length (in ``units``).

Everything is normalized to METERS internally; the original unit is echoed in
the report.

This module deliberately does NOT touch SOR binaries — it only reads the
table and returns ``Anchor`` records.  Resolving an anchor to an actual event
distance happens in ``calibrate`` (which has the trace records).
"""

import csv
import os
from dataclasses import dataclass, field
from typing import Optional


FT_PER_M = 3.280839895013123  # exact 1 m = 1/0.3048 ft
M_PER_FT = 0.3048


def ft_to_m(x):
    return x * M_PER_FT


def m_to_ft(x):
    return x * FT_PER_M


def to_meters(value, units):
    """Normalize a length to meters. ``units`` is 'ft' or 'm'."""
    u = (units or "").strip().lower()
    if u in ("ft", "feet", "foot"):
        return ft_to_m(value)
    if u in ("m", "meter", "meters", "metre", "metres"):
        return float(value)
    raise ValueError(f"unknown length unit {units!r} (expected 'ft' or 'm')")


@dataclass
class Anchor:
    """One anchor row, normalized.

    ``known_distance_m`` is the y-value (known cable-sheath distance in
    meters).  The x-value (OTDR fiber distance) is resolved later by
    ``calibrate`` from the matched trace record, so it is not stored here.
    """
    fiber_id: str               # '' / '*' => applies to all fibers
    anchor_type: str            # 'closure' | 'reel'
    closure_name: str
    event_index: Optional[int]
    approx_otdr_km: Optional[float]
    known_distance_m: Optional[float]
    units: str                  # original units, echoed in report
    direction: str              # 'A' | 'B' | 'both'
    # reel-only
    span_start_event: Optional[int]
    span_end_event: Optional[int]
    segment_length_m: Optional[float]
    notes: str = ""
    row_num: int = 0            # 1-based source row, for error messages

    @property
    def applies_to_all(self):
        return self.fiber_id in ("", "*")


class AnchorError(ValueError):
    pass


# Column aliases -> canonical key.
_ALIASES = {
    "fiber_id": "fiber_id", "fiber": "fiber_id", "fiberid": "fiber_id",
    "fiber id": "fiber_id", "cable_id": "fiber_id",
    "anchor_type": "anchor_type", "type": "anchor_type",
    "anchor type": "anchor_type",
    "closure_name": "closure_name", "closure": "closure_name",
    "event_label": "closure_name", "label": "closure_name",
    "closure name": "closure_name",
    "event_index": "event_index", "event": "event_index",
    "event_idx": "event_index", "event index": "event_index",
    "approx_otdr_km": "approx_otdr_km", "otdr_km": "approx_otdr_km",
    "approx_km": "approx_otdr_km", "approx otdr km": "approx_otdr_km",
    "known_distance": "known_distance", "known": "known_distance",
    "cable_distance": "known_distance", "sheath": "known_distance",
    "known distance": "known_distance", "footage": "known_distance",
    "units": "units", "unit": "units",
    "direction": "direction", "dir": "direction",
    "span_start_event": "span_start_event", "span start event": "span_start_event",
    "span_end_event": "span_end_event", "span end event": "span_end_event",
    "segment_length": "segment_length", "segment length": "segment_length",
    "reel_length": "segment_length",
    "notes": "notes", "note": "notes", "comment": "notes",
}


def _norm_key(k):
    return _ALIASES.get((k or "").strip().lower())


def _to_int(v):
    if v is None or str(v).strip() == "":
        return None
    return int(float(str(v).strip()))


def _to_float(v):
    if v is None or str(v).strip() == "":
        return None
    return float(str(v).strip())


def _read_rows_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows = [dict(r) for r in reader]
    return headers, rows


def _read_rows_xlsx(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        return [], []
    headers = [("" if c is None else str(c)) for c in header_row]
    rows = []
    for r in rows_iter:
        if r is None or all(c is None for c in r):
            continue
        d = {}
        for h, c in zip(headers, r):
            d[h] = c
        rows.append(d)
    wb.close()
    return headers, rows


def load_anchors(path, default_units="ft", default_direction="both"):
    """Load and validate an anchor table.

    ``default_units`` applies to rows that omit the units column.
    ``default_direction`` applies to rows that omit direction.

    Returns ``list[Anchor]`` (already normalized to meters).  Raises
    ``AnchorError`` on a malformed table.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        headers, raw_rows = _read_rows_xlsx(path)
    elif ext in (".csv", ".tsv", ".txt"):
        headers, raw_rows = _read_rows_csv(path)
    else:
        raise AnchorError(f"unsupported anchor table extension {ext!r} "
                          f"(expected .csv or .xlsx)")

    # Build a header -> canonical map; unknown headers are ignored.
    canon = {}
    for h in headers:
        ck = _norm_key(h)
        if ck:
            canon[h] = ck

    anchors = []
    for i, raw in enumerate(raw_rows, start=2):  # row 1 = header
        rec = {}
        for h, v in raw.items():
            ck = canon.get(h)
            if ck:
                rec[ck] = v
        # Skip fully blank rows.
        if not any(str(v).strip() for v in rec.values() if v is not None):
            continue

        anchor_type = (str(rec.get("anchor_type") or "closure").strip().lower())
        if anchor_type not in ("closure", "reel"):
            raise AnchorError(f"row {i}: anchor_type must be 'closure' or "
                              f"'reel', got {anchor_type!r}")

        units = (str(rec.get("units") or default_units).strip().lower())
        direction = (str(rec.get("direction") or default_direction).strip().upper())
        if direction not in ("A", "B", "BOTH"):
            raise AnchorError(f"row {i}: direction must be A/B/both, "
                              f"got {direction!r}")

        event_index = _to_int(rec.get("event_index"))
        approx_km = _to_float(rec.get("approx_otdr_km"))
        known = _to_float(rec.get("known_distance"))
        seg = _to_float(rec.get("segment_length"))

        known_m = to_meters(known, units) if known is not None else None
        seg_m = to_meters(seg, units) if seg is not None else None

        if anchor_type == "closure":
            if event_index is None and approx_km is None:
                raise AnchorError(
                    f"row {i}: closure anchor needs event_index or "
                    f"approx_otdr_km to locate the OTDR event")
            if known_m is None:
                raise AnchorError(
                    f"row {i}: closure anchor needs known_distance "
                    f"(cable-sheath footage)")
        else:  # reel
            sse = _to_int(rec.get("span_start_event"))
            see = _to_int(rec.get("span_end_event"))
            if sse is None or see is None or seg_m is None:
                raise AnchorError(
                    f"row {i}: reel anchor needs span_start_event, "
                    f"span_end_event, and segment_length")

        anchors.append(Anchor(
            fiber_id=str(rec.get("fiber_id") or "").strip(),
            anchor_type=anchor_type,
            closure_name=str(rec.get("closure_name") or "").strip(),
            event_index=event_index,
            approx_otdr_km=approx_km,
            known_distance_m=known_m,
            units=("m" if units in ("m", "meter", "meters", "metre", "metres")
                   else "ft"),
            direction=("both" if direction == "BOTH" else direction),
            span_start_event=_to_int(rec.get("span_start_event")),
            span_end_event=_to_int(rec.get("span_end_event")),
            segment_length_m=seg_m,
            notes=str(rec.get("notes") or "").strip(),
            row_num=i,
        ))

    if not anchors:
        raise AnchorError(f"no valid anchor rows found in {path}")
    return anchors
