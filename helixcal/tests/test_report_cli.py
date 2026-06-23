"""report + cli end-to-end: a folder of real SOR + synthetic anchors produces
a 3-sheet workbook with the expected headline values."""

import csv
import os

import pytest

from helixcal import sor_fields, anchors as anchors_mod, calibrate, report, cli
from helixcal.calibrate import fiber_key_from_id, _interior_event_distances_m


def _build_anchor_csv(path, key, ia, m_true=0.976, b_true=6.0):
    M_PER_FT = anchors_mod.M_PER_FT
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(("fiber_id", "anchor_type", "closure_name",
                    "event_index", "known_distance", "units", "direction"))
        for idx in (1, 3, 5):
            x = ia[idx]
            y_ft = (m_true * x + b_true) / M_PER_FT
            w.writerow((key, "closure", f"S{idx}", idx, round(y_ft, 4),
                        "ft", "A"))


def test_write_report_three_sheets(tmp_path, span_a_files):
    openpyxl = pytest.importorskip("openpyxl")
    recs = [sor_fields.read_trace_record(f) for f in span_a_files]
    recs = [r for r in recs if r]
    rec0 = recs[0]
    key = fiber_key_from_id(rec0["genparams"]["cable_id"])
    ia = _interior_event_distances_m(rec0["events"])
    apath = tmp_path / "anchors.csv"
    _build_anchor_csv(apath, key, ia)
    anchors = anchors_mod.load_anchors(str(apath))
    res = calibrate.calibrate(recs, anchors, cable_type="stranded_loose_tube",
                              expected_ior=1.47)
    out = tmp_path / "report.xlsx"
    report.write_report(res, str(out))
    assert out.exists()

    wb = openpyxl.load_workbook(str(out))
    assert wb.sheetnames == ["Helix Calibration",
                             "Per-Anchor Residuals",
                             "Per-Fiber Spread"]
    # Summary sheet carries the factor row.
    ws = wb["Helix Calibration"]
    labels = [ws.cell(r, 1).value for r in range(1, ws.max_row + 1)]
    assert any(lbl and "Conversion factor m" in str(lbl) for lbl in labels)
    assert any(lbl and "AEN142 band verdict" in str(lbl) for lbl in labels)


def test_cli_run_folder(tmp_path, span_a_files):
    pytest.importorskip("openpyxl")
    # Copy A fixtures into a temp source folder.
    import shutil
    src = tmp_path / "src"
    src.mkdir()
    for f in span_a_files:
        shutil.copy(f, src)
    recs = [sor_fields.read_trace_record(f) for f in span_a_files]
    recs = [r for r in recs if r]
    rec0 = recs[0]
    key = fiber_key_from_id(rec0["genparams"]["cable_id"])
    ia = _interior_event_distances_m(rec0["events"])
    apath = tmp_path / "anchors.csv"
    _build_anchor_csv(apath, key, ia)
    out = tmp_path / "out.xlsx"

    result, opath = cli.run(str(src), str(apath), output_path=str(out),
                            cable_type="stranded_loose_tube",
                            expected_ior=1.47)
    assert os.path.exists(opath)
    assert result.m is not None
    assert abs(result.m - 0.976) < 1e-3


def test_cli_run_zip(tmp_path, span_a_files):
    pytest.importorskip("openpyxl")
    import zipfile
    zp = tmp_path / "src.zip"
    with zipfile.ZipFile(zp, "w") as z:
        for f in span_a_files:
            z.write(f, os.path.basename(f))
    recs = [sor_fields.read_trace_record(f) for f in span_a_files]
    recs = [r for r in recs if r]
    rec0 = recs[0]
    key = fiber_key_from_id(rec0["genparams"]["cable_id"])
    ia = _interior_event_distances_m(rec0["events"])
    apath = tmp_path / "anchors.csv"
    _build_anchor_csv(apath, key, ia)
    out = tmp_path / "out.xlsx"

    result, opath = cli.run(str(zp), str(apath), output_path=str(out),
                            cable_type="central_tube")
    assert os.path.exists(opath)
    # central_tube band -> 0.976 is out of [0.99, 1.0] -> warning.
    assert result.band_verdict.startswith("WARNING")
