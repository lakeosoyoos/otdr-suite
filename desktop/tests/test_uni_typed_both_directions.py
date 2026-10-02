"""A folder typed into the Unidirectional page's own box that holds BOTH
directions offers the Run On choice, as an upload of both does (Robert
2026-10-01: "yes give typed folders the Run On choice").

Such a folder ran one direction and printed a red PARTIAL COVERAGE error for
the other, its files "NOT analysed".  Now the page splits it the way it
splits an upload (the files' own direction stamps say which side is A), A by
default, and runs only the side picked.  The split runs after the foreign-
file audit, so files from another job are still set aside and never offered
as a second direction, and a folder holding one direction runs as before.
"""
from __future__ import annotations

import os
import shutil

from conftest import FIXTURE_DIR
from test_panel_zip_mixed_folder_uni_jump import (A, B, _back, _click, _held, _hub,
                                                   _open, _server, _viewer_url)
from test_uni_own_folder_b_side import _done, _run, _this_tab_links, _uni_box
from test_uni_upload_both_directions import A_TO_B, B_TO_A, _errors, _run_on_radio


def _sors(folder):
    return sorted(f for f in os.listdir(folder) if f.lower().endswith('.sor'))


def _folder(tmp_path, *srcs, name='both directions'):
    d = tmp_path / name
    d.mkdir()
    for src in srcs:
        for f in _sors(src):
            shutil.copy2(os.path.join(src, f), d / f)
    return str(d)


def _typed(tmp_path, monkeypatch, folder):
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    at = _open(_hub(), 'Unidirectional')
    next(t for t in at.main.text_input if 'Paste a Folder' in t.label).input(folder).run()
    assert not at.exception, at.exception
    return at


def _no_coverage_error(at):
    errs = _errors(at)
    assert 'COVERAGE' not in errs and 'mixes' not in errs and 'NOT analysed' not in errs, errs


def test_a_typed_folder_of_both_directions_offers_the_choice_and_runs_b(tmp_path, monkeypatch):
    at = _typed(tmp_path, monkeypatch, _folder(tmp_path, A, B))
    side = _run_on_radio(at)
    assert side is not None, [r.label for r in at.main.radio]        # was no choice
    assert side.value == side.options[0]                             # A by default
    assert side.options[0].startswith(f'A Direction ({A_TO_B}, 24 files')
    assert side.options[1].startswith(f'B Direction ({B_TO_A}, 24 files')
    side.set_value(side.options[1]).run()
    _run(at, str(tmp_path))
    assert _sors(at.session_state['uni_result']['_folder']) == _sors(B)
    done = _done(at)
    assert done and f'direction {B_TO_A}' in done[0], done
    _no_coverage_error(at)


def test_a_typed_folder_of_both_directions_runs_a_by_default(tmp_path, monkeypatch):
    at = _typed(tmp_path, monkeypatch, _folder(tmp_path, A, B))
    _run(at, str(tmp_path))
    assert _sors(at.session_state['uni_result']['_folder']) == _sors(A)
    done = _done(at)
    assert done and f'direction {A_TO_B}' in done[0], done
    _no_coverage_error(at)                                           # was PARTIAL COVERAGE


def test_strays_from_another_job_are_flagged_and_never_offered(tmp_path, monkeypatch):
    folder = _folder(tmp_path, A, FIXTURE_DIR / 'frspan')
    at = _typed(tmp_path, monkeypatch, folder)
    assert _run_on_radio(at) is None
    assert 'EXCLUDED' in ' '.join(w.value for w in at.warning)


def test_a_typed_folder_of_one_direction_runs_as_before(tmp_path, monkeypatch):
    folder = _folder(tmp_path, B, name='one direction')
    at = _typed(tmp_path, monkeypatch, folder)
    assert _run_on_radio(at) is None
    _run(at, str(tmp_path))
    assert at.session_state['uni_result']['_folder'] == folder


def test_a_b_run_opens_the_viewer_on_b_and_back_keeps_the_typed_folder(tmp_path, monkeypatch):
    folder = _folder(tmp_path, A, B)
    at = _typed(tmp_path, monkeypatch, folder)
    side = _run_on_radio(at)
    side.set_value(side.options[1]).run()
    _run(at, str(tmp_path))
    ran = at.session_state['uni_result']['_folder']
    links = _this_tab_links(at)
    assert {q['dir'] for q in links} == {'b'}
    seen = _click(links[0])
    assert '&dir=b' in _viewer_url(seen)
    assert _server() == (None, ran)
    # The left panel's B box names the folder, not its temporary split (read
    # on the Viewer: a trip to the Traces tab puts the tech's own boxes back).
    assert _held(seen, 'A Folder') == ''
    assert _held(seen, 'B Folder') == 'both directions: B Direction (24 files)'
    _back(seen, 'Unidirectional')
    # The typed folder again, Run On still on B, and the report.
    assert _uni_box(seen) == folder
    assert 'otdr_panel_split' not in _uni_box(seen)
    assert _run_on_radio(seen).value == _run_on_radio(seen).options[1]
    assert _done(seen) and seen.session_state['uni_result']['_folder'] == ran
