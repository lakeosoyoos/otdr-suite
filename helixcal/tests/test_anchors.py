"""anchors loader: unit normalization (ft/m) and table validation."""

import csv
import os

import pytest

from helixcal import anchors
from helixcal.anchors import AnchorError, ft_to_m, m_to_ft, to_meters


def test_ft_m_roundtrip():
    for v in (0.0, 1.0, 1234.5, 117319.7):
        assert abs(m_to_ft(ft_to_m(v)) - v) < 1e-6
    # 1000 ft == 304.8 m exactly.
    assert abs(ft_to_m(1000.0) - 304.8) < 1e-9


def test_to_meters_units():
    assert to_meters(100.0, "m") == 100.0
    assert abs(to_meters(100.0, "ft") - 30.48) < 1e-9
    with pytest.raises(ValueError):
        to_meters(100.0, "furlongs")


def _write_csv(path, rows):
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(rows)


def test_load_closure_anchors_ft_to_m(tmp_path):
    p = tmp_path / "a.csv"
    _write_csv(p, [
        ("fiber_id", "anchor_type", "closure_name", "event_index",
         "known_distance", "units", "direction"),
        ("001", "closure", "Splice 1", "2", "5000", "ft", "both"),
        ("001", "closure", "Splice 2", "5", "1000", "m", "A"),
    ])
    al = anchors.load_anchors(str(p))
    assert len(al) == 2
    # 5000 ft -> meters
    assert abs(al[0].known_distance_m - 5000 * 0.3048) < 1e-6
    assert al[0].units == "ft"
    assert al[0].direction == "both"
    # 1000 m stays 1000 m
    assert abs(al[1].known_distance_m - 1000.0) < 1e-9
    assert al[1].units == "m"
    assert al[1].direction == "A"


def test_closure_requires_locator_and_known(tmp_path):
    p = tmp_path / "bad.csv"
    _write_csv(p, [
        ("fiber_id", "anchor_type", "known_distance", "units"),
        ("001", "closure", "5000", "ft"),   # no event_index / approx_otdr_km
    ])
    with pytest.raises(AnchorError):
        anchors.load_anchors(str(p))


def test_reel_anchor_fields(tmp_path):
    p = tmp_path / "reel.csv"
    _write_csv(p, [
        ("fiber_id", "anchor_type", "span_start_event", "span_end_event",
         "segment_length", "units"),
        ("001", "reel", "1", "2", "12000", "ft"),
    ])
    al = anchors.load_anchors(str(p))
    assert len(al) == 1
    a = al[0]
    assert a.anchor_type == "reel"
    assert a.span_start_event == 1 and a.span_end_event == 2
    assert abs(a.segment_length_m - 12000 * 0.3048) < 1e-6


def test_wildcard_fiber_id(tmp_path):
    p = tmp_path / "w.csv"
    _write_csv(p, [
        ("fiber_id", "anchor_type", "approx_otdr_km", "known_distance", "units"),
        ("*", "closure", "10.0", "33000", "ft"),
    ])
    al = anchors.load_anchors(str(p))
    assert al[0].applies_to_all is True


def test_empty_table_raises(tmp_path):
    p = tmp_path / "empty.csv"
    _write_csv(p, [("fiber_id", "anchor_type", "known_distance", "units")])
    with pytest.raises(AnchorError):
        anchors.load_anchors(str(p))


def test_xlsx_roundtrip(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    p = tmp_path / "a.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["fiber_id", "anchor_type", "event_index",
               "known_distance", "units"])
    ws.append(["001", "closure", 2, 5000, "ft"])
    wb.save(p)
    al = anchors.load_anchors(str(p))
    assert len(al) == 1
    assert abs(al[0].known_distance_m - 5000 * 0.3048) < 1e-6
