"""
json_reader.py — Parse EXFO FastReporter JSON exports.

The EXFO JSON format contains more data than the Bellcore SOR:
  * Base64-encoded trace samples (same 35,256 samples as SOR for 15s acq)
  * Events with LSA marker positions (PositionMarker_a/A/B/b) per event
  * Per-section fiber attenuation (slope) and cumulative loss
  * Link results (total span loss, ORL, span length) per direction
  * Full parameter/settings blocks

IMPORTANT: the JSON trace is stored with INVERTED dB convention relative
to the SOR format.  When computing splice loss via LSA on the JSON trace,
the result must be negated to match EXFO's reported positive-for-loss sign.
This is handled by `measure_grey_loss_from_json` below.

Output format
-------------
`parse_otdr_json` returns a dict with the same top-level structure the
splice report script expects from `sor_reader324802a.parse_sor_full`,
plus JSON-only extras.

Public API
----------
    parse_otdr_json(filepath) -> dict
    measure_grey_loss_from_json(json_data, splice_km) -> float
    find_json_file(directory, fiber_num, prefix) -> str | None
    read_span_identifiers(directory) -> dict | None
    span_site_names(dir_a, dir_b) -> (str, str) | None
    read_panel_pigtails(directory, window_m, nm) -> {fiber: dict}
"""
from __future__ import annotations
import base64
import glob
import json
import os
import re
from typing import Any, Optional

import numpy as np


# ═══════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════

def _f(x: Any) -> Optional[float]:
    """Safe float conversion tolerant of comma separators, NaN, empty strings."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace(',', '').strip()
    if s == '' or s.lower() == 'nan':
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ═══════════════════════════════════════════════════════════════════════
#  JSON loader
# ═══════════════════════════════════════════════════════════════════════

def parse_otdr_json(filepath: str) -> dict:
    """Parse an EXFO FastReporter OTDR JSON and return a normalized dict.

    The returned dict matches the shape the splice report script expects
    from the SOR parser, plus JSON-only extras (`_json_markers`, etc.).

    Fields produced:
        filename, filepath
        events          : list of event dicts (dist_km, splice_loss, reflection,
                          slope, type, is_reflective, is_end, number,
                          time_of_travel, _json_markers, _json_cumloss, ...)
        full_trace      : np.ndarray (dB, INVERTED vs SOR convention — DO NOT
                          use directly for display without understanding sign)
        full_points     : int
        num_points      : int
        acq_range       : int (for compatibility with SOR sample-spacing calc)
        ior             : float
        exfo_calibration: dict of calibration parameters
        exfo_sampling_period: float
        # JSON-only extras:
        _json_resolution_m    : sample spacing in meters (use this, not acq_range)
        _json_first_pos_m     : distance (m) of sample index 0 (negative = before span start)
        _json_span_m          : total fiber length in meters
        _json_total_loss_db   : total span loss in dB
        _json_pulse_ns        : OTDR pulse width (ns)
        _json_wavelength_nm   : test wavelength (nm)
        _json_launch_length_m : launch fiber length (m)
    """
    # utf-8-sig: OTDR JSON is UTF-8 per spec and vendor exporters often prepend
    # a BOM.  Without an explicit encoding Windows defaults to cp1252 and
    # crashes on any non-ASCII byte (location/vendor strings, °/µ); plain utf-8
    # would still choke on a leading BOM.  Mac never hits this (defaults UTF-8).
    with open(filepath, encoding="utf-8-sig") as fh:
        data = json.load(fh)

    m_list = data.get('Measurement', {}).get('OtdrMeasurements', [])
    if not m_list:
        raise ValueError(f"No OtdrMeasurements in {filepath}")
    m = m_list[0]

    dp = m.get('DataPoints', {})
    params = m.get('Parameters', {})
    results = m.get('Results', {})

    # Decode trace data
    points_b64 = dp.get('Points', '')
    raw_bytes = base64.b64decode(points_b64) if points_b64 else b''
    full_trace = np.frombuffer(raw_bytes, dtype='<u2').astype(float) / 1000.0

    n_points = int(_f(dp.get('NumberOfPoints')) or len(full_trace))
    resolution_m = _f(dp.get('Resolution')) or 2.5493
    first_pos_m = _f(dp.get('FirstPointPosition')) or 0.0
    span_m = _f(results.get('Length')) or 0.0
    total_loss_db = _f(results.get('AveragedLoss')) or 0.0
    pulse_ns = _f(params.get('Pulse')) or 500.0
    launch_m = _f(params.get('LaunchFiberLength')) or 0.0

    # ── Acquisition-audit fields (model / serial / timestamp / duration) ──
    # These were added for the report's "Acquisition Parameters" sheet.
    # All optional — empty string / None when absent.
    hardware = data.get('Hardware') or {}
    unit_a = hardware.get('UnitA') or hardware.get('unitA') or {}
    otdr_model  = str(unit_a.get('ModelName')  or unit_a.get('modelName')  or '')
    otdr_serial = str(unit_a.get('SerialNumber') or unit_a.get('serialNumber') or '')

    # TestDateTime can be top-level or inside the OtdrMeasurements entry.
    # Stored as ISO-8601 (e.g. '2025-05-21T15:32:11Z') — convert to Unix
    # epoch (UTC) for the audit's calendar-day bucketing.
    test_dt_str = data.get('TestDateTime') or m.get('TestDateTime') or ''
    date_time_epoch = 0
    if test_dt_str:
        import datetime as _dt
        try:
            s = test_dt_str.rstrip('Z')
            # Python <3.11 doesn't accept 'Z' on fromisoformat — chop it.
            dt = _dt.datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            date_time_epoch = int(dt.timestamp())
        except (ValueError, TypeError):
            date_time_epoch = 0

    # Averaging — Parameters.Duration is ISO-8601 (e.g. 'PT15S').
    duration_sec = None
    dur_raw = params.get('Duration') or params.get('duration')
    if isinstance(dur_raw, (int, float)):
        duration_sec = float(dur_raw)
    elif isinstance(dur_raw, str) and dur_raw:
        # Cheap ISO-8601 parse: PT<sec>S or PT<min>M<sec>S — enough for
        # OTDR durations which are always sub-minute / sub-hour.
        import re as _re
        m_match = _re.match(r'^PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?$', dur_raw)
        if m_match:
            h, mn, s = m_match.groups(default='0')
            try:
                duration_sec = int(h) * 3600 + int(mn) * 60 + float(s)
            except (TypeError, ValueError):
                duration_sec = None

    # Wavelength may live under different keys
    wavelength_nm = 1550.0
    # `.get('Results', [{}])` used to return a TRUTHY [{}] when LinkResults was
    # absent, so the per-measurement Wavelength fallback (else branch) was dead
    # code and every LinkResults-less file silently defaulted to 1550 nm — the
    # boss Acquisition sheet then mislabels 1625/1310 runs.  Fall through instead.
    link_results = (data.get('LinkResults') or {}).get('Results') or []
    if link_results:
        wavelength_nm = _f(link_results[0].get('Wavelength')) or 1550.0
    else:
        wl_list = m.get('Wavelength')
        if wl_list:
            wavelength_nm = _f(wl_list) or 1550.0

    # Build events list in the SOR-compatible format.
    # JSON events include a 'Launch Level' reference event at position
    # ≈ -launch_m AND a 'SpanStart' event at position 0 (the actual
    # launch CONNECTOR, with real Loss and Reflectance values).  We
    # skip the LaunchLevel reference because its Loss is always NaN,
    # leaving the SpanStart event as events[0] in the normalized list —
    # that's the launch connector that the launch-issue detector reads.
    events = []
    for i, ev in enumerate(m.get('Events', [])):
        pos_m = _f(ev.get('Position'))
        if pos_m is None:
            continue
        if pos_m < -100:    # skip Launch Level reference marker
            continue

        dist_km = pos_m / 1000.0
        loss = _f(ev.get('Loss')) or 0.0
        reflectance = _f(ev.get('Reflectance')) or 0.0
        # Use TypeCode not Type — "Non-Reflective" contains "reflect" as a substring,
        # which would give a false positive on simple substring check of Type.
        type_code_raw = str(ev.get('TypeCode') or '')
        type_raw = str(ev.get('Type') or '').strip().lower()
        is_reflective = (type_code_raw == 'Reflection') or type_raw == 'reflective'
        status = str(ev.get('Status') or '')
        is_end = ('EndOfFiber' in status) or ('SpanEnd' in status)

        # Per-section attenuation (fiber slope) comes from PreviousFiberSection
        prev_sec = ev.get('PreviousFiberSection') or {}
        slope = _f(prev_sec.get('Attenuation')) or 0.0

        type_code = str(ev.get('TypeCode') or '')
        if is_end:
            type_str = '1E9999LS' if is_reflective else '0E9999LS'
        elif is_reflective:
            type_str = '1F9999LS'
        else:
            type_str = '0F9999LS'

        events.append({
            'number': i,
            'time_of_travel': int(round(pos_m * 2 * 1.4682 / 0.2998e-3 / 1000)),
            'dist_km': dist_km,
            'splice_loss': loss,
            'reflection': reflectance,
            'slope': slope,
            'type': type_str,
            'is_reflective': is_reflective,
            'is_end': is_end,
            # JSON-only extras preserved on each event for downstream use
            '_json_markers': ev.get('Markers') or {},
            '_json_cumloss': _f(ev.get('CumulLoss')),
            '_json_type_code': type_code,
            '_json_status': status,
            '_json_position_m': pos_m,
            '_json_prev_section': prev_sec,
        })

    # ── EndOfFiber sanity check ────────────────────────────────
    # EXFO's auto-detector occasionally tags a high-loss SPLICE event
    # (Loss = NaN, Type = Non-Reflective) as 'EndOfFiber, SpanEnd' even
    # though the trace continues past it with usable backscatter.  Real
    # fiber endpoints are nearly always reflective (the cleaved facet
    # or the receive-fiber connector).  When we find an is_end event
    # that is non-reflective AND a reflective event follows it later
    # in the events list, demote the mis-flagged is_end and promote
    # the trailing reflective event to is_end so b_span ends up at the
    # actual far-end reflection.  Without this fix, fibers like F841
    # (mis-flagged at B_km 80.45 when the real endpoint is at B_km
    # 100.56) get an artificially short b_span and B-fill misses 20 km
    # of recoverable splices.
    for i, ev in enumerate(events):
        if not ev['is_end']:
            continue
        if ev.get('is_reflective'):
            continue   # a reflective is_end is plausibly real — don't touch it
        # Non-reflective is_end — look ahead for a real reflective end
        for j in range(i + 1, len(events)):
            if events[j].get('is_reflective'):
                events[j]['is_end'] = True
                events[j]['_was_promoted_endoffiber'] = True
                ev['is_end'] = False
                ev['_was_demoted_endoffiber'] = True
                break

    # Calibration block built from JSON parameters
    calibration = {
        'NominalPulseWidth': pulse_ns * 1e-9 if pulse_ns else 5e-7,
        'CalibratedPulseWidth': pulse_ns * 1e-9 if pulse_ns else 5e-7,
        'SpansLoss': total_loss_db,
        'SpansLength': span_m,
    }

    result = {
        'filename': os.path.basename(filepath),
        'filepath': filepath,
        'events': events,
        'full_trace': full_trace,
        'full_points': n_points,
        'num_points': n_points,
        'acq_range': 1250000,  # typical 90km range in old SOR units; not used when _json_resolution_m present
        'ior': 1.4682,  # standard for SMF at 1550nm; JSON doesn't always expose IOR directly
        'exfo_calibration': calibration,
        'exfo_sampling_period': 2 * resolution_m * 1.4682 / 2.998e8,  # back-compute
        # Acquisition-audit fields (mirror the SOR parser's keys so the
        # audit module can treat both formats uniformly).
        'date_time':   date_time_epoch,
        'wavelength':  wavelength_nm,                  # nm
        'duration_sec': duration_sec,                  # seconds, from PT…S
        'otdr_model':  otdr_model,
        'otdr_serial': otdr_serial,
        # JSON extras
        '_json_resolution_m': resolution_m,
        '_json_first_pos_m': first_pos_m,
        '_json_span_m': span_m,
        '_json_total_loss_db': total_loss_db,
        '_json_pulse_ns': pulse_ns,
        '_json_wavelength_nm': wavelength_nm,
        '_json_launch_length_m': launch_m,
    }
    return result


# ═══════════════════════════════════════════════════════════════════════
#  Grey-loss LSA measurement from JSON trace
# ═══════════════════════════════════════════════════════════════════════

def measure_grey_loss_from_json(
    json_data: dict,
    splice_km: float,
    outer_m: float = 5000,
    inner_m: float = 60,
    min_valid_samples: int = 10,
    saturation_threshold: float = 63.5,
    min_value: float = 0.5,
) -> Optional[float]:
    """Measure splice loss at a known position from the JSON trace using
    EXFO-style wide-window LSA.  Returns the loss in dB (positive = loss).

    Algorithm:
      1. Place outer markers at ±outer_m from the splice, clamped so they
         don't cross into adjacent event regions (80m buffer past each).
      2. Place inner markers at ±inner_m (the LSA dead zone).
      3. Fit an OLS line to samples in [outer_a..inner_a] and another to
         samples in [inner_b..outer_b].
      4. Extrapolate both lines to the splice position.
      5. Loss = (after_level − before_level), negated because JSON trace
         uses inverted dB convention vs the SOR format.

    Returns None if there aren't enough valid samples on either side.
    """
    trace = json_data['full_trace']
    res = json_data['_json_resolution_m']
    first_pos = json_data['_json_first_pos_m']

    splice_m = splice_km * 1000.0

    # Neighbour clamping: find closest event before and after this position.
    # We INCLUDE is_end events in the clamping so the "after" window can't
    # reach into the EOF connector reflection zone.  Skipping end events
    # used to leave the LSA fit unbounded near the far end of a trace,
    # which inflated the grey reading by ~1 dB on every near-end splice
    # because the polynomial fit absorbed the EOF connector spike.
    prev_m = None
    next_m = None
    for e in json_data['events']:
        ep = e['dist_km'] * 1000.0
        if ep < splice_m - 10:
            prev_m = ep if prev_m is None else max(prev_m, ep)
        elif ep > splice_m + 10:
            next_m = ep if next_m is None else min(next_m, ep)

    outer_a_m = splice_m - outer_m
    outer_b_m = splice_m + outer_m
    if prev_m is not None:
        outer_a_m = max(outer_a_m, prev_m + 80)
    if next_m is not None:
        outer_b_m = min(outer_b_m, next_m - 80)

    # Sample indices
    oa = max(0, int((outer_a_m - first_pos) / res))
    ia = int((splice_m - inner_m - first_pos) / res)
    ib = int((splice_m + inner_m - first_pos) / res)
    ob = min(len(trace) - 1, int((outer_b_m - first_pos) / res))

    if ia - oa < min_valid_samples or ob - ib < min_valid_samples:
        return None

    before = trace[oa:ia]
    after = trace[ib:ob]
    mb = (before > min_value) & (before < saturation_threshold)
    ma = (after > min_value) & (after < saturation_threshold)
    if mb.sum() < min_valid_samples or ma.sum() < min_valid_samples:
        return None

    x_b = np.arange(oa, ia)[mb].astype(float)
    x_a = np.arange(ib, ob)[ma].astype(float)
    y_b = before[mb].astype(float)
    y_a = after[ma].astype(float)

    cb = np.polyfit(x_b, y_b, 1)
    ca = np.polyfit(x_a, y_a, 1)

    splice_idx = (splice_m - first_pos) / res
    raw = float(np.polyval(ca, splice_idx) - np.polyval(cb, splice_idx))
    return -raw  # JSON trace sign is inverted vs SOR


# ═══════════════════════════════════════════════════════════════════════
#  File discovery helpers
# ═══════════════════════════════════════════════════════════════════════

def find_json_file(directory: str, fiber_num: int,
                   prefix: Optional[str] = None) -> Optional[str]:
    """Locate the JSON file for a fiber number within `directory`.

    Handles:
      - Prefix auto-detection (first alphabetic prefix in the directory's files)
      - Zero-padded (F0001) vs unpadded (F1) fiber numbers
      - Optional trailing space before ".json" (seen in real exports)
    """
    if not os.path.isdir(directory):
        return None

    # Infer prefix if not provided: take the first alphabetic chunk of any .json
    if prefix is None:
        any_json = glob.glob(os.path.join(directory, "*.json"))
        if not any_json:
            return None
        base = os.path.basename(any_json[0])
        alpha = ''
        for c in base:
            if c.isalpha():
                alpha += c
            else:
                break
        if not alpha:
            return None
        prefix = alpha

    patterns = [
        f"{prefix}{fiber_num}.json",
        f"{prefix}{fiber_num} .json",
        f"{prefix}{fiber_num:04d}.json",
        f"{prefix}{fiber_num:04d} .json",
        f"{prefix}{fiber_num:03d}.json",
        f"{prefix}{fiber_num:03d} .json",
    ]
    for p in patterns:
        path = os.path.join(directory, p)
        if os.path.exists(path):
            return path
    # Fallback: glob
    for p in [f"{prefix}{fiber_num}.json", f"{prefix}{fiber_num} .json"]:
        matches = glob.glob(os.path.join(directory, p))
        if matches:
            return matches[0]
    return None


def load_all_json(directory: str) -> dict[int, dict]:
    """Load every JSON file in a directory, keyed by fiber number extracted
    from the filename."""
    results: dict[int, dict] = {}
    if not os.path.isdir(directory):
        return results
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        base = os.path.basename(path)
        # Strip prefix (non-digit letters at start) and trailing " .json"
        digits = ''
        started = False
        for c in base:
            if c.isdigit():
                started = True
                digits += c
            elif started:
                break
        if not digits:
            continue
        fnum = int(digits)
        try:
            results[fnum] = parse_otdr_json(path)
        except Exception as exc:  # pragma: no cover - robust to bad files
            print(f"  WARN: failed to parse {base}: {exc}")
    return results


# ═══════════════════════════════════════════════════════════════════════
#  STEP 9 — Who the two ends ARE, read out of the files themselves
#
#  An iOLM job pushed through EXFO Exchange carries the customer's own
#  job config in every measurement: the cable ID, the A-end and Z-end
#  site codes, and the segment's two town names.  That is the only
#  trustworthy statement of which end is A.
#
#  The FOLDER is not.  On AWS / IIG MT.1085 span 27 the SharePoint folder
#  reads "Lavina, MT to Rapelje, MT" while every file in it reads
#  BIL400 (Rapelje) -> RPX400 (Lavina): the folder has the two ends
#  backwards, and a report built from the folder name would label every
#  column with the wrong site (NCT, 2026-09-12).
# ═══════════════════════════════════════════════════════════════════════

#  How many measurements to read before trusting what they say.  They are
#  small reads and a span carries hundreds; a dozen is enough to catch a
#  folder holding two different cables, which is the failure this guards.
_ID_SAMPLE = 12


def _identifier_map(brief: dict) -> dict:
    """{Name: Value} for the measurement's Identifiers block."""
    out = {}
    for item in (brief.get("Identifiers") or []):
        name = str(item.get("Name") or "").strip()
        if name:
            out[name] = str(item.get("Value") or "").strip()
    return out


def _loc_code(value: str) -> str:
    """'LOC=BIL400|FTP=42|Room=B|Rack=204' -> 'BIL400'."""
    for part in (value or "").split("|"):
        part = part.strip()
        if part.upper().startswith("LOC="):
            return part[4:].strip()
    return ""


def _segment_towns(value: str):
    """'Project=...|Span=Span 27|Segment=Rapelje, MT to Lavina, MT'
    -> ('Rapelje', 'Lavina').

    Read the `Segment=` field specifically and never the whole string: the
    Project field carries its own ' to ' ("Lynnwood to Forsyth") and would
    hand back the wrong pair.

    Two separators are in the wild, both from the same customer's job
    configs: span 27 writes "Rapelje, MT to Lavina, MT" and span 25 writes
    "Clyde Park, MT - Big Timber, MT".  Accept either, and only when it
    splits the field in exactly two -- a field that splits three ways is
    not a pair of towns and names nothing."""
    seg = ""
    for part in (value or "").split("|"):
        part = part.strip()
        if part.lower().startswith("segment="):
            seg = part[8:].strip()
    if not seg:
        return ("", "")
    for sep in (" to ", " - ", "\u2013", "\u2014"):
        halves = seg.split(sep)
        if len(halves) == 2:
            break
    else:
        return ("", "")
    # "Rapelje, MT" -> "Rapelje"; a town with no state stays as it is.
    return tuple(h.split(",")[0].strip() for h in halves)


def _span_field(value: str, key: str) -> str:
    for part in (value or "").split("|"):
        part = part.strip()
        if part.lower().startswith(key.lower() + "="):
            return part[len(key) + 1:].strip()
    return ""


def read_span_identifiers(directory: str, sample: int = _ID_SAMPLE):
    """The span's own account of itself, or None when the files do not agree.

    Returns a dict:
        cable_id, a_code, z_code, a_town, z_town, span, project,
        direction ('AB' | 'BA' | ''), n_read

    Returns None — meaning "fall back to whatever the tech typed" — unless
    every measurement read agrees AND three independent statements of the
    A end line up: the Cable ID's leading code, the `A End` identifier's
    LOC, and the first town of `Segment`.  A job not pushed through a
    controlled config carries whatever the tech keyed into the unit, which
    can be blank or inconsistent, and that must never silently name a
    report (NCT, 2026-09-12)."""
    if not directory or not os.path.isdir(directory):
        return None
    files = sorted(glob.glob(os.path.join(directory, "*.json")))[:max(1, sample)]
    if not files:
        return None

    seen = set()
    found = None
    n_read = 0
    for path in files:
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                brief = (json.load(fh) or {}).get("brief") or {}
        except Exception:
            continue          # a damaged sidecar must not abort a report
        ids = _identifier_map(brief)
        cable_id = ids.get("Cable ID", "")
        a_code = _loc_code(ids.get("A End", ""))
        z_code = _loc_code(ids.get("Z End", ""))
        a_town, z_town = _segment_towns(ids.get("Segment", ""))
        if not (cable_id and a_code and z_code and a_town and z_town):
            return None
        seen.add((cable_id, a_code, z_code, a_town, z_town))
        if len(seen) > 1:
            return None       # two cables in one folder — name nothing
        n_read += 1
        if found is None:
            found = {
                "cable_id": cable_id,
                "a_code": a_code, "z_code": z_code,
                "a_town": a_town, "z_town": z_town,
                "span": _span_field(ids.get("Segment", ""), "Span"),
                "project": _span_field(ids.get("Segment", ""), "Project"),
                "direction": str((brief.get("FiberInformation") or {})
                                 .get("LocationDirection") or "").strip().upper(),
            }
    if found is None or not n_read:
        return None

    # The A end has to be the A end three different ways.  "BIL400-RPX400-
    # 0432F-01" leads with the A code, `A End` names it, and `Segment` lists
    # its town first; if those disagree the config is not one we can read.
    head = found["cable_id"].split("-")
    if len(head) < 2 or head[0] != found["a_code"] or head[1] != found["z_code"]:
        return None
    found["n_read"] = n_read
    return found


def span_site_names(dir_a: str, dir_b: str = None):
    """('Rapelje BIL400', 'Lavina RPX400') for the span in these folders, or
    None when the files cannot say so.

    Town and code together, the way the customer identifies a site: the code
    is what their records key on and the town is what makes it readable
    (NCT, 2026-09-12)."""
    info = read_span_identifiers(dir_a) or read_span_identifiers(dir_b)
    if not info:
        return None
    return (f"{info['a_town']} {info['a_code']}",
            f"{info['z_town']} {info['z_code']}")


# ═══════════════════════════════════════════════════════════════════════
#  STEP 10 — The pigtail splice behind a panel
#
#  A panel port is two elements, not one: the mated connector at 0 m and,
#  a few metres behind it, the splice joining the pigtail to the cable.
#  The OTDR cannot always separate them -- at 275 ns the two sit inside one
#  pulse -- but the iOLM's own element list does, and types them:
#
#      Position 0.0   Connector  Loss 0.047  Verdict Pass
#      Position 3.8   Splice     Loss 0.455  Verdict Fail
#
#  which is the AWS / IIG MT.1085 customer's "near 0.047 / pigtail 0.455"
#  on span 17 fiber 397, to the millidecibel.  Their rule going forward
#  (NCT, 2026-09-12): the pigtail is graded as a SPLICE, against the splice
#  limit, "with the connector graded separately".
#
#  Read from the sidecar and nowhere else.  The .sor sometimes carries the
#  pigtail as a second event at 0 m and sometimes merges it into the
#  connector, fiber by fiber, so summing whatever sits at the port would
#  move a panel figure on nothing more physical than whether the firmware
#  happened to split the events.
# ═══════════════════════════════════════════════════════════════════════

_FIBER_IN_NAME = re.compile(r'[-_](\d{3,4})(?=[-_.])')


def _element_loss(element: dict, nm: str) -> Optional[float]:
    """The element's loss at one wavelength, or None."""
    for res in (element.get("Results") or []):
        if str(res.get("Wavelength")).strip() == nm:
            return _f(res.get("Loss"))
    return None


def _fiber_from_name(filename: str) -> Optional[int]:
    hits = _FIBER_IN_NAME.findall(os.path.basename(filename))
    return int(hits[-1]) if hits else None


def read_panel_pigtails(directory: str, window_m: float = 50.0,
                        nm: float = 1550.0) -> dict:
    """{fiber_num: {'position_m', 'loss', 'verdict'}} for every fiber in this
    directory whose sidecar resolves a splice just behind the panel.

    The pigtail is the FIRST element typed `Splice` after the connector at
    0 m and within `window_m` of it.  Fibers whose sidecar shows no such
    element are simply absent from the result: on the spans measured, only
    about a quarter of fibers resolve one, and a fiber that does not resolve
    it has no pigtail reading at all rather than a zero.

    Never raises: a folder with no sidecars, or a damaged one, returns what
    it could read.  The panel connector is graded with or without this."""
    out = {}
    if not directory or not os.path.isdir(directory) or window_m <= 0:
        return out
    want_nm = ("%g" % float(nm))
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        fiber = _fiber_from_name(path)
        if fiber is None:
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                elements = (((json.load(fh) or {}).get("brief") or {})
                            .get("Measurement") or {}).get("Elements") or []
        except Exception:
            continue
        seen_port = False
        for el in elements:
            pos = _f(el.get("Position"))
            if pos is None:
                continue
            kind = str(el.get("Type") or "").strip()
            # The launch reel's own connector sits at a NEGATIVE position;
            # the panel is the one at zero.
            if kind == "Connector" and abs(pos) < 0.5:
                seen_port = True
                continue
            if not seen_port:
                continue
            if pos <= 0.0 or pos > float(window_m):
                break          # past the window: this fiber has no pigtail
            if kind == "Splice":
                loss = _element_loss(el, want_nm)
                if loss is not None:
                    out[fiber] = {"position_m": pos, "loss": loss,
                                  "verdict": str(el.get("ElementVerdict") or "")}
                break
    return out

