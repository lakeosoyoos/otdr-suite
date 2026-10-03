"""Splice Report: the "Add span…" chain (Robert, 2026-09-16).

"can we create the option to add additional span? then the tech would upload
more traces for A and B direction and a splice report if they want. when they
click generate report then the two spans would be run independently but both
placed in the same location as selected by tech" — and then: only span 1 keeps
the grid / the Viewer ("viewer only needs to load from the first span"), the
added spans are report generation only, and the button chains (under span 2
the same button opens span 3).
"""
from __future__ import annotations

import ast

from conftest import (REPO_ROOT, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR,
                      run_streamlit, go_tab)

SRC = (REPO_ROOT / "app.py").read_text(encoding="utf-8")


def _fn(name):
    return SRC.split(f"\ndef {name}(", 1)[1].split("\ndef ", 1)[0]


def _page(**state):
    at = run_streamlit().run()
    for k, v in state.items():
        at.session_state[k] = v
    go_tab(at, "Splice Report")
    return at


def _button(at, label):
    for b in at.button:
        if b.label == label:
            return b
    raise AssertionError(f"{label!r} not rendered; saw {[b.label for b in at.button]}")


# ── the chain of boxes ───────────────────────────────────────────────────
def test_add_span_opens_span_2_boxes_and_the_generate_label_counts_spans():
    at = _page(view_dir_a_input=str(FIXTURE_SPLICE_A_DIR),
               view_dir_b_input=str(FIXTURE_SPLICE_B_DIR))
    assert not at.exception, list(at.exception)
    keys = {w.key for w in at.text_input}
    # span 1 runs on the Traces tab's A and B boxes (drawn on that tab, the
    # sidebar's until 2026-10-01); span 2 has no boxes yet
    assert "sr2_dir_a" not in keys
    assert not keys & {"view_dir_a_input", "view_dir_b_input"}
    assert at.session_state["view_dir_a_input"] == str(FIXTURE_SPLICE_A_DIR)
    assert any("the A and B folders loaded on the Traces tab" in c.value
               for c in at.caption)
    _button(at, "Generate Splice Report")

    _button(at, "➕ Add Span…").click().run()
    assert not at.exception, list(at.exception)
    keys = {w.key for w in at.text_input}
    assert {"sr2_dir_a", "sr2_dir_b", "sr2_site_a", "sr2_site_b"} <= keys
    # Its own optional tech workbook, under its own boxes (AppTest does not
    # list file_uploader widgets, so this one is a source pin).
    inputs = _fn("_sr_span_inputs")
    assert "f'{pre}_tech_xlsx'" in _fn("_sr_span_keys") and "key=k_tech" in inputs
    # Span 2 empty → the tech is told, and Generate waits.
    gen = _button(at, "Generate Splice Reports (2 spans)")
    assert gen.disabled is True
    assert any("Span 2 needs" in i.value for i in at.info)
    # The button chains: under span 2 it offers span 3; only the LAST span
    # can be removed.
    _button(at, "➕ Add Span…")
    _button(at, "✖ Remove Span 2")


def test_span_2_with_folders_enables_generate_and_remove_drops_it():
    at = _page(view_dir_a_input=str(FIXTURE_SPLICE_A_DIR),
               view_dir_b_input=str(FIXTURE_SPLICE_B_DIR))
    _button(at, "➕ Add Span…").click().run()
    at.session_state["sr2_dir_a"] = str(FIXTURE_SPLICE_A_DIR)
    at.session_state["sr2_dir_b"] = str(FIXTURE_SPLICE_B_DIR)
    at.run()
    assert not at.exception, list(at.exception)
    assert _button(at, "Generate Splice Reports (2 spans)").disabled is False
    # Same folders as span 1 → say so (the reports would be identical).
    assert any("same folders as span 1" in w.value for w in at.warning)
    # Span 3 chains on; removing it brings span 2's Remove back.
    _button(at, "➕ Add Span…").click().run()
    _button(at, "Generate Splice Reports (3 spans)")
    _button(at, "✖ Remove Span 3").click().run()
    _button(at, "✖ Remove Span 2").click().run()
    assert "sr2_dir_a" not in {w.key for w in at.text_input}
    _button(at, "Generate Splice Report")


def test_span_2_names_a_folder_that_is_not_there(tmp_path):
    """Both boxes filled in, one with a folder that is not there: the line
    names that box and its path, the way the left panel names its own, and
    does not ask for "both" folders the tech already typed (audit
    2026-10-02)."""
    gone = str(tmp_path / "no_such_folder")
    at = _page(view_dir_a_input=str(FIXTURE_SPLICE_A_DIR),
               view_dir_b_input=str(FIXTURE_SPLICE_B_DIR))
    _button(at, "➕ Add Span…").click().run()
    at.session_state["sr2_dir_a"] = str(FIXTURE_SPLICE_A_DIR)
    at.session_state["sr2_dir_b"] = gone
    at.run()
    assert not at.exception, list(at.exception)
    assert _button(at, "Generate Splice Reports (2 spans)").disabled is True
    infos = [i.value for i in at.info]
    assert any(f"Span 2: B folder not found: {gone}" in i for i in infos), infos
    assert not any("Span 2 needs" in i for i in infos), infos
    assert not any("A folder not found" in i for i in infos), infos

    # One box empty, the other not there: both are said.
    at.session_state["sr2_dir_a"] = ""
    at.run()
    infos = [i.value for i in at.info]
    assert any(f"Span 2: B folder not found: {gone}" in i for i in infos), infos
    assert any("Span 2 needs **both**" in i for i in infos), infos


def test_span_2_one_folder_mode_names_a_folder_that_is_not_there(tmp_path):
    gone = str(tmp_path / "no_such_folder")
    at = _page(view_dir_a_input=str(FIXTURE_SPLICE_A_DIR),
               view_dir_b_input=str(FIXTURE_SPLICE_B_DIR))
    _button(at, "➕ Add Span…").click().run()
    at.session_state["sr2_input_mode"] = "One folder / zip (both directions)"
    at.session_state["sr2_one_folder"] = gone
    at.run()
    assert not at.exception, list(at.exception)
    assert _button(at, "Generate Splice Reports (2 spans)").disabled is True
    infos = [i.value for i in at.info]
    assert any(f"Span 2: folder not found: {gone}" in i for i in infos), infos
    assert not any("Span 2 needs" in i for i in infos), infos
    # ...and under the box itself, in place of "Choose a folder".
    assert any(f"Folder not found: {gone}" in w.value for w in at.warning)
    assert not any("Choose a folder" in i for i in infos), infos


# ── one click, spans run back to back, one destination ───────────────────
def test_generate_queues_one_run_per_span_into_the_one_destination():
    page = _fn("page_splice_report")
    gen = page.split("if st.button(_gen_label, type='primary'", 1)[1]
    assert "spans = [(1, dir_a, dir_b, site_a, site_b)]" in gen
    assert "for _n in sorted(extra):" in gen
    assert "out_xlsx = _project_run_path(_sr_dest, _name, traces=(_da, _db)," in gen  # same folder
    assert "taken=[q['out'] for q in queue]" in gen      # two spans never share a name
    assert "st.session_state[f'{_p}_queue'] = queue" in gen
    # Runs are handed to run_engine_live one at a time, and the next starts
    # when one finishes.
    assert "if _sr_start_next_queued(_p):\n                st.rerun()" in page
    starter = _fn("_sr_start_next_queued")
    assert "queue.pop(0)" in starter
    assert "st.session_state[f'{_p}_pending_cmd'] = run['cmd']" in starter
    # Cancel / timeout drops the rest of the queue.
    assert "st.session_state.pop(f'{_p}_queue', None)" in page


def test_the_same_sites_twice_keep_both_files():
    page = _fn("page_splice_report")
    assert "f'span{_n}_SpliceReport'" in page


# ── span 1 keeps the grid + Viewer; added spans are report-only ──────────
def test_only_span_1_gets_the_grid_and_the_viewer():
    render = _fn("_render_sr_result")
    i_dl = render.index("st.download_button('⬇ Excel Report'")
    i_tech = render.index("_render_tech_comparison(")
    i_gate = render.index("if span != 1:\n        return")
    i_grid = render.index("_render_clickable_grid(")
    assert i_dl < i_tech < i_gate < i_grid
    page = _fn("page_splice_report")
    assert "_follow = shown[0]" in page                      # Viewer = first span
    assert "trace_server.set_dirs(_sd[0]" in page
    # Only span 1 is disk-cached (the grid 'Back' restores).
    assert "_run['span'] == 1 and _sd[0]" in page


def test_span_1_keeps_every_key_the_rest_of_the_suite_pins():
    # The keys live in one map (_sr_span_keys) shared with saved projects.
    import app
    k1 = app._sr_span_keys(1)
    assert (k1['mode'], k1['a'], k1['b'], k1['one'], k1['zip'], k1['tech']) == (
        'sr_input_mode', 'view_dir_a_input', 'view_dir_b_input',
        'sr_one_folder', 'sr_zip', 'sr_tech_xlsx')
    assert (k1['site_a'], k1['site_b'], k1['site_src']) == (
        'sr_site_a', 'sr_site_b', 'sr_site_src')
    k3 = app._sr_span_keys(3)
    assert (k3['a'], k3['tech'], k3['site_a']) == ('sr3_dir_a', 'sr3_tech_xlsx', 'sr3_site_a')
    assert "_sr_span_keys(span)" in _fn("_sr_span_inputs")
    assert "_sr_span_keys(span)" in _fn("_sr_site_inputs")
    slot = _fn("_sr_result_slot")
    assert "sfx = '' if span == 1 else str(span)" in slot


def test_helpers_are_real_top_level_functions():
    names = {n.name for n in ast.parse(SRC).body if isinstance(n, ast.FunctionDef)}
    assert {"_sr_span_inputs", "_sr_site_inputs", "_sr_result_slot",
            "_sr_start_next_queued", "_render_sr_result"} <= names
