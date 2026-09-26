"""Emptying the Fibers box clears the traces (boss 2026-09-26).

"When we clear the fiber number, can it clear the cache so you don't have to
hit Clear All, just have it automatically remove traces."  Plain JS, no
runtime here: pins the source.
"""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def test_an_empty_box_runs_clear_all():
    i = SRC.index("document.getElementById('fiber-input').addEventListener('input'")
    body = SRC[i:i + 200]
    assert "!e.target.value.trim() && gTraces.length" in body and "clearAll()" in body


def test_it_listens_to_edits_not_programmatic_fills():
    # 'input' does not fire when a report click writes .value; 'change' or a
    # value poll would clear what the click just loaded.
    assert "getElementById('fiber-input').addEventListener('change'" not in SRC
