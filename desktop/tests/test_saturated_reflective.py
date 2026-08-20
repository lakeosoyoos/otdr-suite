"""Event-type code `2` (SATURATED REFLECTIVE) must read as reflective.

THE BUG.  All three sor_reader324802a.py copies decoded the Bellcore /
Telcordia SR-4731 KeyEvents code with

    'is_reflective': evt_type[:1] == '1'

The first character of the `xy9999` code is the reflection class and the
standard defines THREE values, not two:

    '0'  non-reflective
    '1'  reflective
    '2'  saturated reflective — the return drove the receiver to its
         ceiling, so the stored reflectance is a floor, not a measurement

so every `2*` event was filed with the NON-reflective events.

WHAT IT COST.  WSC<->SUI fiber 34's Suisun-end launch connector is stored
`2F9999LS` carrying -25.072 dB — the worst reflectance on that 1152-fiber
cable, and the only 2F event in the span's 4,608 files.  The field team's
sheet flags it ("34 -25.1 (Near)"); our report did not.  Cascade: with
is_reflective False the launch pattern (port at tot 0, then a reflective
event inside LAUNCH_FIBER_MAX) does not match, so
`_untrimmed_launch_offset_km` returns 0.0, the event frame is never
shifted, and `_fiber_launch_info` reads events[0] — the OTDR port at
-62.123 dB — as the launch connector.  `_a_launch_conn_event` keys off the
same pattern, so the connector was invisible to the loss gate too.  The
frame error also swept F34's Suisun box connector (landing 917 m off) into
the 63.9701 km splice column and printed a false `34 .255` reburn cell.

The control is in this directory: F491 sits at the SAME position (1.0044
km) with a plain `1F9999LS` and flagged correctly all along.

THE EVIDENCE FOR THE PREDICATE.  Census of 12,960 .sor across 7 spans
(WSC<->SUI, ONT<->BOI, SAN<->DUR, SEA<->NOR, MIL<->TOP, TUL<->ORO,
TUL<->BAR), 125,468 events.  First character is only ever 0, 1 or 2:

    '0'  89,385 events —      2 carry a reflectance ( 0.00%)
    '1'  34,393 events — 30,185 carry a reflectance (87.76%)
    '2'   1,690 events —  1,690 carry a reflectance (100.0%)

and the '2' population is pinned at the physical ceiling (p10 -15.82,
median -15.64, p90 -15.45, max -15.18 dB; the glass/air Fresnel limit is
about -14.7 dB) while '1' spans -79.8 .. -11.6 dB.  A code that ALWAYS
records a reflectance and only ever lands at the top of the scale does not
belong with '0', which records one essentially never.  Independently:
`2E` is a common end-of-fiber code (858 of TUL<->BAR's 862 end events,
828 of MIL<->TOP's) and `is_end` already honours it — an end-of-fiber
event that is not reflective is a contradiction.

Fixtures are real, unmodified acquisitions from
/tmp/ws2/Final Testing/Long/Susisun (task #110's 08-18 data set).

NOT COVERED HERE, deliberately: WSC<->SUI F242's launch connector is
stored as a genuine non-reflective `0F9999LS` with reflectance 0.000.  It
hits the same launch-normalization cascade but it is NOT a type-code
misread — recovering it means relaxing the engine's launch-detection
pattern, which is a separate change with its own false-positive risk
(TUL<->BAR carries 4 fibers whose second event is a non-reflective event
at 0.08-0.19 km, nowhere near a launch connector).
"""
import importlib.util
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
FIXDIR = os.path.join(HERE, 'fixtures', 'satrefl')

SAT = os.path.join(FIXDIR, 'SUIWSC0034.sor')     # 2F9999LS launch connector
CTL = os.path.join(FIXDIR, 'SUIWSC0491.sor')     # 1F9999LS, same position

# The three engines ship deliberately isolated copies under ONE module name,
# so they cannot all be imported by name in one interpreter.  Load each from
# its own path under a distinct alias instead.
ENGINES = ('splicereport', 'viewer', 'secretsauce')


def _reader(engine):
    path = os.path.join(ROOT, engine, 'sor_reader324802a.py')
    spec = importlib.util.spec_from_file_location(f'_sor_{engine}', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _events(engine, path):
    mod = _reader(engine)
    data = open(path, 'rb').read()
    return mod._parse_key_events(data, mod._parse_block_directory(data))


# ── the raw bytes, so the test cannot drift off the real file ──────────────

def test_fixture_carries_the_real_2F_bytes():
    """Pin the byte pattern itself: if the fixture is ever swapped for one
    without a saturated event, this test says so instead of passing hollow."""
    raw = open(SAT, 'rb').read()
    assert raw.count(b'2F9999LS') == 1, 'fixture must carry exactly one 2F event'
    assert raw.count(b'1F9999LS') == 3
    ctl = open(CTL, 'rb').read()
    assert b'2F9999LS' not in ctl, 'control fixture must have no saturated event'


# ── the decode, in every engine ───────────────────────────────────────────

@pytest.mark.parametrize('engine', ENGINES)
def test_saturated_reflective_event_is_reflective(engine):
    """THE regression.  Before the fix this event came back is_reflective
    False in all three readers."""
    evs = _events(engine, SAT)
    conn = evs[1]
    assert conn['type'] == '2F9999LS', conn['type']
    assert conn['is_reflective'] is True, (
        f"{engine}: 2F9999LS decoded as non-reflective — the saturated "
        f"reflective class was filed with the '0' non-reflective events"
    )
    assert conn['is_end'] is False


@pytest.mark.parametrize('engine', ENGINES)
def test_the_reflectance_the_field_team_flagged_is_on_that_event(engine):
    """-25.072 dB is the team's '34 -25.1 (Near)'.  It was always parsed
    correctly; it was the is_reflective flag that hid it downstream."""
    conn = _events(engine, SAT)[1]
    assert conn['reflection'] == pytest.approx(-25.072, abs=5e-4)
    assert conn['dist_km'] == pytest.approx(1.0044, abs=5e-4)


@pytest.mark.parametrize('engine', ENGINES)
def test_zero_prefix_stays_non_reflective(engine):
    """The fix must widen the predicate, not blunt it: '0F' events keep
    is_reflective False."""
    evs = _events(engine, SAT)
    zeros = [e for e in evs if e['type'].startswith('0')]
    assert zeros, 'fixture should carry non-reflective events too'
    assert all(e['is_reflective'] is False for e in zeros)


@pytest.mark.parametrize('engine', ENGINES)
def test_control_fiber_is_unchanged(engine):
    """F491: same position, plain 1F — the fiber that always worked.  Its
    decode must be untouched by the fix."""
    conn = _events(engine, CTL)[1]
    assert conn['type'] == '1F9999LS'
    assert conn['is_reflective'] is True
    assert conn['reflection'] == pytest.approx(-46.539, abs=5e-4)


@pytest.mark.parametrize('engine', ENGINES)
def test_predicate_is_an_explicit_membership_test(engine):
    """Guard against a silent revert to `== '1'`, and against the sloppier
    `!= '0'`: an unrecognised future code must fail CLOSED (non-reflective),
    not default to reflective."""
    src = open(os.path.join(ROOT, engine, 'sor_reader324802a.py'),
               encoding='utf-8').read()
    line = next(l for l in src.splitlines()
                if l.strip().startswith("'is_reflective':"))
    assert "in ('1', '2')" in line, line.strip()


# ── the downstream consequence that actually mattered ─────────────────────

def test_launch_normalization_now_sees_f34s_connector():
    """The cascade, at the engine boundary.  `_untrimmed_launch_offset_km`
    returned 0.0 for F34 (no shift -> every event 1.0044 km out of frame,
    which is what swept its box connector into the 63.9701 km splice column
    and printed the bogus `34 .255` cell).  It must now return the launch
    reel length."""
    sys.path.insert(0, os.path.join(ROOT, 'splicereport'))
    try:
        import splicereportmatchexfo as E
    finally:
        sys.path.pop(0)
    evs = _events('splicereport', SAT)
    off = E._untrimmed_launch_offset_km(evs)
    assert off == pytest.approx(1.0044, abs=5e-4), (
        f'launch offset {off} — 0.0 means the untrimmed-launch pattern still '
        f'does not match, so the frame is never shifted'
    )
    # and the control, which was always fine
    assert E._untrimmed_launch_offset_km(_events('splicereport', CTL)) == \
        pytest.approx(1.0044, abs=5e-4)


def test_a_launch_conn_event_returns_the_saturated_connector():
    """The loss gate reads the connector through `_a_launch_conn_event`,
    which keys off the same reflective pattern — F34's connector was
    invisible to it too."""
    sys.path.insert(0, os.path.join(ROOT, 'splicereport'))
    try:
        import splicereportmatchexfo as E
    finally:
        sys.path.pop(0)
    evt = E._a_launch_conn_event({'_raw_events': _events('splicereport', SAT)})
    assert evt is not None, 'launch connector still invisible to the loss gate'
    assert evt['type'] == '2F9999LS'
    assert evt['reflection'] == pytest.approx(-25.072, abs=5e-4)


# ══════════════════════════════════════════════════════════════════════════
#  THE ENGINE HALF — raw type-string sites that ignored the reader's flag
#
#  Fixing the reader alone left seven sites in splicereportmatchexfo.py
#  deciding an event's class from the raw type STRING, so `2F` stayed
#  invisible to them.  The live one was the mid-span REF/BREAK detector:
#
#      is_reflective = ea['type'].startswith('1F')          # was
#      has_weak_fresnel = ea['reflection'] < -25.0
#      is_refl_event_candidate = is_reflective and has_weak_fresnel and mid_span
#
#  MIL<->TOP fiber 388 carries a `2F9999LS` at 11.2221 km — dead on Splice 2
#  (11.2247 km) — with -32.003 dB and 0.459 dB of loss.  Both other conjuncts
#  hold, so the type test was the SOLE gate: the report called it a plain
#  reburn (`388 .349`, "re-splice this") instead of what it is
#  (`388 ref .349 (refl -32dB) DIRTY CONNECTOR`) — a different repair for the
#  field crew off the same number.
#
#  The corroboration is in the B-direction fixture: the SAME physical event
#  read from the far end is a plain `1F9999LS` at -32.768 dB.  One event, two
#  directions, reflectances 0.765 dB apart, and only the type code differs —
#  so `2F` and `1F` are the same physical class and our classification was
#  direction-dependent purely through the decode.
# ══════════════════════════════════════════════════════════════════════════

MTA = os.path.join(FIXDIR, 'MILTOPls0388_1550.sor')   # A-dir, the 2F
MTB = os.path.join(FIXDIR, 'TOPMIL0388_1550.sor')     # B-dir, same event as 1F


def _engine():
    sys.path.insert(0, os.path.join(ROOT, 'splicereport'))
    try:
        import splicereportmatchexfo as E
    finally:
        sys.path.pop(0)
    return E


def test_engine_type_predicates_accept_the_saturated_class():
    E = _engine()
    for t in ('1F9999LS', '2F9999LS'):
        assert E._is_reflective_type(t) is True, t
        assert E._is_inspan_event_type(t) is True, t
    assert E._is_reflective_type('0F9999LS') is False
    assert E._is_inspan_event_type('0F9999LS') is True
    # terminal codes are not in-span events: end-of-fiber in all three
    # reflection classes, and '1O' (short shot that never reached the end)
    for t in ('0E9999LS', '1E9999LS', '2E9999LS', '1O9999LS'):
        assert E._is_inspan_event_type(t) is False, t
        assert E._is_reflective_type(t) is False, t
    assert E._is_reflective_type(None) is False
    assert E._is_inspan_event_type('') is False


def test_no_bare_1F_string_test_survives_in_the_engine():
    """Every `startswith('1F')` left in the engine must also consult the
    reader's is_reflective flag (or go through the helpers).  A bare one is
    a fresh blind spot for the whole '2' class."""
    src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    bare = []
    for i, line in enumerate(src.splitlines(), 1):
        code = line.split('#', 1)[0]
        if "startswith('1F')" not in code:
            continue
        if 'is_reflective' in code and '=' not in code.split('is_reflective')[0][-2:]:
            continue                      # reads the flag, doesn't just assign it
        if 'get(\'is_reflective\')' in code:
            continue
        bare.append((i, line.strip()))
    assert not bare, f"bare 1F string tests left in the engine: {bare}"


def test_no_bare_slice_spelling_of_the_alphabet_survives():
    """`startswith('1F')` is not the only way to write the old alphabet.

    `uni_prebreak_damage` spelled it `str(...)[:2] in ('0F','1F')`, which the
    test above cannot see -- so that site was missed when the '2' class was
    added and a saturated event stopped counting as stored corroboration.
    Ban the slice spelling too, or the next one hides the same way.
    """
    src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    bad = []
    for i, line in enumerate(src.splitlines(), 1):
        code = line.split('#', 1)[0]
        if re.search(r"\[:2\]\s*(?:in|==)\s*[\('\"]", code) and "'2F'" not in code:
            bad.append((i, line.strip()))
    assert not bad, (
        "the event-type alphabet is spelled as a 2-char slice without '2F'; "
        f"use _is_inspan_event_type / _is_reflective_type instead: {bad}")


def test_miltop_f388_saturated_event_reaches_the_reflective_path():
    """The masked case, on the real bytes.  All three conjuncts of
    `is_refl_event_candidate` must hold — before the fix the type test alone
    was False and the event was reported as an ordinary splice reburn."""
    E = _engine()
    ea = next(e for e in _events('splicereport', MTA)
              if e['type'].startswith('2'))
    assert ea['type'] == '2F9999LS'
    assert ea['dist_km'] == pytest.approx(11.2221, abs=5e-4)
    assert ea['reflection'] == pytest.approx(-32.003, abs=5e-4)
    assert ea['splice_loss'] == pytest.approx(0.459, abs=5e-4)

    # conjunct 1 — the predicate that was the sole gate
    assert (ea.get('is_reflective') or E._is_reflective_type(ea['type'])) is True
    # conjunct 2 — has_weak_fresnel: reflection < -25.0
    assert ea['reflection'] < -25.0
    # conjunct 3 — mid_span: 11.22 km is nowhere near this 61.7 km span's end
    assert ea['dist_km'] < (61.74 - E.END_REGION_KM)


def test_the_same_event_is_plain_1F_from_the_other_direction():
    """B-direction corroboration: one physical event, two type codes.  This
    is why routing '2F' through the reflective path is a correction and not
    a new judgement call."""
    eb = next(e for e in _events('splicereport', MTB)
              if abs(e['dist_km'] - 50.525) <= 0.05)
    assert eb['type'] == '1F9999LS'
    assert eb['is_reflective'] is True
    assert eb['reflection'] == pytest.approx(-32.768, abs=5e-4)
    # the two directions agree on the reflectance to well under a dB
    ea = next(e for e in _events('splicereport', MTA)
              if e['type'].startswith('2'))
    assert abs(ea['reflection'] - eb['reflection']) < 1.0


def test_uni_whitelists_no_longer_drop_saturated_events():
    """The four uni-pipeline `('0F','1F')` whitelists decided which stored
    events may become a discovered column or a uni flag; a `2F` was dropped
    outright."""
    src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    assert "t.startswith('0F') or t.startswith('1F')" not in src
    calls = src.count('if not _is_inspan_event_type(t):')
    assert calls == 4, f'expected 4 uni whitelist call sites, found {calls}'
