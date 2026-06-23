"""cable_db — cable construction/size → expected helix (EFL% / m) knowledge.

This is the "know the helix factor we are dealing with" layer.  Each entry
maps a cable construction TYPE (and optionally a SIZE / fiber-count class)
to the *expected* band for two equivalent quantities:

  * EFL%  — excess fiber length percentage, EFL% = (1/m − 1)·100.
  * m     — the conversion factor cable-sheath-distance / OTDR-fiber-distance
            (so m < 1: the fiber is longer than the sheath because it follows
            a helical/SZ-stranded path inside the tube).

Source of the seed numbers: Corning Application Engineering Note AEN-142
("Calculating Sheath Distance from Optical Distance"):

    stranded loose-tube   EFL ≈ 2–3 %   →  m ≈ 0.970 – 0.980
    central (mono) tube   EFL ≈ 0–1 %   →  m ≈ 0.990 – 1.000

The band is the SANITY band used by ``calibrate``: a fitted m outside the
selected type's band is almost certainly a wrong IOR or a mismatched anchor,
NOT a real cable factor, so the calibration warns instead of silently
returning it.

Design goals
------------
* **Extensible** — entries live in a registry keyed by a canonical type id.
  ``register()`` adds/overrides an entry; ``all_types()`` lists them.  New
  cable families (ribbon, micro-duct, ADSS, etc.) are one ``register`` call.
* **Two ways to pick a type**:
    1. AUTO from the SOR GenParams (``detect_from_genparams``) — reads the
       cable_code / cable_id strings and matches known construction tokens.
       On the HOWESPAN→LANCASTER span those GenParams fields are EMPTY/junk,
       so this returns ``None`` and the caller must fall back (by design).
    2. MANUAL — a cable_type string supplied by the user (CLI flag, anchor
       table header, or the OTDR-settings cable-type dropdown in the app).
  ``resolve_cable_type`` applies the precedence explicit → auto → default and
  reports WHICH path won, so the report can say how the type was obtained.
"""

from dataclasses import dataclass, field
from typing import Optional


# ── Entry container ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class CableEntry:
    """One cable construction's expected helix band.

    ``m_low``/``m_high`` is the authoritative sanity band used by calibrate.
    ``efl_low``/``efl_high`` is the same band expressed as EFL%, kept for the
    report and for humans (the two are tied: EFL% = (1/m − 1)·100, so the
    EFL band is the m band inverted — efl_low corresponds to m_high).
    ``match_tokens`` are uppercase substrings that, if present in a GenParams
    cable_code/cable_id string, identify this construction.
    """
    key: str
    label: str
    construction: str          # human family, e.g. "stranded loose-tube"
    size: Optional[str]        # optional fiber-count / size class, or None
    m_low: float
    m_high: float
    efl_low: float
    efl_high: float
    source: str = "Corning AEN-142"
    match_tokens: tuple = field(default_factory=tuple)

    @property
    def m_band(self):
        return (self.m_low, self.m_high)

    @property
    def efl_band(self):
        return (self.efl_low, self.efl_high)

    @property
    def m_nominal(self):
        return 0.5 * (self.m_low + self.m_high)


def _efl_from_m(m):
    return (1.0 / m - 1.0) * 100.0


# ── Fiber group index (IOR) reference ───────────────────────────────────
# Corning SMF-28 Ultra / SMF-28e SPECIFIED effective group index of refraction
# (neff) — the value an OTDR uses to convert time-of-flight to fiber distance,
# and the value the IOR guardrail checks each trace's stored IOR against.  A
# 0.1% IOR error swamps the whole helix effect, so this is the reference for
# --expected-ior when the span uses SMF-28-family fiber.
# Source: Corning SMF-28 Ultra PI sheet (PI-1424-AEN) + SMF-28e datasheet
# (web research, 2026-06; SMF-28e reads 1.4677 @1310, Ultra 1.4676 @1310).
SMF28_GROUP_INDEX = {1310: 1.4676, 1550: 1.4682}


def reference_ior(wavelength_nm=1550):
    """SMF-28 Ultra/e specified group index for the OTDR IOR / guardrail
    (defaults to the 1550 nm value, 1.4682)."""
    return SMF28_GROUP_INDEX.get(int(round(wavelength_nm)), SMF28_GROUP_INDEX[1550])


def _entry(key, label, construction, m_low, m_high, size=None,
           source="Corning AEN-142", match_tokens=()):
    """Build a CableEntry, deriving the EFL band from the m band so the two
    can never drift apart.  (efl_low ↔ m_high, efl_high ↔ m_low.)"""
    return CableEntry(
        key=key, label=label, construction=construction, size=size,
        m_low=m_low, m_high=m_high,
        efl_low=round(_efl_from_m(m_high), 4),
        efl_high=round(_efl_from_m(m_low), 4),
        source=source,
        match_tokens=tuple(t.upper() for t in match_tokens),
    )


# ── Registry ────────────────────────────────────────────────────────────
# Canonical type id → CableEntry.  Seeded from AEN-142; extend with register().
_REGISTRY = {}


def register(entry):
    """Add or override a CableEntry in the registry (keyed by entry.key)."""
    if not isinstance(entry, CableEntry):
        raise TypeError("register expects a CableEntry")
    _REGISTRY[entry.key] = entry
    return entry


def _seed():
    register(_entry(
        "stranded_loose_tube",
        "stranded loose-tube (AEN-142)",
        "stranded loose-tube",
        m_low=0.970, m_high=0.980,
        match_tokens=("LOOSE", "LT", "SLT", "STRAND", "ALTOS", "SST"),
    ))
    register(_entry(
        "central_tube",
        "central tube (AEN-142)",
        "central / mono tube",
        m_low=0.990, m_high=1.000,
        match_tokens=("CENTRAL", "CT", "MONOTUBE", "MONO", "SMU", "MINI"),
    ))
    # Modern Corning CENTRAL-TUBE ribbon (MiniXtend / Contour Flow — the family
    # in use here, incl. the HOWLAN 864F span) carries ~0 to <1% EFL = the
    # CENTRAL-tube band (m ≈ 0.99–1.00), NOT the 2–3% loose-tube band the seed
    # assumed.  Verified by web research (Corning / cablinginstall: "a typical
    # ribbon cable has zero to a fraction of 1 percent excess fiber length"; the
    # inflated 2–8% figure was refuted) and corroborated by the HOWLAN ~0.8%
    # cross-fiber measurement.  Older stranded-loose-tube ribbon should be set
    # manually to 'stranded_loose_tube'.
    register(_entry(
        "ribbon",
        "central-tube ribbon (Corning MiniXtend / Contour Flow)",
        "central-tube ribbon",
        m_low=0.990, m_high=1.000,
        source="Corning ribbon EFL (web research 2026-06) + AEN-142 central-tube band",
        match_tokens=("RIBBON", "RBN", "MINIXTEND", "XTEND", "CONTOUR", "FLOW", "MTC"),
    ))


_seed()


# ── Lookups ─────────────────────────────────────────────────────────────
DEFAULT_CABLE_TYPE = "stranded_loose_tube"


def get(cable_type):
    """Return the CableEntry for ``cable_type`` or ``None`` if unknown."""
    if cable_type is None:
        return None
    return _REGISTRY.get(cable_type)


def all_types():
    """Sorted list of registered cable_type keys."""
    return sorted(_REGISTRY.keys())


def entries():
    """All CableEntry objects (for the report / a settings dropdown)."""
    return [_REGISTRY[k] for k in all_types()]


def band_for(cable_type):
    """Return the (m_low, m_high, label) sanity-band tuple for ``cable_type``,
    or ``None`` if the type is unknown.  This is the shape ``calibrate``
    historically consumed (CABLE_TYPE_BANDS values)."""
    e = get(cable_type)
    if e is None:
        return None
    return (e.m_low, e.m_high, e.label)


def bands_map():
    """Back-compat view: {cable_type: (m_low, m_high, label)} for every
    registered entry.  ``calibrate.CABLE_TYPE_BANDS`` is an alias of this so
    existing callers/tests keep working while the data lives here."""
    return {k: band_for(k) for k in all_types()}


# ── GenParams-based auto-detection ──────────────────────────────────────
def detect_from_genparams(genparams):
    """Try to identify the cable construction from the SOR GenParams.

    ``genparams`` is the dict from ``sor_fields.read_genparams`` (keys
    ``cable_id``, ``location_a``, ``location_b``; ``cable_code`` may also be
    present if the caller surfaces it).  We scan the cable_code and cable_id
    strings for any registered entry's match tokens.

    Returns ``(cable_type, matched_token)`` on a hit, or ``(None, None)`` when
    no construction token is present.  On the HOWESPAN→LANCASTER span the
    cable_code/cable_id fields are empty/junk, so this returns ``(None, None)``
    and the caller MUST fall back to the manual setting — that fallback is the
    designed behavior here, not an error.
    """
    if not genparams:
        return (None, None)
    hay = " ".join(
        str(genparams.get(k) or "")
        for k in ("cable_code", "cable_id", "cable_type")
    ).upper()
    hay_compact = hay.strip()
    if not hay_compact:
        return (None, None)
    # Prefer longer, more specific tokens first so 'RIBBON' beats 'RR', etc.
    candidates = []
    for e in entries():
        for tok in e.match_tokens:
            candidates.append((len(tok), tok, e.key))
    for _, tok, key in sorted(candidates, reverse=True):
        # Word-ish boundary: token must be surrounded by non-alphanumerics or
        # string ends, so a stray 'CT' inside 'OCTANE' does not match.
        if _token_present(hay, tok):
            return (key, tok)
    return (None, None)


def _token_present(hay, tok):
    """True if ``tok`` appears in ``hay`` at an alnum boundary."""
    start = 0
    n = len(tok)
    while True:
        i = hay.find(tok, start)
        if i < 0:
            return False
        before = hay[i - 1] if i > 0 else ""
        after = hay[i + n] if i + n < len(hay) else ""
        if not before.isalnum() and not after.isalnum():
            return True
        start = i + 1


# ── Resolution with provenance ──────────────────────────────────────────
@dataclass
class CableTypeResolution:
    cable_type: str
    source: str                 # 'manual' | 'genparams' | 'default'
    entry: Optional[CableEntry]
    matched_token: Optional[str] = None
    note: str = ""

    @property
    def band(self):
        return None if self.entry is None else (
            self.entry.m_low, self.entry.m_high, self.entry.label)


def resolve_cable_type(explicit=None, genparams=None,
                       default=DEFAULT_CABLE_TYPE):
    """Decide which cable_type to use and report HOW it was decided.

    Precedence (highest first):
      1. ``explicit`` — a manual cable_type the user supplied (CLI flag,
         anchor-table header, or the OTDR-settings cable-type dropdown).  If
         it is a known registered type it wins and source='manual'.  If it is
         given but unknown, we keep it (so the band check is skipped and
         warns) and note that.
      2. ``genparams`` auto-detect — if the SOR cable_code/cable_id carries a
         recognizable construction token, source='genparams'.
      3. ``default`` — the package default, source='default'.

    Returns a CableTypeResolution.  This is what lets the report say e.g.
    "cable type stranded_loose_tube (manual)" vs "(auto from GenParams: LT)"
    vs "(default — GenParams empty on this span)".
    """
    # 1) explicit / manual
    if explicit:
        entry = get(explicit)
        if entry is not None:
            return CableTypeResolution(
                cable_type=explicit, source="manual", entry=entry,
                note="manual cable-type setting")
        # explicit but unknown — honor it but flag that no band applies
        return CableTypeResolution(
            cable_type=explicit, source="manual", entry=None,
            note=f"manual cable-type {explicit!r} is not in the cable DB; "
                 f"sanity-band check will be skipped")

    # 2) auto from GenParams
    key, tok = detect_from_genparams(genparams)
    if key is not None:
        return CableTypeResolution(
            cable_type=key, source="genparams", entry=get(key),
            matched_token=tok,
            note=f"auto-detected from GenParams (matched {tok!r})")

    # 3) default fallback
    entry = get(default)
    gp_empty = not (genparams and any(
        str(genparams.get(k) or "").strip()
        for k in ("cable_code", "cable_id")))
    why = ("GenParams cable_code/cable_id empty — using default"
           if gp_empty else
           "no construction token recognized in GenParams — using default")
    return CableTypeResolution(
        cable_type=default, source="default", entry=entry, note=why)
