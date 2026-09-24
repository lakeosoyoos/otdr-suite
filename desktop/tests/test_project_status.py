"""Project status: what the FQA package still needs, ticked off from files.

Robert, 2026-09-23: "we will want a project status area, where we see what
we need. as we collect photos, GPS coordinates, traces, etc those would be
removed from what we need. the basis for what we need is the completed FQA
form."  Option 2: the field's emailed files are dropped on the page and kept
in <project folder>/Field/.

The fixtures are built the way the real files are: the FQA workbook is the
blank Lumen form with cells filled and photos anchored under a per-location
caption on the Pictures tab (fieldcapture/web/fqa.js), the capture sheet has
Field Capture's 'Submissions' columns (fieldcapture/web/app.js buildWorkbook).
"""
from __future__ import annotations

import io
import re
import shutil

import pytest

from conftest import (REPO_ROOT, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR,
                      run_streamlit)

TEMPLATE = REPO_ROOT / "fqa" / "templates" / "FQA_Site_Survey_v1_1.xlsm"


@pytest.fixture
def hub():
    import app
    return app


@pytest.fixture
def settings_dir(tmp_path, monkeypatch):
    d = tmp_path / "settings"
    d.mkdir()
    monkeypatch.setenv("OTDR_SETTINGS_DIR", str(d))
    return d


@pytest.fixture
def span_dir(tmp_path):
    root = tmp_path / "ELMDALE-MILLER"
    shutil.copytree(FIXTURE_SPLICE_A_DIR, root / "A")
    shutil.copytree(FIXTURE_SPLICE_B_DIR, root / "B")
    return root


def _png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 80, 40)).save(buf, "PNG")
    buf.seek(0)
    return buf


def field_fqa(path, cells=None, photos=None):
    """The blank form with Site Survey cells filled and Pictures bands:
    photos = {'A-Location: ELMDALE, photos by RC, 9/23/2026': 2, ...}."""
    import openpyxl
    from openpyxl.drawing.image import Image as XLImage
    wb = openpyxl.load_workbook(TEMPLATE, keep_vba=True)
    ss = wb["Site Survey Data"]
    for ref, v in (cells or {}).items():
        sheet, _, ref = ref.rpartition("!")
        (wb[sheet] if sheet else ss)[ref] = v
    pics = wb["Pictures"]
    row = 1
    for caption, n in (photos or {}).items():
        pics[f"A{row}"] = caption
        for k in range(n):
            img = XLImage(_png())
            pics.add_image(img, f"B{row + 1 + 3 * k}")
        row += 2 + 3 * n
    wb.save(path)
    return path


def capture_sheet(path, rows):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Submissions"
    head = ["#", "Location", "Date", "Time", "Tester Initials", "Room", "Aisle",
            "Bay", "GPS Latitude", "GPS Longitude", "GPS Accuracy (m)",
            "GPS (lat, lon)", "GPS Source", "Photos", "Sheet"]
    ws.append(head)
    for i, r in enumerate(rows, 1):
        ws.append([i, r["loc"], "9/23/2026", "10:00", "RC", None, None, None,
                   r.get("lat"), r.get("lon"), r.get("acc"), None, "Phone GPS",
                   r.get("photos", 0), f"Site {i}"])
    wb.save(path)
    return path


def _by_item(items):
    return {i["item"]: i for i in items}


def _snap(span_dir):
    return {"spans": [{"mode": "two", "dir_a": str(span_dir / "A"),
                       "dir_b": str(span_dir / "B"), "folder": "",
                       "site_a": "ELMDALE", "site_b": "MILLER"}]}


# ── the list mirrors Lumen's form ────────────────────────────────────────
def test_checklist_cells_match_the_forms_own_submittal_checklist(hub):
    """Every "Entered?" row of the Submittal Checklist tests one Site Survey
    cell.  If Lumen moves a cell in a form revision, this fails."""
    import openpyxl
    wb = openpyxl.load_workbook(TEMPLATE, read_only=True)
    ws = wb["Submittal Checklist"]
    form = []
    for r in range(6, 21):
        f = ws.cell(r, 2).value
        m = re.search(r"'Site Survey Data'!([A-Z]+\d+)", str(f))
        form.append((f"{ws.cell(r, 3).value:.2f}", m.group(1)))
    wb.close()
    ours = [(no, ref) for no, _what, _sheet, ref in hub.FQA_CHECKLIST_CELLS]
    assert ours == form


def test_blank_form_needs_everything_and_the_traces_are_counted(hub, span_dir, tmp_path):
    field = tmp_path / "Field"
    field.mkdir()
    field_fqa(field / "blank.xlsm")
    fqa, caps = hub.collect_field_files(str(field))
    assert [n for n, _ in fqa] == ["blank.xlsm"] and caps == []
    items = hub.project_status(_snap(span_dir), fqa, caps)
    by = _by_item(items)
    assert not by["1.01  Bldg 1 (A end) site address"]["ok"]
    assert not by["2.01  FAT table completed"]["ok"]
    gps = [i for i in items if i["item"] == "GPS fix"]
    assert len(gps) == 2 and not any(i["ok"] for i in gps)
    need = [i for i in items if not i["ok"]]
    assert len(need) >= 15 + 7 + 8          # checklist + FAT/Event Log + both ends
    # The fixture span has both directions for every fiber.
    assert by["A-direction traces"]["ok"] and by["B-direction traces"]["ok"]
    assert by["Every fiber shot both ways"]["ok"]


def test_field_files_tick_off_section_1_2_gps_and_photos(hub, span_dir, tmp_path):
    field = tmp_path / "Field"
    field.mkdir()
    field_fqa(field / "FQA field 1.2.xlsm",
              cells={"F51": "1", "H51": "COLO", "J51": "12", "L51": "4",
                     "F52": "RMU 30", "F56": "LC", "M56": "OSX", "J53": 144,
                     "E9": "123 Main St, Elmdale", "E10": "ELMDALE CO"},
              photos={"A-Location: ELMDALE CO, photos by RC, 9/23/2026": 3,
                      "Z-Location, photos by RC, 9/23/2026": 1})
    capture_sheet(field / "OTDR_Field_RC.xlsx",
                  [{"loc": "A-Location", "lat": 38.5, "lon": -98.1, "acc": 5, "photos": 3}])
    fqa, caps = hub.collect_field_files(str(field))
    assert len(fqa) == 1 and len(caps) == 1
    pics = dict(fqa[0][1]["pictures"])
    assert pics["A-Location: ELMDALE CO, photos by RC, 9/23/2026"] == 3
    items = hub.project_status(_snap(span_dir), fqa, caps)
    a = {i["item"]: i for i in items if i["group"] == "Field: A end"}
    z = {i["item"]: i for i in items if i["group"] == "Field: Z end"}
    assert a["Rack location (floor, room, aisle, bay)"]["ok"]
    assert a["Panel details (RMU, connector, panel type)"]["ok"]
    assert a["GPS fix"]["ok"] and "38.500000, -98.100000" in a["GPS fix"]["detail"]
    assert a["GPS fix"]["source"] == "OTDR_Field_RC.xlsx"
    assert a["Photos"]["ok"] and a["Photos"]["detail"] == "3 photos"
    # Z: one photo, nothing else yet.
    assert z["Photos"]["ok"] and not z["GPS fix"]["ok"]
    assert not z["Rack location (floor, room, aisle, bay)"]["ok"]
    by = _by_item(items)
    assert by["1.01  Bldg 1 (A end) site address"]["ok"]
    assert by["1.07  Bldg 1 panel port count (the form's \"test-from device\")"]["ok"]
    assert not by["1.02  Bldg 2 (Z end) site address"]["ok"]


def test_form_prompts_do_not_count_as_entered(hub, tmp_path, span_dir):
    field = tmp_path / "Field"
    field.mkdir()
    field_fqa(field / "f.xlsm", cells={"F85": "<Select Package Type>", "F86": "<Select>",
                                        "F97": "  "})
    fqa, caps = hub.collect_field_files(str(field))
    by = _by_item(hub.project_status(_snap(span_dir), fqa, caps))
    assert not by["1.11  Package type"]["ok"]
    assert not by["1.10  Test revision"]["ok"]
    assert not by["1.09  Number of fibers tested"]["ok"]


def test_a_cell_counts_when_any_collected_workbook_fills_it(hub, tmp_path, span_dir):
    # The field copy fills 1.2; the FQA Builder's copy fills the cover.
    field = tmp_path / "Field"
    field.mkdir()
    field_fqa(field / "field.xlsm", cells={"H51": "COLO"})
    field_fqa(field / "builder.xlsm", cells={"E11": "9 Oak Ave, Miller",
                                              "Event Log!W8": 42.01})
    fqa, caps = hub.collect_field_files(str(field))
    by = _by_item(hub.project_status(_snap(span_dir), fqa, caps))
    assert by["1.02  Bldg 2 (Z end) site address"]["source"] == "builder.xlsm"
    assert by["3.01  Length entered"]["ok"]


def test_missing_fibers_and_the_forms_fiber_count(hub, tmp_path, span_dir):
    # Drop two B traces: those fibers are shot one way only.
    b = sorted(p for p in (span_dir / "B").iterdir() if p.suffix.lower() == ".sor")
    import trace_server
    gone = sorted(trace_server.extract_fiber_num(p.name) for p in b[:2])
    for p in b[:2]:
        p.unlink()
    field = tmp_path / "Field"
    field.mkdir()
    field_fqa(field / "f.xlsm", cells={"F97": 48})
    fqa, caps = hub.collect_field_files(str(field))
    by = _by_item(hub.project_status(_snap(span_dir), fqa, caps))
    both = by["Every fiber shot both ways"]
    assert not both["ok"] and both["detail"].startswith("B missing ")
    assert str(gone[0]) in both["detail"]
    cnt = by["48 fibers, as the form says were tested"]
    assert not cnt["ok"] and cnt["detail"] == "22 have both directions"
    # F97 is date-formatted on Lumen's form; the tech typed 48.
    assert by["1.09  Number of fibers tested"]["detail"] == "48"


def test_a_one_folder_span_waits_for_the_splice_report_to_sort_it(hub, tmp_path):
    snap = {"spans": [{"mode": "one", "folder": str(tmp_path), "dir_a": "", "dir_b": ""}]}
    by = _by_item(hub.project_status(snap, [], []))
    assert "Splice Report page" in by["A and B trace folders"]["detail"]


def test_dropped_file_is_kept_once_and_a_different_one_gets_a_new_name(hub, tmp_path):
    class Up:
        def __init__(self, name, data):
            self.name, self._d = name, data

        def getvalue(self):
            return self._d
    d = tmp_path / "Field"
    p1, new1 = hub._store_dropped(str(d), Up("x.xlsx", b"one"))
    p2, new2 = hub._store_dropped(str(d), Up("x.xlsx", b"one"))
    p3, new3 = hub._store_dropped(str(d), Up("x.xlsx", b"two"))
    assert (new1, new2, new3) == (True, False, True)
    assert p1 == p2 and p3.endswith("x (2).xlsx")


def test_another_spans_workbook_is_flagged(hub, tmp_path):
    field_fqa(tmp_path / "f.xlsm", cells={"E9": "1 Road, Tooele", "E10": "TOOELE"})
    wb = hub.read_fqa_workbook(str(tmp_path / "f.xlsm"))
    assert hub._fqa_names_mismatch(wb, ("ELMDALE", "MILLER"))
    assert not hub._fqa_names_mismatch(wb, ("TOOELE", "KNOLLS"))
    assert not hub._fqa_names_mismatch(wb, ("A", "B"))        # no names to compare


# ── the page ─────────────────────────────────────────────────────────────
def test_page_asks_for_a_project_first(settings_dir):
    at = run_streamlit().run()
    tool = next(r for r in at.sidebar.radio if r.label == "Tool")
    at = tool.set_value("Project status").run()
    assert not at.exception, list(at.exception)
    assert any("Open or save a project first" in i.value for i in at.info)


def test_page_shows_what_is_still_needed_from_the_field_folder(settings_dir, span_dir):
    a, b = str(span_dir / "A"), str(span_dir / "B")
    at = run_streamlit().run()
    at.session_state["view_dir_a_input"] = a
    at.session_state["view_dir_b_input"] = b
    at.sidebar.radio[0].set_value("Splice Report").run()
    at.session_state["project_path_input"] = str(span_dir / "p.otdrproj")
    at.run()
    next(x for x in at.button if x.label == "Save as…").click().run()
    field = span_dir / "Field"
    field.mkdir()
    capture_sheet(field / "cap.xlsx", [{"loc": "Z-Location", "lat": 1.0, "lon": 2.0, "photos": 2}])
    tool = next(r for r in at.sidebar.radio if r.label == "Tool")
    at = tool.set_value("Project status").run()
    assert not at.exception, list(at.exception)
    text = " ".join(m.value for m in at.markdown)
    assert "Still needed" in text and "Bldg 1 (A end) site address" in text
    # Z's GPS came in with the capture sheet: it moved to "In hand".
    assert "✓ GPS fix · 1.000000, 2.000000" in text
    assert "✓ Photos · 2 photos" in text
