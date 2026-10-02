"""A click on an event's heading ("Event 1") opens its span menu.

Robert 2026-10-02: "clicking on an event like 1 or 2 should take us to that
drop down that gives us start span, reset, etc".  The heading only zoomed
the chart; the menu needed the small ⋯ beside it.  Now a click anywhere on
the heading zooms to the event and opens the menu the ⋯ opens (the span
menu for one direction, Which Direction? for two), in all four tables.

Plain JS, no runtime here: pins the source.  Browser-checked 2026-10-02 on
WSC<->SUI 1-6, Suite mode, A+B and A only.
"""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def _body(name: str) -> str:
    body = SRC[SRC.index("function " + name + "("):]
    return body[:body.index("\n}\n")]


def test_the_event_heading_click_zooms_and_opens_the_menu():
    for name in ("renderFastReporterGrid", "paintFrBidiGrid", "paintSuiteBidiGrid"):
        body = _body(name)
        click = body.split("th.addEventListener('click', (ev) => {", 1)[1].split("});", 1)[0]
        assert "zoomToKm(+th.dataset.km, 2);" in click, name
        assert "const picks = colPicks(+th.dataset.col);" in click, name
        assert "if (picks.length) showColumnMenu(r.left, r.bottom + 2, picks);" in click, name
        # the heading no longer stops at the zoom
        assert "if (!btn) { zoomToKm(+th.dataset.km, 2); return; }" not in body, name
        assert 'km, span and settings">' in body, name


def test_the_fec_heading_click_zooms_and_opens_the_menu():
    body = _body("paintFecGrid")
    click = body.split("hdr.addEventListener('click', (ev) => {", 1)[1].split("});", 1)[0]
    assert "zoomToKm(kms[Math.floor(kms.length / 2)], 0.3);" in click
    assert "if (picks.length) showColumnMenu(b.left, b.bottom + 2, picks);" in click
    assert "return;" not in click
