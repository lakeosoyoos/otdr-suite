"""
Acquisition-parameters audit
============================
Inspects the per-file records produced by the SOR / JSON parser and
reports whether every trace in the run was shot with the same
instrument and settings.

For each of:
    * test timestamp        (calendar-day bucketed)
    * OTDR model
    * OTDR serial number
    * wavelength            (compares the SET of wavelengths)
    * pulse width           (ns; per wavelength)
    * averaging             (count for SOR/TRC, time for JSON; per wavelength)

…the helper returns either an "all match" verdict (the value is THE
SPEC) or a "majority" verdict with the count + the list of files whose
value differs.

Result shape:
    {
        "file_fields": [
            {"name": "Test date",     "result": <Verdict>},
            {"name": "OTDR model",    "result": <Verdict>},
            ...
        ],
        "per_wavelength": [
            {"wavelength_nm": 1550.0,
             "rows": [
                {"name": "Pulse width", "result": <Verdict>},
                {"name": "Averaging",   "result": <Verdict>},
             ]},
            ...
        ],
        "n_files": <int>,
        "earliest_iso": <str>,
        "latest_iso":   <str>,
    }

Verdict shape (one consistent contract — the renderer doesn't care
which field produced it):
    {
        "all_match":   bool,
        "spec":        str,                  # the displayed value
        "majority":    str | None,
        "majority_n":  int,
        "outliers":    [(filename, value_str), ...],
        "all_missing": bool,                  # field not stored in this format
    }
"""
from __future__ import annotations

import datetime as _dt
from collections import Counter, defaultdict
from typing import Any, Iterable


# ─────────────────────────────────────────────────────────────────────────────
#  The one helper, reused for every field
# ─────────────────────────────────────────────────────────────────────────────
def consistency_check(samples: list[tuple[str, Any]],
                      display: callable = None) -> dict:
    """Given [(filename, value), ...], return a Verdict dict.

    `display` is an optional formatter applied to a single value for
    rendering (e.g. wavelength → '1550.0 nm').  Defaults to str().

    Rules:
      * A null value (None / '' / NaN) counts as differing — it shows
        as '(missing)' in the outlier list.
      * Majority = the most-common non-null value.  Ties broken by
        first-seen order (Counter.most_common is stable on ties in
        Python 3.7+).
      * all_missing = every sample is null.
    """
    display = display or (lambda v: str(v))

    def _is_null(v):
        if v is None: return True
        if isinstance(v, float) and v != v: return True   # NaN
        if isinstance(v, str)   and not v.strip(): return True
        return False

    nonnull_pairs = [(fn, v) for fn, v in samples if not _is_null(v)]
    if not nonnull_pairs:
        return {
            "all_match":   False,
            "spec":        "Not available (not stored in this file type)",
            "majority":    None,
            "majority_n":  0,
            "outliers":    [],
            "all_missing": True,
        }

    counts = Counter(v for _, v in nonnull_pairs)
    majority_val, majority_n = counts.most_common(1)[0]
    majority_display = display(majority_val)

    outliers = []
    for fn, v in samples:
        if _is_null(v):
            outliers.append((fn, "(missing)"))
        elif v != majority_val:
            outliers.append((fn, display(v)))

    if not outliers:
        return {
            "all_match":   True,
            "spec":        f"✓ All match: {majority_display}",
            "majority":    majority_val,
            "majority_n":  majority_n,
            "outliers":    [],
            "all_missing": False,
        }
    total = len(samples)
    return {
        "all_match":   False,
        "spec":        (f"⚠ Majority: {majority_display} "
                        f"({majority_n} of {total}) — "
                        f"{len(outliers)} differ"),
        "majority":    majority_val,
        "majority_n":  majority_n,
        "outliers":    outliers,
        "all_missing": False,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Per-field extractors — read the right key for the file's source format
# ─────────────────────────────────────────────────────────────────────────────
def _fmt_timestamp(epoch_ts: int) -> str:
    if not epoch_ts:
        return ""
    try:
        return _dt.datetime.utcfromtimestamp(int(epoch_ts)).strftime(
            "%Y-%m-%d %H:%M UTC")
    except (OSError, OverflowError, ValueError):
        return ""


def _calendar_day(epoch_ts: int) -> str:
    """Return YYYY-MM-DD for a UTC epoch, or '' if invalid/missing."""
    if not epoch_ts:
        return ""
    try:
        return _dt.datetime.utcfromtimestamp(int(epoch_ts)).strftime(
            "%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


def _pulse_ns(rec: dict) -> float | None:
    """Pulse width in nanoseconds (normalized across formats)."""
    cal = rec.get('exfo_calibration') or {}
    # JSON: the calibration block already stores NominalPulseWidth in
    # seconds (we set it to pulse_ns * 1e-9 in json_reader).  Round to
    # 1 ns to absorb float jitter so equal pulse widths compare equal.
    for k in ('NominalPulseWidth', 'CalibratedPulseWidth'):
        v = cal.get(k)
        if v is not None:
            return round(v * 1e9, 0)
    # JSON-only fallback
    if '_json_pulse_ns' in rec and rec['_json_pulse_ns'] is not None:
        return round(float(rec['_json_pulse_ns']), 0)
    return None


def _averaging(rec: dict) -> tuple[str, Any] | tuple[str, None]:
    """Returns (kind, value).  kind ∈ {'count', 'time_sec', None}.

    Format-aware:
      * JSON       — Parameters.Duration parsed to seconds  → ('time_sec', s)
      * SOR        — FxdParams Duration (the *tech-chosen* averaging time
                     shown in the EXFO viewer) when present, otherwise the
                     NumberOfAverages count from the proprietary block.
                     Duration is preferred for EXFO FTBx instruments because
                     NumberOfAverages varies adaptively per trace even when
                     every other setting is identical — using the count
                     marks a healthy run as "⚠ 852 differ" when nothing is
                     actually wrong.  Count is still the right answer for
                     instruments that let the tech set an exact average
                     count, so it's the documented fallback.
    """
    src = rec.get('_source')
    if src == 'json':
        dur = rec.get('duration_sec')
        if dur is not None:
            return ('time_sec', round(float(dur), 1))
        return (None, None)
    # SOR path
    dur = rec.get('duration_sec')
    if dur is not None and dur > 0:
        return ('time_sec', round(float(dur), 1))
    cal = rec.get('exfo_calibration') or {}
    n = cal.get('NumberOfAverages')
    if n is not None:
        return ('count', int(n))
    return (None, None)


def _wavelength_nm(rec: dict) -> float | None:
    """Test wavelength in nm.  Prefer the exact wavelength from the EXFO
    proprietary block (~1545.8 / 1625.5 for matched-lot lasers); fall
    back to the nominal FxdParams / JSON value."""
    v = rec.get('exfo_wavelength_nm')
    if v is not None:
        return round(float(v), 1)
    v = rec.get('_json_wavelength_nm')
    if v is not None:
        return round(float(v), 1)
    v = rec.get('wavelength')
    if v is not None:
        return round(float(v), 1)
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Public entry point
# ─────────────────────────────────────────────────────────────────────────────
def audit_acquisition(fibers_a: dict, fibers_b: dict) -> dict:
    """Run the audit across every per-file record in fibers_a + fibers_b.

    Each fibers_* entry is a dict whose value is the record returned by
    parse_sor_full() or parse_otdr_json() — i.e. carries the same keys
    we just extended in those parsers.
    """
    records = []
    for fnum, r in sorted((fibers_a or {}).items()):
        if isinstance(r, dict):
            records.append(r)
    for fnum, r in sorted((fibers_b or {}).items()):
        if isinstance(r, dict):
            records.append(r)

    # ── File-level fields ────────────────────────────────────────────
    by_file = [(r.get('filename') or '?', r) for r in records]

    # Calendar day for test timestamp
    day_samples = [(fn, _calendar_day(r.get('date_time'))) for fn, r in by_file]
    date_verdict = consistency_check(day_samples)

    # Earliest / latest full timestamp span (for context display)
    epoch_vals = [int(r.get('date_time') or 0) for _, r in by_file]
    epoch_vals = [v for v in epoch_vals if v > 0]
    earliest_iso = _fmt_timestamp(min(epoch_vals)) if epoch_vals else ""
    latest_iso   = _fmt_timestamp(max(epoch_vals)) if epoch_vals else ""

    model_samples  = [(fn, (r.get('otdr_model')  or '').strip()) for fn, r in by_file]
    serial_samples = [(fn, (r.get('otdr_serial') or '').strip()) for fn, r in by_file]
    model_verdict  = consistency_check(model_samples)
    serial_verdict = consistency_check(serial_samples)

    # Wavelength as a set per file (handles the multi-wavelength case
    # where one file holds several traces; here each record is one
    # trace, so the set is a single value — but we compare across the
    # full collection).
    wl_samples = [(fn, _wavelength_nm(r)) for fn, r in by_file]
    wl_verdict = consistency_check(
        wl_samples, display=lambda v: f"{v:.1f} nm")

    file_fields = [
        {"name": "Test date (calendar day)", "result": date_verdict},
        {"name": "OTDR model",                "result": model_verdict},
        {"name": "OTDR serial",               "result": serial_verdict},
        {"name": "Wavelength",                "result": wl_verdict},
    ]

    # ── Per-wavelength section: pulse width + averaging ─────────────
    by_wl = defaultdict(list)
    for fn, r in by_file:
        wl = _wavelength_nm(r)
        by_wl[wl].append((fn, r))

    per_wavelength = []
    for wl in sorted(by_wl.keys(),
                     key=lambda v: (v is None, v if v is not None else 0)):
        pairs = by_wl[wl]
        # Pulse width
        pw_samples = [(fn, _pulse_ns(r)) for fn, r in pairs]
        pw_verdict = consistency_check(
            pw_samples, display=lambda v: f"{int(v)} ns")
        # Averaging — keep the (kind, value) tuples so different formats
        # don't appear "equal" by coincidence.
        avg_pairs = [(fn, _averaging(r)) for fn, r in pairs]
        avg_samples = [(fn, t) for fn, t in avg_pairs]

        def _display_avg(t):
            kind, v = t
            if kind == 'count':   return f"{v} avg"
            if kind == 'time_sec': return f"{v:g} s"
            return "(missing)"
        avg_verdict = consistency_check(avg_samples, display=_display_avg)

        per_wavelength.append({
            "wavelength_nm": wl,
            "wavelength_label": (f"{wl:.1f} nm" if wl is not None
                                   else "(unknown wavelength)"),
            "n_files": len(pairs),
            "rows": [
                {"name": "Pulse width", "result": pw_verdict},
                {"name": "Averaging",   "result": avg_verdict},
            ],
        })

    return {
        "n_files":       len(records),
        "earliest_iso":  earliest_iso,
        "latest_iso":    latest_iso,
        "file_fields":   file_fields,
        "per_wavelength": per_wavelength,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Excel renderer — inserts as sheet 0 + sets as active
# ─────────────────────────────────────────────────────────────────────────────
def render_xlsx_sheet(wb, audit: dict, font_name: str = "Calibri",
                       font_size: int = 11) -> None:
    """Insert the audit as the FIRST sheet of `wb` (an openpyxl Workbook).
    Sets it as the active sheet so the workbook opens on it.

    Layout:
        Header row              Calibri 12 bold, light grey fill
        Parameter | Result      two-column table
        Green fill              all_match rows
        Amber fill              outlier rows
        Indent (col B)          outlier files listed one per row
    """
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    ws = wb.create_sheet("Acquisition Parameters", 0)
    wb.active = wb.sheetnames.index(ws.title)

    fnt_normal = Font(name=font_name, size=font_size)
    fnt_bold   = Font(name=font_name, size=font_size, bold=True)
    fnt_header = Font(name=font_name, size=font_size + 1, bold=True)
    fnt_small  = Font(name=font_name, size=font_size - 1, italic=True,
                       color="555555")

    fill_header = PatternFill("solid", fgColor="D9D9D9")
    fill_green  = PatternFill("solid", fgColor="E2EFDA")   # all-match
    fill_amber  = PatternFill("solid", fgColor="FFF2CC")   # outliers
    fill_grey   = PatternFill("solid", fgColor="F2F2F2")   # section banner

    thin = Side(style="thin", color="BFBFBF")
    box  = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 80

    row = 1
    ws.cell(row=row, column=1, value="Acquisition Parameters").font = fnt_header
    ws.cell(row=row, column=2,
            value=(f"{audit['n_files']} trace(s); "
                    f"first {audit['earliest_iso']} → last "
                    f"{audit['latest_iso']}")).font = fnt_small
    row += 1
    ws.cell(row=row, column=1, value="").fill = fill_grey
    ws.cell(row=row, column=2, value="").fill = fill_grey
    row += 1

    # Header
    ws.cell(row=row, column=1, value="Parameter").font = fnt_bold
    ws.cell(row=row, column=2, value="Result").font = fnt_bold
    ws.cell(row=row, column=1).fill = fill_header
    ws.cell(row=row, column=2).fill = fill_header
    ws.cell(row=row, column=1).border = box
    ws.cell(row=row, column=2).border = box
    row += 1

    def _emit(name: str, verdict: dict):
        nonlocal row
        fill = fill_green if verdict["all_match"] else fill_amber
        a = ws.cell(row=row, column=1, value=name)
        b = ws.cell(row=row, column=2, value=verdict["spec"])
        a.font = fnt_normal
        b.font = fnt_normal
        a.fill = fill
        b.fill = fill
        a.border = box
        b.border = box
        a.alignment = Alignment(vertical="top")
        b.alignment = Alignment(vertical="top", wrap_text=True)
        row += 1
        # Outlier list (indented)
        for fn, val in verdict.get("outliers", [])[:60]:
            ws.cell(row=row, column=1, value="").border = box
            c = ws.cell(row=row, column=2, value=f"      {fn} = {val}")
            c.font = fnt_small
            c.fill = fill_amber
            c.border = box
            c.alignment = Alignment(vertical="top", wrap_text=True)
            row += 1
        if len(verdict.get("outliers", [])) > 60:
            extra = len(verdict["outliers"]) - 60
            c = ws.cell(row=row, column=2,
                          value=f"      … and {extra} more")
            c.font = fnt_small
            c.fill = fill_amber
            c.border = box
            row += 1

    # Per-wavelength SUMMARY first — the green all-match rollup is the quick read,
    # so it goes at the top, above the long per-trace detail.
    for w in audit["per_wavelength"]:
        a = ws.cell(row=row, column=1,
                      value=f"— {w['wavelength_label']}  "
                            f"({w['n_files']} trace(s))")
        a.font = fnt_bold
        a.fill = fill_grey
        ws.cell(row=row, column=2, value="").fill = fill_grey
        row += 1
        for entry in w["rows"]:
            _emit(entry["name"], entry["result"])
        row += 1   # blank spacer after each wavelength block

    # Per-trace DETAIL below (file-level fields + their outlier file lists)
    a = ws.cell(row=row, column=1, value="Per-trace detail")
    a.font = fnt_bold
    a.fill = fill_grey
    ws.cell(row=row, column=2, value="").fill = fill_grey
    row += 1
    for entry in audit["file_fields"]:
        _emit(entry["name"], entry["result"])

    # Freeze the header row for scrolling on long outlier lists
    ws.freeze_panes = "A4"
