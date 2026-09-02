#!/usr/bin/env python3
"""Trace server for the OTDR Suite viewer page.

A small HTTP server that parses OTDR SOR/JSON files on demand and serves
trace + event JSON to the canvas viewer (viewer.html).  Designed to run as
a background daemon thread *inside* the Streamlit hub process so the whole
suite ships as one launcher.

The A/B folders are held in a module-level CONFIG dict that the hub writes
when the user picks folders in the sidebar — same process, shared state, no
second config channel needed.

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
import threading
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import numpy as np

# These resolve from the viewer/ package dir, which the hub puts on sys.path.
from sor_reader324802a import parse_sor_full, _sor_ior_from_events, _sor_first_pos_m, parse_genparams
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
CONFIG = {'dir_a': None, 'dir_b': None}

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


def engine_thresholds():
    """{'reburn': 0.16, 'uni_bend': 0.10, 'single_dir': 0.25} from the engine."""
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
