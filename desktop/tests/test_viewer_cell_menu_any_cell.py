"""Every cell of an event table opens the span menu on a right-click.

Robert 2026-10-02: "fix that it doesnt open in all cells".  Only a
direction's loss cell with its own event opened it (107 of 576 cells on a
6-fiber A+B table in OTDR Suite mode, 154 of 1044 in FastReporter mode,
56 of 536 one direction); a Refl. cell, the identifier cells, a gray
cell and every Average row gave the browser's own menu.  Now a cell takes
its own event, or the event column it sits after, or each direction's
event there for the fiber (two ask which direction first); a cell before
any event takes the row's first event.  Browser-checked: every fiber cell
opens it in all three tables, and every cell that opened it before picks
the same event.

Plain JS, no runtime here: pins the source.
"""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def _body(name: str) -> str:
    body = SRC[SRC.index("function " + name + "("):]
    return body[:body.index("\n}\n")]


def test_every_event_table_wires_every_cell():
    for name in ("renderFastReporterGrid", "paintFrBidiGrid", "paintSuiteBidiGrid"):
        body = _body(name)
        assert "tb.addEventListener('contextmenu', (ev) => wireCellMenu(ev, tb));" in body, name
        assert "closest('td[data-km]')" not in body.split("addEventListener('contextmenu'", 1)[1][:200], name


def test_a_cell_finds_its_column_and_its_fibers_rows():
    fn = _body("cellMenuPicks")
    # a Refl. / Type / Section cell belongs to the event column before it
    assert "while (c && c.dataset.col == null) c = c.previousElementSibling;" in fn
    # the fiber's direction rows, this one first
    assert 'tr[data-dir][data-row^="${fid}-"]' in fn
    assert "rows.sort((a, b) => (b === tr) - (a === tr));" in fn
    # its own event wins; otherwise one pick per direction
    assert "const own = tr.dataset.dir ? pickOf(tr) : null;" in fn
    assert "if (own) return [own];" in fn
    assert "!picks.some(q => q.dir === p.dir)" in fn
    # no event column yet: the row's first event
    assert ": row.querySelector('td[data-km]');" in fn


def test_the_menu_is_the_span_menu_or_the_direction_chooser():
    fn = _body("wireCellMenu")
    # no pick at all: the View items alone (showColumnMenu([])), never the browser's
    assert "if (!picks.length) return;" not in fn
    assert "ev.preventDefault();" in fn
    assert "showColumnMenu(ev.clientX, ev.clientY, cellMenuPicks(tb, td));" in fn
