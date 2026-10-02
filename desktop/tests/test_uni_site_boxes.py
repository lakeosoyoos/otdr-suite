"""The Unidirectional page's two site boxes (A-End site, B-End site), the
Splice Report's pair: filled with the names the traces store, typed over by a
presenter (WEST / EAST), kept across a trip to another tool, and printed by the
report in the direction of the shot, in the workbook and on screen."""
from __future__ import annotations

import openpyxl

from conftest import run_streamlit, finish_engine_run, go_tab, FIXTURE_B_DIR

RUN = "Run Unidirectional Report"


def _page(dest):
    at = run_streamlit(default_timeout=180).run()
    at.session_state["uni_folder_input"] = str(FIXTURE_B_DIR)
    at.session_state["uni_report_dest"] = str(dest)
    go_tab(at, "Unidirectional")
    assert not at.exception, list(at.exception)
    return at


def _box(at, label):
    return next(t for t in at.text_input if t.label == label)


def test_uni_site_boxes_default_type_over_and_print(tmp_path):
    at = _page(tmp_path)
    # The stored GenParams names, exactly as stored: the B folder's files
    # carry the same pair, in the same order, as the A folder's.
    assert _box(at, "A-End Site").value == "ELMDALE"
    assert _box(at, "B-End Site").value == "MILLER"
    _box(at, "A-End Site").input("WEST").run()
    _box(at, "B-End Site").input("EAST").run()
    # a trip to another tool and back keeps what was typed
    go_tab(at, "Viewer")
    at.run()
    go_tab(at, "Unidirectional")
    assert _box(at, "A-End Site").value == "WEST"
    assert _box(at, "B-End Site").value == "EAST"
    next(b for b in at.button if b.label == RUN).click().run()
    finish_engine_run(at, "uni")
    assert not at.exception, list(at.exception)
    # on screen: a B shot runs from the B end
    assert any("direction EAST → WEST" in s.value for s in at.success), \
        [s.value for s in at.success]
    out = at.session_state["uni_result"]["out"]
    assert openpyxl.load_workbook(out)["Unidir Events"]["A1"].value == "EAST → WEST:"
