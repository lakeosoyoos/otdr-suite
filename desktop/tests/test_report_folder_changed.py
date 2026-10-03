"""A report whose folders changed after it ran says so (2026-10-02 click audit).

A Splice Report ran on A and B folders of 24 fibers each.  The tech then
copied fibers 25-240 into the SAME two folders.  The left panel said "A: 240
fibers · B: 240 fibers", but the Splice Report page kept the 24-fiber report
and grid with nothing said, and a new session brought the 24-fiber report back
from its saved copy: the copy is keyed on the folder paths only, and the
session's result goes only when a box changes.

Now every run records what its folders hold (`_trace_files`: each folder's
trace files by name, size and time, and its fiber count), on the result and
so in the saved copy too.  The page compares that with the folders now: the
same, the page is as it was; changed, a line over the report says so and to
run it again.  A saved copy from before the record has none: nothing is said.
"""
from __future__ import annotations

import json
import os
import shutil

import pytest

from conftest import (APP_PATH, run_streamlit, finish_engine_run, go_tab, load_traces,
                      import_trace_server, FIXTURE_SPLICE_A_DIR,
                      FIXTURE_SPLICE_B_DIR)

TS = import_trace_server()
APP = APP_PATH.read_text(encoding='utf-8')


class _St:
    def __init__(self):
        self.said = []

    def warning(self, text):
        self.said.append(text)


def _helpers(**extra):
    """The record and note helpers out of app.py, on their own."""
    start = APP.index('\n_TRACE_SIG_EXTS = ')
    end = APP.index('\ndef _take_panel_ss_folder(')
    caches = {}
    body = APP.split('\ndef _remember(', 1)[1].split('\n\n\n', 1)[0]
    ns = {'os': os, 'trace_server': TS, '_rerun_caches': lambda: caches,
          'st': _St(), '_RERUN_CACHE_KEPT': 50}
    exec('def _remember(' + body, ns)
    exec(APP[start:end], ns)
    ns.update(extra)                  # stand-ins replace the real helpers
    return ns


def _span(root, fibers, sides=('A', 'B')):
    out = []
    for side, src in zip(sides, (FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)):
        d = root / side
        d.mkdir(exist_ok=True)
        for p in sorted(src.glob('*.sor')):
            if int(p.name[6:10]) in fibers:
                shutil.copy(p, d / p.name)
        out.append(str(d))
    return out


def _add(folder, fibers, like):
    """Copy fiber `like`'s shot in as each of `fibers` (a tech copying in the
    rest of the span)."""
    src = next(p for p in sorted(os.listdir(folder)) if int(p[6:10]) == like)
    for f in fibers:
        shutil.copy(os.path.join(folder, src),
                    os.path.join(folder, f'{src[:6]}{f:04d}{src[10:]}'))


# ── the signature ──────────────────────────────────────────────────────────
def test_the_signature_moves_when_files_are_added_removed_or_replaced(tmp_path):
    ns = _helpers()
    sig = ns['_trace_files_sig']
    a, _b = _span(tmp_path, set(range(1, 13)))
    first = sig(a)
    assert first[1] == 12
    assert sig(a) == first                       # nothing changed, same answer
    (tmp_path / 'A' / 'notes.txt').write_text('not a trace', encoding='utf-8')
    assert sig(a) == first                       # only trace files count
    _add(a, range(13, 17), like=1)
    grown = sig(a)
    assert grown[0] != first[0] and grown[1] == 16
    os.remove(os.path.join(a, sorted(n for n in os.listdir(a) if n.endswith('.sor'))[-1]))
    assert sig(a)[1] == 15 and sig(a)[0] not in (first[0], grown[0])
    # A shot replaced by another of the same name (written beside it and
    # moved over it, as a copy or a sync does).
    before = sig(a)
    name = next(n for n in sorted(os.listdir(a)) if n.endswith('.sor'))
    tmp = os.path.join(a, 'swap.tmp')
    shutil.copy(os.path.join(str(FIXTURE_SPLICE_A_DIR), sorted(
        n for n in os.listdir(FIXTURE_SPLICE_A_DIR) if n.endswith('.sor'))[-1]), tmp)
    os.replace(tmp, os.path.join(a, name))
    after = sig(a)
    assert after[0] != before[0] and after[1] == before[1]
    assert sig(str(tmp_path / 'gone')) is None


def test_on_windows_a_shot_copied_over_in_place_is_seen(tmp_path, monkeypatch):
    """The listing carries each file's size and time there, so a file written
    over in place, which leaves the folder's time alone, moves it too."""
    ns = _helpers()
    a, _b = _span(tmp_path, set(range(1, 5)))
    first = ns['_trace_files_sig'](a)
    target = os.path.join(a, sorted(os.listdir(a))[0])
    stamp = os.stat(a).st_mtime_ns
    with open(target, 'ab') as fh:
        fh.write(b'\0' * 64)
    os.utime(a, ns=(stamp, stamp))              # the folder's time as it was
    monkeypatch.setattr(os, 'name', 'nt')
    assert ns['_trace_files_sig'](a)[0] != first[0]


# ── the line's words ───────────────────────────────────────────────────────
def _text(rec, now, again='Generate the report again'):
    ns = _helpers(_trace_files_sig=lambda f: now.get(f))
    ns['os'] = type('os', (), {'path': type('p', (), {'isdir': staticmethod(lambda f: True)})})
    return ns['_folders_changed_text'](rec, again)


def test_the_audit_case_both_folders_grew():
    rec = [['/span/A', 'A', 'a1', 24], ['/span/B', 'B', 'b1', 24]]
    words = _text(rec, {'/span/A': ('a2', 240), '/span/B': ('b2', 240)})
    assert words == ('The A and B folders changed since this report was made '
                     '(24 fibers then, 240 now). Generate the report again to '
                     'include them.')


def test_one_folder_or_unequal_counts_or_a_swap():
    rec = [['/span/A', 'A', 'a1', 24], ['/span/B', 'B', 'b1', 24]]
    assert _text(rec, {'/span/A': ('a2', 240), '/span/B': ('b1', 24)}) == (
        'The A folder changed since this report was made (24 fibers then, '
        '240 now). Generate the report again to include them.')
    assert _text(rec, {'/span/A': ('a2', 240), '/span/B': ('b2', 120)}) == (
        'The A and B folders changed since this report was made (A: 24 fibers '
        'then, 240 now; B: 24 fibers then, 120 now). Generate the report '
        'again to include them.')
    assert _text(rec, {'/span/A': ('a2', 24), '/span/B': ('b1', 24)}) == (
        'The A folder changed since this report was made. Generate the '
        'report again to read the files there now.')
    assert _text(rec, {'/span/A': ('a2', 12), '/span/B': ('b2', 12)},
                 again='Run the analysis again') == (
        'The A and B folders changed since this report was made (24 fibers '
        'then, 12 now). Run the analysis again to read the files there now.')
    assert _text([['/f', None, 's1', 1]], {'/f': ('s2', 2)},
                 again='Run the report again') == (
        'The folder changed since this report was made (1 fiber then, 2 now). '
        'Run the report again to include them.')
    for w in (_text(rec, {'/span/A': ('a2', 240), '/span/B': ('b2', 120)}),
              _text(rec, {'/span/A': ('a2', 24), '/span/B': ('b1', 24)})):
        assert '—' not in w and '–' not in w


def test_nothing_is_said_when_nothing_changed_or_nothing_was_recorded():
    rec = [['/span/A', 'A', 'a1', 24], ['/span/B', 'B', 'b1', 24]]
    assert _text(rec, {'/span/A': ('a1', 24), '/span/B': ('b1', 24)}) is None
    assert _text(None, {}) is None                 # a report from before the record
    assert _text([], {}) is None
    assert _text([['/span/A', 'A']], {'/span/A': ('a2', 3)}) is None
    assert _text(rec, {}) is None                  # a folder that cannot be read now


def test_every_report_records_its_folders_when_its_run_starts():
    assert "'files': _trace_files_record((_da, 'A', _raws[0])," in APP
    assert "manifest['_trace_files'] = _run.get('files')" in APP
    for page in ('uni', 'ss', 'fec'):
        assert f"st.session_state['{page}_run_files'] = " in APP
        assert (f"manifest['_trace_files'] = st.session_state.pop('{page}_run_files', None)"
                in APP)
    # SR, Uni, SS x2, FEC
    assert APP.count('    _folders_changed_note(') == 5


# ── the Splice Report page, end to end ────────────────────────────────────
@pytest.fixture
def _own_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setenv('OTDR_SETTINGS_DIR', str(tmp_path / 'settings'))


def _hub(a, b):
    at = run_streamlit(default_timeout=180).run()
    load_traces(at, a, b)                  # the top bar's Traces tab
    assert not at.exception, at.exception
    return at


def _stale(at):
    return [w.value for w in at.warning if 'changed since this report' in w.value]


def _summary(at):
    return next(s.value for s in at.success if '·' in s.value and 'fiber' in s.value)


def test_splice_report_on_folders_that_filled_up(tmp_path, _own_cache):
    a, b = _span(tmp_path, set(range(1, 13)))
    at = _hub(a, b)
    at.session_state['sr_report_dest'] = str(tmp_path)
    go_tab(at, 'Splice Report')
    next(x for x in at.main.button if x.label.startswith('Generate')).click().run()
    finish_engine_run(at, 'sr')
    assert not at.exception, at.exception
    assert '  ·  12 fibers  ·  ' in _summary(at)
    assert _stale(at) == []                        # the folders as the run read them
    rec = at.session_state['sr_result']['_trace_files']
    assert [(r[0], r[1], r[3]) for r in rec] == [(a, 'A', 12), (b, 'B', 12)]

    # The rest of the span copied into the same two folders.
    _add(a, range(13, 25), like=1)
    _add(b, range(13, 25), like=1)
    at.run()
    assert not at.exception, at.exception
    assert _stale(at) == ['The A and B folders changed since this report was made '
                          '(12 fibers then, 24 now). Generate the report again '
                          'to include them.']
    assert '  ·  12 fibers  ·  ' in _summary(at)   # the report itself is as it was

    # A new session brings the report back from its saved copy, and says so.
    fresh = _hub(a, b)
    go_tab(fresh, 'Splice Report')
    assert not fresh.exception, fresh.exception
    assert '  ·  12 fibers  ·  ' in _summary(fresh)
    assert len(_stale(fresh)) == 1

    # A saved copy from before the record: no line, the report as before.
    cache_dir = os.environ['OTDR_CACHE_DIR']
    path = next(os.path.join(cache_dir, n) for n in os.listdir(cache_dir)
                if n.endswith('sr_grid_cache.json'))
    with open(path, encoding='utf-8') as fh:
        saved = json.load(fh)
    assert saved['manifest']['_trace_files'] == rec
    saved['manifest'].pop('_trace_files')
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(saved, fh)
    old = _hub(a, b)
    go_tab(old, 'Splice Report')
    assert not old.exception, old.exception
    assert '  ·  12 fibers  ·  ' in _summary(old)
    assert _stale(old) == []
    with open(path, 'w', encoding='utf-8') as fh:     # the record back
        saved['manifest']['_trace_files'] = rec
        json.dump(saved, fh)

    # A cell click to a fiber the folders no longer hold does not take the
    # hub down (the Viewer says the fiber is not in the folder).
    for f in os.listdir(a):
        if int(f[6:10]) > 4:
            os.remove(os.path.join(a, f))
    view = run_streamlit(default_timeout=180)
    for k, v in {'nav': 'viewer', 'fiber': '9', 'km': '1.0', 'dir': 'both',
                 'sra': a, 'srb': b, 'src': 'sr', 'pa': a, 'pb': b}.items():
        view.query_params[k] = v
    view.run()
    assert not view.exception, view.exception
    view.run()
    assert not view.exception, view.exception
    next(x for x in view.button if x.label == '← Back to Splice Report').click().run()
    assert not view.exception, view.exception
    assert len(_stale(view)) == 1


# ── the other report pages, end to end ─────────────────────────────────────
def _run(at, label, prefix):
    next(x for x in at.main.button if x.label == label).click().run()
    finish_engine_run(at, prefix)
    assert not at.exception, at.exception
    at.run()
    assert not at.exception, at.exception
    return at


def test_unidirectional_says_so_too(tmp_path, _own_cache):
    a, b = _span(tmp_path, set(range(1, 13)))
    at = _hub(a, b)
    at.session_state['uni_report_dest'] = str(tmp_path)
    go_tab(at, 'Unidirectional')
    _run(at, 'Run Unidirectional Report', 'uni')
    assert [s.value for s in at.success if s.value.startswith('Done: 12 fibers')]
    assert _stale(at) == []
    _add(a, range(13, 25), like=1)
    at.run()
    assert not at.exception, at.exception
    assert _stale(at) == ['The A folder changed since this report was made (12 '
                          'fibers then, 24 now). Run the report again to include them.']
    fresh = _hub(a, b)                       # the saved copy, in a new session
    go_tab(fresh, 'Unidirectional')
    assert not fresh.exception, fresh.exception
    assert len(_stale(fresh)) == 1


def test_secret_sauce_says_so_too(tmp_path, _own_cache):
    a, b = _span(tmp_path, set(range(1, 13)))
    at = _hub(a, b)
    at.session_state['ss_report_dest'] = str(tmp_path / 'ss')
    go_tab(at, 'Secret Sauce')
    _run(at, 'Run Analysis', 'ss')
    assert at.session_state['ss_result'].get('ok')
    assert _stale(at) == []
    _add(b, range(13, 15), like=1)
    at.run()
    assert not at.exception, at.exception
    assert _stale(at) == ['The B folder changed since this report was made (12 '
                          'fibers then, 14 now). Run the analysis again to '
                          'include them.']


def test_splice_report_fec_says_so_too(tmp_path, _own_cache):
    a, b = _span(tmp_path, set(range(1, 5)))
    at = run_streamlit(default_timeout=180).run()
    at.session_state['fec_dir_a'] = a
    at.session_state['fec_dir_b'] = b
    at.session_state['fec_report_dest'] = str(tmp_path)
    go_tab(at, 'Splice Report FEC')
    assert not at.exception, at.exception
    _run(at, 'Run FEC Report', 'fec')
    assert at.session_state['fec_result'].get('ok')
    assert _stale(at) == []
    os.remove(os.path.join(a, sorted(os.listdir(a))[0]))
    at.run()
    assert _stale(at) == ['The A end folder changed since this report was made '
                          '(4 fibers then, 3 now). Run the report again to read '
                          'the files there now.']
