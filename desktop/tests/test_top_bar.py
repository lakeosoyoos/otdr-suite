"""The hub's top bar (Robert 2026-10-01): one bar across the top of every
page in place of the sidebar.

    tabs         a tab per tool, in a fixed order; the App edition adds FQA
                 Builder and Field Capture at the end.  A tab writes the page
                 into session_state['nav_radio'], which every Back button
                 and report link also writes.  The app opens on the Viewer,
                 and the logo goes back to it.
    Traces tab   the A and B folder boxes, drawn on that tab only.  What
                 they hold must survive every other tab, and the bar's two
                 switches, which rerun the page before it draws.
    Viewer       with no folders loaded it says where they are.
    update menu  far right: 'Dev Build' or 'Version N'.  An orange Update
                 under it when a newer update is published or the machine
                 keeps no updates (pinned); pinned never asks the server.
                 The menu says what the old banner said: the reinstall or
                 pinned note alone, else "Update N is available" and Update
                 & Restart.
    narrow bar   the width the bar folds into a menu at follows the product
                 name and the tab list.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import urllib.request

import pytest

from conftest import (REPO_ROOT, run_streamlit, page_of, go_tab, trace_box,
                      trace_box_value, load_traces, FIXTURE_SPLICE_A_DIR,
                      FIXTURE_SPLICE_B_DIR)
from test_update_nudge import _arm, _fake_manifest

A, B = str(FIXTURE_SPLICE_A_DIR), str(FIXTURE_SPLICE_B_DIR)
APP_SRC = (REPO_ROOT / "app.py").read_text(encoding="utf-8")

SUITE_TABS = [('Traces', 'Traces'), ('Splice Report', 'Splice Report'),
              ('Uni', 'Unidirectional'), ('Splice Report FEC', 'Splice Report FEC'),
              ('Secret Sauce', 'Secret Sauce'), ('Viewer', 'Viewer'),
              ('Viewer FEC', 'Viewer FEC')]
APP_TABS = SUITE_TABS + [('FQA Builder', 'FQA Builder'),
                         ('Field Capture', 'Field Capture')]
APP_NAME = 'OTDR Suite App'
NO_TRACES = 'No traces loaded.'
PIN_ENV = 'OTDR_SUITE_CACHE_PINNED'
NEEDS_ENV = 'OTDR_SUITE_NEEDS_INSTALL'


@pytest.fixture(autouse=True)
def _own_settings(tmp_path, monkeypatch):
    """The Analysis Mode switch writes settings.json: never the tech's own.
    And the Suite edition unless a test asks for the App."""
    monkeypatch.setenv('OTDR_SETTINGS_DIR', str(tmp_path / 'settings'))
    monkeypatch.delenv('OTDR_SUITE_EDITION', raising=False)
    for k in (PIN_ENV, NEEDS_ENV, 'OTDR_SUITE_ENGINE_FILES'):
        monkeypatch.delenv(k, raising=False)


def _hub():
    at = run_streamlit(default_timeout=180).run()
    assert not at.exception, at.exception
    return at


def _tabs(at):
    """(label, page) of every tab in the bar, in order."""
    return [(b.label, b.key[len('nav_tab_'):]) for b in at.button
            if (b.key or '').startswith('nav_tab_')]


def _boxes(at):
    return trace_box_value(at, 'a'), trace_box_value(at, 'b')


def _toggle(at, label):
    return next(t for t in at.toggle if t.label == label)


# ── the tabs ──────────────────────────────────────────────────────────────

def test_the_suite_has_a_tab_per_tool_in_order():
    at = _hub()
    assert _tabs(at) == SUITE_TABS
    assert next(b for b in at.button if b.key == 'nav_logo_btn').label == 'OTDR Suite'


def test_the_app_edition_adds_its_two_tools_at_the_end(monkeypatch):
    monkeypatch.setenv('OTDR_SUITE_EDITION', APP_NAME)
    at = _hub()
    assert _tabs(at) == APP_TABS
    assert next(b for b in at.button if b.key == 'nav_logo_btn').label == APP_NAME


def test_viewer_fec_has_its_own_tab():
    at = go_tab(_hub(), 'Viewer FEC')
    assert not at.exception, at.exception
    assert page_of(at) == 'Viewer FEC'


def test_the_app_opens_on_the_viewer():
    at = _hub()
    assert page_of(at) == 'Viewer'
    # the open tab is the lit one
    lit = [b.key for b in at.button if (b.key or '').startswith('nav_tab_')
           and b.proto.type == 'primary']
    assert lit == ['nav_tab_Viewer']


@pytest.mark.parametrize('label,page', SUITE_TABS)
def test_a_tab_opens_its_page(label, page):
    at = _hub()
    if page == 'Viewer':
        go_tab(at, 'Traces')                 # somewhere else first
    go_tab(at, page)
    assert not at.exception, at.exception
    assert page_of(at) == page
    assert at.session_state['nav_radio'] == page
    lit = [b.key for b in at.button if (b.key or '').startswith('nav_tab_')
           and b.proto.type == 'primary']
    assert lit == [f'nav_tab_{page}']


def test_nav_radio_is_no_widget_any_more():
    """Every Back button and report link writes nav_radio; with no widget
    owning it, a write from anywhere is allowed and opens that page."""
    at = _hub()
    assert not at.radio or all(r.key != 'nav_radio' for r in at.radio)
    at.session_state['nav_radio'] = 'Secret Sauce'
    at.run()
    assert not at.exception, at.exception
    assert page_of(at) == 'Secret Sauce'


def test_the_logo_goes_to_the_viewer():
    at = go_tab(_hub(), 'Splice Report')
    at.button(key='nav_logo_btn').click().run()
    assert not at.exception, at.exception
    assert page_of(at) == 'Viewer'


# ── the Traces tab's boxes keep what they hold ─────────────────────────────

def test_the_boxes_are_drawn_on_the_traces_tab_only():
    at = _hub()
    keys = {'view_dir_a_input', 'view_dir_b_input'}
    for _label, page in SUITE_TABS:
        go_tab(at, page)
        drawn = keys & {t.key for t in at.text_input}
        assert drawn == (keys if page == 'Traces' else set()), page
        clear = [b for b in at.button if b.key == 'side_clear_traces']
        assert bool(clear) == (page == 'Traces'), page


def test_the_boxes_keep_their_folders_across_every_tab():
    at = load_traces(_hub(), A, B)
    for _label, page in SUITE_TABS + [('Traces', 'Traces')]:
        go_tab(at, page)
        assert not at.exception, (page, at.exception)
        assert _boxes(at) == (A, B), page
        at.run()                             # a second run on the same page
        assert _boxes(at) == (A, B), page
    # and the boxes on screen show them
    assert (trace_box(at, 'a').value, trace_box(at, 'b').value) == (A, B)


@pytest.mark.parametrize('where', ['Traces', 'Splice Report', 'Viewer'])
@pytest.mark.parametrize('switch', ['Analysis Mode', 'Theme'])
def test_the_boxes_keep_their_folders_across_a_switch(where, switch):
    """Both switches rerun the page before it draws: a run cut short, which
    must not lose what the boxes hold."""
    at = load_traces(_hub(), A, B)
    go_tab(at, where)
    before = {'Analysis Mode': at.session_state['analysis_mode'],
              'Theme': at.session_state['ui_theme']}[switch]
    knob = _toggle(at, switch)
    knob.set_value(not knob.value).run()
    assert not at.exception, at.exception
    after = {'Analysis Mode': at.session_state['analysis_mode'],
             'Theme': at.session_state['ui_theme']}[switch]
    assert after != before, 'the switch did not switch'
    assert page_of(at) == where
    assert _boxes(at) == (A, B)
    at.run()
    assert _boxes(at) == (A, B)
    go_tab(at, 'Splice Report' if where != 'Splice Report' else 'Viewer')
    assert _boxes(at) == (A, B)
    assert (trace_box(at, 'a').value, trace_box(at, 'b').value) == (A, B)


def test_the_traces_tab_counts_the_fibers_of_each_side():
    at = load_traces(_hub(), A, B)
    caps = [c.value for c in at.main.caption]
    assert f'A: {len(os.listdir(A))} fibers' in caps, caps
    assert f'B: {len(os.listdir(B))} fibers' in caps, caps


# ── the Viewer with nothing loaded ────────────────────────────────────────

@pytest.mark.parametrize('a,b,shown', [('', '', True), (A, '', False),
                                       ('', B, False), (A, B, False)])
def test_the_viewer_says_where_the_folders_are_only_with_none_loaded(a, b, shown):
    at = _hub()
    if a or b:
        load_traces(at, a or None, b or None)
        go_tab(at, 'Viewer')
    assert page_of(at) == 'Viewer'
    caps = [c.value for c in at.main.caption]
    assert any(NO_TRACES in c and '**Traces** tab' in c for c in caps) == shown, caps


# ── the update menu ───────────────────────────────────────────────────────

def _texts(at):
    out = []
    for kind in ('warning', 'error', 'info', 'success', 'caption'):
        out += [e.value for e in getattr(at, kind)]
    return out


def _flag(at):
    return any('<p class="nav-update-flag">Update</p>' in m.value for m in at.markdown)


def _menu_label(at):
    (pop,) = at.get('popover')
    return pop.proto.popover.label


def _restart_keys(at):
    return [b.key for b in at.button if b.key in ('upd_menu_restart', 'upd_restart')]


def test_a_dev_checkout_shows_dev_build_and_no_flag(monkeypatch, tmp_path):
    monkeypatch.delenv('OTDR_SUITE_SOURCE', raising=False)
    at = _hub()
    assert _menu_label(at) == 'Dev Build'
    assert 'OTDR Suite · dev' in [c.value for c in at.caption]
    assert any(b.key == 'upd_check' for b in at.button)
    assert not _flag(at)


def test_up_to_date_no_flag_and_nothing_on_offer(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path, ('build 136 (2026-08-05)', 'bundled'), _fake_manifest(136))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    at = _hub()
    assert _menu_label(at) == 'Version 136'
    assert not _flag(at)
    assert not any('is available' in t for t in _texts(at)), _texts(at)
    assert _restart_keys(at) == []


def test_a_newer_update_flags_and_offers_the_restart(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path, ('build 134 (2026-08-01)', 'bundled'), _fake_manifest(136))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    at = _hub()
    assert _menu_label(at) == 'Version 134'
    assert _flag(at)
    assert any('Update 136 is available (running 134)' in t for t in _texts(at)), _texts(at)
    assert _restart_keys(at) == ['upd_menu_restart']


def test_a_dev_run_of_a_newer_update_says_restart_not_a_button(monkeypatch, tmp_path):
    """Not frozen (a source run): the menu says how to apply it, no button."""
    _arm(monkeypatch, tmp_path, ('build 134 (2026-08-01)', 'bundled'), _fake_manifest(136))
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    at = _hub()
    assert _flag(at)
    assert any('Update 136 is available (running 134)' in t for t in _texts(at))
    assert any('Restart the app to apply' in t for t in _texts(at))
    assert _restart_keys(at) == []


def test_a_newer_update_that_needs_an_install_says_only_that(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path, ('build 134 (2026-08-01)', 'bundled'), _fake_manifest(136))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setenv(NEEDS_ENV, 'update 136 needs a fresh install (app build 134)')
    at = _hub()
    assert _flag(at)
    t = _texts(at)
    assert any('Update 136 needs a fresh install (running 134)' in x for x in t), t
    assert not any('is available' in x for x in t), t
    assert _restart_keys(at) == []


@pytest.mark.parametrize('published', [134, 136], ids=['current', 'newer'])
def test_pinned_flags_says_only_the_pin_note_and_never_asks(monkeypatch, tmp_path,
                                                            published):
    hits = []
    fake = _fake_manifest(published)

    def spy(*a, **k):
        hits.append(1)
        return fake(*a, **k)

    _arm(monkeypatch, tmp_path, ('build 134 (2026-08-01)', 'bundled'), spy)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setenv(PIN_ENV, 'engine files disappeared 2 times in 7 days')
    at = _hub()
    assert _flag(at)
    t = _texts(at)
    assert any('Updates cannot be kept on this computer' in x for x in t), t
    assert not any('is available' in x for x in t), t
    assert _restart_keys(at) == []
    assert hits == [], 'a pinned machine asked the update server'
    # The tech's own Check for Updates may ask, and still offers nothing.
    at.button(key='upd_check').click().run()
    assert not at.exception, at.exception
    t = _texts(at)
    assert not any('is available' in x for x in t), t
    assert _restart_keys(at) == []


def test_the_spy_sees_an_unpinned_fetch(monkeypatch, tmp_path):
    """The other half of the test above: unpinned, the bar does ask."""
    hits = []
    fake = _fake_manifest(136)

    def spy(*a, **k):
        hits.append(1)
        return fake(*a, **k)

    _arm(monkeypatch, tmp_path, ('build 134 (2026-08-01)', 'bundled'), spy)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    _hub()
    assert hits


def test_a_manual_check_offers_the_restart_the_bar_did_not_know_of(monkeypatch,
                                                                    tmp_path):
    """The bar's own look found nothing (the server was down); the tech's
    Check for Updates finds 136: the menu says so and offers its restart."""
    def down(*a, **k):
        raise OSError('no route to host')

    _arm(monkeypatch, tmp_path, ('build 134 (2026-08-01)', 'bundled'), down)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    at = _hub()
    assert not _flag(at) and _restart_keys(at) == []
    monkeypatch.setattr(urllib.request, 'urlopen', _fake_manifest(136))
    at.button(key='upd_check').click().run()
    assert not at.exception, at.exception
    assert any('Update 136 is available (running 134)' in t for t in _texts(at)), _texts(at)
    assert _restart_keys(at) == ['upd_restart']


def test_a_manual_check_that_needs_an_install_says_only_that(monkeypatch, tmp_path):
    """As above, but this copy cannot carry the update: the reinstall note
    alone, no "is available" beside it, no restart."""
    def down(*a, **k):
        raise OSError('no route to host')

    _arm(monkeypatch, tmp_path, ('build 134 (2026-08-01)', 'bundled'), down)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setenv(NEEDS_ENV, 'update 136 needs a fresh install (app build 134)')
    at = _hub()
    monkeypatch.setattr(urllib.request, 'urlopen', _fake_manifest(136))
    at.button(key='upd_check').click().run()
    assert not at.exception, at.exception
    t = _texts(at)
    assert any('Update 136 needs a fresh install (running 134)' in x for x in t), t
    assert not any('is available' in x for x in t), t
    assert _restart_keys(at) == []


def test_a_manual_check_when_up_to_date_says_so(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path, ('build 136 (2026-08-05)', 'bundled'), _fake_manifest(136))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    at = _hub()
    at.button(key='upd_check').click().run()
    assert any('Up to date: engine 136 is the latest.' in s.value for s in at.success)


@pytest.mark.parametrize('key', ['upd_menu_restart', 'upd_restart'])
def test_a_restart_that_cannot_start_says_so(monkeypatch, tmp_path, key):
    def down(*a, **k):
        raise OSError('no route to host')

    _arm(monkeypatch, tmp_path, ('build 134 (2026-08-01)', 'bundled'),
         _fake_manifest(136) if key == 'upd_menu_restart' else down)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)

    def boom(*a, **k):
        raise OSError('cannot start')

    monkeypatch.setattr(subprocess, 'Popen', boom)
    at = _hub()
    if key == 'upd_restart':
        monkeypatch.setattr(urllib.request, 'urlopen', _fake_manifest(136))
        at.button(key='upd_check').click().run()
    at.button(key=key).click().run()
    assert not at.exception, at.exception
    assert any("Couldn't start the restart. Close OTDR Suite" in e.value
               for e in at.error), [e.value for e in at.error]
    assert '_upd_watchdog' not in at.session_state


def test_the_restart_watchdog_is_drawn_on_the_page(monkeypatch, tmp_path):
    """A restart that starts asks for the watchdog, drawn under the bar on
    the next run (not in the menu, which a click elsewhere closes)."""
    _arm(monkeypatch, tmp_path, ('build 134 (2026-08-01)', 'bundled'), _fake_manifest(136))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    at = _hub()
    at.session_state['_upd_watchdog'] = True
    at.run()
    assert not at.exception, at.exception
    assert '_upd_watchdog' not in at.session_state
    assert any('/_stcore/health' in (f.proto.srcdoc or '') for f in at.get('iframe'))
    # and the watchdog is not inside the menu
    (pop,) = at.get('popover')
    assert not any('/_stcore/health' in (f.proto.srcdoc or '') for f in pop.get('iframe'))


# ── how wide the bar has to be ────────────────────────────────────────────

def _bar_sizes(product_name, edition):
    """NAV_TABS, NAV_LOGO_PX and NAV_MENU_BELOW_PX as app.py works them out
    for one product name and edition, lifted out of app.py by AST."""
    body = ast.parse(APP_SRC).body
    names = lambda n: {t.id for t in getattr(n, 'targets', []) if isinstance(t, ast.Name)}
    start = next(i for i, n in enumerate(body) if 'NAV_TABS' in names(n))
    end = next(i for i, n in enumerate(body) if 'NAV_MENU_BELOW_PX' in names(n))
    env = {'OTDR_SUITE_EDITION': edition} if edition else {}

    class _Os:
        environ = env
    ns = {'os': _Os, 'PRODUCT_NAME': product_name}
    exec(compile(ast.Module(body=body[start:end + 1], type_ignores=[]),
                 'app.py', 'exec'), ns)
    return ns['NAV_TABS'], ns['NAV_LOGO_PX'], ns['NAV_MENU_BELOW_PX']


def test_the_bar_width_follows_the_name_and_the_tabs():
    tabs_s, logo_s, menu_s = _bar_sizes('OTDR Suite', None)
    tabs_a, logo_a, menu_a = _bar_sizes(APP_NAME, APP_NAME)
    assert tabs_s == SUITE_TABS and tabs_a == APP_TABS
    # the logo is wide enough for the name, and the App's longer name gets more
    assert logo_s >= 9.5 * len('OTDR Suite') and logo_a > logo_s
    # the App's bar holds more, so it folds into the menu at a wider window
    assert menu_a > menu_s
    # the Suite's bar comes to about 1,345 px
    assert 1300 <= menu_s <= 1400, menu_s
    # a longer name alone, or a tab more, moves it
    assert _bar_sizes('OTDR Suite XX', None)[2] > menu_s
    assert _bar_sizes('OTDR Suite', 'x')[2] > menu_s


@pytest.mark.parametrize('edition', [None, APP_NAME], ids=['suite', 'app'])
def test_the_page_folds_the_bar_at_that_width(monkeypatch, edition):
    if edition:
        monkeypatch.setenv('OTDR_SUITE_EDITION', edition)
    _tabs_, _logo, menu = _bar_sizes(edition or 'OTDR Suite', edition)
    at = _hub()
    css = next(m.value for m in at.markdown if '.st-key-top_nav{' in m.value)
    assert f'@media (max-width:{menu}px)' in css
