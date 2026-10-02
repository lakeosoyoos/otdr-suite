"""Every column header opens the span menu on a right-click.

Robert 2026-10-02: "when we get too many columns in viewer we lose our
elipsis" and "we need to be able to right click in any header of the column
and have the access to that menu".  Only the one-direction grid and the FEC
table had the ⋯; the two A+B tables (OTDR Suite and FastReporter mode), the
wide ones, never did.  Now all four carry it, and every header cell of an
event column (the event, its kind and distance, its Loss / Refl. / Type)
opens the same menu on a right-click.  A header over no event (Identifiers,
a Section, the Statistics) opens the View items.

Plain JS, no runtime here: pins the source.  Browser-checked 2026-10-02 on
a 19-fiber bidirectional job in both modes, one direction, A+B and FEC.
"""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")

PAINTERS = ("renderFastReporterGrid", "paintFrBidiGrid", "paintSuiteBidiGrid", "paintFecGrid")


def _body(name: str) -> str:
    body = SRC[SRC.index("function " + name + "("):]
    return body[:body.index("\n}\n")]


def test_every_table_wires_its_whole_header():
    for name in PAINTERS:
        assert "wireHeaderMenu(table, " in _body(name), name


def test_every_event_table_carries_the_dots():
    for name in PAINTERS:
        assert 'class="fr-evmenu"' in _body(name), name


def test_every_header_row_of_an_event_column_names_its_column():
    # the event, its kind / distance and its unit cells, in each event table
    for name in ("renderFastReporterGrid", "paintFrBidiGrid", "paintSuiteBidiGrid"):
        body = _body(name)
        assert 'class="fr-evhdr" data-km=' in body and 'data-col="${i}"' in body.split('class="fr-evhdr"', 1)[1][:80], name
        assert 'class="fr-evsub" data-col="${i}"' in body, name
        assert '<th class="fr-sub" data-col="${i}">Loss<br>(dB)</th>' in body, name
        assert '<th class="fr-sub" data-col="${i}">Refl.<br>(dB)</th>' in body, name
    # FastReporter mode's mixed column has a Type header too
    assert '<th class="fr-sub" data-col="${i}">Type</th>' in _body("paintFrBidiGrid")


def test_the_right_click_opens_the_menu_and_never_the_browser_one():
    fn = SRC[SRC.index("function wireHeaderMenu("):]
    fn = fn[:fn.index("\n}\n")]
    assert "table.tHead.addEventListener('contextmenu'" in fn
    assert "ev.preventDefault();" in fn
    # no data-col: the View items alone
    assert "th.dataset.col != null ? picksFor(+th.dataset.col) : []" in fn
    chooser = SRC[SRC.index("function showDirChooser("):][:400]
    assert "(picks.length ? `<div class=\"span-menu-hd\">Which Direction?</div>` : '')" in chooser


def test_the_a_plus_b_headers_pick_each_directions_own_raw_km():
    # as a right-click on that direction's cell does
    fr = _body("paintFrBidiGrid")
    assert "rawKm(legOf(fi).pos_m)" in fr
    assert "!legOf(k).synthetic" in fr          # a measured leg before FR's transplant
    su = _body("paintSuiteBidiGrid")
    assert "rawKm(legOf(fi).km)" in su
    assert "LEGS.filter(l => l !== 'avg')" in su
