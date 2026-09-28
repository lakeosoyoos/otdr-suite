"""With traces loaded in the left panel, the tools draw no loader of their own
(Robert 2026-09-28): one place to load traces, not one per tool.

    Splice Report     runs on the panel's A and B
    Unidirectional    runs on the panel's A, or its B (the tech says which)
    Secret Sauce      runs on both, as the one folder the sidebar builds

With the panel empty each tool loads its own traces, as before.

A click into the Viewer tab starts a new session and points the A box at the
folder the Viewer must read.  The tech's own A and B ride the link and come
back when the tech leaves the Viewer.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conftest import (run_streamlit, run_secretsauce, import_trace_server,
                      SPLICEREPORT_DIR, FIXTURE_SPLICE_A_DIR,
                      FIXTURE_SPLICE_B_DIR)

A, B = str(FIXTURE_SPLICE_A_DIR), str(FIXTURE_SPLICE_B_DIR)
N_A, N_B = len(os.listdir(A)), len(os.listdir(B))


def _box(at, label):
    return next(t for t in at.sidebar.text_input if t.label == label)


def _hub(a='', b='', tmp_path=None, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    at = run_streamlit(default_timeout=180).run()
    if a:
        _box(at, 'A folder').input(a).run()
    if b:
        _box(at, 'B folder').input(b).run()
    assert not at.exception, at.exception
    return at


def _open(at, page):
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, at.exception
    return at


def _main(at):
    """What the page itself drew: (radio labels, button labels, text-input
    labels, uploader labels, captions)."""
    return {
        'radio': [r.label for r in at.main.radio],
        'button': [b.label for b in at.main.button],
        'text': [t.label for t in at.main.text_input],
        'upload': [u.label for u in at.main.get('file_uploader')],
        'caption': ' '.join(c.value for c in at.main.caption),
    }


def _is_loader(label):
    return ('drag & drop' in label or 'drop the span' in label
            or 'Browse for folder' in label or 'paste a folder' in label
            or label in ('A folder', 'B folder', 'Folder (both directions)',
                         '📁 Folder with BOTH directions'))


def _loaders(at):
    m = _main(at)
    return [l for k in ('button', 'text', 'upload') for l in m[k] if _is_loader(l)]


# ── the panel empty: every tool loads its own ─────────────────────────────

@pytest.fixture(autouse=True)
def _empty_trace_server():
    """The trace server's folders are process-wide and seed the boxes of a
    new session: start every test from an empty Suite."""
    tv = import_trace_server()
    tv.set_dirs(None, None)
    yield
    tv.set_dirs(None, None)


def test_with_the_panel_empty_the_splice_report_offers_its_own_loader():
    at = _open(_hub(), 'Splice Report')
    assert 'Select Traces' in _main(at)['radio']


@pytest.mark.parametrize('page', ['Unidirectional', 'Secret Sauce'])
def test_with_the_panel_empty_the_tool_offers_its_own_loader(page):
    at = _open(_hub(), page)
    found = _loaders(at)
    assert any('Browse for folder' in l for l in found)
    assert any('paste a folder' in l for l in found)
    assert any('drag & drop' in l for l in found)


# ── the panel loaded: no loader in the tools ──────────────────────────────

@pytest.mark.parametrize('page', ['Splice Report', 'Unidirectional', 'Secret Sauce'])
@pytest.mark.parametrize('a,b', [(A, B), (A, ''), ('', B)])
def test_with_the_panel_loaded_no_tool_draws_a_loader(page, a, b):
    at = _open(_hub(a, b), page)
    assert _loaders(at) == []
    assert 'Select Traces' not in _main(at)['radio']
    assert 'left panel' in _main(at)['caption']


def test_the_splice_report_runs_on_the_panels_pair():
    at = _open(_hub(A, B), 'Splice Report')
    sites = {t.label: t.value for t in at.main.text_input if 'ILA' in t.label}
    assert sites == {'A-direction ILA / site': 'ELMDALE',
                     'B-direction ILA / site': 'MILLER'}
    gen = next(b for b in at.main.button if b.label.startswith('Generate'))
    assert not gen.disabled
    # the tech's own report to compare against is not a trace loader: it stays
    assert any('compare against' in l for l in _main(at)['upload'])
    # so does chaining another span on
    assert '➕ Add span…' in _main(at)['button']


@pytest.mark.parametrize('a,b,missing', [(A, '', 'B'), ('', B, 'A')])
def test_the_splice_report_says_which_direction_is_missing(a, b, missing):
    at = _open(_hub(a, b), 'Splice Report')
    assert f'Load the {missing} folder there too' in _main(at)['caption']
    assert not [b_ for b_ in at.main.button if b_.label.startswith('Generate')
                and not b_.disabled]


@pytest.fixture
def dest(tmp_path, monkeypatch):
    """Where the runs below save: never the tech's Downloads folder, which is
    where a page saves when its 'Save reports to' box is left empty."""
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    out = tmp_path / 'saved reports'
    out.mkdir()
    return str(out)


def _run(at, button, dest_key, result_key, dest):
    """Press the page's Run button with the report saved under `dest`, and
    return the folder the report says it ran on."""
    assert os.path.isdir(dest) and 'Downloads' not in dest
    at.session_state[dest_key] = dest
    at.run()
    next(b for b in at.main.button if b.label == button).click().run()
    for _ in range(3):                       # the page reruns itself to finish
        if result_key in at.session_state:
            break
        at.run()
    assert not at.exception, at.exception
    return at.session_state[result_key]['_folder']


def _uni_folder_of_the_run(at, dest):
    return _run(at, 'Run unidirectional report', 'uni_report_dest', 'uni_result', dest)


def test_unidirectional_runs_on_a_and_offers_b(dest):
    at = _open(_hub(A, B), 'Unidirectional')
    side = next(r for r in at.main.radio if r.label == 'Run On')
    assert list(side.options) == ['A folder', 'B folder'] and side.value == 'A folder'
    assert _uni_folder_of_the_run(at, dest) == A


def test_unidirectional_on_b_stays_on_b_across_a_trip_to_another_tool(dest):
    at = _open(_hub(A, B), 'Unidirectional')
    next(r for r in at.main.radio if r.label == 'Run On').set_value('B folder').run()
    _open(at, 'Viewer')
    _open(at, 'Unidirectional')
    assert next(r for r in at.main.radio if r.label == 'Run On').value == 'B folder'
    assert _uni_folder_of_the_run(at, dest) == B


@pytest.mark.parametrize('a,b,want', [(A, '', A), ('', B, B)])
def test_unidirectional_with_one_folder_loaded_runs_on_it(a, b, want, dest):
    at = _open(_hub(a, b), 'Unidirectional')
    assert 'Run On' not in _main(at)['radio']
    assert _uni_folder_of_the_run(at, dest) == want


def _ss_folder_of_the_run(at, dest):
    return _run(at, 'Run analysis', 'ss_report_dest', 'ss_result', dest)


def test_secret_sauce_runs_on_both_directions_as_one_folder(dest):
    at = _open(_hub(A, B), 'Secret Sauce')
    folder = _ss_folder_of_the_run(at, dest)
    assert folder not in (A, B)
    assert len(os.listdir(folder)) == N_A + N_B


@pytest.mark.parametrize('a,b,want', [(A, '', A), ('', B, B)])
def test_secret_sauce_with_one_folder_loaded_runs_on_it(a, b, want, dest):
    at = _open(_hub(a, b), 'Secret Sauce')
    assert _ss_folder_of_the_run(at, dest) == want


def test_a_folder_that_does_not_exist_is_not_a_loaded_panel(tmp_path):
    at = _open(_hub(str(tmp_path / 'nowhere')), 'Unidirectional')
    assert any('Browse for folder' in l for l in _loaders(at))


# ── Clear Traces brings the loaders back ──────────────────────────────────

@pytest.mark.parametrize('page', ['Unidirectional', 'Secret Sauce'])
def test_clear_traces_brings_the_tools_loader_back(page):
    at = _open(_hub(A, B), page)
    assert _loaders(at) == []
    next(b for b in at.sidebar.button if b.label == 'Clear Traces').click().run()
    next(b for b in at.button if b.label == 'Allow').click().run()
    assert not at.exception, at.exception
    assert any('Browse for folder' in l for l in _loaders(at))


def test_clear_traces_brings_the_splice_reports_loader_back():
    at = _open(_hub(A, B), 'Splice Report')
    next(b for b in at.sidebar.button if b.label == 'Clear Traces').click().run()
    next(b for b in at.button if b.label == 'Allow').click().run()
    assert 'Select Traces' in _main(at)['radio']


# ── the way back from the Viewer tab ──────────────────────────────────────

def _click(params):
    """A click on a report link: a URL navigation, so a NEW session."""
    at = run_streamlit(default_timeout=180)
    for k, v in params.items():
        at.query_params[k] = v
    at.run()
    assert not at.exception, at.exception
    return at


def _back(at, to):
    next(b for b in at.button if b.label == f'← Back to {to}').click().run()
    assert not at.exception, at.exception
    return at


def test_every_link_into_the_viewer_tab_carries_the_panels_folders():
    from conftest import APP_PATH
    src = APP_PATH.read_text(encoding='utf-8')
    assert src.count('ssfolder={ssq}') == 2
    for line in src.splitlines():
        if 'ssfolder={ssq}' in line:
            assert '_panel_qs()' in line or '_panel_qs()' in src[src.index(line):][:200]
    assert '_dirs_qs += _panel_qs()' in src
    assert 'src=uni{_uni_pq}' in src and '_uni_pq = _panel_qs()' in src


def test_a_pair_click_gives_the_panel_back_with_the_report(tmp_path, monkeypatch):
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    at = _open(_hub(A, B), 'Secret Sauce')
    ran_on = at.session_state['_ss_panel_folder']
    rc, man, err = run_secretsauce(ran_on, tmp_path, 'pairs')
    assert rc == 0 and man and man.get('ok'), err[-800:]
    # saved the way the page saves it
    import test_clear_report as tcr
    man['_folder'] = os.path.abspath(ran_on)
    with open(tcr._cache_path('pairs_cache.json', man['_folder']), 'w',
              encoding='utf-8') as fh:
        json.dump(man, fh)

    seen = _click({'nav': 'viewer', 'fibers': '1,2', 'dir': 'a',
                   'ssfolder': ran_on, 'pa': A, 'pb': B})
    # in the Viewer the A box is the folder the pair is read from...
    assert _box(seen, 'A folder').value == ran_on
    _back(seen, 'Secret Sauce')
    # ...and on the way back the tech's own folders return, with the report
    assert _box(seen, 'A folder').value == A and _box(seen, 'B folder').value == B
    assert 'ss_pairs_result' in seen.session_state
    assert any(b.label == 'Clear Report' for b in seen.button)
    assert _loaders(seen) == []


def test_a_unidirectional_click_on_b_gives_the_panel_back_with_the_report(
        tmp_path, monkeypatch):
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    p = subprocess.run(
        [sys.executable, str(SPLICEREPORT_DIR / 'run_splicereport.py'), '--uni',
         '--dir-a', B, '--out', str(tmp_path / 'uni.xlsx'), '--analysis', 'suite'],
        capture_output=True, text=True)
    man = next((json.loads(l) for l in reversed(p.stdout.strip().splitlines())
                if l.strip().startswith('{')), None)
    assert p.returncode == 0 and man and man.get('ok'), p.stderr[-800:]
    import test_clear_report as tcr
    man['_folder'] = B
    with open(tcr._cache_path('uni_result_cache.json', B), 'w', encoding='utf-8') as fh:
        json.dump(man, fh)

    seen = _click({'nav': 'viewer', 'fiber': '3', 'km': '1.0', 'dir': 'a',
                   'sra': B, 'src': 'uni', 'pa': A, 'pb': B})
    assert _box(seen, 'A folder').value == B          # what the Viewer reads
    _back(seen, 'Unidirectional')
    assert _box(seen, 'A folder').value == A and _box(seen, 'B folder').value == B
    assert next(r for r in seen.main.radio if r.label == 'Run On').value == 'B folder'
    assert (seen.session_state['uni_result'] or {}).get('_folder') == B
    assert any(b.label == 'Clear Report' for b in seen.button)


def test_leaving_the_viewer_by_the_tool_list_gives_the_panel_back_too():
    seen = _click({'nav': 'viewer', 'fiber': '3', 'km': '1.0', 'dir': 'a',
                   'sra': B, 'src': 'uni', 'pa': A, 'pb': B})
    _open(seen, 'Splice Report')
    assert _box(seen, 'A folder').value == A and _box(seen, 'B folder').value == B
    assert '_panel_restore' not in seen.session_state


def test_a_link_from_an_empty_panel_gives_an_empty_panel_back(tmp_path):
    own = str(tmp_path / 'own')
    os.makedirs(own)
    seen = _click({'nav': 'viewer', 'fibers': '1,2', 'dir': 'a',
                   'ssfolder': own, 'pa': '', 'pb': ''})
    assert _box(seen, 'A folder').value == own
    _back(seen, 'Secret Sauce')
    assert _box(seen, 'A folder').value == '' and _box(seen, 'B folder').value == ''
    # the tool is back on its own loader, on the folder the tech gave it
    assert any('paste a folder' in l for l in _loaders(seen))
    assert seen.session_state['ss_folder_input'] == own
