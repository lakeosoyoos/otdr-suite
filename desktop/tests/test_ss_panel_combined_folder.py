"""Secret Sauce on the Traces tab's A and B folders: the ONE folder the hub
builds from the two (app._panel_ss_folder), every trace of both, flat.

It follows the traces on disk.  The folder used to be reused on the pair of
folders, their file count and the newest file time, and a name already in it
was never placed again.  A trace swapped for a different file of the same
name and size with an OLDER time (what an Explorer zip extraction leaves)
changed none of those, so Duplicate Check went on running on the old trace,
in the same session and in a new one, and the report saved for the old
traces came back as if it were for the new ones.

Every B trace goes in.  A B file named like an A file (F0001_1550.sor in
both, as with names that carry no site code) used to be left out, so the run
covered one direction for those fibres.  It now goes in as B_<name>, and the
page says so.  The prefix leaves every fibre number where it was.
"""
from __future__ import annotations

import json
import os
import shutil

import pytest

from conftest import (run_streamlit, run_secretsauce, finish_engine_run,
                      import_trace_server, go_tab, trace_box, clear_traces, FIXTURE_A_DIR, FIXTURE_B_DIR)


def _span(tmp_path, same_names):
    """An A and a B folder of four traces each.  With `same_names` both use
    F0001_1550.sor ... F0004_1550.sor; without, each has a name of its own."""
    a, b = tmp_path / 'A', tmp_path / 'B'
    a.mkdir()
    b.mkdir()
    for src, dst, own in ((FIXTURE_A_DIR, a, 'AAABBB'), (FIXTURE_B_DIR, b, 'BBBAAA')):
        for i, f in enumerate(sorted(os.listdir(src)), 1):
            name = f'F{i:04d}_1550.sor' if same_names else f'{own}{i:04d}_1550.sor'
            shutil.copy2(src / f, dst / name)
    return str(a), str(b)


def _read(path):
    with open(path, 'rb') as fh:
        return fh.read()


def _swap(path):
    """Put a different trace at `path`: same name, same size, stamped a month
    older than the one it replaces, in a new file (as an Explorer zip
    extraction leaves it).  Returns the new bytes."""
    data = bytearray(_read(path))
    old = os.stat(path)
    mid = len(data) // 2                    # trace samples, not the header
    for k in range(mid, mid + 64):
        data[k] ^= 0x5A
    os.remove(path)
    with open(path, 'wb') as fh:
        fh.write(data)
    t = old.st_mtime - 30 * 86400
    os.utime(path, (t, t))
    return bytes(data)


def _box(at, label):
    """The Traces tab's box (goes to the Traces tab first)."""
    return trace_box(at, label[0])


def _hub(a, b, tmp_path, monkeypatch):
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    at = run_streamlit(default_timeout=180).run()
    _box(at, 'A Folder').input(a).run()
    _box(at, 'B Folder').input(b).run()
    go_tab(at, 'Secret Sauce')
    assert not at.exception, at.exception
    return at


def _caption(at):
    return ' '.join(c.value for c in at.main.caption)


def _save_pairs_report(folder, tmp_path):
    """Run Secret Sauce on `folder` and save the report the way the page
    does, so a new session on the same folder brings it back."""
    import test_clear_report as tcr
    rc, man, err = run_secretsauce(folder, tmp_path / 'out', 'pairs')
    assert rc == 0 and man and man.get('ok'), err[-800:]
    man['_folder'] = os.path.abspath(folder)
    with open(tcr._cache_path('pairs_cache.json', man['_folder']), 'w',
              encoding='utf-8') as fh:
        json.dump(man, fh)
    return man


# ── it follows the traces on disk ─────────────────────────────────────────

def test_a_swapped_trace_reaches_the_folder_in_the_same_session(tmp_path, monkeypatch):
    a, b = _span(tmp_path, same_names=False)
    at = _hub(a, b, tmp_path, monkeypatch)
    before = at.session_state['_ss_panel_folder']
    new = _swap(os.path.join(a, 'AAABBB0001_1550.sor'))
    at.run()
    assert not at.exception, at.exception
    now = at.session_state['_ss_panel_folder']
    assert now != before, 'the page kept the folder built from the old trace'
    assert _read(os.path.join(now, 'AAABBB0001_1550.sor')) == new
    assert len(os.listdir(now)) == 8


def test_a_swapped_trace_reaches_the_folder_in_a_new_session(tmp_path, monkeypatch):
    a, b = _span(tmp_path, same_names=False)
    before = _hub(a, b, tmp_path, monkeypatch).session_state['_ss_panel_folder']
    new = _swap(os.path.join(a, 'AAABBB0001_1550.sor'))
    now = _hub(a, b, tmp_path, monkeypatch).session_state['_ss_panel_folder']
    assert now != before
    assert _read(os.path.join(now, 'AAABBB0001_1550.sor')) == new
    # ...and a new folder, not the old one topped up: nothing else in it
    assert sorted(os.listdir(now)) == sorted(os.listdir(a) + os.listdir(b))


def test_the_same_traces_give_the_same_folder(tmp_path, monkeypatch):
    """A report is saved under the folder it ran on: untouched traces must
    give the same folder in every session, or the report is lost on every
    trip to the Viewer tab."""
    a, b = _span(tmp_path, same_names=False)
    one = _hub(a, b, tmp_path, monkeypatch).session_state['_ss_panel_folder']
    two = _hub(a, b, tmp_path, monkeypatch).session_state['_ss_panel_folder']
    assert one == two


def test_a_report_for_the_old_traces_is_not_brought_back(tmp_path, monkeypatch):
    a, b = _span(tmp_path, same_names=False)
    folder = _hub(a, b, tmp_path, monkeypatch).session_state['_ss_panel_folder']
    _save_pairs_report(folder, tmp_path)
    # the same traces: the saved report comes back
    assert 'ss_pairs_result' in _hub(a, b, tmp_path, monkeypatch).session_state
    _swap(os.path.join(a, 'AAABBB0001_1550.sor'))
    # a trace changed: that report was not run on these traces
    again = _hub(a, b, tmp_path, monkeypatch)
    assert 'ss_pairs_result' not in again.session_state
    assert not any(b_.label == 'Clear Report' for b_ in again.button)


def test_a_run_after_a_swap_runs_on_the_new_trace(tmp_path, monkeypatch):
    a, b = _span(tmp_path, same_names=False)
    at = _hub(a, b, tmp_path, monkeypatch)
    new = _swap(os.path.join(a, 'AAABBB0001_1550.sor'))
    out = tmp_path / 'saved reports'
    out.mkdir()
    at.session_state['ss_report_dest'] = str(out)
    at.run()
    next(b_ for b_ in at.main.button if b_.label == 'Run Analysis').click().run()
    finish_engine_run(at, 'ss')
    assert not at.exception, at.exception
    ran_on = at.session_state['ss_result']['_folder']
    assert _read(os.path.join(ran_on, 'AAABBB0001_1550.sor')) == new


def test_clear_traces_then_the_same_span_needs_a_fresh_run(tmp_path, monkeypatch):
    """Clear Traces forgets the report saved for the folder built from the
    Traces tab, so loading the same span again does not bring it back."""
    a, b = _span(tmp_path, same_names=False)
    at = _hub(a, b, tmp_path, monkeypatch)
    _save_pairs_report(at.session_state['_ss_panel_folder'], tmp_path)
    at.run()
    assert 'ss_pairs_result' in at.session_state
    clear_traces(at, allow=True)
    assert not at.exception, at.exception
    _box(at, 'A Folder').input(a).run()
    _box(at, 'B Folder').input(b).run()
    go_tab(at, 'Secret Sauce')          # the page that would bring it back
    assert not at.exception, at.exception
    assert 'ss_pairs_result' not in at.session_state


# ── every B trace goes in ─────────────────────────────────────────────────

def test_a_b_file_named_like_an_a_file_goes_in_under_its_own_name(tmp_path, monkeypatch):
    a, b = _span(tmp_path, same_names=True)
    at = _hub(a, b, tmp_path, monkeypatch)
    folder = at.session_state['_ss_panel_folder']
    names = [f'F{i:04d}_1550.sor' for i in range(1, 5)]
    assert sorted(os.listdir(folder)) == sorted(names + ['B_' + n for n in names])
    for n in names:
        assert _read(os.path.join(folder, n)) == _read(os.path.join(a, n))
        assert _read(os.path.join(folder, 'B_' + n)) == _read(os.path.join(b, n))
    cap = _caption(at)
    assert 'F0001_1550.sor' in cap and 'B_F0001_1550.sor' in cap, cap


def test_names_of_their_own_are_left_alone(tmp_path, monkeypatch):
    a, b = _span(tmp_path, same_names=False)
    at = _hub(a, b, tmp_path, monkeypatch)
    folder = at.session_state['_ss_panel_folder']
    assert sorted(os.listdir(folder)) == sorted(os.listdir(a) + os.listdir(b))
    assert 'B_' not in _caption(at)


def test_a_name_that_differs_only_in_case_is_the_same_name(tmp_path, monkeypatch):
    """Windows does not tell F0001_1550.sor from f0001_1550.SOR: the second
    would land on the first."""
    a, b = _span(tmp_path, same_names=True)
    os.rename(os.path.join(b, 'F0001_1550.sor'), os.path.join(b, 'f0001_1550.SOR'))
    folder = _hub(a, b, tmp_path, monkeypatch).session_state['_ss_panel_folder']
    assert 'B_f0001_1550.SOR' in os.listdir(folder)
    assert len(os.listdir(folder)) == 8


def test_secret_sauce_compares_both_directions_of_every_fibre(tmp_path, monkeypatch):
    a, b = _span(tmp_path, same_names=True)
    folder = _hub(a, b, tmp_path, monkeypatch).session_state['_ss_panel_folder']
    rc, man, err = run_secretsauce(folder, tmp_path / 'out', 'pairs')
    assert rc == 0 and man and man.get('ok'), err[-800:]
    assert man['n_files'] == 8
    # the B copies keep their fibre numbers
    for p in man['pairs']:
        for side in ('A', 'B'):
            stem = p['file' + side]
            assert p['fiber' + side] == int(stem.split('_')[-2][-4:]), p


@pytest.mark.parametrize('name,fibre', [
    ('F0001_1550.sor', 1), ('AAABBB0001.sor', 1), ('0012.XYZ1.1550.sor', 12),
    ('AAA1AAA50007withstartstop.sor', 7), ('Aaa to Bbb d.0431.sor', 431),
    ('TEST0001_155016251310.trc', 1), ('AAA1AAA60145.sor', 145)])
def test_the_viewer_reads_the_same_fibre_from_a_b_name(name, fibre):
    """A pair click opens the Viewer on this folder, which finds a fibre by
    the number in its file name."""
    ts = import_trace_server()
    assert ts.extract_fiber_num(name) == fibre
    assert ts.extract_fiber_num('B_' + name) == fibre
    assert ts.extract_fiber_num('B2_' + name) == fibre


# ── folder_intake: the names and the build ────────────────────────────────

def _touch(path, data=b'x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(data)
    return path


def test_combined_names_give_every_b_file_a_name_of_its_own(tmp_path):
    import folder_intake as fi
    a = [_touch(str(tmp_path / 'A' / n)) for n in ('F0001_1550.sor', 'F0002_1550.sor')]
    b = [_touch(str(tmp_path / 'B' / n)) for n in
         ('B_F0001_1550.sor', 'F0001_1550.sor', 'F0003_1550.sor', 'f0002_1550.SOR')]
    placed, renamed = fi.combined_names(sorted(a), sorted(b))
    names = [n for _p, n in placed]
    assert len(names) == 6 and len({n.lower() for n in names}) == 6
    assert dict(renamed) == {'F0001_1550.sor': 'B2_F0001_1550.sor',
                             'f0002_1550.SOR': 'B_f0002_1550.SOR'}
    # a B file with a name of its own keeps it, the B_ one it already had too
    assert 'F0003_1550.sor' in names and 'B_F0001_1550.sor' in names


def test_a_name_repeated_inside_one_folder_keeps_its_first_copy(tmp_path):
    """As before: the combined folder takes one file per name from each
    folder.  Only a B file meeting an A name is new."""
    import folder_intake as fi
    a = [_touch(str(tmp_path / 'A' / 'F0001_1550.sor')),
         _touch(str(tmp_path / 'A' / 'old' / 'F0001_1550.sor'))]
    placed, renamed = fi.combined_names(sorted(a), [])
    assert placed == [(a[0], 'F0001_1550.sor')] and renamed == []


def test_an_existing_combined_folder_is_used_as_it_is(tmp_path):
    import folder_intake as fi
    src = _touch(str(tmp_path / 'A' / 'F0001_1550.sor'))
    dest = str(tmp_path / 'all')
    assert fi.materialize_combined([(src, 'F0001_1550.sor')], dest) == dest
    other = _touch(str(tmp_path / 'A' / 'F0002_1550.sor'))
    fi.materialize_combined([(src, 'F0001_1550.sor'), (other, 'F0002_1550.sor')], dest)
    assert os.listdir(dest) == ['F0001_1550.sor'], 'topped up an older build'
    assert sorted(os.listdir(tmp_path)) == ['A', 'all'], 'left a part-built folder'


def test_a_build_that_fails_leaves_nothing_behind(tmp_path):
    import folder_intake as fi
    src = _touch(str(tmp_path / 'A' / 'F0001_1550.sor'))
    gone = str(tmp_path / 'A' / 'F0002_1550.sor')
    dest = str(tmp_path / 'all')
    with pytest.raises(OSError):
        fi.materialize_combined([(src, 'F0001_1550.sor'), (gone, 'F0002_1550.sor')], dest)
    assert sorted(os.listdir(tmp_path)) == ['A']
