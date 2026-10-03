"""The hub's top bar (Robert 2026-10-01): one bar across the top of every
page in place of the sidebar.

OTDR App: the bar is drawn on every screen, and what it holds follows the
screen.

    tabs         Quick Analysis: a tab per trace tool, in a fixed order (FQA
                 Builder and Field Capture are project work, never tabs
                 here, whatever the edition).  A tab writes the page into
                 session_state['nav_radio'], which every Back button and
                 report link also writes.  Quick Analysis opens on the
                 Viewer.
    logo         the old Home button (key go_home): it goes to the Home
                 screen from anywhere.
    project      the logo, a Project tab (the old Back to Project) and the
                 tool open from the project screen; no Analysis Mode.
    Home / New   the logo, Theme and the update menu: no tabs, no Analysis
    Project      Mode.
    Traces tab   the A and B folder boxes, From SharePoint under them and
                 Clear Traces, drawn on that tab only.  What the boxes hold
                 must survive every other tab, and the bar's two switches,
                 which rerun the page before it draws.
    Viewer       with no folders loaded it says where they are.
    update menu  far right: 'Dev Build' or 'Version N'.  An orange Update
                 under it when a newer update is published or the machine
                 keeps no updates (pinned); pinned never asks the server.
                 The menu says what the old banner said: the reinstall or
                 pinned note alone, else "Update N is available" and Update
                 & Restart.
                 A build that never updates (OTDR_SUITE_NO_UPDATE) says so
                 in place of the Check button.
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
                      trace_box_value, load_traces, open_in_project, goto,
                      FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)
from test_update_nudge import _arm, _fake_manifest

A, B = str(FIXTURE_SPLICE_A_DIR), str(FIXTURE_SPLICE_B_DIR)
APP_SRC = (REPO_ROOT / "app.py").read_text(encoding="utf-8")

SUITE_TABS = [('Traces', 'Traces'), ('Splice Report', 'Splice Report'),
              ('Uni', 'Unidirectional'), ('Splice Report FEC', 'Splice Report FEC'),
              ('Secret Sauce', 'Secret Sauce'), ('Viewer', 'Viewer')]
APP_NAME = 'OTDR Suite App'
PROJECT_TAB = ('Project', 'Project Status')
NO_TRACES = 'No traces loaded.'
PIN_ENV = 'OTDR_SUITE_CACHE_PINNED'
NEEDS_ENV = 'OTDR_SUITE_NEEDS_INSTALL'


@pytest.fixture(autouse=True)
def _own_settings(tmp_path, monkeypatch):
    """The Analysis Mode switch writes settings.json: never the tech's own.
    And the default product name unless a test asks for the App's."""
    monkeypatch.setenv('OTDR_SETTINGS_DIR', str(tmp_path / 'settings'))
    monkeypatch.delenv('OTDR_SUITE_EDITION', raising=False)
    for k in (PIN_ENV, NEEDS_ENV, 'OTDR_SUITE_ENGINE_FILES', 'OTDR_SUITE_NO_UPDATE'):
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


def _switches(at):
    return sorted(t.label for t in at.toggle if t.label in ('Analysis Mode', 'Theme'))


def _lit(at):
    return [b.key for b in at.button if (b.key or '').startswith('nav_tab_')
            and b.proto.type == 'primary']


def _on_home(at):
    return (at.session_state['app_mode'] if 'app_mode' in at.session_state
            else None) not in ('traces', 'project', 'setup') and any(
                b.key == 'home_traces' for b in at.button)


# ── the tabs ──────────────────────────────────────────────────────────────

def test_quick_analysis_has_a_tab_per_tool_in_order():
    at = _hub()
    assert _tabs(at) == SUITE_TABS
    assert at.button(key='go_home').label == 'OTDR Suite'
    assert _switches(at) == ['Analysis Mode', 'Theme']


def test_the_app_edition_has_the_same_tabs_and_its_own_name(monkeypatch):
    """FQA Builder and Field Capture are project work (Robert 2026-09-24):
    no tab for them in Quick Analysis, whatever the edition."""
    monkeypatch.setenv('OTDR_SUITE_EDITION', APP_NAME)
    at = _hub()
    assert _tabs(at) == SUITE_TABS
    assert at.button(key='go_home').label == APP_NAME


def test_no_viewer_fec_tab_and_its_old_page_opens_the_viewer():
    """FEC shots load in the Viewer itself (main #589, Robert 2026-10-02):
    no Viewer FEC tab, and a page name left from before opens the Viewer."""
    at = _hub()
    assert 'Viewer FEC' not in [p for _l, p in _tabs(at)]
    at.session_state['nav_radio'] = 'Viewer FEC'
    at.run()
    assert not at.exception, at.exception
    assert page_of(at) == 'Viewer'


def test_the_app_opens_on_the_viewer():
    at = _hub()
    assert page_of(at) == 'Viewer'
    # the open tab is the lit one
    assert _lit(at) == ['nav_tab_Viewer']


@pytest.mark.parametrize('label,page', SUITE_TABS)
def test_a_tab_opens_its_page(label, page):
    at = _hub()
    if page == 'Viewer':
        go_tab(at, 'Traces')                 # somewhere else first
    go_tab(at, page)
    assert not at.exception, at.exception
    assert page_of(at) == page
    assert at.session_state['nav_radio'] == page
    assert _lit(at) == [f'nav_tab_{page}']


def test_nav_radio_is_no_widget_any_more():
    """Every Back button and report link writes nav_radio; with no widget
    owning it, a write from anywhere is allowed and opens that page."""
    at = _hub()
    assert not at.radio or all(r.key != 'nav_radio' for r in at.radio)
    at.session_state['nav_radio'] = 'Secret Sauce'
    at.run()
    assert not at.exception, at.exception
    assert page_of(at) == 'Secret Sauce'


def test_the_logo_goes_home():
    """The logo is the old Home button (key go_home, read by _mode_actions
    at the top of the next run)."""
    at = go_tab(_hub(), 'Splice Report')
    at.button(key='go_home').click().run()
    assert not at.exception, at.exception
    assert _on_home(at)


# ── the bar on the other screens ──────────────────────────────────────────

def _home(monkeypatch):
    monkeypatch.setenv('OTDR_TEST_HOME', '1')
    at = run_streamlit(default_timeout=180).run()
    assert not at.exception, at.exception
    return at


def test_the_home_screen_bar_has_no_tabs_and_no_mode_switch(monkeypatch):
    at = _home(monkeypatch)
    assert _on_home(at)
    assert _tabs(at) == []
    assert _switches(at) == ['Theme']
    assert at.button(key='go_home').label == 'OTDR Suite'
    assert _menu_label(at) == 'Dev Build'


def test_new_project_bar_has_no_tabs_and_no_mode_switch(monkeypatch):
    at = _home(monkeypatch)
    at.button(key='home_new').click().run()
    assert not at.exception, at.exception
    assert at.session_state['app_mode'] == 'setup'
    assert _tabs(at) == []
    assert _switches(at) == ['Theme']
    assert _menu_label(at) == 'Dev Build'
    # and its logo goes Home too
    at.button(key='go_home').click().run()
    assert not at.exception, at.exception
    assert _on_home(at)


def test_a_project_bar_is_the_logo_and_its_project_tab(monkeypatch, tmp_path):
    """No tool list and no Analysis Mode in a project (Robert 2026-09-27):
    the Project tab is the way back to the project screen."""
    at = open_in_project(tmp_path / 'Span', monkeypatch)
    assert not at.exception, at.exception
    assert at.session_state['app_mode'] == 'project'
    assert page_of(at) == 'Project Status'
    assert _tabs(at) == [PROJECT_TAB]
    assert _lit(at) == ['nav_tab_Project Status']
    assert _switches(at) == ['Theme']
    assert _menu_label(at) == 'Dev Build'


@pytest.mark.parametrize('label,page', [('Viewer', 'Viewer'),
                                        ('Splice Report', 'Splice Report'),
                                        ('Uni', 'Unidirectional'),
                                        ('FQA Builder', 'FQA Builder')])
def test_a_project_tool_page_adds_its_own_tab_and_project_goes_back(
        monkeypatch, tmp_path, label, page):
    at = goto(open_in_project(tmp_path / 'Span', monkeypatch), page)
    assert not at.exception, at.exception
    assert page_of(at) == page
    assert _tabs(at) == [PROJECT_TAB, (label, page)]
    assert _lit(at) == [f'nav_tab_{page}']
    assert _switches(at) == ['Theme']
    # no Trace Folders in a project, and no Traces tab to reach them
    assert not [b for b in at.button if b.key == 'side_clear_traces']
    go_tab(at, 'Project Status')
    assert not at.exception, at.exception
    assert page_of(at) == 'Project Status'
    assert at.session_state['app_mode'] == 'project'
    assert _tabs(at) == [PROJECT_TAB]


def test_the_logo_goes_home_from_a_project(monkeypatch, tmp_path):
    at = goto(open_in_project(tmp_path / 'Span', monkeypatch), 'Viewer')
    at.button(key='go_home').click().run()
    assert not at.exception, at.exception
    assert _on_home(at)


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
        browse = {b.key for b in at.button} & {'side_browse_a', 'side_browse_b'}
        assert bool(browse) == (page == 'Traces'), page
        sp = [e for e in at.expander if e.label == '☁️ From SharePoint']
        assert len(sp) == (1 if page == 'Traces' else 0), page


def test_from_sharepoint_sits_under_the_boxes_on_the_traces_tab():
    """OTDR App: one SharePoint folder, under the A and B boxes (Robert
    2026-09-30), now on the Traces tab with them."""
    at = go_tab(_hub(), 'Traces')
    assert not at.exception, at.exception
    (sp,) = [e for e in at.main.expander if e.label == '☁️ From SharePoint']
    assert not sp.proto.expanded


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


def _menu(at):
    """The bar's update menu (a project screen has popovers of its own)."""
    (pop,) = [p for p in at.get('popover')
              if p.proto.popover.label == 'Dev Build'
              or p.proto.popover.label.startswith('Version ')]
    return pop


def _menu_label(at):
    return _menu(at).proto.popover.label


def _restart_keys(at):
    return [b.key for b in at.button if b.key in ('upd_menu_restart', 'upd_restart')]


def test_a_dev_checkout_shows_dev_build_and_no_flag(monkeypatch, tmp_path):
    monkeypatch.delenv('OTDR_SUITE_SOURCE', raising=False)
    at = _hub()
    assert _menu_label(at) == 'Dev Build'
    assert 'OTDR Suite · dev' in [c.value for c in at.caption]
    assert any(b.key == 'upd_check' for b in at.button)
    assert not _flag(at)


@pytest.mark.parametrize('screen', ['quick', 'home'])
def test_a_build_that_never_updates_says_so_in_place_of_the_check(
        monkeypatch, screen):
    """OTDR App: a build that never updates itself (OTDR_SUITE_NO_UPDATE)
    could only ever say "could not reach the update server" from a Check
    button: the menu says how it updates instead."""
    monkeypatch.setenv('OTDR_SUITE_NO_UPDATE', '1')
    at = _hub() if screen == 'quick' else _home(monkeypatch)
    pop = _menu(at)
    assert 'Updates: install a newer build to update.' in [c.value for c in pop.caption]
    assert not any(b.key == 'upd_check' for b in at.button)
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
    pop = _menu(at)
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


def _menu_below_px(product_name, tabs, show_mode=True):
    """app.py's _nav_menu_below_px for one product name: the width a bar
    holding `tabs` folds at."""
    body = ast.parse(APP_SRC).body
    keep = [n for n in body if (isinstance(n, ast.FunctionDef)
                                and n.name == '_nav_menu_below_px')
            or (isinstance(n, ast.Assign) and any(
                getattr(t, 'id', None) == 'NAV_LOGO_PX' for t in n.targets))]
    ns = {'PRODUCT_NAME': product_name}
    exec(compile(ast.Module(body=keep, type_ignores=[]), 'app.py', 'exec'), ns)
    return ns['_nav_menu_below_px'](tabs, show_mode)


def test_the_bar_width_follows_the_name_and_the_tabs():
    tabs_s, logo_s, menu_s = _bar_sizes('OTDR Suite', None)
    tabs_a, logo_a, menu_a = _bar_sizes(APP_NAME, APP_NAME)
    # OTDR App: the edition does not add tabs (FQA Builder and Field
    # Capture are project work)
    assert tabs_s == SUITE_TABS and tabs_a == SUITE_TABS
    assert _bar_sizes('OTDR Suite', 'x')[2] == menu_s
    # the logo is wide enough for the name, and the App's longer name gets more
    assert logo_s >= 9.5 * len('OTDR Suite') and logo_a > logo_s
    # so the App's bar folds into the menu at a wider window
    assert menu_a > menu_s
    # the Quick Analysis bar comes to about 1,258 px with the default name
    assert 1200 <= menu_s <= 1300, menu_s
    # a longer name alone, or a tab more, moves it
    assert _bar_sizes('OTDR Suite XX', None)[2] > menu_s
    assert _menu_below_px('OTDR Suite', SUITE_TABS + [('X', 'X')]) > menu_s
    assert _menu_below_px('OTDR Suite', SUITE_TABS) == menu_s
    # and a bar with no Analysis Mode switch folds sooner
    assert _menu_below_px('OTDR Suite', SUITE_TABS, False) < menu_s


def _fold_css(at):
    return next(m.value for m in at.markdown if '.st-key-top_nav{' in m.value)


@pytest.mark.parametrize('edition', [None, APP_NAME], ids=['suite', 'app'])
def test_the_page_folds_the_bar_at_that_width(monkeypatch, edition):
    if edition:
        monkeypatch.setenv('OTDR_SUITE_EDITION', edition)
    _tabs_, _logo, menu = _bar_sizes(edition or 'OTDR Suite', edition)
    at = _hub()
    assert f'@media (max-width:{menu}px)' in _fold_css(at)


def test_the_other_screens_fold_at_their_own_width(monkeypatch, tmp_path):
    """The Home screen's bar and a project's hold less, so they fold at the
    width of what they hold."""
    at = _home(monkeypatch)
    home = _menu_below_px('OTDR Suite', [], False)
    assert f'@media (max-width:{home}px)' in _fold_css(at)
    at = goto(open_in_project(tmp_path / 'Span', monkeypatch), 'Viewer')
    proj = _menu_below_px('OTDR Suite', [PROJECT_TAB, ('Viewer', 'Viewer')], False)
    assert f'@media (max-width:{proj}px)' in _fold_css(at)
