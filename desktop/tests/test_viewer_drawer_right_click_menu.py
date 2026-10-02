"""A right-click anywhere on the chart's event drawer opens the span menu.

The boss, 2026-10-02: a hard time getting the menu from an event in the
drawer.  Only a mark that is a real event of its file carried one; a
right-click on a column's tag (the boxes along the bottom) or on a gray
filled-in mark fell through to the browser's own menu, and on Windows the
right press also picked or let go of the fiber and jumped the table, as a
left click does.  Now every drawer hit names its own picks (`menu`): a mark
its own event, a gray mark the cell's real leg, a tag every direction's
first fibre with a real event (worst first), and two directions ask which
one first.  Plain JS, no runtime here: pins the source.
"""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")
CTX = SRC.split("canvas.addEventListener('contextmenu', (ev) => {", 1)[1].split("});", 1)[0]
DOWN = SRC.split("canvas.addEventListener('mousedown', (ev) => {", 1)[1].split(
    "gTraceClick = null;", 1)[0]


def test_a_drawer_pick_is_a_real_event_only():
    assert "const legPick = (t, leg) => t && leg && !leg.hollow && leg.km != null" in SRC
    assert "[legPick(p.ta, cell.legs.a), legPick(p.tb, cell.legs.b)].filter(Boolean)" in SRC


def test_every_drawer_mark_and_tag_names_its_picks():
    # a mark: its own event; a gray (filled-in) mark: the cell's real leg
    assert "menu: q.leg.hollow ? cellPicks(D.have[fi], cell) : [legPick(q.t, q.leg)] };" in SRC
    # a one-fibre tag: that cell; a column tag: worst fibre first
    assert "menu: cellPicks(p, cell)," in SRC
    assert "menu: colPicks([...drawn.filter(c => c.fi === s.worstFi)," in SRC
    # both tag shapes (text and the bare strip) carry it to the hit
    assert SRC.count("go: t.go, t: null, e: null, menu: t.menu }") == 2


def test_right_click_opens_the_menu_or_asks_the_direction():
    assert "const picks = lh.menu || (lh.t && lh.e" in CTX
    assert "if (!picks.length) return;" in CTX
    assert "ev.preventDefault();" in CTX
    assert "if (picks.length === 1) showSpanMenu(ev.clientX, ev.clientY, p.dir, p.km, p.fiber, p.src);" in CTX
    assert "else showDirChooser(ev.clientX, ev.clientY, picks);" in CTX
    # the old guard that sent a tag or gray mark to the browser's menu
    assert "if (!lh || !lh.t) return;" not in CTX


def test_a_right_press_on_a_label_is_not_a_click():
    assert "if (ev.button === 0) gLabelClick = { hit: lh, x: ev.offsetX, y: ev.offsetY };" in DOWN
