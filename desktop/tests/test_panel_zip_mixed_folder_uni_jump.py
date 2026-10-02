"""The Traces tab's A and B boxes, read the same way by every tool, and the
way into the Viewer tab from a Unidirectional cell (click-through audit on
main b4b30bb, a 24-fibre bidirectional span, 2026-09-29).

1. A .zip in each box: the Viewer read 24 + 24 fibers, but the Splice Report
   said "Pick both an A and a B folder" with no Generate button and the
   Unidirectional page ignored it.  Its own "…or Paste a Folder Path" box
   ignored a pasted .zip too, with nothing said.
2. One folder holding BOTH directions in the A box: the Viewer listed every
   B file under the A file's key (and opened the A file for either), and the
   Splice Report named both directions after one site.  A drop on the Viewer
   already split such a folder; the boxes now split it the same way.
3. "This tab" on a Unidirectional cell: the Viewer graded at the Splice
   Report's one-direction gate (no &src=uni in its URL) and the B box was
   empty while in the Viewer.
4. "Cell clicks open in" and "Save reports to" came back as the defaults
   after "← Back", and a rerun of the same span wrote over the report before
   it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile

import pytest

from conftest import (run_streamlit, finish_engine_run, import_trace_server,
                      go_tab, trace_box, trace_box_value, SPLICEREPORT_DIR, FIXTURE_SPLICE_A_DIR,
                      FIXTURE_SPLICE_B_DIR)

A, B = str(FIXTURE_SPLICE_A_DIR), str(FIXTURE_SPLICE_B_DIR)
N_A = len([f for f in os.listdir(A) if f.lower().endswith('.sor')])
N_B = len([f for f in os.listdir(B) if f.lower().endswith('.sor')])
_SITES = {}


def _plain_folder_sites():
    """The site names the Splice Report shows for the fixture's A and B
    folders as plain folders: what every other way of loading them must
    show too.  Read off the page once, so no site name is written here."""
    if not _SITES:
        at = _open(_hub(A, B), 'Splice Report')
        _SITES.update(_sites(at))
        assert set(_SITES) == {'A-Direction ILA / Site', 'B-Direction ILA / Site'}
        assert len(set(_SITES.values())) == 2 and 'A' not in _SITES.values()
    return dict(_SITES)


# ── helpers ───────────────────────────────────────────────────────────────

def _box(at, label):
    """The Traces tab's box (goes to the Traces tab first)."""
    return trace_box(at, label[0])


def _held(at, label):
    """What a Traces tab box holds, read without leaving the page."""
    return trace_box_value(at, label[0])


def _counts(at):
    """The Traces tab's fiber counts (goes to the Traces tab first)."""
    if at.session_state['nav_radio'] != 'Traces':
        go_tab(at, 'Traces')
    return [c.value for c in at.caption
            if c.value[:3] in ('A: ', 'B: ') and c.value.endswith(' fibers')]


def _hub(a='', b=''):
    at = run_streamlit(default_timeout=180).run()
    if a:
        _box(at, 'A Folder').input(a).run()
    if b:
        _box(at, 'B Folder').input(b).run()
    assert not at.exception, at.exception
    return at


def _open(at, page):
    go_tab(at, page)
    assert not at.exception, at.exception
    return at


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


def _texts(els):
    return ' '.join(str(e.value) for e in els)


def _sites(at):
    return {t.label: t.value for t in at.main.text_input if 'ILA' in t.label}


def _generate(at):
    return next((b for b in at.main.button if b.label.startswith('Generate')), None)


def _viewer_url(at):
    return next(e.proto.src for e in at.get('iframe')
                if '127.0.0.1' in (e.proto.src or '') and 'host=' not in e.proto.src)


def _zip(folder, dest):
    with zipfile.ZipFile(dest, 'w') as z:
        for f in sorted(os.listdir(folder)):
            if f.lower().endswith('.sor'):
                z.write(os.path.join(folder, f), f)
    return str(dest)


@pytest.fixture
def zips(tmp_path):
    return (_zip(A, tmp_path / 'ELMMIL.zip'), _zip(B, tmp_path / 'MILELM.zip'))


@pytest.fixture
def mixed(tmp_path):
    """One folder holding both directions, as a tech pastes it."""
    d = tmp_path / 'both directions'
    d.mkdir()
    for src in (A, B):
        for f in os.listdir(src):
            if f.lower().endswith('.sor'):
                shutil.copy2(os.path.join(src, f), d / f)
    return str(d)


@pytest.fixture
def dest(tmp_path, monkeypatch):
    """Where the runs below save: never the tech's Downloads folder."""
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    out = tmp_path / 'saved reports'
    out.mkdir()
    return str(out)


def _server():
    tv = import_trace_server()
    return tv.CONFIG.get('dir_a'), tv.CONFIG.get('dir_b')


def _sor_names(folder):
    return sorted(f for f in os.listdir(folder) if f.lower().endswith('.sor'))


# ── 1. a .zip in the Traces tab, and in the Unidirectional page's own box ──

def test_the_splice_report_runs_on_a_zip_in_each_box(zips):
    want = _plain_folder_sites()
    at = _open(_hub(*zips), 'Splice Report')
    assert 'Pick **both**' not in _texts(at.main.info)
    assert _sites(at) == want
    assert _generate(at) is not None and not _generate(at).disabled
    assert 'Traces tab' in _texts(at.main.caption)


def test_the_viewer_and_the_splice_report_read_the_zips_the_same(zips):
    at = _hub(*zips)
    assert _counts(at) == [f'A: {N_A} fibers', f'B: {N_B} fibers']
    _open(at, 'Viewer')
    served = _server()
    tv = import_trace_server()
    assert len(tv.list_fibers(served[0])) == N_A and len(tv.list_fibers(served[1])) == N_B
    _open(at, 'Splice Report')
    assert _generate(at) is not None
    # the report runs on the very folders the Viewer was given
    assert tuple(at.session_state['sr_site_src'][:2]) == served


def test_unidirectional_runs_on_a_zip_in_the_panel(zips, dest):
    at = _open(_hub(zips[0]), 'Unidirectional')
    assert 'Choose the folder' not in _texts(at.main.info)
    assert not [t for t in at.main.text_input if 'Paste a Folder' in t.label]
    at.session_state['uni_report_dest'] = dest
    at.run()
    next(b for b in at.main.button if b.label == 'Run Unidirectional Report').click().run()
    finish_engine_run(at, 'uni')
    assert not at.exception, at.exception
    ran_on = at.session_state['uni_result']['_folder']
    assert _sor_names(ran_on) == _sor_names(A)


def test_unidirectional_reads_a_zip_pasted_in_its_own_box(zips):
    at = _open(_hub(), 'Unidirectional')
    next(t for t in at.main.text_input if 'Paste a Folder' in t.label).input(zips[0]).run()
    assert not at.exception, at.exception
    assert 'Choose the folder' not in _texts(at.main.info)
    assert any(b.label == 'Run Unidirectional Report' for b in at.main.button)
    assert '.zip' in _texts(at.main.caption)


def test_unidirectional_says_so_when_the_pasted_path_is_not_there(tmp_path):
    at = _open(_hub(), 'Unidirectional')
    gone = str(tmp_path / 'nowhere.zip')
    next(t for t in at.main.text_input if 'Paste a Folder' in t.label).input(gone).run()
    assert 'Not found' in _texts(at.main.warning)


def test_the_splice_reports_one_folder_box_takes_a_zip_of_both_directions(tmp_path):
    both = tmp_path / 'both.zip'
    with zipfile.ZipFile(both, 'w') as z:
        for src in (A, B):
            for f in _sor_names(src):
                z.write(os.path.join(src, f), f)
    want = _plain_folder_sites()
    at = _open(_hub(), 'Splice Report')
    next(r for r in at.main.radio if r.label == 'Select Traces').set_value(
        'One folder / zip (both directions)').run()
    next(t for t in at.main.text_input if t.label == 'Folder (Both Directions)').input(
        str(both)).run()
    assert not at.exception, at.exception
    assert 'Auto-split by direction' in _texts(at.main.caption)
    assert _sites(at) == want
    assert _generate(at) is not None


# ── 2. one folder holding both directions, pasted into one box ────────────

@pytest.mark.parametrize('box', ['A', 'B'])
def test_a_folder_with_both_directions_is_split_like_a_drop(mixed, box):
    at = _open(_hub(*((mixed, '') if box == 'A' else ('', mixed))), 'Viewer')
    dir_a, dir_b = _server()
    assert _sor_names(dir_a) == _sor_names(A)
    assert _sor_names(dir_b) == _sor_names(B)
    # the Viewer serves each fibre once per direction: no a-1 twice
    tv = import_trace_server()
    assert len(tv.list_fibers(dir_a)) == N_A and len(tv.list_fibers(dir_b)) == N_B
    # ...and the Traces tab says so
    assert _counts(at) == [f'A: {N_A} fibers', f'B: {N_B} fibers']
    assert 'holds both directions' in _texts(at.caption)


def test_the_splice_report_names_both_sites_of_a_split_folder(mixed):
    want = _plain_folder_sites()
    at = _open(_hub(mixed), 'Splice Report')
    assert _sites(at) == want
    assert _generate(at) is not None and not _generate(at).disabled


def test_the_split_matches_what_a_drop_of_the_same_files_does(mixed):
    tv = import_trace_server()
    paths = [os.path.join(mixed, f) for f in _sor_names(mixed)]
    split = tv.split_directions(paths)
    got = {side: sorted(os.path.basename(p) for p in files)
           for side, (_k, files) in zip(split['sides'], split['keep'])}
    assert got == {'A': _sor_names(A), 'B': _sor_names(B)}


def test_both_boxes_full_leaves_the_folders_alone_and_says_why(mixed):
    at = _open(_hub(mixed, B), 'Viewer')
    assert _server() == (mixed, B)
    assert 'holds both directions' in _texts(at.main.warning)


def test_a_secret_sauce_pair_folder_is_not_split(mixed):
    seen = _click({'nav': 'viewer', 'fibers': '1,2', 'dir': 'a',
                   'ssfolder': mixed, 'pa': '', 'pb': ''})
    assert _held(seen, 'A Folder') == mixed
    assert _server()[0] == mixed


# ── 3. "This tab" from a Unidirectional cell ──────────────────────────────

@pytest.mark.parametrize('side', ['a', 'b'])
def test_a_uni_cell_in_this_tab_keeps_both_folders_and_the_uni_gate(side):
    seen = _click({'nav': 'viewer', 'fiber': '3', 'km': '1.0', 'dir': 'a',
                   'sra': A if side == 'a' else B, 'src': 'uni',
                   'pa': A, 'pb': B, 'pside': side})
    url = _viewer_url(seen)
    assert '&src=uni' in url
    assert f'&dir={side}' in url
    assert _held(seen, 'A Folder') == A and _held(seen, 'B Folder') == B
    # the Viewer has both directions while the tech is in it
    tv = import_trace_server()
    dir_a, dir_b = _server()
    assert len(tv.list_fibers(dir_a)) == N_A and len(tv.list_fibers(dir_b)) == N_B
    _back(seen, 'Unidirectional')
    assert _held(seen, 'A Folder') == A and _held(seen, 'B Folder') == B
    assert next(r for r in seen.main.radio if r.label == 'Run On').value == (
        'A folder' if side == 'a' else 'B folder')


def test_a_splice_report_cell_in_this_tab_names_its_report():
    seen = _click({'nav': 'viewer', 'fiber': '3', 'km': '1.0', 'dir': 'both',
                   'sra': A, 'srb': B, 'src': 'sr', 'pa': A, 'pb': B})
    assert '&src=sr' in _viewer_url(seen)


def test_the_uni_grid_link_says_which_panel_folder_it_ran_on(dest):
    at = _open(_hub(A, B), 'Unidirectional')
    next(r for r in at.main.radio if r.label == 'Run On').set_value('B folder').run()
    at.session_state['uni_report_dest'] = dest
    at.run()
    next(b for b in at.main.button if b.label == 'Run Unidirectional Report').click().run()
    finish_engine_run(at, 'uni')
    next(r for r in at.main.radio if r.label == 'Cell Clicks Open In').set_value(
        'This tab (Viewer page)').run()
    grid = _texts(at.main.markdown)
    assert '?nav=viewer&fiber=' in grid, 'no flagged cell to click in the grid'
    assert '&src=uni' in grid and '&pside=b' in grid


# ── 4. the page's choices come back after ← Back; no silent overwrite ─────

def _uni_report_on_a(tmp_path, monkeypatch):
    """A saved Unidirectional report on the Traces tab's A folder, as the page saves
    it, so the page draws its grid (and the click choice) without a run."""
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    p = subprocess.run(
        [sys.executable, str(SPLICEREPORT_DIR / 'run_splicereport.py'), '--uni',
         '--dir-a', A, '--out', str(tmp_path / 'uni.xlsx'), '--analysis', 'suite'],
        capture_output=True, text=True)
    man = next((json.loads(l) for l in reversed(p.stdout.strip().splitlines())
                if l.strip().startswith('{')), None)
    assert p.returncode == 0 and man and man.get('ok'), p.stderr[-800:]
    import test_clear_report as tcr
    man['_folder'] = A
    with open(tcr._cache_path('uni_result_cache.json', A), 'w', encoding='utf-8') as fh:
        json.dump(man, fh)


def _click_choice(at):
    return next(r for r in at.main.radio if r.label == 'Cell Clicks Open In').value


def test_the_click_choice_survives_a_trip_to_another_tool(tmp_path, monkeypatch):
    _uni_report_on_a(tmp_path, monkeypatch)
    at = _open(_hub(A), 'Unidirectional')
    next(r for r in at.main.radio if r.label == 'Cell Clicks Open In').set_value(
        'This tab (Viewer page)').run()
    _open(at, 'Viewer')
    _open(at, 'Unidirectional')
    assert _click_choice(at) == 'This tab (Viewer page)'


def test_the_click_choice_and_save_folder_ride_a_this_tab_click(tmp_path, monkeypatch):
    _uni_report_on_a(tmp_path, monkeypatch)
    at = _open(_hub(A), 'Unidirectional')
    next(r for r in at.main.radio if r.label == 'Cell Clicks Open In').set_value(
        'This tab (Viewer page)').run()
    keep = str(tmp_path / 'my reports')
    next(t for t in at.main.text_input if t.label == 'Save Reports To').input(keep).run()
    assert not at.exception, at.exception
    seen = _click({'nav': 'viewer', 'fiber': '3', 'km': '1.0', 'dir': 'a',
                   'sra': A, 'src': 'uni', 'pa': A, 'pb': '', 'pside': 'a',
                   'cs': at.session_state['_carry_id']})
    _back(seen, 'Unidirectional')
    assert _click_choice(seen) == 'This tab (Viewer page)'
    assert next(t for t in seen.main.text_input
                if t.label == 'Save Reports To').value == keep


def test_a_second_splice_report_does_not_overwrite_the_first(dest):
    at = _open(_hub(A, B), 'Splice Report')
    site_a, site_b = (_sites(at)[k] for k in ('A-Direction ILA / Site',
                                              'B-Direction ILA / Site'))
    name = f'{site_a}_to_{site_b}_SpliceReport'
    first = os.path.join(dest, name + '.xlsx')
    with open(first, 'wb') as fh:
        fh.write(b'the report before')
    at.session_state['sr_report_dest'] = dest
    at.run()
    _generate(at).click().run()
    finish_engine_run(at, 'sr')
    assert not at.exception, at.exception
    with open(first, 'rb') as fh:
        assert fh.read() == b'the report before'
    second = os.path.join(dest, name + ' (2).xlsx')
    assert os.path.isfile(second)
    assert at.session_state['sr_result'].get('out', second) == second


def test_a_second_unidirectional_report_does_not_overwrite_the_first(dest):
    first = os.path.join(dest, 'unidirectional_events.xlsx')
    with open(first, 'wb') as fh:
        fh.write(b'the report before')
    at = _open(_hub(A), 'Unidirectional')
    at.session_state['uni_report_dest'] = dest
    at.run()
    next(b for b in at.main.button if b.label == 'Run Unidirectional Report').click().run()
    finish_engine_run(at, 'uni')
    assert not at.exception, at.exception
    with open(first, 'rb') as fh:
        assert fh.read() == b'the report before'
    assert os.path.isfile(os.path.join(dest, 'unidirectional_events (2).xlsx'))
