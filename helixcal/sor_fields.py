"""sor_fields — thin adapter over the suite's existing SOR parser.

This is the ONLY module in helixcal that touches SOR binary offsets, and it
does so by *reusing* the existing parser
(``splicereport/sor_reader324802a.py``).  It never forks or rewrites the
block-directory / FxdParams / KeyEvents parsing.

It adds the two fields ``parse_sor_full`` does not surface but the
calibration needs:

  * ``read_stored_ior``   — the STORED group index from the FxdParams block
                            (FxdParams body + 28, uint32 = IOR * 100000).
                            This is the guardrail value.  It is NOT the same
                            as ``sor_reader._read_ior`` (an unanchored scan of
                            the first 1000 bytes) — that one is used only as a
                            last-resort fallback / cross-check here.
  * ``read_genparams``    — the GenParams location/fiber-id strings.  The
                            stock ``_parse_block_directory`` resolves the
                            GenParams *body* to the directory-header
                            occurrence (GenParams is the first block, so
                            ``find`` lands on the directory entry, not the
                            data block).  We fix that by searching the LAST
                            occurrence of ``b'GenParams\\x00'`` and bounding by
                            the SupParams offset.

``read_trace_record`` is the convenience that the calibration consumes: it
runs ``parse_sor_full`` and augments the dict with stored IOR, derived IOR,
GenParams locations and the EOF distance.
"""

import os
import struct
import sys

# ── Reuse the suite's parser (no fork) ──────────────────────────────────
# The suite imports it flat (``from sor_reader324802a import ...``) with
# ``splicereport/`` on sys.path.  Mirror that contract so we share the exact
# same parser instance rather than a copy.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SPLICEREPORT_DIR = os.path.normpath(os.path.join(_THIS_DIR, os.pardir, "splicereport"))
if _SPLICEREPORT_DIR not in sys.path:
    sys.path.insert(0, _SPLICEREPORT_DIR)

import sor_reader324802a as _sr  # noqa: E402  (path set above)


# ── Constants ───────────────────────────────────────────────────────────
# FxdParams body offset of the stored IOR (verified on the EXFO HOWESPAN→
# LANCASTER span: ver 200, body size 92, num_pw == 1).  The offset assumes a
# single pulse-width (num_pw == 1); for multi-PW files the layout shifts, so
# we sanity-bound the value and fall back rather than trust a stray uint32.
_FXD_IOR_OFFSET = 28
_IOR_U32_SCALE = 100000.0
# Plausible single-mode group-index band (1310/1550 nm).  A stored value
# outside this is treated as a bad read, not a real IOR.
_IOR_SANE_MIN = 1.40
_IOR_SANE_MAX = 1.55


def read_stored_ior_from_blocks(data, blocks):
    """Read the STORED IOR from an already-parsed file.

    ``data`` is the raw bytes; ``blocks`` is the dict returned by
    ``sor_reader324802a._parse_block_directory(data)``.  Returns the IOR as a
    float, or ``None`` if FxdParams is missing or the value is out of the
    sane band (in which case the caller should fall back to a derived IOR and
    flag the trace).
    """
    fxd = blocks.get("FxdParams")
    if not fxd:
        return None
    body = fxd.get("body")
    if body is None:
        return None
    try:
        raw = struct.unpack_from("<I", data, body + _FXD_IOR_OFFSET)[0]
    except struct.error:
        return None
    ior = raw / _IOR_U32_SCALE
    if _IOR_SANE_MIN <= ior <= _IOR_SANE_MAX:
        return ior
    return None


def read_stored_ior(filepath):
    """Open ``filepath`` and return its STORED IOR (FxdParams body+28),
    or ``None`` if it cannot be read in the sane band."""
    with open(filepath, "rb") as f:
        data = f.read()
    blocks = _sr._parse_block_directory(data)
    return read_stored_ior_from_blocks(data, blocks)


def read_genparams_from_blocks(data, blocks):
    """Read GenParams location/fiber-id strings.

    Works around the directory-vs-data ambiguity by searching the LAST
    occurrence of ``b'GenParams\\x00'`` (the data block) and bounding the
    read by the SupParams offset.  Returns a dict with keys
    ``cable_id`` (fiber id string, e.g. 'HOWLAN001'), ``location_a``
    (originating, e.g. 'HOWESPAN'), ``location_b`` (terminating, e.g.
    'LANCASTER').  Missing fields come back as empty strings.

    NOTE: on this span the cable_code field is junk/non-informative, so we do
    NOT surface it — cable-type must be supplied manually (see calibrate).
    """
    out = {"cable_id": "", "location_a": "", "location_b": ""}
    needle = b"GenParams\x00"
    last = data.rfind(needle)
    if last < 0:
        return out
    gp_body = last + len(needle)
    # Bound by the next block start so we don't run into SupParams strings.
    sup = blocks.get("SupParams") or {}
    end = sup.get("offset")
    if not end or end <= gp_body:
        end = min(len(data), gp_body + 256)
    chunk = data[gp_body:end]
    parts = chunk.split(b"\x00")

    def _clean(b):
        # Strip a leading 2-byte language code and non-printable bytes,
        # keep the trailing printable run (the location code sits after a
        # short binary prefix on field[2]).
        s = b.decode("latin-1", errors="replace")
        printable = "".join(ch for ch in s if 32 <= ord(ch) < 127)
        return printable.strip()

    # parts[0] = 'EN ' language code; [1] = fiber/cable id; [2] tail =
    # originating location (after a binary prefix); [3] = terminating.
    if len(parts) > 1:
        out["cable_id"] = _clean(parts[1])
    if len(parts) > 2:
        out["location_a"] = _clean(parts[2])
    if len(parts) > 3:
        out["location_b"] = _clean(parts[3])
    return out


def read_genparams(filepath):
    """Open ``filepath`` and return GenParams locations (see
    ``read_genparams_from_blocks``)."""
    with open(filepath, "rb") as f:
        data = f.read()
    blocks = _sr._parse_block_directory(data)
    return read_genparams_from_blocks(data, blocks)


def _eof_km(events):
    """End-of-fiber distance: the dist_km of the first is_end (1E) event."""
    for e in events or []:
        if e.get("is_end"):
            return e.get("dist_km")
    return None


def read_trace_record(filepath):
    """Parse one .sor into a calibration-ready record.

    Returns a dict augmenting ``parse_sor_full`` with:
        stored_ior      — FxdParams body+28 (None if unreadable/out-of-band)
        derived_ior     — event-derived IOR cross-check (_sor_ior_from_events)
        ior_source      — 'stored' | 'derived' | 'fallback'
        ior             — the IOR actually trusted for this trace
        genparams       — {cable_id, location_a, location_b}
        eof_km          — span length (1E event dist_km)
        events          — KeyEvents (passthrough)
        wavelength      — passthrough
        filename/filepath — passthrough

    Returns ``None`` if the file is not a parseable trace (mirrors
    ``parse_sor_full`` returning None).
    """
    full = _sr.parse_sor_full(filepath)
    if full is None:
        return None
    with open(filepath, "rb") as f:
        data = f.read()
    blocks = _sr._parse_block_directory(data)

    stored = read_stored_ior_from_blocks(data, blocks)
    derived = _sr._sor_ior_from_events(full)

    if stored is not None:
        ior, ior_source = stored, "stored"
    elif derived is not None:
        ior, ior_source = derived, "derived"
    else:
        ior, ior_source = _sr._read_ior(data), "fallback"

    rec = {
        "filename": full.get("filename"),
        "filepath": full.get("filepath", filepath),
        "events": full.get("events") or [],
        "wavelength": full.get("wavelength"),
        "acq_range": full.get("acq_range"),
        "stored_ior": stored,
        "derived_ior": derived,
        "ior": ior,
        "ior_source": ior_source,
        "genparams": read_genparams_from_blocks(data, blocks),
        "eof_km": _eof_km(full.get("events")),
    }
    return rec
