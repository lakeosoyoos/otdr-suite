#!/usr/bin/env python3
"""Trace server for the OTDR Suite viewer page.

A small HTTP server that parses OTDR SOR/JSON files on demand and serves
trace + event JSON to the canvas viewer (viewer.html).  Designed to run as
a background daemon thread *inside* the Streamlit hub process so the whole
suite ships as one launcher.

The A/B folders are held in a module-level CONFIG dict that the hub writes
when the user picks folders in the sidebar — same process, shared state, no
second config channel needed.  The gates the current report ran at ride the
same dict (set_thresholds), which is what lets them survive the cell-click
URL nav that wipes Streamlit's session_state.

Endpoints:
  GET /                          -> viewer.html
  GET /api/list                  -> {dir_a, dir_b, fibers_a:[...], fibers_b:[...]}
  GET /api/trace?dir=a&fiber=64  -> {dist_km, trace_db, events, ...}
  GET /api/traces?dir=a&fibers=1-1152&maxpts=2000
                                 -> {traces:[...], missing:[...]}  (bulk overview)

Trace sign convention served to the browser:
  Higher value = stronger signal (descending = loss), FastReporter-style.
  SOR DataPts is ascending-loss, so we negate.  JSON full_trace is already
  descending-signal, served as-is.
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import struct
import threading
import zlib
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import numpy as np

# These resolve from the viewer/ package dir, which the hub puts on sys.path.
from sor_reader324802a import parse_sor_full, _sor_ior_from_events, _sor_first_pos_m, parse_genparams
from sor_reader324802a import _IOR_SANE_MIN, _IOR_SANE_MAX
from json_reader import parse_otdr_json

# Stdlib-only Slack reporting (repo root is on sys.path when the hub imports us).
try:
    from error_report import report_error
except Exception:                                  # standalone/dev — best-effort
    def report_error(*a, **k):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
VIEWER_HTML = os.path.join(HERE, 'viewer.html')

# Shared, hub-writable configuration.  No pre-seeded sample folders: a hardcoded
# dev path (a Mac Downloads folder) is meaningless on a tech's Windows box and
# only produced confusion (and auto-loaded an unreadable path).  The Viewer opens
# with an explicit "pick / paste a folder" prompt; the hub's Load span or the
# sidebar folder boxes set these.
CONFIG = {'dir_a': None, 'dir_b': None,
          # Gates the CURRENT report ran at (see engine_thresholds).  None =
          # no report has pointed us anywhere, so the engine baseline stands.
          'thresholds': None}

_server = None
_thread = None
_started_port = None


# ─── Fiber-number extraction ────────────────────────────────────────────
_FIBER_NUM_RE = re.compile(r'(\d{3,4})_\d{3,4}\b')

def extract_fiber_num(fn):
    """Extract fiber number from a SOR/JSON/TRC filename.

    Handles every naming pattern that has shown up on the user's disk
    after a survey of ~38k real files across 11 cable codes (see
    Project Memory note from 2026-06-13):

      ``LAGDUR0001.sor``                  -> 1     (run after a prefix)
      ``Norsea001_1550.sor``              -> 1     (strip _<wavelength>)
      ``Seattle to Spokane d.0431.sor``   -> 431   (rightmost digit run)
      ``20260520_LAGDUR0001.sor``         -> 1     (date prefix ignored)
      ``DURSAN001_1550 .json``            -> 1     (EXFO trailing-space)
      ``VERSLK001_131015501625 .json``    -> 1     (multi-λ suffix)
      ``TEST0001_155016251310.trc``       -> 1     (multi-λ TRC)
      ``CHC-HCH-LS-089.trc``              -> 89    (dashed long-shot)
      ``._STRROM0001_1550.sor``           -> None  (macOS AppleDouble)

    Rule:
      1. Strip the extension AND any leading "._" (AppleDouble metadata
         that lands next to real files in zips extracted on Mac).
      2. Right-strip whitespace (EXFO FastReporter exports JSON with a
         trailing space between the wavelength code and ``.json``).
      3. Strip one OR MORE concatenated trailing wavelength codes,
         e.g. ``_131015501625`` is three wavelengths jammed together.
      4. Take the RIGHTMOST run of digits — fiber numbers are
         conventionally last after any cable / span / date prefix.
      5. Tie-panel filenames butt a 1-digit ILA/panel suffix straight
         against a 4-digit zero-padded port with NO delimiter, e.g.
         ``PTL1PTL60145`` (ILA1→ILA6, port 0145) or ``DNW1DNW50148``
         (port 0148).  The rightmost run then reads as one number
         (60145) instead of the real port (145) and every fiber lands
         far past any real cable → the stray-fiber guard drops them all
         and aborts.  When the run ends in a 4-char zero-padded field
         (``0NNN``) with extra digits jammed in front, trust the padded
         port.  A genuine 4-digit fiber (1050, 1152) has no leading
         zero, so it is left untouched.

    Returns None when no fiber number can be extracted (which causes
    ``_load_dir`` to skip the file rather than silently overwrite a
    valid fiber).
    """
    # AppleDouble sidecars created by macOS zip-extractors mirror the
    # real filename with a "._" prefix; they're not OTDR data.  Skip
    # them BEFORE the digit walk so they never collide with the real
    # fiber that follows.
    if os.path.basename(fn).startswith("._"):
        return None
    stem, _ = os.path.splitext(fn)
    stem = stem.rstrip()
    # Strip ONE OR MORE concatenated trailing wavelength codes.  The
    # multi-λ EXFO exports we see in the field write all three
    # wavelengths jammed together: ``_131015501625``.  Without the +
    # quantifier the whole concatenation reads as one giant fiber
    # number (~131_500_000_000) and every fiber collides.
    stem = re.sub(
        r'[\s_\-.](?:850|1300|1310|1383|1490|1550|1577|1625|1650)+$', '', stem)
    matches = re.findall(r'\d+', stem)
    if not matches:
        return None
    run = matches[-1]
    # Tie-panel filenames jam a 1-digit ILA/panel suffix onto the 4-digit
    # zero-padded port (``PTL1PTL60145`` → run ``60145``).  If the run ends
    # in a zero-padded 4-char field with digits in front of it, the padded
    # field is the real port; the prefix is the ILA suffix.  (A real 4-digit
    # fiber like 1050 has no leading zero, so ``0\d{3}$`` won't match it.)
    m = re.search(r'0\d{3}$', run)
    if m and len(m.group()) < len(run):
        return int(m.group())
    return int(run)


# ─── Engine thresholds, read from the engine's SOURCE ───────────────────
#
# The Viewer has to reach the same verdict as the report the tech clicked
# through from, so it must gate on the ENGINE's numbers, not on numbers
# retyped here.  It cannot import the engine — viewer/ and splicereport/
# carry different `sor_reader324802a` copies that collide on sys.path — so
# the constants are read out of the source, the same trick app.py uses.
#
# Defaults match the engine as of this writing and exist only so a viewer
# shipped without the engine beside it still runs; the regex is the truth.
_ENGINE_SRC = os.path.join(os.path.dirname(HERE), 'splicereport',
                           'splicereportmatchexfo.py')
_THRESHOLD_DEFAULTS = {'reburn': 0.160, 'uni_bend': 0.100, 'single_dir': 0.250}
_THRESHOLD_NAMES = {'reburn': 'REBURN_THRESHOLD',
                    'uni_bend': 'UNI_BEND_THRESHOLD',
                    'single_dir': 'SINGLE_DIR_THRESHOLD'}
_THRESHOLD_CACHE = {}


def _source_thresholds():
    """The engine's BASELINE gates, parsed out of its source (cached).

    This is the whole story only for a run nobody overrode.  Reading the
    source is deliberately not enough on its own: it sees the constant as
    typed, and a customer profile mutates that constant in the engine
    subprocess at run time.  See engine_thresholds."""
    if _THRESHOLD_CACHE:
        return dict(_THRESHOLD_CACHE)
    out = dict(_THRESHOLD_DEFAULTS)
    try:
        with open(_ENGINE_SRC, encoding='utf-8') as fh:
            src = fh.read()
        for key, name in _THRESHOLD_NAMES.items():
            m = re.search(r'^%s\s*=\s*([0-9.]+)' % name, src, re.M)
            if m:
                out[key] = float(m.group(1))
    except OSError:
        pass                       # engine not beside us — defaults stand
    _THRESHOLD_CACHE.update(out)
    return dict(out)


def engine_thresholds():
    """{'reburn': 0.16, 'uni_bend': 0.25, 'single_dir': 0.25} — the gates the
    report on screen actually ran at.

    Source-parsed baseline, then whatever the CURRENT report ran at on top.
    The source parse alone was a half-truth: app.py's CUSTOMER_PROFILES
    rewrite these constants per customer (Lumen 0.120, Zayo 0.200, AWS / IIG
    0.200) by pushing --overrides into the engine subprocess, and none of
    that reached here.  The Viewer therefore judged every run at the 0.160
    baseline, so under IIG a 0.17 dB cell was unflagged in the report and
    over-threshold in the Viewer — a 40 mdB band where the two screens
    contradicted each other, which is exactly what reading the engine's
    numbers instead of retyping them was supposed to prevent.

    The override arrives through set_thresholds, and it is the value the
    engine ECHOED BACK after applying the panel (see run_splicereport's
    manifest), not the value the hub asked for: the runner's guards skip an
    override it rejects, so a requested 0 leaves the run at 0.160 and the
    Viewer has to gate at 0.160 too."""
    out = _source_thresholds()
    ov = CONFIG.get('thresholds')
    if isinstance(ov, dict):
        for key, name in _THRESHOLD_NAMES.items():
            try:
                v = float(ov[name])
            except (KeyError, TypeError, ValueError):
                continue           # absent / unusable → keep the baseline
            # Same shape the engine runner demands of an override before it
            # applies one; a gate the engine would not have accepted must not
            # become a gate the Viewer judges by.
            if math.isfinite(v) and v > 0:
                out[key] = v
    return out


def set_thresholds(mapping):
    """Point the Viewer at the gates ONE report ran at, by engine-global name
    ({'REBURN_THRESHOLD': 0.2, ...}) — the `thresholds` block of that run's
    manifest.  None / anything else clears back to the engine baseline.

    Deliberately NOT folded into set_dirs: page_viewer calls set_dirs on
    every rerun from its own sidebar, so a cell click into the in-app Viewer
    tab would immediately clear the gate the click was meant to carry.  The
    two report grids call this alongside their set_dirs, which is also the
    path a restored-from-disk-cache grid takes, so 'Back' from the Viewer
    keeps judging by the report still on screen."""
    CONFIG['thresholds'] = dict(mapping) if isinstance(mapping, dict) else None


def _dir_has_json(d):
    if not d or not os.path.isdir(d):
        return False
    return any(fn.lower().endswith('.json') for fn in os.listdir(d))


# GenParams header read cap: the GenParams block sits in the first KBs of a
# SOR (block map at top).  The rescue must NEVER read whole files — a span
# where every filename needs rescuing would otherwise re-read hundreds of MB
# on every request (single-threaded server → viewer looks dead).
_GENPARAMS_READ_CAP = 262_144

# Folder-listing cache: list_fibers runs on EVERY /api/list and /api/trace
# request; the listing (and any GenParams rescues) only need recomputing when
# the folder actually changes.  Keyed on (dir mtime, entry count).
_LIST_CACHE = {}


def _folder_sig(directory):
    try:
        st = os.stat(directory)
        return (st.st_mtime_ns, len(os.listdir(directory)))
    except OSError:
        return None


def list_fibers(directory):
    """Return sorted [(fiber_num, filename), ...] for a directory.

    The file type is chosen by whichever extension has MORE valid fiber-numbered
    files, NOT by "any .json present".  A single stray .json (an EXFO export, a
    report, or a Secret Sauce pairs_cache.json dropped in the folder) used to
    flip the loader into JSON-only mode and hide a folder full of .sor — the
    viewer then showed the tech "0 fibers" and looked completely dead."""
    if not directory or not os.path.isdir(directory):
        return []
    sig = _folder_sig(directory)
    cached = _LIST_CACHE.get(directory)
    if cached is not None and sig is not None and cached[0] == sig:
        return cached[1]
    try:
        names = os.listdir(directory)
    except OSError:
        # Unreadable folder (permissions / moved / locked): return empty rather
        # than let PermissionError crash the whole Viewer page render.
        return []
    buckets = {'.sor': [], '.json': []}
    for fn in names:
        if fn.startswith('._'):          # AppleDouble files from Mac zips
            continue
        low = fn.lower()
        for ext in ('.sor', '.json'):
            if low.endswith(ext):
                fnum = extract_fiber_num(fn)
                if fnum is None and ext == '.sor':
                    # GenParams rescue (mirrors the Splice Report's identity
                    # rule): the filename gave no fiber number — read the
                    # file's INTERNAL GenParams fiber id so a span the report
                    # can grid is also clickable through to the viewer.
                    try:
                        with open(os.path.join(directory, fn), 'rb') as fh:
                            _head = fh.read(_GENPARAMS_READ_CAP)
                    except OSError:
                        _head = b''
                    gp = parse_genparams(_head) or {}
                    gid = (gp.get('fiber_id') or '').strip()
                    if gid:
                        try:
                            fnum = extract_fiber_num(gid + '.sor')
                        except (ValueError, TypeError):
                            fnum = None
                if fnum is not None:
                    buckets[ext].append((fnum, fn))
                break
    # Prefer JSON when it has AT LEAST AS MANY fiber files as .sor — preserving
    # the original "JSON is richer, use it when available" behavior for a real
    # export folder (equal counts → JSON) — but a MINORITY stray .json can no
    # longer outvote a folder full of .sor, so it can't zero the list.
    if buckets['.json'] and len(buckets['.json']) >= len(buckets['.sor']):
        out = buckets['.json']
    else:
        out = buckets['.sor']
    # Deterministic pick on duplicate fiber numbers (multi-λ folders hold
    # e.g. Norsea001_1310 + Norsea001_1550 → both fiber 1): stable sort on
    # (fiber, name) so the SAME file is chosen every session, not listdir
    # order.  Consumers building {n: fn} maps then last-win deterministically.
    out.sort(key=lambda t: (t[0], t[1]))
    if sig is not None:
        _LIST_CACHE[directory] = (sig, out)
    return out


# ─── Reel geometry: where the CABLE starts and ends inside a raw trace ──
#
# A shot taken through a launch reel carries ~1 km of the tech's own fiber
# before the cable under test, and (when a receive reel is used) ~1 km after
# it.  Both show up as reflective connector events.  Stacked mode has to know
# where the cable actually begins so that A and B land on the same physical
# metre — see the mirror math in viewer.html.
#
# Presence is decided as a POPULATION fact, never per fiber.  One fiber's
# "reflective event 1 km in" could be a real mid-span connector; 400 fibers
# agreeing to within 50 m is a reel.  This is the same discipline that stopped
# the connector pass reporting 399 of Lumen 432's fibers dark.
# The ITU/telecom windows an OTDR actually fires in.  Used only to label the
# lambda column the way FastReporter does; nothing measures against these.
NOMINAL_WAVELENGTHS_NM = (850, 1300, 1310, 1383, 1490, 1550, 1577, 1625, 1650)
WAVELENGTH_SNAP_NM     = 12    # 1546.0 -> 1550; anything further stays as read

LAUNCH_MAX_KM   = 3.0        # a launch reel is at most this long
TAIL_MAX_KM     = 3.0        # ditto a receive reel
REEL_MIN_KM     = 0.05       # below this it is a bulkhead, not a reel
REEL_TOL_KM     = 0.05       # population agreement window (±50 m)
REEL_MIN_FRAC   = 0.50       # this share of sampled fibers must agree
REEL_SAMPLE     = 12         # fibers measured per folder (cost cap)
# 12, not 40: this runs inside /api/list on a single-threaded server, so it is
# dead time at boot, and each sample is a full parse that also evicts a slot
# from the 64-entry trace cache the tech is about to use.  Twelve fibers spread
# across a folder settle a reel either way — the readings inside one span agree
# to millimetres — and it keeps the measurement to about a fifth of a second.


def _trace_end_km(events):
    end = next((float(e.get('dist_km') or 0.0)
                for e in (events or []) if e.get('is_end')), None)
    return end


def _trace_launch_km(events):
    """This trace's launch-connector position, or None when it has no reel.

    Deliberately the SAME rule as the Splice Report engine's
    `_untrimmed_launch_offset_km`: event 0 is the OTDR port (reflective, with
    a zero time-of-travel) and event 1 is the reel's far connector.  The two
    must agree, because a report grid hands the viewer cell distances already
    shifted by the engine's number — measure it differently here and every
    deep link lands in the wrong place.

    Being positional also disposes of the ~87,594 km time-of-travel artifact
    the viewer's reader still emits: on a trimmed trace that phantom IS event
    0, the port test fails, and the answer is correctly "no reel"."""
    if not events or len(events) < 3:
        return None
    e0, e1 = events[0], events[1]
    if (e0.get('is_reflective') and not e0.get('is_end')
            and e0.get('time_of_travel') == 0
            and e1.get('is_reflective') and not e1.get('is_end')
            and 0.0 < float(e1.get('dist_km') or 0.0) < LAUNCH_MAX_KM):
        return float(e1['dist_km'])
    return None


def _trace_tail_setback_km(events):
    """How far this trace's far connector sits BEFORE its end event, or None.

    On a receive-reel shot the cable's far end is a reflective connector and
    the end event is the reel's own tail; with no reel the two coincide.

    The geometry has to close: reel + cable + reel cannot exceed the trace, and
    what is left over has to be more cable than reel.  Without that check a
    60 m panel jumper reads its own far end as a "receive reel" and the frame
    collapses."""
    end = _trace_end_km(events)
    if end is None:
        return None
    launch = _trace_launch_km(events) or 0.0
    best = None
    for e in events:
        km = float(e.get('dist_km') or 0.0)
        if not e.get('is_reflective') or e.get('is_end') or km >= end or km <= launch:
            continue
        gap = end - km
        if REEL_MIN_KM <= gap <= TAIL_MAX_KM:
            best = gap if best is None else min(best, gap)
    if best is None or (end - launch - best) <= REEL_MIN_KM:
        return None                    # no cable would be left between them
    return best


# ─── Files that carry a DECLARED span ───────────────────────────────────
#
# FastReporter lets a tech mark where the cable starts and stops (Spans by
# Distance, or the events themselves).  When it saves that, it re-bases every
# KeyEvent so the span start is 0 — and leaves the TRACE alone.  Verified by
# diffing a file before and after FR set one: DataPts, FxdParams and SupParams
# byte-identical, only KeyEvents and the proprietary block move.
#
# So such a file has its events in the span frame and its samples in the raw
# frame, about a kilometre apart, and stores NOTHING recording the gap.  Every
# numeric field was searched for it on real production files; FR's own Launch /
# Receive / Absolute lengths appear nowhere.  FR derives them, and so must we.
#
# The two tie-panel folders on disk are exactly this: a 1 km launch reel, a
# ~62 m jumper as the declared span, a 1 km receive reel.
#
# The derivation: in the span frame the LAST event is the far end of the
# receive reel, and the same point measured off the samples is the absolute
# end of fiber.  The difference is where the span start sits in the raw
# acquisition — which is FR's "Launch fiber length".  Checked against FR's own
# dialog on both folders:
#
#     FTH01 tie panel        FR 1.0449 km    ours 1.0449   -0.0 m
#     PTL1PTL6 Reubensville  FR 1.0294 km    ours 1.0287   -0.7 m
#
# Per fiber it is not always that good — FTH01 fiber 010 has seven events and a
# trace that stops early, and comes out a kilometre off — so the folder decides
# by median, the same way it decides a reel.  13 of 14 agreed within 3 m there
# and the odd one is outvoted.
SPAN_EOF_WIN = 40          # samples averaged before looking for the end of fiber


def _trace_eof_km(t):
    """Where the curve itself falls off, from the samples alone.

    The steepest drop of a smoothed trace.  Deliberately not a threshold on the
    noise floor: a single spike in the noise is not an end of fiber, and a
    threshold picks one up several kilometres past the real end."""
    xs = t.get('dist_km'); ys = t.get('trace_db')
    if xs is None or ys is None or len(ys) < 4 * SPAN_EOF_WIN:
        return None
    x = np.asarray(xs, dtype=np.float64)
    y = np.asarray(ys, dtype=np.float64)
    k = np.ones(SPAN_EOF_WIN) / SPAN_EOF_WIN
    ys_s = np.convolve(y, k, mode='same')
    drop = ys_s[SPAN_EOF_WIN:] - ys_s[:-SPAN_EOF_WIN]
    # `drop[i]` measures across i..i+WIN, so the edge it found is in the MIDDLE
    # of that window, not at its start.  Without the half-window the answer is
    # short by WIN/2 samples every time — which is exactly the -1.6 m and -2.3 m
    # this came out against FastReporter's own launch lengths before the shift
    # was added, on traces whose sample pitch is about 0.08 m.
    return float(x[min(int(np.argmin(drop)) + SPAN_EOF_WIN // 2, len(x) - 1)])


def _trace_span_launch_km(t):
    """This fiber's declared-span launch offset, or None.

    A negative event position is the marker: nothing else puts one there, and
    it is precisely what re-basing to a span start produces.

    PREFER WHAT THE FILE SAYS.  GenParams records this offset exactly -- it is
    the number FastReporter's own Spans by Distance dialog shows as "Launch
    fiber length" -- and reading it beats measuring it:

                          stored     measured from the samples
        launch_set        +0.01 m    -13.88 m
        FTH01 tie panel   -0.03 m     +0.13 m
        PTL1PTL6          +0.03 m     -0.67 m

    The measurement below was written first, before the field was found, and it
    stays as the fallback: it is the only thing that can answer for a file that
    carries a declared span with no offset recorded.  None seen -- the two agree
    40 out of 40 on the tie-panel folders -- so this is insurance, not a path
    anything currently takes.
    """
    ev = [float(e.get('dist_km') or 0.0) for e in (t.get('events') or [])]
    if not ev or min(ev) >= -0.001:
        return None                      # no declared span on this fiber
    stored = t.get('user_offset_km')
    if stored:
        return float(stored)
    eof = _trace_eof_km(t)
    if eof is None:
        return None
    return eof - max(ev)


def _median_of(values):
    """Median of the readings that exist, ignoring fibers that have none.

    Matches how the Splice Report aggregates its launch offset (median over
    the non-zero readings) so the two land on the same number."""
    vals = [v for v in values if v is not None]
    return float(np.median(vals)) if vals else None


def _agreed(values, n_sampled):
    """The population's value, or None when the population does not agree.

    Stricter than `_median_of`: a clear majority of ALL sampled fibers must sit
    within REEL_TOL_KM of the median, so fibers with no reading count AGAINST.
    Used for the receive reel, which nothing else cross-checks — one fiber's
    reflective event near the end must not move where a whole B direction gets
    drawn."""
    med = _median_of(values)
    if med is None or n_sampled <= 0:
        return None
    agree = sum(1 for v in values if v is not None and abs(v - med) <= REEL_TOL_KM)
    return med if agree >= REEL_MIN_FRAC * n_sampled else None


def _span_estimate(lengths):
    """The cable's length, from the fibers that actually reach the far end.

    The Splice Report's idiom verbatim — median of the TOP QUARTILE:

        b_span_est = float(np.median(b_eofs[int(len(b_eofs) * 0.75):]))

    Taking the top quarter is what makes it survive breaks.  A fiber that
    snaps at 47 km on a 69 km span has an end event, and it is not the cable's
    end; a plain median over a folder with several such fibers would drag the
    span short and mis-place every B trace drawn against it."""
    vals = sorted(v for v in lengths if v is not None and v > 0)
    if not vals:
        return None
    return float(np.median(vals[int(len(vals) * 0.75):]))


# How far short of the span a fiber may end and still be treated as reaching
# the far end.  Inside this, use the fiber's OWN far connector (it carries that
# fiber's real length, which varies by a few tens of metres across a ribbon);
# beyond it the fiber is broken or short and its end says nothing about where
# the cable ends, so the population's span is the only honest answer.
SHORT_FIBER_TOL_KM = 0.50

_FRAME_CACHE = {}


def frame_facts(directory):
    """{'launch_km': float|None, 'tail_km': float|None} for a folder.

    Cached on the folder signature alongside the listing cache, and measured
    from at most REEL_SAMPLE fibers spread across the folder."""
    if not directory or not os.path.isdir(directory):
        return {'span_launch_km': None, 'launch_km': None, 'tail_km': None,
                'span_km': None, 'cable_end_known': False}
    sig = _folder_sig(directory)
    hit = _FRAME_CACHE.get(directory)
    if hit is not None and sig is not None and hit[0] == sig:
        return hit[1]
    fibers = list_fibers(directory)
    if not fibers:
        return {'span_launch_km': None, 'launch_km': None, 'tail_km': None,
                'span_km': None, 'cable_end_known': False}
    step = max(1, len(fibers) // REEL_SAMPLE)
    sample = fibers[::step][:REEL_SAMPLE]
    loaded = []
    for _fnum, fn in sample:
        try:
            mtime = os.stat(os.path.join(directory, fn)).st_mtime_ns
            t = _load_trace_cached(directory, fn, mtime)
        except OSError:
            continue
        if t:
            loaded.append(t)

    # The declared span comes FIRST, because everything below reads event
    # positions and a span-declared file states them in a different frame.
    # Measure it, agree on it across the folder, and only then look for reels —
    # otherwise the reel rules are reading a frame a kilometre from the one the
    # samples are in.
    span_votes = [_trace_span_launch_km(t) for t in loaded]
    span_launch = _agreed(span_votes, len(loaded))

    launches, tails, lengths, n, ends = [], [], [], 0, 0
    for t in loaded:
        n += 1
        ev = t.get('events')
        if span_launch:
            ev = [dict(e, dist_km=round(e['dist_km'] + span_launch, 4)) for e in (ev or [])]
        launch = _trace_launch_km(ev)
        tail = _trace_tail_setback_km(ev)
        launches.append(launch)
        tails.append(tail)
        end = _trace_end_km(ev)
        if end is not None:
            ends += 1
            lengths.append(end - (tail or 0.0) - (launch or 0.0))
    # Does this folder know where its cable ENDS?  A short shot does not: it is
    # a deliberately truncated near-end acquisition, so its trace simply runs
    # out of range with no end-of-fiber event at all (ELMMILsh / MILELMsh: 0 of
    # 29 fibers have one, last sample 4.98 km on a 67.5 km cable).  Without a
    # cable end there is no way to know where the cable's FAR end sits in this
    # direction's frame, so a B trace from such a folder cannot be mirrored on
    # to an A trace — the two cover opposite ends of the cable and never meet.
    # Saying so beats mirroring about the acquisition range, which is what the
    # end-event fallback silently did and which looks entirely plausible.
    out = {'span_launch_km': span_launch,
           'launch_km': _median_of(launches), 'tail_km': _agreed(tails, n),
           'span_km': _span_estimate(lengths),
           'cable_end_known': bool(n) and ends >= REEL_MIN_FRAC * n}
    if sig is not None:
        _FRAME_CACHE[directory] = (sig, out)
    return out


# ─── The DECLARED span ──────────────────────────────────────────────────
#
# FastReporter does not infer a launch reel; it reads one.  Its Spans by
# Distance dialog holds a Launch / Span / Receive length per measurement, and
# the tech sets it — usually by nominating the events that bound the cable.
#
# Our files carry no such declaration.  Swept over 19 folders, `SpansLength` in
# the EXFO proprietary block equals the trace's end event every time (worst
# difference 2.8 m, which is last-sample versus end-event) and `StartPosition`
# is 0.0 even on spans with a real 1 km reel.  There is nothing to read, which
# is why FR's own dialog reads Launch 0.0000 on them.
#
# So the declaration is ours to make.  This is where it lives: keyed on the
# folder PAIR, in the app's own state directory rather than beside the tech's
# data, and stored as raw-frame kilometres rather than event numbers — a
# re-analysis renumbers events, but the metre a connector sits at does not.
#
# Deliberately readable by something other than the viewer: the engine derives
# the same launch offset independently (`_untrimmed_launch_offset_km`), and
# when a declared span is promoted span-wide it should read THIS, not a second
# copy of it.
SPAN_STORE = os.path.join(os.path.expanduser('~'), '.otdrSuite', 'spans.json')


def _span_key(dir_a, dir_b):
    """Stable, readable key for a folder pair.  Normalised so a trailing
    separator or a different case on Windows does not open a second entry."""
    def norm(d):
        return os.path.normcase(os.path.abspath(d)).rstrip('/\\') if d else ''
    return norm(dir_a) + '|' + norm(dir_b)


def _span_store_read():
    try:
        with open(SPAN_STORE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # A missing or corrupt store is not an error worth surfacing: the
        # viewer simply falls back to measuring the frame, which is what it
        # did before any of this existed.
        return {}


def span_decl(dir_a=None, dir_b=None):
    """{'a': {...}|None, 'b': {...}|None} for a folder pair — the tech's own
    span, or empty when they have not set one."""
    entry = _span_store_read().get(
        _span_key(dir_a or CONFIG['dir_a'], dir_b or CONFIG['dir_b'])) or {}
    return {'a': entry.get('a') or None, 'b': entry.get('b') or None}


def span_decl_set(direction, edge, km, dir_a=None, dir_b=None):
    """Record one edge of one direction's span, or clear that direction.

    `edge` is 'start' | 'end' | 'clear'.  `km` is in that direction's OWN raw
    frame, the frame every event distance is already in.
    """
    if direction not in ('a', 'b'):
        raise ValueError('direction must be a or b')
    if edge not in ('start', 'end', 'clear'):
        raise ValueError("edge must be start, end or clear")
    da = dir_a or CONFIG['dir_a']
    db = dir_b or CONFIG['dir_b']
    store = _span_store_read()
    key = _span_key(da, db)
    entry = store.get(key) or {}
    entry['dir_a'], entry['dir_b'] = da or '', db or ''
    if edge == 'clear':
        entry.pop(direction, None)
    else:
        side = dict(entry.get(direction) or {})
        side[edge + '_km'] = round(float(km), 6)
        entry[direction] = side
    if entry.get('a') or entry.get('b'):
        store[key] = entry
    else:
        store.pop(key, None)          # nothing declared: drop the row entirely
    try:
        os.makedirs(os.path.dirname(SPAN_STORE), exist_ok=True)
        tmp = SPAN_STORE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(store, f, indent=1, sort_keys=True)
        os.replace(tmp, SPAN_STORE)   # atomic: a killed write must not eat the store
    except OSError as e:
        raise RuntimeError('could not write %s: %s' % (SPAN_STORE, e))
    return span_decl(da, db)


def _trace_frame(directory, t):
    """Per-trace launch position and far-connector position, gated on the
    folder's population verdict.

    A fiber whose own reading disagrees with its folder falls back to the
    population median, so one odd trace cannot shift itself out of frame."""
    facts = frame_facts(directory)
    ev = t.get('events') or []
    end = next((float(e.get('dist_km') or 0.0) for e in ev if e.get('is_end')), None)
    if end is None:
        xs = t.get('dist_km') or []
        end = float(xs[-1]) if xs else 0.0

    launch = 0.0
    if facts['launch_km'] is not None:
        mine = _trace_launch_km(ev)
        launch = (mine if mine is not None
                  and abs(mine - facts['launch_km']) <= REEL_TOL_KM
                  else facts['launch_km'])

    far = end
    if facts['tail_km'] is not None:
        mine = _trace_tail_setback_km(ev)
        gap = (mine if mine is not None
               and abs(mine - facts['tail_km']) <= REEL_TOL_KM
               else facts['tail_km'])
        far = end - gap

    # A fiber that ends well short of the span did NOT reach the cable's far
    # end — it broke, or it is a short shot.  Its end event marks the break,
    # so mirroring a B trace about it throws that trace kilometres out of
    # place (MILELM F231 breaks at 47.26 km on a 69.57 km span: mirroring
    # about the break misplaced it by 22.3 km).  A broken fiber never reached
    # the receive reel either, so the tail subtraction above is wrong for it
    # too.  Fall back to where the population says the cable ends.
    span = facts.get('span_km')
    if span is not None and (far - launch) < span - SHORT_FIBER_TOL_KM:
        far = launch + span
    return round(float(launch), 4), round(float(far), 4)


# ─── Trace loader (cached on directory+filename+mtime) ──────────────────
@lru_cache(maxsize=64)
def _load_trace_cached(directory, filename, mtime):
    # `mtime` is part of the cache KEY only (unused in the body): when a tech
    # re-shoots and overwrites a fiber, or the viewer is opened while files are
    # still copying, the file's mtime changes → a fresh parse instead of a stale
    # cached trace, and a transient None cached mid-copy is superseded once mtime
    # advances (so a fiber can't 404 forever after its copy finishes).
    path = os.path.join(directory, filename)
    if filename.lower().endswith('.json'):
        r = parse_otdr_json(path)
        if r is None:
            return None
        trace = r['full_trace']
        res_m = float(r.get('_json_resolution_m') or 2.5493)
        first_pos_m = float(r.get('_json_first_pos_m') or 0.0)
        display_trace = trace.astype(np.float64)            # already descending-signal
        pulse_ns = r.get('_json_pulse_ns')
        ior = float(r.get('ior') or 1.4682)
    else:
        r = parse_sor_full(path, trim=False)
        if r is None:
            return None
        trace = r['trace']
        ior = _sor_ior_from_events(r)
        sp_s = float(r.get('exfo_sampling_period') or 5e-08)
        res_m = 299_792_458.0 * sp_s / 2.0 / ior
        first_pos_m = _sor_first_pos_m(r, res_m)
        display_trace = -trace.astype(np.float64)           # flip to descending-signal
        pulse_ns = r.get('fxd_pulse_ns')

    n = len(display_trace)
    dist_km = (np.arange(n, dtype=np.float64) * res_m + first_pos_m) / 1000.0

    baseline = float(np.median(display_trace[:200])) if n >= 200 else float(display_trace[0])
    display_trace = display_trace - baseline

    events = []
    for e in (r.get('events') or []):
        events.append({
            'number': int(e.get('number') or 0),
            'dist_km': round(float(e.get('dist_km') or 0.0), 4),
            'splice_loss': round(float(e.get('splice_loss') or 0.0), 3),
            'reflection': round(float(e.get('reflection') or 0.0), 2),
            'slope': round(float(e.get('slope') or 0.0), 3),
            'type': str(e.get('type') or ''),
            'is_reflective': bool(e.get('is_reflective')),
            'is_end': bool(e.get('is_end')),
            # Identifies the OTDR port (tot 0) — the launch-reel rule keys on
            # it, exactly as the Splice Report engine's does.
            'time_of_travel': int(e.get('time_of_travel') or 0),
        })

    # FastReporter's event table carries a wavelength column, and the tech
    # reads it to tell a 1310 shot from a 1550 one in a multi-lambda folder.
    # Prefer EXFO's exact figure from the proprietary block; fall back to the
    # FxdParams value, which is stored in tenths of a nm.
    # NOMINAL, not measured.  FastReporter's lambda column reads 1550 on these
    # files; the SOR's own FxdParams says 1546.0 and EXFO's proprietary block
    # carries the laser's true centre.  Neither is what the tech sees on their
    # screen, so snap to the nearest standard window and only fall back to the
    # raw figure when nothing is close (an unusual source we should not relabel).
    wl = r.get('wavelength') or r.get('exfo_wavelength_nm')
    try:
        wl = float(wl) if wl else None
    except (TypeError, ValueError):
        wl = None
    if wl:
        near = min(NOMINAL_WAVELENGTHS_NM, key=lambda n: abs(n - wl))
        wl = near if abs(near - wl) <= WAVELENGTH_SNAP_NM else round(wl)

    # The acquisition's pulse width, and the IOR the distances were computed
    # with.  The FR grid turns the two into a length -- how far apart two
    # readings of the SAME physical point can land -- and uses that as its
    # column tolerance instead of a flat 200 m.  None when the file does not
    # carry a pulse width; the grid falls back rather than guessing.
    try:
        pulse_ns = float(pulse_ns) if pulse_ns else None
    except (TypeError, ValueError):
        pulse_ns = None

    return {
        'filename': filename,
        'wavelength_nm': wl,
        'pulse_ns': pulse_ns,
        'ior': round(float(ior), 5),
        'num_points': n,
        'dx_km': res_m / 1000.0,
        'first_pos_km': first_pos_m / 1000.0,
        'dist_km': [round(float(x), 5) for x in dist_km.tolist()],
        'trace_db': [round(float(x), 3) for x in display_trace.tolist()],
        'events': events,
        # Where a DECLARED span starts in the raw acquisition, straight from
        # GenParams, or 0.0 when the file declares none.  Carried through here
        # deliberately: _trace_span_launch_km prefers it over measuring the
        # offset off the samples, and without it that preference silently never
        # fires -- the field is on the reader's result, not on this dict, and a
        # missing key reads as "no stored offset" rather than as an error.
        'user_offset_km': (r.get('user_offset_km') or 0.0) if isinstance(r, dict) else 0.0,
    }


def decimate_minmax(dist_km, trace_db, max_pts):
    """Reduce a trace to at most `max_pts` samples, PRESERVING SPIKES.

    Whole-cable overview (1152 fibers in one direction) cannot ship full
    resolution — 1152 x 39,173 points is 340 MB of JSON.  But the reason to
    look at a whole cable at once is to spot spikes and outliers, so the
    decimation must not be the thing that removes them.

    Plain striding does exactly that.  Measured on WSC_SUIsh F19, whose real
    0.943 dB reflective glint is ~4 samples wide: reduced to ~1000 points,
    plain stride keeps 0.111 dB of it (88% of the feature gone) while
    per-bucket min/max keeps 0.957 dB.  So each bucket contributes BOTH its
    extremes, in trace order, which bounds the envelope and cannot hide a
    narrow spike of either polarity.

    Returns (dist_km, trace_db) unchanged when the trace already fits.
    """
    n = len(trace_db)
    if max_pts is None or max_pts <= 0 or n <= max_pts:
        return dist_km, trace_db
    # Two output points per bucket, so aim for half as many buckets.
    nb = max(1, int(max_pts) // 2)
    step = max(1, n // nb)
    nb = n // step
    if nb < 1:
        return dist_km, trace_db
    y = np.asarray(trace_db[:nb * step], dtype=np.float64).reshape(nb, step)
    x = np.asarray(dist_km[:nb * step], dtype=np.float64).reshape(nb, step)
    lo_i = y.argmin(axis=1)
    hi_i = y.argmax(axis=1)
    rows = np.arange(nb)
    # Emit each bucket's two extremes in the order they occur, so the polyline
    # never doubles back on itself and the x axis stays monotonic.
    first_is_lo = lo_i <= hi_i
    ax = np.where(first_is_lo, x[rows, lo_i], x[rows, hi_i])
    ay = np.where(first_is_lo, y[rows, lo_i], y[rows, hi_i])
    bx = np.where(first_is_lo, x[rows, hi_i], x[rows, lo_i])
    by = np.where(first_is_lo, y[rows, hi_i], y[rows, lo_i])
    ox = np.empty(nb * 2); oy = np.empty(nb * 2)
    ox[0::2], ox[1::2] = ax, bx
    oy[0::2], oy[1::2] = ay, by
    # Keep the true final sample so the trace still ends where the fiber does.
    tail = nb * step - 1
    if tail < n - 1:
        ox = np.append(ox, float(dist_km[n - 1]))
        oy = np.append(oy, float(trace_db[n - 1]))
    return ([round(float(v), 5) for v in ox],
            [round(float(v), 3) for v in oy])


def load_trace(direction, fiber, max_pts=None):
    d = CONFIG['dir_a'] if direction == 'a' else CONFIG['dir_b']
    fmap = {n: fn for n, fn in list_fibers(d)}
    fn = fmap.get(fiber)
    if fn is None:
        return None
    try:
        mtime = os.stat(os.path.join(d, fn)).st_mtime_ns
    except OSError:
        return None
    t = _load_trace_cached(d, fn, mtime)
    if t is None:
        # Never let a None parse stay memoized under this mtime key - on
        # coarse-mtime filesystems (FAT/exFAT/SMB, 2 s granularity) a file
        # overwritten in place can keep its key and 404 forever.  Evict so
        # the next request re-parses.
        _load_trace_cached.cache_clear()
        return None
    # Reel geometry rides along with each trace so stacked mode can put A and
    # B on the same physical metre.
    #
    # Derived BEFORE decimation, off the full-resolution trace: _trace_frame
    # falls back to dist_km[-1] for a trace that carries no end event, and
    # decimation only re-appends the true final sample when the last bucket
    # does not already end on it.  Framing first keeps the answer exact and
    # identical for decimated and full requests alike.
    # Put a declared-span file back in one frame before anything reads it.  The
    # mirror, the report deep links and the event grid all speak the RAW frame,
    # so a file whose events were re-based to its span start has to arrive
    # looking like every other file.  The samples were never moved, so it is the
    # events that come back.
    span_launch = frame_facts(d).get('span_launch_km')
    if span_launch:
        t = dict(t)
        t['events'] = [dict(e, dist_km=round(e['dist_km'] + span_launch, 4))
                       for e in (t.get('events') or [])]
        t['span_launch_km'] = round(span_launch, 4)
    launch_km, far_conn_km = _trace_frame(d, t)
    if max_pts:
        # Decimate a COPY - the cache holds FULL resolution, so zooming into
        # one fiber afterwards still gets every sample.
        dx, dy = decimate_minmax(t['dist_km'], t['trace_db'], max_pts)
        if len(dy) != t['num_points']:
            t = dict(t)
            t['dist_km'] = dx
            t['trace_db'] = dy
            t['decimated_from'] = t['num_points']
            t['num_points'] = len(dy)
    # Returned as a COPY: the cached dict is keyed on the file alone, while
    # the frame depends on the folder.
    return {**t, 'launch_km': launch_km, 'far_conn_km': far_conn_km}


def _finite(o):
    """Recursively replace non-finite floats (NaN, ±inf) with None so json.dumps
    emits VALID JSON.  Real EXFO JSON exports carry literal NaN Loss values;
    json.dumps' default allow_nan emitted a bare `NaN` token, so the browser's
    JSON.parse threw and the whole trace pane failed to load for exactly the
    high-loss fibers a tech most needs to see."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, list):
        return [_finite(x) for x in o]
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    return o


# ─── HTTP handler ───────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, payload, status=200):
        body = json.dumps(_finite(payload)).encode('utf-8')   # non-finite → null (valid JSON)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype='text/html; charset=utf-8'):
        try:
            with open(path, 'rb') as f:
                body = f.read()
        except OSError as e:
            self.send_error(404, str(e))
            return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_list(self):
        fa = list_fibers(CONFIG['dir_a'])
        fb = list_fibers(CONFIG['dir_b'])
        self._send_json({
            'dir_a': CONFIG['dir_a'] or '',
            'dir_b': CONFIG['dir_b'] or '',
            # rstrip both separators: a pasted Windows path ending in a
            # backslash otherwise labels the folder "(none)"
            'dir_a_name': os.path.basename((CONFIG['dir_a'] or '').rstrip('/\\')) or '(none)',
            'dir_b_name': os.path.basename((CONFIG['dir_b'] or '').rstrip('/\\')) or '(none)',
            'fibers_a': [n for n, _ in fa],
            'fibers_b': [n for n, _ in fb],
            # Span-level reel geometry.  `launch_a_km` defines the viewer's
            # display frame: report grids hand us cell distances already
            # shifted into A's RAW frame (app.py's _vkm adds launch_a_km), so
            # a flipped B trace has to be mirrored into that same frame.
            'launch_a_km': frame_facts(CONFIG['dir_a']).get('launch_km'),
            'launch_b_km': frame_facts(CONFIG['dir_b']).get('launch_km'),
            # False when B is a short shot — a truncated near-end acquisition
            # with no end-of-fiber event.  Its far end is not in the file, so
            # there is nothing to mirror A on to and the viewer says so
            # instead of mirroring about the acquisition range.
            'cable_end_known_b': frame_facts(CONFIG['dir_b']).get('cable_end_known'),
            # The tech's own span, when they have set one.  It OUTRANKS both
            # the measured frame and the inferred reels — it is the only one of
            # the three that somebody actually knows to be true.
            'span_decl': span_decl(),
            # The gates the REPORTS use.  The Viewer picks whichever belongs to
            # the report that opened it, so a cell that flags in the report
            # flags here too instead of on a number typed into the viewer.
            'thresholds': engine_thresholds(),
        })

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ('/', '/index.html', '/viewer.html'):
            self._send_file(VIEWER_HTML)
            return
        if u.path == '/api/list':
            try:
                self._api_list()
            except Exception as e:      # noqa: BLE001 — a listing crash must
                # surface as JSON + Slack, never a silent connection reset
                try:
                    from error_report import report_error
                    report_error('viewer /api/list', e)
                except Exception:
                    pass
                self._send_json({'dir_a': None, 'dir_b': None,
                                 'fibers_a': [], 'fibers_b': [],
                                 'error': str(e)})
            return

        if u.path == '/api/trace_settings':
            q = parse_qs(u.query)
            try:
                self._send_json(trace_settings(str((q.get('dir') or [''])[0]),
                                               int((q.get('fiber') or ['0'])[0])))
            except (ValueError, TypeError) as e:
                self._send_json({'error': str(e)}, status=400)
            return

        if u.path == '/api/traces':
            # BULK overview: one request for a whole cable in one direction.
            # 1152 separate /api/trace calls against this single-threaded
            # server is thousands of serial round trips and the browser looks
            # frozen; and at full resolution the payload is 340 MB.  Both are
            # solved here: one response, spike-preserving decimation applied
            # server-side (see decimate_minmax).
            q = parse_qs(u.query)
            direction = (q.get('dir') or [''])[0].lower()
            if direction not in ('a', 'b'):
                self._send_json({'error': 'dir must be a or b'}, status=400)
                return
            try:
                max_pts = int((q.get('maxpts') or ['2000'])[0])
            except ValueError:
                max_pts = 2000
            max_pts = max(200, min(max_pts, 20000))
            fibers = []
            for part in (q.get('fibers') or [''])[0].split(','):
                part = part.strip()
                if not part:
                    continue
                if '-' in part:
                    try:
                        lo, hi = part.split('-', 1)
                        fibers.extend(range(int(lo), int(hi) + 1))
                    except ValueError:
                        continue
                else:
                    try:
                        fibers.append(int(part))
                    except ValueError:
                        continue
            # Hard ceiling: a cable is 1152 fibers; anything larger is a typo
            # or a hostile query, and either way must not pin the server.
            fibers = sorted(set(f for f in fibers if f > 0))[:1152]
            out, missing = [], []
            for f in fibers:
                try:
                    t = load_trace(direction, f, max_pts=max_pts)
                except Exception as exc:
                    report_error('viewer bulk trace load', exc,
                                 {'direction': direction, 'fiber': f})
                    missing.append(f)
                    continue
                if t is None:
                    missing.append(f)
                    continue
                out.append({'direction': direction.upper(), 'fiber': f, **t})
            self._send_json({'direction': direction.upper(), 'maxpts': max_pts,
                             'requested': len(fibers), 'traces': out,
                             'missing': missing})
            return

        if u.path == '/api/trace':
            q = parse_qs(u.query)
            direction = (q.get('dir') or [''])[0].lower()
            try:
                fiber = int((q.get('fiber') or [''])[0])
            except ValueError:
                self._send_json({'error': 'invalid fiber'}, status=400)
                return
            if direction not in ('a', 'b'):
                self._send_json({'error': 'dir must be a or b'}, status=400)
                return
            try:
                t = load_trace(direction, fiber)
            except Exception as exc:                       # surface parse errors as JSON
                report_error("viewer trace load", exc,
                             {"direction": direction, "fiber": fiber})
                self._send_json({'error': f'parse failed: {exc}'}, status=500)
                return
            if t is None:
                self._send_json({'error': f'fiber {fiber} not found in dir {direction}'}, status=404)
                return
            self._send_json({'direction': direction.upper(), 'fiber': fiber, **t})
            return
        self.send_error(404, 'unknown route')

    def _origin_is_local(self):
        """Reject cross-origin POSTs.  This localhost server has no CORS/CSRF
        guard, so ANY website the tech visits while the hub runs could POST to
        /api/jserror (a CORS "simple" request needs no preflight) and, because
        the Slack dedup is keyed on the attacker-controlled message, flood the
        shared channel with arbitrary text.  A real cross-site POST always sends
        an Origin header pointing at the attacker's site; a same-origin fetch
        from viewer.html sends a loopback Origin (or, in some browsers, none).
        Allow only loopback origins (any port) and a missing Origin."""
        origin = self.headers.get('Origin')
        if not origin:
            return True
        try:
            host = urlparse(origin).hostname
        except Exception:
            return False
        return host in ('127.0.0.1', 'localhost', '::1')

    def do_POST(self):
        # Browser JS errors from viewer.html POST here → Slack via report_error.
        u = urlparse(self.path)
        if u.path == '/api/jserror':
            if not self._origin_is_local():
                self.send_error(403, 'cross-origin POST rejected')
                return
            try:
                n = int(self.headers.get('Content-Length', 0) or 0)
                data = json.loads((self.rfile.read(n) if n else b'{}').decode('utf-8') or '{}')
            except Exception:
                data = {}
            msg = str(data.get('message') or 'unknown JS error')[:300]
            stack = str(data.get('stack') or '')[:800]
            page = str(data.get('page') or '')[:200]
            try:
                raise RuntimeError(msg)               # give report_error an exc + a frame
            except Exception as exc:
                report_error("viewer (browser JS)", exc, {"js_stack": stack, "url": page})
            self._send_json({'ok': True})
            return
        if u.path == '/api/span':
            if not self._origin_is_local():
                self.send_error(403, 'cross-origin POST rejected')
                return
            try:
                n = int(self.headers.get('Content-Length', 0) or 0)
                data = json.loads((self.rfile.read(n) if n else b'{}').decode('utf-8') or '{}')
                out = span_decl_set(str(data.get('dir') or ''),
                                    str(data.get('edge') or ''),
                                    data.get('km') or 0.0)
            except (ValueError, TypeError) as e:
                self._send_json({'error': str(e)}, status=400)
                return
            except Exception as e:                    # noqa: BLE001 — a store
                try:                                  # write failure must be
                    report_error('viewer /api/span', e)   # visible, not silent
                except Exception:
                    pass
                    # NOTE: no `from error_report import ...` here.  An import
                    # inside this function makes `report_error` local to ALL of
                    # do_POST, and /api/jserror above then raises
                    # UnboundLocalError instead of reporting anything.
                self._send_json({'error': str(e)}, status=500)
                return
            self._send_json({'ok': True, 'span_decl': out})
            return
        if u.path == '/api/trace_edit':
            if not self._origin_is_local():
                self.send_error(403, 'cross-origin POST rejected')
                return
            try:
                n = int(self.headers.get('Content-Length', 0) or 0)
                data = json.loads((self.rfile.read(n) if n else b'{}').decode('utf-8') or '{}')
                out = edit_traces(str(data.get('dir') or ''),
                                  data.get('fibers') if data.get('fibers') == 'all'
                                  else list(data.get('fibers') or []),
                                  ior=data.get('ior'),
                                  fields=dict(data.get('fields') or {}),
                                  dest_name=data.get('dest_name'),
                                  span=dict(data.get('span') or {}))
            except (ValueError, TypeError) as e:
                self._send_json({'error': str(e)}, status=400)
                return
            except Exception as e:                    # noqa: BLE001 - a write
                try:                                  # failure must be seen
                    report_error('viewer /api/trace_edit', e)
                except Exception:
                    pass
                self._send_json({'error': str(e)}, status=500)
                return
            self._send_json({'ok': True, **out})
            return
        self.send_error(404, 'unknown route')


# ─── Bootstrap helpers ──────────────────────────────────────────────────
def set_dirs(dir_a, dir_b):
    """Hub calls this when the user picks folders.  Returns True if either
    directory changed.  (Only _load_trace_cached is memoized, and it keys on
    directory+filename, so a folder swap can't serve stale traces.)"""
    changed = (CONFIG['dir_a'] != (dir_a or None)) or (CONFIG['dir_b'] != (dir_b or None))
    CONFIG['dir_a'] = dir_a or None
    CONFIG['dir_b'] = dir_b or None
    return changed


def is_running():
    return _server is not None


def get_port():
    return _started_port


def find_free_port(start):
    for port in range(start, start + 50):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', port))
                return port
        except OSError:
            continue
    raise RuntimeError(f'no free port in {start}-{start + 49}')


def start_in_thread(port=8771):
    """Start the server once per process (idempotent).  Returns the port."""
    global _server, _thread, _started_port
    if _server is not None:
        return _started_port
    actual = find_free_port(port)
    _server = HTTPServer(('127.0.0.1', actual), Handler)
    _thread = threading.Thread(target=_server.serve_forever, daemon=True)
    _thread.start()
    _started_port = actual
    return actual


# ─── CLI (standalone dev) ───────────────────────────────────────────────

# ═══════════════════════════════════════════════════════════════════════
# .sor WRITER
#
# Write a Bellcore/Telcordia SR-4731 .sor back out, edited.
#
# The suite has read these files for years and never written one.  This module
# exists because the boss asked to edit a trace's settings in our software and
# save the .sor -- IOR first ("IOR is wrong and correct with software"), trace
# name second, a declared span start/stop third.
#
# RULES THIS MODULE ENFORCES, NOT JUST DOCUMENTS
# ----------------------------------------------
# * Never in place.  `write()` refuses a destination that is the source, or that
#   already exists unless told to overwrite.  These are customer measurement
#   records and sometimes the only copy.
# * Byte-exact round trip.  A file split and rebuilt with no edit must come back
#   identical, and `roundtrip_ok()` checks it before any edited write is offered.
# * The checksum is recomputed, always.  CRC-16, polynomial 0x1021, init 0x9ECF,
#   non-reflected, no final xor -- worked out from real files and verified on
#   every one since.  A file with a stale Cksum is a file FastReporter may refuse.
#
# WHAT AN IOR EDIT ACTUALLY TOUCHES
# ---------------------------------
# Event positions and the trace's x-axis are DERIVED: KeyEvents store time of
# travel, DataPts store samples at a fixed sampling period, and every distance is
# `time * c / IOR`.  So changing the group index in FxdParams re-scales all of
# them for free -- verified: a 1.47 -> 1.467 edit moves a 2.0415 km end event to
# 2.0456, exactly the ratio.
#
# But the format ALSO stores distances outright, as twins beside their times, and
# those go stale unless rewritten:
#
#     GenParams  user_offset_dist    (0.1 m)    twin of user_offset   (time)
#     FxdParams  acq_offset_dist     (0.1 m)    twin of acq_offset    (time)
#     FxdParams  acq_range_dist      (0.1 m)    twin of acq_range     (time)
#
# `set_ior()` rewrites those by the same ratio.  And EXFO's proprietary block
# carries its own `Ior` as a float64 plus a family of positions in METRES
# (Position, SpansLength, the cursors).  Which of those FastReporter trusts was
# run as an experiment, not assumed.  Three files cut from the same source, each
# opened cold in FR 3 and its IOR-by-distance dialog and event table read:
#
#     Bellcore group index only                      FR: 2.0414 km  (ignored)
#     + proprietary float64 Ior                      FR: 2.0414 km  (STILL ignored)
#     + proprietary metre fields scaled by old/new   FR: 2.0456 km  (correct)
#
# So FR never recomputes a distance from an IOR; it reads the stored metres.
# Our three readers read the Bellcore field.  A writer that stops short of
# scaling the metres therefore produces a file our tools and FR disagree about
# by the full ratio -- the exact interop failure this exists to prevent.  The
# `proprietary=False` flag survives only to reproduce that experiment.
#
# The RawSamples payload is never touched, and that is checked, not hoped: the
# decoder invents pseudo-fields inside it, and the scan stops at the payload's
# exact byte span.
#
# WHAT A NAME EDIT ACTUALLY TOUCHES
# ---------------------------------
# GenParams holds cable id, fiber id, locations, operator and comment as Latin-1
# strings, and every reader of ours speaks it.  FastReporter does not: its
# Identification tab is driven by UTF-16 records in the proprietary stream --
# UserNameA, CustomerName, CompanyName, Comment, a Job-ID `Identifier`, and a
# Name/Value "Identifiers" list for Cable ID / Fiber ID / Location A / B.  An
# edit to GenParams alone changed nothing on FR's screen; that was tried.
#
# Those records change LENGTH when edited, and the stream is a tree of absolute
# offsets -- every 16-byte descriptor, every type-0 child pointer, and the block
# header's stream length -- so `set_identifiers()` re-lays the stream, rebasing
# all of them, and re-chunks at 32 KiB.  Verified in FR on a file whose stream
# grew by 126 bytes: every edited field displayed, every untouched one intact.
# Both copies are written, so one file gives one answer everywhere.
#
# Lives in this file rather than its own module on purpose: a new engine
# file cannot reach the fleet by hot update (the launcher rejects any
# manifest whose file set differs from the list baked into its exe), and
# the Viewer is where the edit UI belongs anyway.
# ═══════════════════════════════════════════════════════════════════════

C_M_PER_S = 299_792_458.0

CRC_POLY, CRC_INIT = 0x1021, 0x9ECF


# ─── checksum ─────────────────────────────────────────────────────────────

def sor_crc(data: bytes) -> int:
    """CRC-16 over everything before the Cksum block's 2-byte body."""
    c = CRC_INIT
    for by in data:
        c ^= by << 8
        for _ in range(8):
            c = ((c << 1) ^ CRC_POLY) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c


# ─── block directory ──────────────────────────────────────────────────────

class Block:
    __slots__ = ('name', 'ver', 'body', 'scaled_fields')

    def __init__(self, name: bytes, ver: int, body: bytes):
        self.name, self.ver, self.body = name, ver, body

    def __repr__(self):
        return f'Block({self.name!r}, ver={self.ver}, {len(self.body)} bytes)'


def split(data: bytes):
    """-> (map_version, [Block, ...]) in physical order, Cksum last.

    Walks the directory's SIZES, which is what the format specifies.  Not a
    search-by-name: GenParams is the first block and its name also appears in
    the directory, so searching lands on the directory entry (wrong on 25/25
    real files -- see the readers' `_block_body_offsets`).
    """
    if data[:4] != b'Map\x00':
        raise ValueError('not a Bellcore SOR (no Map block)')
    ne = data.index(b'\x00') + 1
    mapver = struct.unpack_from('<H', data, ne)[0]
    mapsize = struct.unpack_from('<I', data, ne + 2)[0]
    nb = struct.unpack_from('<H', data, ne + 6)[0]
    off, cum, out = ne + 8, mapsize, []
    for _ in range(nb - 1):
        e = data.index(b'\x00', off) + 1
        nm = data[off:e - 1]
        bv = struct.unpack_from('<H', data, e)[0]
        bs = struct.unpack_from('<I', data, e + 2)[0]
        hdr = nm + b'\x00'
        if data[cum:cum + len(hdr)] != hdr:
            raise ValueError(f'block {nm!r} not where the directory says (offset {cum})')
        out.append(Block(nm, bv, data[cum + len(hdr): cum + bs]))
        cum += bs
        off = e + 6
    if off != mapsize:
        raise ValueError(f'directory walk ended at {off}, map says {mapsize}')
    return mapver, out


def build(mapver: int, blocks) -> bytes:
    """Re-emit the file: directory, blocks, then Cksum computed over all of it."""
    if not blocks or blocks[-1].name != b'Cksum' or len(blocks[-1].body) != 2:
        raise ValueError('Cksum must be the last block with a 2-byte body')
    mapsize = 12 + sum(len(b.name) + 1 + 6 for b in blocks)
    hdr = b'Map\x00' + struct.pack('<HIH', mapver, mapsize, len(blocks) + 1)
    for b in blocks:
        hdr += b.name + b'\x00' + struct.pack('<HI', b.ver, len(b.name) + 1 + len(b.body))
    out = bytearray(hdr)
    for b in blocks[:-1]:
        out += b.name + b'\x00' + b.body
    out += b'Cksum\x00' + struct.pack('<H', sor_crc(bytes(out)))
    return bytes(out)


def roundtrip_ok(data: bytes) -> bool:
    mv, bl = split(data)
    return build(mv, bl) == data


def _find(blocks, name: bytes) -> Block:
    for b in blocks:
        if b.name == name:
            return b
    raise KeyError(name.decode())


# ─── field maps (verified byte-exact against real files, probe2.py) ───────

def _cstr_end(b: bytes, p: int) -> int:
    return b.index(b'\x00', p) + 1


def genparams_offsets(body: bytes) -> dict:
    """Offsets INTO the GenParams body of the fields that carry distance."""
    p = 2                                   # language
    p = _cstr_end(body, p)                  # cable id
    p = _cstr_end(body, p)                  # fiber id
    p += 4                                  # fiber type, wavelength
    p = _cstr_end(body, p)                  # location A
    p = _cstr_end(body, p)                  # location B
    p = _cstr_end(body, p)                  # cable code
    p += 2                                  # build condition
    return {'user_offset': p, 'user_offset_dist': p + 4}


def fxdparams_offsets(body: bytes) -> dict:
    """Offsets INTO the FxdParams body: group index and the distance twins."""
    p = 4 + 2 + 2                           # date, units, wavelength
    acq_offset, acq_offset_dist = p, p + 4
    p += 8
    npw = struct.unpack_from('<H', body, p)[0]
    p += 2
    p += 2 * npw                            # pulse widths
    p += 4 * npw                            # data spacing
    p += 4 * npw                            # points per width
    group_index = p
    p += 4                                  # group index
    p += 2 + 4 + 2                          # backscatter, averages, avg time
    acq_range, acq_range_dist = p, p + 4
    return {'acq_offset': acq_offset, 'acq_offset_dist': acq_offset_dist,
            'group_index': group_index,
            'acq_range': acq_range, 'acq_range_dist': acq_range_dist}


def read_ior(data: bytes) -> float:
    _, bl = split(data)
    fx = _find(bl, b'FxdParams').body
    return struct.unpack_from('<I', fx, fxdparams_offsets(fx)['group_index'])[0] / 1e5


# ─── the proprietary block ────────────────────────────────────────────────
#
# ExfoNewProprietaryBlock: a 36-byte header, then [uint32 length][zlib chunk]
# repeated.  The decompressed chunks concatenate into one field stream.  To
# patch a value we find which CHUNK holds it, patch inside that chunk's
# decompressed bytes, recompress that chunk only, and fix its length prefix --
# every other chunk stays byte-identical.  Re-chunking the whole stream would
# work too, but would touch bytes we have no reason to touch.

_PROP_HDR = 36


def _prop_chunks(body: bytes):
    """-> (header, [(raw_len_prefix_offset, decompressed_bytes), ...], tail)"""
    pos, out = _PROP_HDR, []
    while pos + 4 <= len(body):
        sz = struct.unpack_from('<I', body, pos)[0]
        if sz < 2 or pos + 4 + sz > len(body):
            break
        chunk = body[pos + 4:pos + 4 + sz]
        if chunk[:1] != b'\x78':
            break
        out.append((pos, zlib.decompress(chunk)))
        pos += 4 + sz
    return body[:_PROP_HDR], out, body[pos:]


def _prop_rebuild(header: bytes, chunks_dec, tail: bytes) -> bytes:
    out = bytearray(header)
    for dec in chunks_dec:
        comp = zlib.compress(dec)
        out += struct.pack('<I', len(comp)) + comp
    out += tail
    return bytes(out)


def _prop_find_float64(chunks_dec, name: bytes, current: float):
    """Locate the float64 field `name` -> (chunk_idx, offset_in_chunk).

    The stream's record layout is EXFO's, not documented; rather than assume
    it, find the field NAME and then the exact 8-byte little-endian double of
    the value the Bellcore side currently holds, within the next 64 bytes.
    That pair -- name, then this precise value -- cannot be a coincidence, and
    it cannot be a pseudo-field the decoder invents inside RawSamples because
    the search stops at that payload.  Exactly one hit is required."""
    needle = name + b'\x00'
    pat = struct.pack('<d', float(current))
    found = []
    for ci, dec in enumerate(chunks_dec):
        lo, hi = _rawsamples_span(dec)
        start = 0
        while True:
            i = dec.find(needle, start)
            if i < 0:
                break
            if lo is not None and lo <= i < hi:
                start = i + 1
                continue
            j = dec.find(pat, i, i + 64)
            if j >= 0:
                found.append((ci, j))
            start = i + 1
    if len(found) != 1:
        raise ValueError(f'expected exactly one {name!r} float64 = {current}, found {len(found)}')
    return found[0]


# Which proprietary float64 fields are DISTANCES IN METRES and therefore go
# stale when the IOR changes.  Enumerated by probe4.py against five real files;
# the uint32 twins of the cursors are sample INDICES and are not touched.
# Established in FastReporter, not assumed: with only the Bellcore group index
# changed FR still printed the old distances, and with the float64 `Ior` also
# changed it STILL printed the old distances -- FR reads these stored metres,
# it does not recompute them.
_PROP_METRE_FIELDS = (b'Position', b'Length', b'SpansLength',
                      b'CursorAPosition', b'CursorBPosition',
                      b'SubCursorAPosition', b'SubCursorBPosition',
                      b'CursorA', b'CursorB', b'SubCursorA', b'SubCursorB',
                      b'ManualZoomXMin', b'ManualZoomXMax')


def _rawsamples_span(stream: bytes):
    """[lo, hi) of the RawSamples PAYLOAD in the stream, or (None, None).

    Its record header (name_off, type, size) sits 12 bytes before the name;
    the payload is `size` bytes after the name's NUL.  Real geometry fields
    live AFTER this span -- `Ior` at ~144 k, `SpansLength` at ~50 k on a
    Defuniak file -- so the exclusion has to be this exact byte range, not
    'everything past the name'.  Inside it the decoder invents pseudo-fields
    (probe5.py); that is what we must never touch."""
    i = stream.find(b'RawSamples\x00')
    if i < 0 or i < 16:
        return None, None
    self_off, tc, sz = struct.unpack_from('<III', stream, i - 16)
    if self_off != i:
        return None, None
    lo = i + len(b'RawSamples\x00')
    return lo, lo + sz


def _prop_float64_payloads(stream: bytes, names):
    """-> [(stream_offset_of_payload, current_value)] for every float64 record
    named in `names`, skipping anything inside the RawSamples payload.

    Record layout (secretsauce/exfo_proprietary_decoder.py, confirmed on real
    bytes): a 16-byte descriptor [self_off][type][size][next_ref] then
    `name\\0`, then the payload.  self_off must equal the name's own offset;
    a wrong layout guess therefore yields nothing rather than patching the
    wrong bytes -- which is exactly what happened the first time."""
    lo, hi = _rawsamples_span(stream)
    out = []
    for name in names:
        needle = name + b'\x00'
        start = 0
        while True:
            i = stream.find(needle, start)
            if i < 0:
                break
            start = i + 1
            if lo is not None and lo <= i < hi:
                continue
            # descriptor precedes the name by 16 bytes:
            #   [self_off][type][size][next_ref] Name\0 payload
            # self_off must point back at THIS name.  That also rejects a
            # substring hit -- `CursorAPosition\0` occurs inside
            # `SubCursorAPosition\0`, and for that hit self_off will not match.
            if i >= 16:
                self_off, tc, sz = struct.unpack_from('<III', stream, i - 16)
                if self_off == i and tc == 3 and sz == 8:
                    pay = i + len(needle)
                    out.append((pay, struct.unpack_from('<d', stream, pay)[0]))
    return out


def _stream_to_chunk(chunk_lens, off):
    """Absolute stream offset -> (chunk index, offset within that chunk)."""
    base = 0
    for ci, ln in enumerate(chunk_lens):
        if off < base + ln:
            return ci, off - base
        base += ln
    raise IndexError(off)


# ─── the proprietary stream as a RECORD TREE (for size-changing edits) ─────
#
# Layout, read off real bytes and confirmed on four files (574 / 574 / 642 /
# 1047 records):  a 16-byte descriptor [self_off][type][size][payload_off],
# then `name\0`, then `size` bytes of payload.  Every offset is ABSOLUTE in
# the decompressed stream.  Type 0 records are not containers -- their
# payload is an array of uint32 child offsets (OtdrFile -> one child at 41,
# Fiber0 -> sixteen).  Type 4 is a UTF-16LE string, NUL-terminated, `size`
# counting the bytes.  The 36-byte block header carries the stream length at
# +24, and the stream is chunked at exactly 32 KiB before compression.
#
# So changing a string's length means: rewrite that record's size and payload,
# add the delta to every descriptor offset and every child pointer that lies
# beyond the edit, re-chunk, recompress, and fix the header length.  Anything
# less and FR reads a stream whose pointers no longer land on records.

_PROP_CHUNK = 32768
_PROP_HDR_LEN_OFF = 24


def _prop_records(stream: bytes):
    """Every record, found by its descriptor pointing back at its own name."""
    recs, i, n = [], 16, len(stream)
    while i < n - 1:
        j = stream.find(b'\x00', i)
        if j < 0:
            break
        nm = stream[i:j]
        if 2 <= len(nm) < 100 and nm[:1].isalpha() and all(32 <= c < 127 for c in nm):
            so, tc, sz, pay = struct.unpack_from('<IIII', stream, i - 16)
            if so == i and pay == j + 1:
                recs.append({'desc': i - 16, 'name': nm.decode(), 'tc': tc,
                             'size': sz, 'pay': pay})
                i = j + 1
                continue
        i += 1
    return recs


def _prop_strings(stream: bytes):
    """-> [(record, value)] for the UTF-16 string records."""
    out = []
    for r in _prop_records(stream):
        if r['tc'] == 4:
            v = stream[r['pay']:r['pay'] + r['size']].decode('utf-16-le', 'replace').rstrip('\x00')
            out.append((r, v))
    return out


def _prop_set_string_payloads(stream: bytes, edits) -> bytes:
    """Apply {payload_offset: new_text} edits with full pointer rebasing.

    Edits are applied from the END of the stream backwards, so each delta only
    disturbs offsets past a point every earlier edit has already been placed
    before.  After each, every descriptor offset and every type-0 child pointer
    greater than the edit point moves by the delta."""
    recs = _prop_records(stream)
    by_pay = {r['pay']: r for r in recs}
    starts = {r['desc'] for r in recs}
    for r in recs:                        # the pointer hypothesis is a hard guard
        if r['tc'] == 0:
            for k in range(0, r['size'], 4):
                v = struct.unpack_from('<I', stream, r['pay'] + k)[0]
                if v not in starts and v != len(stream):
                    raise ValueError(f'type-0 {r["name"]!r} holds {v}, not a record offset; '
                                     'refusing a size-changing edit on this layout')
    s = bytearray(stream)
    for pay in sorted(edits, reverse=True):
        r = by_pay[pay]
        if r['tc'] != 4:
            raise ValueError(f'{r["name"]!r} is not a string record')
        new = edits[pay].encode('utf-16-le') + b'\x00\x00'
        delta = len(new) - r['size']
        s[pay:pay + r['size']] = new
        struct.pack_into('<I', s, r['desc'] + 8, len(new))         # its own size
        if delta:
            cut = pay + r['size']                                    # old end of payload
            # Rebase every descriptor past the cut.  Descriptors at or beyond
            # `cut` have physically moved by delta in the buffer; address them
            # through that shift, then fix the offsets they carry.
            shifted = []
            for q in recs:
                d = q['desc']
                if d >= cut:
                    d += delta
                so, tc, sz, pf = struct.unpack_from('<IIII', s, d)
                if so > pay:  so += delta
                if pf > pay:  pf += delta
                struct.pack_into('<IIII', s, d, so, tc, sz, pf)
                if tc == 0:
                    for k in range(0, sz, 4):
                        v = struct.unpack_from('<I', s, pf + k)[0]
                        if v > pay:
                            struct.pack_into('<I', s, pf + k, v + delta)
                shifted.append((q, d))
            # keep `recs` addressable for the next (earlier) edit: only descriptors
            # past this cut moved, and later edits are all EARLIER than this one,
            # so their own descriptors did not move.  Update the map anyway.
            for q, d in shifted:
                q['desc'] = d
                if q['pay'] > pay:
                    q['pay'] += delta
            by_pay = {q['pay']: q for q in recs}
    return bytes(s)


def _prop_rebuild_stream(header: bytes, stream: bytes) -> bytes:
    """Re-chunk at 32 KiB, recompress, and write the stream length into the header."""
    h = bytearray(header)
    struct.pack_into('<I', h, _PROP_HDR_LEN_OFF, len(stream))
    out = bytearray(h)
    for i in range(0, len(stream), _PROP_CHUNK):
        comp = zlib.compress(stream[i:i + _PROP_CHUNK])
        out += struct.pack('<I', len(comp)) + comp
    return bytes(out)


# What FastReporter's Identification tab actually reads.  GenParams is NOT it:
# an edit there changed nothing on screen.  These are the UTF-16 records it
# shows, found by matching every displayed string to the stream.  The
# "Name"/"Value" pairs are the Identifiers list; Cable ID's Value is EMPTY on
# every file seen, which is why FR showed it blank -- not an edit failing.
_PROP_ID_MAP = {
    'cable_id':   [('Cable', None), ('Value', 'Cable ID')],
    'fiber_id':   [('Identifier', 'first'), ('Value', 'Fiber ID')],
    'loc_a':      [('LocationA', None), ('Value', 'Location A')],
    'loc_b':      [('LocationB', None), ('Value', 'Location B')],
    'operator':   [('UserNameA', None)],
    'operator_b': [('UserNameB', None)],
    'customer':   [('CustomerName', None)],
    'company':    [('CompanyName', None)],
    'job_id':     [('Identifier', 'job')],
    'comment':    [('Comment', 'nonempty')],
}


def _prop_id_targets(stream: bytes, field: str):
    """Payload offsets of every proprietary record that carries `field`."""
    strs = _prop_strings(stream)
    out = []
    for rec_name, rule in _PROP_ID_MAP[field]:
        if rec_name == 'Value':
            # the Identifiers list: Value follows the Name record naming it
            for k, (r, v) in enumerate(strs):
                if r['name'] == 'Name' and v == rule and k + 1 < len(strs) \
                        and strs[k + 1][0]['name'] == 'Value':
                    out.append(strs[k + 1][0]['pay'])
        elif rec_name == 'Identifier':
            hits = [r for r, v in strs if r['name'] == 'Identifier']
            if rule == 'first' and hits:
                out.append(hits[0]['pay'])
            elif rule == 'job' and len(hits) > 1:
                out.append(hits[1]['pay'])
        elif rec_name == 'Comment':
            out.extend(r['pay'] for r, v in strs if r['name'] == 'Comment' and v)
        else:
            out.extend(r['pay'] for r, v in strs if r['name'] == rec_name)
    return out


# ─── edits ────────────────────────────────────────────────────────────────

def set_ior(data: bytes, new_ior: float, proprietary: bool = True) -> bytes:
    """Return a new file with the group index changed to `new_ior`.

    Bellcore side: the FxdParams group index, and the three distance twins
    (user_offset_dist, acq_offset_dist, acq_range_dist) scaled by old/new so
    they keep agreeing with their time fields.

    `proprietary=True` also patches EXFO's own float64 `Ior`.  Whether that is
    necessary for FastReporter is the experiment this flag exists to run.
    """
    if not (1.40 <= new_ior <= 1.55):
        raise ValueError(f'group index {new_ior} outside the sane band 1.40-1.55')
    mv, bl = split(data)
    fx = _find(bl, b'FxdParams')
    fo = fxdparams_offsets(fx.body)
    old_raw = struct.unpack_from('<I', fx.body, fo['group_index'])[0]
    old_ior = old_raw / 1e5
    new_raw = int(round(new_ior * 1e5))
    ratio = old_ior / new_ior                # distance scales by old/new

    body = bytearray(fx.body)
    struct.pack_into('<I', body, fo['group_index'], new_raw)
    for k in ('acq_offset_dist', 'acq_range_dist'):
        v = struct.unpack_from('<i', body, fo[k])[0]
        struct.pack_into('<i', body, fo[k], int(round(v * ratio)))
    fx.body = bytes(body)

    gp = _find(bl, b'GenParams')
    go = genparams_offsets(gp.body)
    body = bytearray(gp.body)
    v = struct.unpack_from('<i', body, go['user_offset_dist'])[0]
    struct.pack_into('<i', body, go['user_offset_dist'], int(round(v * ratio)))
    gp.body = bytes(body)

    if proprietary:
        for b in bl:
            if b.name.startswith(b'ExfoNewProprietaryBlock'):
                hdr, chunks, tail = _prop_chunks(b.body)
                decs = [d for _, d in chunks]
                ci, off = _prop_find_float64(decs, b'Ior', old_ior)
                d = bytearray(decs[ci])
                struct.pack_into('<d', d, off, float(new_ior))
                decs[ci] = bytes(d)
                # the stored metres: every one scales by old/new, and the
                # RawSamples payload must come through byte-identical
                stream = b''.join(decs)
                lo, hi = _rawsamples_span(stream)
                raw_before = stream[lo:hi] if lo is not None else b''
                lens = [len(x) for x in decs]
                bufs = [bytearray(x) for x in decs]
                n_scaled = 0
                for pay, val in _prop_float64_payloads(stream, _PROP_METRE_FIELDS):
                    if val != val or val == 0.0:      # NaN / unset: leave
                        continue
                    cj, o = _stream_to_chunk(lens, pay)
                    if o + 8 > lens[cj]:
                        raise ValueError('float64 straddles a chunk boundary')
                    struct.pack_into('<d', bufs[cj], o, val * ratio)
                    n_scaled += 1
                decs = [bytes(x) for x in bufs]
                after = b''.join(decs)
                if lo is not None and after[lo:hi] != raw_before:
                    raise ValueError('RawSamples payload changed; refusing to write')
                if n_scaled == 0:
                    raise ValueError('no proprietary metre fields found to scale')
                b.body = _prop_rebuild(hdr, decs, tail)
                b.scaled_fields = n_scaled
                break
    return build(mv, bl)


# ─── identifiers (the "trace name") ───────────────────────────────────────
#
# GenParams carries the human-facing text: cable id, fiber id, the two location
# codes, cable code, operator, comment.  They are NUL-terminated strings, so an
# edit changes the block's SIZE and the directory has to be re-laid -- which
# `build()` does.  They appear NOWHERE else: searched the proprietary stream
# for every one of them on a real file and found none, so this is a Bellcore-
# only edit with no second copy to go stale.

GENPARAMS_STRINGS = ('cable_id', 'fiber_id', 'loc_a', 'loc_b', 'cable_code',
                     'operator', 'comment')
# FR-side extras that GenParams has no slot for.  Editable, proprietary only.
EXTRA_STRINGS = ('operator_b', 'customer', 'company', 'job_id')
ALL_STRINGS = GENPARAMS_STRINGS + EXTRA_STRINGS


def read_identifiers(data: bytes) -> dict:
    _, bl = split(data)
    b = _find(bl, b'GenParams').body
    out, q = {}, 2
    for k in ('cable_id', 'fiber_id'):
        e = b.index(b'\x00', q); out[k] = b[q:e].decode('latin-1'); q = e + 1
    q += 4                                          # fiber type, wavelength
    for k in ('loc_a', 'loc_b', 'cable_code'):
        e = b.index(b'\x00', q); out[k] = b[q:e].decode('latin-1'); q = e + 1
    q += 2 + 8                                      # build condition, user offsets
    for k in ('operator', 'comment'):
        e = b.index(b'\x00', q); out[k] = b[q:e].decode('latin-1'); q = e + 1
    return out


def _enc(k: str, v: str) -> bytes:
    try:
        raw = v.encode('latin-1')
    except UnicodeEncodeError:
        raise ValueError(f'{k}: only Latin-1 text fits in a .sor')
    if b'\x00' in raw:
        raise ValueError(f'{k}: NUL is the string terminator and cannot be in the text')
    if len(raw) > 255:
        raise ValueError(f'{k}: {len(raw)} bytes is longer than any reader expects')
    return raw


def read_fr_identifiers(data: bytes) -> dict:
    """The identifiers as FastReporter shows them: read from the proprietary
    UTF-16 records, for every field in ALL_STRINGS that the file carries.

    GenParams is what our readers speak; this is what FR's Identification tab
    speaks.  The edit dialog shows both so a tech is not shown a blank
    Customer on a file whose Customer FR displays -- typing it back in would
    be a pointless rewrite.  A file with no proprietary block gives {}.
    """
    _, bl = split(data)
    pb = next((b for b in bl if b.name.startswith(b'ExfoNewProprietaryBlock')), None)
    if pb is None:
        return {}
    try:
        _, chunks, _ = _prop_chunks(pb.body)
    except Exception:                                # noqa: BLE001 - not our stream
        return {}
    stream = b''.join(d for _, d in chunks)
    by_pay = {r['pay']: v for r, v in _prop_strings(stream)}
    out = {}
    for k in ALL_STRINGS:
        if k not in _PROP_ID_MAP:
            continue
        for pay in _prop_id_targets(stream, k):
            if pay in by_pay:
                out[k] = by_pay[pay]
                break
    return out


def set_identifiers(data: bytes, **fields) -> bytes:
    """Return a new file with the given GenParams strings replaced.

    Any of GENPARAMS_STRINGS may be passed.  Everything else in the block --
    fiber type, wavelength, build condition, the user offsets -- is copied
    through untouched, byte for byte.

    A word on `fiber_id`: the suite identifies a fiber by its FILENAME, and the
    completeness auditor cross-checks that against this field.  Changing one
    without the other is allowed here, because a tech correcting a mislabelled
    file may need exactly that -- but it is the one identifier whose edit can
    make another tool disagree with the name on disk.
    """
    bad = set(fields) - set(ALL_STRINGS)
    if bad:
        raise ValueError(f'not editable identifiers: {sorted(bad)}')
    for k, v in fields.items():
        _enc(k, v)
    mv, bl = split(data)
    # FastReporter reads its Identification tab from the proprietary stream,
    # so that is rewritten first; GenParams below keeps the Bellcore side in
    # step for every reader that speaks it.
    for pb in bl:
        if pb.name.startswith(b'ExfoNewProprietaryBlock'):
            hdr, chunks, tail = _prop_chunks(pb.body)
            stream = b''.join(d for _, d in chunks) + tail
            edits = {}
            for k, v in fields.items():
                for pay in _prop_id_targets(stream, k):
                    edits[pay] = v
            if edits:
                stream = _prop_set_string_payloads(stream, edits)
                pb.body = _prop_rebuild_stream(hdr, stream)
            break
    fields = {k: v for k, v in fields.items() if k in GENPARAMS_STRINGS}
    if not fields:
        return build(mv, bl)
    gp = _find(bl, b'GenParams')
    b = gp.body
    cur = read_identifiers(data)
    new = {k: (_enc(k, fields[k]) if k in fields else cur[k].encode('latin-1'))
           for k in GENPARAMS_STRINGS}

    # re-emit the block around the fixed-width fields, which are copied verbatim
    q = 2
    e = b.index(b'\x00', q); e = b.index(b'\x00', e + 1)          # past cable, fiber
    q_fixed1 = e + 1                                              # fiber type + wl
    e = b.index(b'\x00', q_fixed1 + 4)
    e = b.index(b'\x00', e + 1); e = b.index(b'\x00', e + 1)     # past locA, locB, code
    q_fixed2 = e + 1                                              # build + offsets
    e = b.index(b'\x00', q_fixed2 + 10); e = b.index(b'\x00', e + 1)   # past op, comment
    tail = b[e + 1:]                                              # anything after

    out = b[:2]
    out += new['cable_id'] + b'\x00' + new['fiber_id'] + b'\x00'
    out += b[q_fixed1:q_fixed1 + 4]
    out += new['loc_a'] + b'\x00' + new['loc_b'] + b'\x00' + new['cable_code'] + b'\x00'
    out += b[q_fixed2:q_fixed2 + 10]
    out += new['operator'] + b'\x00' + new['comment'] + b'\x00'
    out += tail
    gp.body = bytes(out)
    return build(mv, bl)


# ─── safe write ───────────────────────────────────────────────────────────

# ─── span start / end ─────────────────────────────────────────────────────
# WHAT A SPAN EDIT ACTUALLY TOUCHES -- read off a file FastReporter 3 saved
# after its span was set to events 3 and 4 (desktop/tests/fixtures/frspan),
# diffed block by block and record by record against the untouched original:
#
#   GenParams   user_offset = the start event's time of travel,
#               user_offset_dist = its metres (0.1 m).  DataPts, FxdParams and
#               SupParams are byte-identical: the samples never move.
#   KeyEvents   every time of travel and all five LSA markers shifted by
#               -start, so the start event sits at 0 and anything ahead of it
#               goes negative; the OTDR-port event at exactly 0 is dropped;
#               events renumbered; the summary's total loss recomputed, its
#               markers shifted.
#   Proprietary every Position and Cursor*Position shifted by -start metres;
#               SpansLength = end - start; SpansLoss = event losses from the
#               start event to the end event, both inclusive, plus the
#               section losses between; IncludeSpanStart/End = 1; Status bit
#               64 moves to the start event and bit 128 to the end event;
#               ReflectiveEndOfFiber cleared.  All 574 records stay.
#   Replayed on that fixture pair this reproduces FR's file byte for byte in
#   every block except the two ORL numbers below (test_sor_span_write.py).
#
# Two things FR did that this does NOT reproduce, on purpose:
#   * TotalOrl (and the KeyEvents ORL markers) -- FR re-integrates the trace
#     over the new span; the algorithm is its own and a wrong number is worse
#     than the old one.  Left as-is and documented, not faked.
#   * Nothing here re-analyses.  Losses, reflectances and sections are the
#     instrument's; only the frame moves.
_PROP_SHIFT_FIELDS = (b'Position', b'CursorAPosition', b'CursorBPosition',
                      b'SubCursorAPosition', b'SubCursorBPosition')
_STATUS_SPAN_START, _STATUS_SPAN_END = 64, 128
_TOT_M_PER_UNIT = 0.02998            # metres per time-of-travel unit, x 1/IOR
SPAN_WRITE_SNAP_M = 20.0             # a declared km must land on an event


def _kev_parse(body: bytes):
    """-> (events, summary) -- events as dicts with the raw fields, summary
    as (total_loss_mdb, loss_start, loss_end, orl, orl_start, orl_end)."""
    n = struct.unpack_from('<H', body, 0)[0]
    p, evs = 2, []
    for _ in range(n):
        num, tot, slope, loss, refl = struct.unpack_from('<Hihhi', body, p)
        code = body[p + 14:p + 22]
        marks = list(struct.unpack_from('<iiiii', body, p + 22))
        p += 42
        j = body.index(b'\x00', p)
        evs.append({'num': num, 'tot': tot, 'slope': slope, 'loss': loss, 'refl': refl,
                    'code': code, 'marks': marks, 'comment': body[p:j]})
        p = j + 1
    if len(body) - p != 22:
        raise ValueError('KeyEvents summary is not 22 bytes; refusing')
    summary = list(struct.unpack_from('<iiiHii', body, p))
    return evs, summary


def _kev_build(evs, summary) -> bytes:
    out = bytearray(struct.pack('<H', len(evs)))
    for e in evs:
        out += struct.pack('<Hihhi', e['num'], e['tot'], e['slope'], e['loss'], e['refl'])
        out += e['code']
        out += struct.pack('<iiiii', *e['marks'])
        out += e['comment'] + b'\x00'
    out += struct.pack('<iiiHii', *summary)
    return bytes(out)


def _prop_typed(stream: bytes):
    """Every record with its decoded scalar, in stream order, RawSamples excluded."""
    lo, hi = _rawsamples_span(stream)
    out = []
    for r in _prop_records(stream):
        if lo is not None and lo <= r['pay'] < hi:
            continue
        if r['tc'] == 1 and r['size'] == 4:
            v = struct.unpack_from('<I', stream, r['pay'])[0]
        elif r['tc'] == 3 and r['size'] == 8:
            v = struct.unpack_from('<d', stream, r['pay'])[0]
        else:
            v = None
        out.append((r, v))
    return out


def _prop_event_groups(typed):
    """FR's per-event records, in order: [{'pos','status','loss','section'}].

    The stream is a run of Position-led segments.  A segment that carries a
    uint32 Type is an event (Position, Length, Comment, Type, Status, cursors,
    Loss, ...); one that does not is the section after the previous event
    (Position, Loss).  Read off the real layout, not assumed."""
    starts = [i for i, (r, v) in enumerate(typed) if r['name'] == 'Position' and r['tc'] == 3]
    groups = []
    for k, i in enumerate(starts):
        j = starts[k + 1] if k + 1 < len(starts) else len(typed)
        seg = typed[i + 1:j]
        is_event = any(r['name'] == 'Type' and r['tc'] == 1 for r, v in seg)
        if is_event:
            g = {'pos': typed[i], 'status': None, 'loss': None, 'section': None}
            for r, v in seg:
                if r['name'] == 'Status' and r['tc'] == 1 and g['status'] is None:
                    g['status'] = (r, v)
                elif r['name'] == 'Loss' and r['tc'] == 3 and g['loss'] is None:
                    g['loss'] = (r, v)
            groups.append(g)
        elif groups and groups[-1]['section'] is None:
            loss = next(((r, v) for r, v in seg if r['name'] == 'Loss' and r['tc'] == 3), None)
            groups[-1]['section'] = loss
    return groups


def read_span(data: bytes) -> dict:
    """The span the file carries: {'start_km', 'end_km', 'offset_km'} in the
    file's CURRENT event frame, from the KeyEvents codes and GenParams."""
    _, bl = split(data)
    ior = read_ior(data)
    evs, _ = _kev_parse(_find(bl, b'KeyEvents').body)
    gp = _find(bl, b'GenParams').body
    uo = struct.unpack_from('<i', gp, genparams_offsets(gp)['user_offset'])[0]
    km = lambda tot: tot * _TOT_M_PER_UNIT / ior / 1000.0
    end = next((e for e in evs if e['code'][1:2] == b'E'), None)
    return {'offset_km': km(uo), 'start_km': 0.0 if uo else None,
            'end_km': km(end['tot']) if end else None}


def set_span(data: bytes, start_km=None, end_km=None) -> bytes:
    """Return a new file with a declared span start and/or end, FR-style.

    `start_km` / `end_km` are in the file's RAW frame -- the frame the Viewer's
    span store speaks, where a file that already carries a span has had its
    offset added back.  Each must land within SPAN_WRITE_SNAP_M of an event;
    the event's own stored position is what is written, exactly as FR snaps.
    """
    if start_km is None and end_km is None:
        raise ValueError('nothing to change')
    mapver, bl = split(data)
    ior = read_ior(data)
    kev = _find(bl, b'KeyEvents')
    if len(kev.body) < 2 or struct.unpack_from('<H', kev.body, 0)[0] == 0:
        raise ValueError('no events; nothing to anchor a span to')
    evs, summary = _kev_parse(kev.body)
    gp = _find(bl, b'GenParams')
    go = genparams_offsets(gp.body)
    old_uo = struct.unpack_from('<i', gp.body, go['user_offset'])[0]
    old_uod = struct.unpack_from('<i', gp.body, go['user_offset_dist'])[0]
    m_per_tot = _TOT_M_PER_UNIT / ior

    def pick(km, what):
        raw_m = km * 1000.0
        best = min(range(len(evs)), key=lambda i: abs((evs[i]['tot'] + old_uo) * m_per_tot - raw_m))
        off = abs((evs[best]['tot'] + old_uo) * m_per_tot - raw_m)
        if off > SPAN_WRITE_SNAP_M:
            raise ValueError('span %s %.4f km is %.0f m from the nearest event; refusing'
                             % (what, km, off))
        return best

    i_start = pick(start_km, 'start') if start_km is not None else None
    i_end = pick(end_km, 'end') if end_km is not None else None
    if i_start is not None and i_end is not None and i_end <= i_start:
        raise ValueError('span end must be after span start')

    # ── proprietary: find the event groups first, they carry exact metres
    pb = next((b for b in bl if b.name.startswith(b'ExfoNewProprietaryBlock')), None)
    groups, stream, hdr, lens, tail = [], b'', b'', [], b''
    if pb is not None:
        hdr, chunks, tail = _prop_chunks(pb.body)
        decs = [d for _, d in chunks]
        lens = [len(d) for d in decs]
        stream = b''.join(decs)
        groups = _prop_event_groups(_prop_typed(stream))
        # KeyEvents and FR's records are matched by POSITION, not by index: a
        # file FR already re-based keeps the port event in its records but
        # not in KeyEvents, so the tables differ in length.
        gi = []
        for e in evs:
            m = e['tot'] * m_per_tot
            k = min(range(len(groups)), key=lambda j: abs(groups[j]['pos'][1] - m)) if groups else None
            if k is None or abs(groups[k]['pos'][1] - m) > 1.0:
                raise ValueError('no proprietary record within 1 m of the event at %.1f m; refusing' % m)
            gi.append(k)
        if len(set(gi)) != len(gi):
            raise ValueError('two events map to one proprietary record; refusing')
    else:
        gi = list(range(len(evs)))

    delta_tot = evs[i_start]['tot'] if i_start is not None else 0
    if groups and i_start is not None:
        delta_m = groups[gi[i_start]]['pos'][1]
    else:
        delta_m = delta_tot * m_per_tot

    # ── KeyEvents
    new_evs = []
    for i, e in enumerate(evs):
        if delta_tot > 0 and e['tot'] == 0 and old_uo == 0:
            continue    # the OTDR port event (raw-frame 0): FR drops it.  A file
                        # already re-based has its START at 0, which stays.
        q = dict(e)
        q['tot'] = e['tot'] - delta_tot
        q['marks'] = [m - delta_tot for m in e['marks']]
        code = bytearray(e['code'])
        if i_end is not None:
            code[1:2] = b'E' if i == i_end else (b'F' if code[1:2] == b'E' else code[1:2])
        q['code'] = bytes(code)
        new_evs.append(q)
    for k, q in enumerate(new_evs, 1):
        q['num'] = k
    if len(new_evs) < len(evs) and new_evs:
        # FR's file: the event that becomes first carries slope 0, the way the
        # port event did -- the slope is the section INTO the event, and the
        # first row has none.
        new_evs[0]['slope'] = 0
    end_idx = i_end if i_end is not None else next(
        (i for i, e in enumerate(evs) if e['code'][1:2] == b'E'), len(evs) - 1)
    start_idx = i_start if i_start is not None else 0

    # SpansLoss: every event loss from the start event to the END event, both
    # inclusive, plus the sections between them.  The end event's own loss
    # counts: FR's Summary shows 0.471 dB for an end declared on the 0.616 dB
    # event 3 of the fixture (0.187 + -0.333 + 0.616), and it read 0.802 on
    # its own file only because that end event's loss is NaN.  From the
    # proprietary doubles when present (what FR sums), else Bellcore mdB.
    def _f(x):
        return 0.0 if x is None or x != x else x
    if groups:
        tot_loss = 0.0
        for k in range(gi[start_idx], gi[end_idx] + 1):
            g = groups[k]
            tot_loss += _f(g['loss'][1] if g['loss'] else None)
            if k < gi[end_idx]:
                tot_loss += _f(g['section'][1] if g['section'] else None)
    else:
        tot_loss = sum(e['loss'] for e in evs[start_idx:end_idx + 1]) / 1000.0
    summary[0] = int(round(tot_loss * 1000))
    summary[1] -= delta_tot
    summary[2] = evs[end_idx]['tot'] - delta_tot
    summary[4] -= delta_tot
    summary[5] -= delta_tot
    kev.body = _kev_build(new_evs, summary)

    # ── GenParams
    body = bytearray(gp.body)
    struct.pack_into('<i', body, go['user_offset'], old_uo + delta_tot)
    struct.pack_into('<i', body, go['user_offset_dist'], old_uod + int(round(delta_m * 10)))
    gp.body = bytes(body)

    # ── proprietary
    if pb is not None:
        s = bytearray(stream)
        lo, hi = _rawsamples_span(stream)
        raw_before = stream[lo:hi] if lo is not None else b''
        typed = _prop_typed(stream)
        for r, v in typed:
            if r['tc'] == 3 and r['name'].encode() in _PROP_SHIFT_FIELDS and v == v:
                struct.pack_into('<d', s, r['pay'], v - delta_m)
        end_pos = groups[gi[end_idx]]['pos'][1] - delta_m
        start_pos = groups[gi[start_idx]]['pos'][1] - delta_m
        def put1(name, val, tc='<d'):
            hits = [r for r, v in typed if r['name'] == name and r['tc'] == (3 if tc == '<d' else 1)]
            if len(hits) != 1:
                raise ValueError('expected exactly one %s record, found %d' % (name, len(hits)))
            struct.pack_into(tc, s, hits[0]['pay'], val)
        put1('SpansLength', end_pos - start_pos)
        put1('SpansLoss', tot_loss)
        if i_start is not None:
            put1('IncludeSpanStart', 1, '<I')
        if i_end is not None:
            put1('IncludeSpanEnd', 1, '<I')
        put1('ReflectiveEndOfFiber', 0, '<I')
        for k, g in enumerate(groups):
            if g['status'] is None:
                continue
            r, v = g['status']
            v &= ~(_STATUS_SPAN_START | _STATUS_SPAN_END)
            if k == gi[start_idx]:
                v |= _STATUS_SPAN_START
            if k == gi[end_idx]:
                v |= _STATUS_SPAN_END
            struct.pack_into('<I', s, r['pay'], v)
        s = bytes(s)
        if lo is not None and s[lo:hi] != raw_before:
            raise ValueError('RawSamples payload changed; refusing to write')
        decs, p = [], 0
        for ln in lens:
            decs.append(s[p:p + ln]); p += ln
        pb.body = _prop_rebuild(hdr, decs, tail)

    return build(mapver, bl)


def write(data: bytes, dst: str, src: str | None = None, overwrite: bool = False) -> str:
    """Write `data` to `dst`.  Refuses the source path and existing files."""
    dst_abs = os.path.abspath(dst)
    if src is not None and os.path.abspath(src) == dst_abs:
        raise ValueError('refusing to write over the source file')
    if os.path.exists(dst_abs) and not overwrite:
        raise FileExistsError(dst_abs)
    tmp = dst_abs + '.part'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, dst_abs)
    return dst_abs



# ─── Editing a trace's settings from the Viewer ─────────────────────────
# The boss's ask: "edit settings on internal trace and save that .sor (IOR
# is wrong and correct with software, trace name ...)".  The writer above
# does the bytes; this is the thin layer the Viewer's dialog talks to.
#
# Two rules the dialog cannot break, because they are enforced here:
#   * Copies only.  Every edited file lands in a SIBLING folder of the source
#     folder (default "<folder> edited"), never in the source folder, and
#     never over an existing file.  The originals are customer measurement
#     records and sometimes the only copy.
#   * A whole direction at once, or one fiber.  A wrong IOR is wrong for the
#     whole shoot, so "all fibers" is the common case; a fiber id is the one
#     field that is per-fiber, so it is refused for the all-fibers scope
#     rather than silently stamped on every file.
EDITED_SUFFIX = ' edited'


def _fiber_path(directory, fiber):
    fmap = {n: fn for n, fn in list_fibers(directory)}
    fn = fmap.get(int(fiber))
    return os.path.join(directory, fn) if fn else None


def _dest_default(directory):
    return os.path.basename(os.path.normpath(directory)) + EDITED_SUFFIX


def _dest_dir(directory, dest_name):
    """The sibling folder edited copies go to.  A bare NAME, not a path: the
    tech picks what to call it, the server decides where it lives."""
    name = (dest_name or '').strip() or _dest_default(directory)
    if os.path.basename(name) != name or name in ('.', '..'):
        raise ValueError('destination must be a folder name, not a path')
    parent = os.path.dirname(os.path.normpath(directory))
    dest = os.path.join(parent, name)
    if os.path.normcase(os.path.abspath(dest)) == os.path.normcase(os.path.abspath(directory)):
        raise ValueError('destination is the source folder; copies only')
    return dest


def trace_settings(direction, fiber, dir_a=None, dir_b=None):
    """What the edit dialog pre-fills: the file's IOR and identifiers.

    Returns {'filename', 'editable', 'why', 'ior', 'identifiers',
    'dest_default'} - a JSON file, or a .sor that does not round-trip, is
    reported as not editable with the reason, rather than 404ing.
    """
    d = (dir_a or CONFIG['dir_a']) if direction == 'a' else (dir_b or CONFIG['dir_b'])
    if direction not in ('a', 'b') or not d:
        raise ValueError('direction must be a or b, with a folder loaded')
    path = _fiber_path(d, fiber)
    if path is None:
        raise ValueError('no file for fiber %s' % fiber)
    out = {'filename': os.path.basename(path), 'editable': False, 'why': '',
           'ior': None, 'identifiers': {}, 'dest_default': _dest_default(d)}
    if not path.lower().endswith('.sor'):
        out['why'] = 'only .sor files can be edited (this is a JSON export)'
        return out
    raw = open(path, 'rb').read()
    try:
        if not roundtrip_ok(raw):
            out['why'] = 'this file does not rebuild byte-exact; refusing to edit it'
            return out
        out['ior'] = read_ior(raw)
        # FR's records first, GenParams over them: GenParams is what every
        # reader of ours speaks, and the two agree on every real file seen.
        ids = read_fr_identifiers(raw)
        ids.update({k: v for k, v in read_identifiers(raw).items() if v})
        out['identifiers'] = ids
    except Exception as e:                       # noqa: BLE001 - a parse
        out['why'] = 'could not read this file: %s' % e   # failure is an answer
        return out
    out['editable'] = True
    return out


def edit_traces(direction, fibers, ior=None, fields=None, dest_name=None,
                dir_a=None, dir_b=None, span=None):
    """Write edited COPIES of one fiber's file or every file in a direction.

    `fibers` is 'all' or a list of fiber numbers.  `ior` None = unchanged.
    `fields` maps identifier names (ALL_STRINGS) to new text; blank = unchanged.
    `span` is {'start_km', 'end_km'} in the direction's raw frame (the span
    store's frame); each fiber snaps to its own event, as the store promises.
    Returns {'dest', 'written': [fiber...], 'skipped': [{'fiber','reason'}]}.
    Per-file failures skip that file and say why; they never stop the batch.
    """
    d = (dir_a or CONFIG['dir_a']) if direction == 'a' else (dir_b or CONFIG['dir_b'])
    if direction not in ('a', 'b') or not d:
        raise ValueError('direction must be a or b, with a folder loaded')
    fields = {k: str(v) for k, v in (fields or {}).items() if str(v).strip() != ''}
    bad = sorted(set(fields) - set(ALL_STRINGS))
    if bad:
        raise ValueError('not editable identifiers: %s' % bad)
    if ior is not None:
        ior = float(ior)
        if not (_IOR_SANE_MIN <= ior <= _IOR_SANE_MAX):
            raise ValueError('IOR %.5f is outside the sane band %.2f-%.2f'
                             % (ior, _IOR_SANE_MIN, _IOR_SANE_MAX))
    span = {k: float(v) for k, v in (span or {}).items()
            if k in ('start_km', 'end_km') and v is not None}
    if ior is None and not fields and not span:
        raise ValueError('nothing to change')
    all_fibers = [n for n, _ in list_fibers(d)]
    if fibers == 'all':
        if 'fiber_id' in fields:
            raise ValueError('a fiber id is per-fiber; edit one fiber at a time to change it')
        todo = all_fibers
    else:
        todo = sorted({int(f) for f in fibers})
        if not todo:
            raise ValueError('no fibers given')
    dest = _dest_dir(d, dest_name)
    os.makedirs(dest, exist_ok=True)
    written, skipped = [], []
    for n in todo:
        path = _fiber_path(d, n)
        if path is None:
            skipped.append({'fiber': n, 'reason': 'no file'}); continue
        if not path.lower().endswith('.sor'):
            skipped.append({'fiber': n, 'reason': 'not a .sor'}); continue
        dst = os.path.join(dest, os.path.basename(path))
        if os.path.exists(dst):
            skipped.append({'fiber': n, 'reason': 'already exists in ' + os.path.basename(dest)}); continue
        try:
            raw = open(path, 'rb').read()
            if not roundtrip_ok(raw):
                skipped.append({'fiber': n, 'reason': 'does not rebuild byte-exact'}); continue
            out = raw
            if ior is not None:
                out = set_ior(out, ior)
            if fields:
                out = set_identifiers(out, **fields)
            if span:
                out = set_span(out, start_km=span.get('start_km'), end_km=span.get('end_km'))
            write(out, dst, src=path)
            written.append(n)
        except Exception as e:                   # noqa: BLE001 - one bad file
            skipped.append({'fiber': n, 'reason': str(e)[:200]})   # must not stop the batch
    return {'dest': dest, 'written': written, 'skipped': skipped}


def _main():
    import argparse
    import time
    import webbrowser
    ap = argparse.ArgumentParser(description='OTDR Suite trace server (standalone)')
    ap.add_argument('--dir-a', default=None)
    ap.add_argument('--dir-b', default=None)
    ap.add_argument('--port', type=int, default=8771)
    ap.add_argument('--no-browser', action='store_true')
    args = ap.parse_args()
    set_dirs(args.dir_a, args.dir_b)
    port = start_in_thread(args.port)
    url = f'http://127.0.0.1:{port}/'
    print(f'Trace server at {url}')
    if not args.no_browser:
        time.sleep(0.3)
        webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print('\nShutting down.')
        _server.shutdown()


if __name__ == '__main__':
    _main()
