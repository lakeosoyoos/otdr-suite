"""The Customer profile dropdown sits above the A/B boxes on the Splice Report
page; the OTDR settings expander stays where it was, below.

Robert, 2026-09-16: "so a tech can choose either default or customer settings
before they select A and B."  Pins the source order.
"""
from __future__ import annotations

from conftest import REPO_ROOT

SRC = (REPO_ROOT / "app.py").read_text(encoding="utf-8")


def test_the_profile_picker_renders_before_the_input_radio_and_the_settings_after():
    page = SRC.split("def page_splice_report():", 1)[1]
    i_pick = page.index("_render_customer_profile_picker()")
    # The Input radio + A/B boxes moved into _sr_span_inputs (second-span
    # option); the page calls it for span 1 right where the boxes were.
    i_mode = page.index("_sr_span_inputs(1)")
    i_panel = page.index("_render_otdr_settings_panel()")
    assert i_pick < i_mode < i_panel


def test_the_dropdown_left_the_expander_but_kept_its_state_and_reload():
    panel = SRC.split("def _render_otdr_settings_panel():", 1)[1].split("\ndef ", 1)[0]
    assert "otdr_profile_select" not in panel
    picker = SRC.split("def _render_customer_profile_picker():", 1)[1].split("\ndef ", 1)[0]
    assert "key='otdr_profile_select'" in picker
    assert "st.session_state.otdr_settings = _otdr_settings_from_profile(_picked)" in picker
    assert "st.session_state.conn_settings = _conn_settings_from_profile(_picked)" in picker
