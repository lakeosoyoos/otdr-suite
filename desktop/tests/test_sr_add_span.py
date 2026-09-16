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
                      run_streamlit)

SRC = (REPO_ROOT / "app.py").read_text(encoding="utf-8")


def _fn(name):
    return SRC.split(f"\ndef {name}(", 1)[1].split("\ndef ", 1)[0]


def _page(**state):
    at = run_streamlit().run()
    for k, v in state.items():
        at.session_state[k] = v
    at.sidebar.radio[0].set_value("Splice Report").run()
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
    assert "view_dir_a_input" in keys and "sr2_dir_a" not in keys
    _button(at, "Generate Splice Report")

    _button(at, "➕ Add span…").click().run()
    assert not at.exception, list(at.exception)
    keys = {w.key for w in at.text_input}
    assert {"sr2_dir_a", "sr2_dir_b", "sr2_site_a", "sr2_site_b"} <= keys
    # Its own optional tech workbook, under its own boxes (AppTest does not
    # list file_uploader widgets, so this one is a source pin).
    inputs = _fn("_sr_span_inputs")
    assert "f'{_k}_tech_xlsx'" in inputs and "key=k_tech" in inputs
    # Span 2 empty → the tech is told, and Generate waits.
    gen = _button(at, "Generate Splice Reports (2 spans)")
    assert gen.disabled is True
    assert any("Span 2 needs" in i.value for i in at.info)
    # The button chains: under span 2 it offers span 3; only the LAST span
    # can be removed.
    _button(at, "➕ Add span…")
    _button(at, "✖ Remove span 2")


def test_span_2_with_folders_enables_generate_and_remove_drops_it():
    at = _page(view_dir_a_input=str(FIXTURE_SPLICE_A_DIR),
               view_dir_b_input=str(FIXTURE_SPLICE_B_DIR))
    _button(at, "➕ Add span…").click().run()
    at.session_state["sr2_dir_a"] = str(FIXTURE_SPLICE_A_DIR)
    at.session_state["sr2_dir_b"] = str(FIXTURE_SPLICE_B_DIR)
    at.run()
    assert not at.exception, list(at.exception)
    assert _button(at, "Generate Splice Reports (2 spans)").disabled is False
    # Same folders as span 1 → say so (the reports would be identical).
    assert any("same folders as span 1" in w.value for w in at.warning)
    # Span 3 chains on; removing it brings span 2's Remove back.
    _button(at, "➕ Add span…").click().run()
    _button(at, "Generate Splice Reports (3 spans)")
    _button(at, "✖ Remove span 3").click().run()
    _button(at, "✖ Remove span 2").click().run()
    assert "sr2_dir_a" not in {w.key for w in at.text_input}
    _button(at, "Generate Splice Report")


# ── one click, spans run back to back, one destination ───────────────────
def test_generate_queues_one_run_per_span_into_the_one_destination():
    page = _fn("page_splice_report")
    gen = page.split("if st.button(_gen_label, type='primary'", 1)[1]
    assert "spans = [(1, dir_a, dir_b, site_a, site_b)]" in gen
    assert "for _n in sorted(extra):" in gen
    assert "out_xlsx = os.path.join(_sr_dest, _name)" in gen     # same folder
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
    assert "_span{_n}{_suffix}" in page


# ── span 1 keeps the grid + Viewer; added spans are report-only ──────────
def test_only_span_1_gets_the_grid_and_the_viewer():
    render = _fn("_render_sr_result")
    i_dl = render.index("st.download_button('⬇ Excel report'")
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
    inputs = _fn("_sr_span_inputs")
    for k in ("'sr_input_mode'", "'view_dir_a_input'", "'view_dir_b_input'",
              "'sr_one_folder'", "'sr_zip'", "'sr_tech_xlsx'"):
        assert k in inputs
    sites = _fn("_sr_site_inputs")
    assert "pre = 'sr' if span == 1 else f'sr{span}'" in sites
    slot = _fn("_sr_result_slot")
    assert "sfx = '' if span == 1 else str(span)" in slot


def test_helpers_are_real_top_level_functions():
    names = {n.name for n in ast.parse(SRC).body if isinstance(n, ast.FunctionDef)}
    assert {"_sr_span_inputs", "_sr_site_inputs", "_sr_result_slot",
            "_sr_start_next_queued", "_render_sr_result"} <= names
