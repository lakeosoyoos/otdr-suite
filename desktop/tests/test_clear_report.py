"""Clear Report on the three report pages, and what a clear takes with it
(Robert 2026-09-28).

Each report page draws a Clear Report button above its report.  It opens a
pop-up with three answers:

    Skip                       nothing changes
    Clear Report Only          this page's report goes, the traces stay
    Clear Report and Traces    every tool's traces and reports go, and the
                               Traces tab's A and B folders empty with them

A cleared report is cleared for good: its saved copy under the hub's cache
goes too, so the same folder needs a fresh run.  The same holds for the
Traces tab's Clear Traces.  The traces and the report files the tech saved to a
folder are never touched.

The reports on screen here are real: the three engines run once on the
fixtures and their manifests are handed to the pages.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys

import pytest

from conftest import (run_streamlit, run_splicereport, run_secretsauce,
                      import_trace_server, go_tab, trace_box, trace_box_value,
                      APP_PATH, SPLICEREPORT_DIR,
                      FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)

A, B = str(FIXTURE_SPLICE_A_DIR), str(FIXTURE_SPLICE_B_DIR)
PAGE = {'sr': 'Splice Report', 'uni': 'Unidirectional', 'ss': 'Secret Sauce'}
ANSWERS = ['Skip', 'Clear Report Only', 'Clear Report and Traces']


def _cache_path(name, folder):
    """The hub's own path rule, lifted out of app.py without importing it."""
    tree = ast.parse(APP_PATH.read_text(encoding='utf-8'))
    mod = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef)
                           and n.name == '_hub_cache_path'], type_ignores=[])
    ns = {'os': os}
    exec(compile(mod, 'app.py', 'exec'), ns)
    return ns['_hub_cache_path'](name, folder)


@pytest.fixture(scope='module')
def manifests(tmp_path_factory):
    out = tmp_path_factory.mktemp('reports')
    rc, sr, err = run_splicereport(A, B, out / 'sr.xlsx')
    assert rc == 0 and sr and sr.get('ok'), err[-800:]
    rc, ss, err = run_secretsauce(A, out, 'xlsx')
    assert rc == 0 and ss and ss.get('ok'), err[-800:]
    p = subprocess.run(
        [sys.executable, str(SPLICEREPORT_DIR / 'run_splicereport.py'), '--uni',
         '--dir-a', A, '--out', str(out / 'uni.xlsx'), '--analysis', 'suite'],
        capture_output=True, text=True)
    uni = next((json.loads(l) for l in reversed(p.stdout.strip().splitlines())
                if l.strip().startswith('{')), None)
    assert p.returncode == 0 and uni and uni.get('ok'), p.stderr[-800:]
    return {'sr': sr, 'uni': uni, 'ss': ss}


def _boxes(at):
    """What the Traces tab's A and B boxes hold, read on any page."""
    return trace_box_value(at, 'a'), trace_box_value(at, 'b')


def _press_clear_traces(at):
    """The Traces tab's Clear Traces, which asks first."""
    go_tab(at, 'Traces')
    at.button(key='side_clear_traces').click().run()
    assert not at.exception, at.exception
    return at


def _btn(at, label):
    return next(b for b in at.button if b.label == label)


def _has(at, label):
    return any(b.label == label for b in at.button)


@pytest.fixture
def hub(manifests, tmp_path, monkeypatch):
    """The Suite with a span loaded and all three reports run: on screen (in
    the session) and saved (in the hub's cache).  Returns (at, saved), where
    saved maps each report to the files of its saved copy."""
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    at = run_streamlit(default_timeout=180).run()
    trace_box(at, 'a').input(A).run()
    trace_box(at, 'b').input(B).run()
    ss_folder = os.path.abspath(at.session_state['ss_folder_input'])
    at.session_state['uni_folder_input'] = A
    at.session_state['sr_result'] = manifests['sr']
    at.session_state['sr_dirs'] = (A, B)
    at.session_state['uni_result'] = dict(manifests['uni'], _folder=A)
    at.session_state['ss_result'] = dict(manifests['ss'], _folder=ss_folder)
    saved = {'sr': _cache_path('.sr_grid_cache.json', A),
             'uni': _cache_path('uni_result_cache.json', A),
             'ss': _cache_path('ss_result_cache.json', ss_folder)}
    with open(saved['sr'], 'w', encoding='utf-8') as fh:
        json.dump({'manifest': manifests['sr'], '_dirs': [A, B]}, fh)
    with open(saved['uni'], 'w', encoding='utf-8') as fh:
        json.dump(at.session_state['uni_result'], fh)
    with open(saved['ss'], 'w', encoding='utf-8') as fh:
        json.dump(at.session_state['ss_result'], fh)
    # a span nobody has loaded: its saved report is not this clear's to take
    saved['other'] = _cache_path('.sr_grid_cache.json', str(tmp_path / 'other_span'))
    with open(saved['other'], 'w', encoding='utf-8') as fh:
        json.dump({'manifest': manifests['sr'], '_dirs': ['x', 'y']}, fh)
    at.run()
    assert not at.exception, at.exception
    return at, saved


RESULT_KEYS = {'sr': ('sr_result', 'sr_dirs'), 'uni': ('uni_result',),
               'ss': ('ss_result',)}


def _open(at, which):
    go_tab(at, PAGE[which])
    assert not at.exception, at.exception
    return at


def _on_screen(at, which):
    return all(k in at.session_state for k in RESULT_KEYS[which])


# ── the button ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('which', ['sr', 'uni', 'ss'])
def test_a_report_page_offers_clear_report_with_its_report(hub, which):
    at, _ = hub
    _open(at, which)
    assert _on_screen(at, which)
    assert [b.label for b in at.main.button].count('Clear Report') == 1


@pytest.mark.parametrize('which', ['sr', 'uni', 'ss'])
def test_no_report_no_button(which, tmp_path, monkeypatch):
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    at = run_streamlit(default_timeout=180).run()
    trace_box(at, 'a').input(A).run()
    trace_box(at, 'b').input(B).run()
    at.session_state['uni_folder_input'] = A
    at.run()
    _open(at, which)
    assert not _has(at, 'Clear Report')


def test_the_viewer_has_no_clear_report(hub):
    at, _ = hub
    go_tab(at, 'Viewer')
    assert not _has(at, 'Clear Report')


# ── the pop-up ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('which', ['sr', 'uni', 'ss'])
def test_clear_report_asks_with_three_answers(hub, which):
    at, saved = hub
    _open(at, which)
    _btn(at, 'Clear Report').click().run()
    assert not at.exception, at.exception
    dialog = at.get('dialog')
    assert len(dialog) == 1
    assert [b.label for b in dialog[0].button] == ANSWERS
    said = ' '.join(m.value for m in dialog[0].markdown)
    assert 'Traces tab' in said and 'fresh run' in said
    # asking takes nothing
    assert _on_screen(at, which) and os.path.exists(saved[which])
    assert trace_box_value(at, 'a') == A


@pytest.mark.parametrize('which', ['sr', 'uni', 'ss'])
def test_skip_changes_nothing(hub, which):
    at, saved = hub
    _open(at, which)
    _btn(at, 'Clear Report').click().run()
    _btn(at, 'Skip').click().run()
    assert not at.exception, at.exception
    assert not _has(at, 'Skip')                                # pop-up closed
    for w in ('sr', 'uni', 'ss'):
        assert _on_screen(at, w), w
        assert os.path.exists(saved[w]), w
    assert _boxes(at) == (A, B)
    assert _has(at, 'Clear Report')


@pytest.mark.parametrize('which', ['sr', 'uni', 'ss'])
def test_clear_report_only_takes_this_report_and_leaves_the_traces(hub, which):
    tv = import_trace_server()
    at, saved = hub
    _open(at, which)
    _btn(at, 'Clear Report').click().run()
    _btn(at, 'Clear Report Only').click().run()
    assert not at.exception, at.exception
    assert not _has(at, 'Skip')
    # this report: off the screen, and its saved copy with it
    assert not any(k in at.session_state for k in RESULT_KEYS[which])
    assert not os.path.exists(saved[which])
    assert not _has(at, 'Clear Report')
    # the other tools' reports are not this button's to take
    for w in {'sr', 'uni', 'ss'} - {which}:
        assert _on_screen(at, w), w
        assert os.path.exists(saved[w]), w
    assert os.path.exists(saved['other'])
    # the traces stay, on the Traces tab and in every tool
    assert _boxes(at) == (A, B)
    assert at.session_state['uni_folder_input'] == A
    assert at.session_state['ss_folder_input']
    go_tab(at, 'Viewer')
    assert tv.CONFIG['dir_a'] == A and tv.CONFIG['dir_b'] == B


@pytest.mark.parametrize('which', ['sr', 'uni', 'ss'])
def test_a_cleared_report_does_not_come_back(hub, which):
    """The page restores a report from its saved copy whenever it has none.
    That is what made a cleared report reappear."""
    at, _ = hub
    _open(at, which)
    _btn(at, 'Clear Report').click().run()
    _btn(at, 'Clear Report Only').click().run()
    go_tab(at, 'Viewer')
    _open(at, which)
    assert not any(k in at.session_state for k in RESULT_KEYS[which])
    assert not _has(at, 'Clear Report')


@pytest.mark.parametrize('which', ['sr', 'uni'])
def test_the_viewer_stops_judging_by_a_cleared_report(hub, which):
    tv = import_trace_server()
    at, _ = hub
    _open(at, which)
    assert tv.CONFIG.get('thresholds')            # the report set its gates
    _btn(at, 'Clear Report').click().run()
    _btn(at, 'Clear Report Only').click().run()
    assert tv.CONFIG.get('thresholds') is None
    assert tv.CONFIG.get('end_refl') is None
    assert tv.CONFIG.get('panel_span') is None


@pytest.mark.parametrize('which', ['sr', 'uni', 'ss'])
def test_clear_report_and_traces_empties_the_traces_tab_and_every_tool(hub, which):
    tv = import_trace_server()
    at, saved = hub
    go_tab(at, 'Viewer')
    assert tv.CONFIG['dir_a'] == A
    _open(at, which)
    _btn(at, 'Clear Report').click().run()
    _btn(at, 'Clear Report and Traces').click().run()
    assert not at.exception, at.exception
    assert not _has(at, 'Skip')
    assert _boxes(at) == ('', '')
    for k in ('uni_folder_input', 'ss_folder_input'):
        assert at.session_state[k] == '', k
    for w in ('sr', 'uni', 'ss'):
        assert not any(k in at.session_state for k in RESULT_KEYS[w]), w
        assert not os.path.exists(saved[w]), w
    assert os.path.exists(saved['other'])
    assert not tv.CONFIG['dir_a'] and not tv.CONFIG['dir_b']
    assert '_clear_traces_go' not in at.session_state
    # the site names read out of the cleared traces go back to A and B
    go_tab(at, 'Splice Report')
    sites = {t.label: t.value for t in at.main.text_input if 'ILA' in t.label}
    assert sites == {'A-Direction ILA / Site': 'A', 'B-Direction ILA / Site': 'B'}
    for page in PAGE.values():
        go_tab(at, page)
        assert not at.exception, (page, at.exception)
        assert not _has(at, 'Clear Report')


# ── the Traces tab's Clear Traces takes the saved copies too ──────────────

def test_clear_traces_allow_forgets_the_saved_reports(hub):
    at, saved = hub
    _press_clear_traces(at)
    said = ' '.join(m.value for m in at.get('dialog')[0].markdown)
    assert 'fresh run' in said
    _btn(at, 'Allow').click().run()
    assert not at.exception, at.exception
    for w in ('sr', 'uni', 'ss'):
        assert not os.path.exists(saved[w]), w
    assert os.path.exists(saved['other'])


def test_clear_traces_cancel_keeps_the_saved_reports(hub):
    at, saved = hub
    _press_clear_traces(at)
    _btn(at, 'Cancel').click().run()
    for w in ('sr', 'uni', 'ss'):
        assert os.path.exists(saved[w]), w
        assert _on_screen(at, w), w


def test_the_same_folder_needs_a_fresh_run_after_a_clear(hub):
    at, saved = hub
    _press_clear_traces(at)
    _btn(at, 'Allow').click().run()
    trace_box(at, 'a').input(A).run()
    trace_box(at, 'b').input(B).run()
    at.session_state['uni_folder_input'] = A
    at.run()
    for which in ('sr', 'uni', 'ss'):
        _open(at, which)
        assert not any(k in at.session_state for k in RESULT_KEYS[which]), which
        assert not _has(at, 'Clear Report'), which


def test_the_traces_and_the_saved_report_files_are_never_touched(hub, manifests):
    at, _ = hub
    before = {d: sorted(os.listdir(d)) for d in (A, B)}
    xlsx = manifests['sr'].get('xlsx')
    assert xlsx and os.path.exists(xlsx)
    _open(at, 'sr')
    _btn(at, 'Clear Report').click().run()
    _btn(at, 'Clear Report and Traces').click().run()
    assert {d: sorted(os.listdir(d)) for d in (A, B)} == before
    assert os.path.exists(xlsx)
