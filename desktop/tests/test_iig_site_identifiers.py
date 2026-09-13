"""SITE_NAMES_FROM_IDENTIFIERS — the span names its own two ends.

An iOLM job pushed to the units through EXFO Exchange carries the customer's
job config in every measurement: the cable ID, the A-end and Z-end site
codes, and the segment's two towns.  With that on file the tech types
nothing, and the report prints "Rapelje BIL400" — the code the customer's
records key on, the town for readability (NCT, 2026-09-12).

The folder is NOT the source, and AWS / IIG MT.1085 span 27 is why: it sits
in a SharePoint folder called "Lavina, MT to Rapelje, MT" while every file in
it declares BIL400 (Rapelje) as the A end and RPX400 (Lavina) as the Z end.
A report built from the folder name labels every column with the wrong site.

This is per-profile and not simply always on: on a job that did NOT come
through a controlled push, the identifiers are whatever the tech keyed into
the unit and can be blank or inconsistent.  The reader stays silent unless
the files agree and the A end is the A end three independent ways, and
silence means the tech's own typed names stand.
"""
from __future__ import annotations

import importlib
import json
import sys

from conftest import SPLICEREPORT_DIR

sys.path.insert(0, str(SPLICEREPORT_DIR))
import json_reader as J          # noqa: E402
import splicereportmatchexfo as E  # noqa: E402

IIG = "AWS / IIG MT.1085"

SEGMENT = ("Project=MT.1085 - Lynnwood to Forsyth|Span=Span 27|"
           "Segment=Rapelje, MT to Lavina, MT")


def _sidecar(folder, name, *, cable="BIL400-RPX400-0432F-01",
             a_end="LOC=BIL400|FTP=42|Room=B|Rack=204",
             z_end="LOC=RPX400|FTP=42|Room=B|Rack=104",
             segment=SEGMENT, direction="AB"):
    """One Exchange-style measurement sidecar, shaped like the real ones."""
    folder.mkdir(parents=True, exist_ok=True)
    doc = {"brief": {
        "FiberInformation": {"LocationDirection": direction},
        "Identifiers": [
            {"Name": "Cable ID", "Value": cable},
            {"Name": "Location A", "Value": ""},
            {"Name": "Fiber ID", "Value": name},
            {"Name": "Location B", "Value": ""},
            {"Name": "Segment", "Value": segment},
            {"Name": "A End", "Value": a_end},
            {"Name": "Z End", "Value": z_end},
        ]}}
    (folder / f"{cable}-{name}-{direction}.json").write_text(
        json.dumps(doc), encoding="utf-8")


def _span(tmp_path, n=3, **kw):
    d = tmp_path / "iOLM AB"
    for i in range(1, n + 1):
        _sidecar(d, f"{i:04d}", **kw)
    return d


# ── the pieces ───────────────────────────────────────────────────────────

def test_segment_towns_is_not_fooled_by_the_project_name():
    """The Project field carries its own ' to ' — "Lynnwood to Forsyth" —
    so splitting the whole string hands back the wrong pair."""
    assert J._segment_towns(SEGMENT) == ("Rapelje", "Lavina")
    assert J._segment_towns("Project=A to B|Span=1|Segment=Turah, MT to "
                            "Drummond, MT") == ("Turah", "Drummond")
    assert J._segment_towns("Project=A to B|Span=1") == ("", "")
    assert J._segment_towns("") == ("", "")


def test_loc_code():
    assert J._loc_code("LOC=BIL400|FTP=42|Room=B|Rack=204") == "BIL400"
    assert J._loc_code("FTP=42|LOC=MSO403") == "MSO403"
    assert J._loc_code("FTP=42|Room=B") == ""
    assert J._loc_code("") == ""


# ── reading a span ───────────────────────────────────────────────────────

def test_reads_a_consistent_span(tmp_path):
    info = J.read_span_identifiers(_span(tmp_path))
    assert info["a_code"] == "BIL400" and info["z_code"] == "RPX400"
    assert info["a_town"] == "Rapelje" and info["z_town"] == "Lavina"
    assert info["span"] == "Span 27"
    assert info["project"] == "MT.1085 - Lynnwood to Forsyth"
    assert info["direction"] == "AB"
    assert info["n_read"] == 3


def test_site_names_are_town_then_code(tmp_path):
    assert J.span_site_names(_span(tmp_path)) == ("Rapelje BIL400",
                                                  "Lavina RPX400")


def test_the_folder_name_is_never_consulted(tmp_path):
    """Span 27's real trap: the folder says Lavina first, the files say
    Rapelje.  The files win."""
    folder = tmp_path / "02  (Span 27) Lavina, MT to Rapelje, MT" / "iOLM AB"
    for i in range(1, 4):
        _sidecar(folder, f"{i:04d}")
    assert J.span_site_names(folder) == ("Rapelje BIL400", "Lavina RPX400")


def test_falls_back_to_dir_b_when_dir_a_has_no_sidecars(tmp_path):
    empty = tmp_path / "iOLM AB"
    empty.mkdir()
    b = tmp_path / "iOLM BA"
    for i in range(1, 3):
        _sidecar(b, f"{i:04d}", direction="BA")
    assert J.span_site_names(str(empty), str(b)) == ("Rapelje BIL400",
                                                     "Lavina RPX400")


# ── when it must stay silent ─────────────────────────────────────────────

def test_silent_when_there_are_no_sidecars(tmp_path):
    d = tmp_path / "iOLM AB"
    d.mkdir()
    assert J.read_span_identifiers(d) is None
    assert J.span_site_names(str(d)) is None


def test_silent_when_the_folder_holds_two_cables(tmp_path):
    d = _span(tmp_path, n=2)
    _sidecar(d, "0009", cable="MSO401-MSO402-0432F-01",
             a_end="LOC=MSO401", z_end="LOC=MSO402",
             segment="Span=Span 17|Segment=Superior, MT to Frenchtown, MT")
    assert J.read_span_identifiers(d) is None


def test_silent_when_an_identifier_is_blank(tmp_path):
    """A job not pushed through a controlled config: the tech left it empty."""
    assert J.read_span_identifiers(_span(tmp_path, a_end="")) is None
    assert J.read_span_identifiers(_span(tmp_path, segment="")) is None
    assert J.read_span_identifiers(_span(tmp_path, cable="")) is None


def test_silent_when_the_a_end_is_not_the_a_end_three_ways(tmp_path):
    """Cable ID leads with BIL400 but the A End identifier names the other
    site — the config contradicts itself, so name nothing."""
    d = _span(tmp_path, a_end="LOC=RPX400", z_end="LOC=BIL400")
    assert J.read_span_identifiers(d) is None


def test_a_damaged_sidecar_never_aborts_a_report(tmp_path):
    d = _span(tmp_path, n=2)
    (d / "BIL400-RPX400-0432F-01-0003-AB.json").write_text(
        "{not json", encoding="utf-8")
    assert J.span_site_names(d) == ("Rapelje BIL400", "Lavina RPX400")


# ── wiring ───────────────────────────────────────────────────────────────

def test_switch_ships_off():
    assert E.SITE_NAMES_FROM_IDENTIFIERS == 0


def test_profile_carries_the_switch_and_the_whitelist_allows_it():
    app = importlib.import_module('app')  # engine imported first, on purpose
    assert "SITE_NAMES_FROM_IDENTIFIERS" in app._PROFILE_ENGINE_KEYS
    assert app.CUSTOMER_PROFILES[IIG]["engine"][
        "SITE_NAMES_FROM_IDENTIFIERS"] == 1
    assert app._engine_extras_from_profile(IIG)[
        "SITE_NAMES_FROM_IDENTIFIERS"] == 1.0
    for name, prof in app.CUSTOMER_PROFILES.items():
        if name == IIG:
            continue
        assert "SITE_NAMES_FROM_IDENTIFIERS" not in (prof.get("engine") or {}), name


def test_hub_helper_reads_identifiers_only_for_the_profile_that_asks(tmp_path):
    """Off, the hub keeps the folder-derived ILA names it always used; on,
    the measurements name the ends."""
    app = importlib.import_module('app')
    d = _span(tmp_path)
    assert app._site_names_for(str(d), str(d), profile_name=IIG) == (
        "Rapelje BIL400", "Lavina RPX400")
    other = next(n for n in app.CUSTOMER_PROFILES if n != IIG)
    assert app._site_names_for(str(d), str(d), profile_name=other) != (
        "Rapelje BIL400", "Lavina RPX400")
