"""A report made before a Viewer Remove says so (2026-10-02 click audit).

After a Splice Report ran, the tech removed a fiber in the Viewer (right-click,
Remove This Fiber) and came back.  The page said the 2 removed files "are left
out of this report" over the OLD report, which still had the fiber flagged in
its grid, and the fiber's cell opened a blank Viewer under the hub's "Jumped
to fiber ...".  Now

  hub     every report records the files removed in the Viewer when its run
          starts (`_viewer_removed`); the note compares that with the files
          removed now: the same, the note as before; different, it says the
          report was made before the change and to run it again
  grid    a cell of a fiber removed in the Viewer keeps its value with no
          link, and its hover says "Removed in the Viewer"
  Viewer  a report link or a pop-out jump to fibers whose every file was
          removed says so on the status line instead of loading nothing

The page runs in JavaScriptCore where present.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from conftest import APP_PATH, VIEWER_DIR, import_trace_server, run_streamlit
from conftest import COPY_HELPERS_JS  # noqa: E402

TS = import_trace_server()
APP = APP_PATH.read_text(encoding='utf-8')
SRC = (VIEWER_DIR / 'viewer.html').read_text(encoding='utf-8')
JSC = ('/System/Library/Frameworks/JavaScriptCore.framework/'
       'Versions/Current/Helpers/jsc')
needs_jsc = pytest.mark.skipif(not os.path.exists(JSC),
                               reason='JavaScriptCore shell (macOS) not present')


def _py_fn(name):
    body = APP.split(f'\ndef {name}(', 1)[1].split('\ndef ', 1)[0]
    ns = {}
    exec(f'def {name}(' + body, ns)
    return ns[name]


# ── the note's words ───────────────────────────────────────────────────────
def test_same_set_keeps_the_note_as_before():
    text = _py_fn('_removed_note_text')
    two = [['F.A.0005.sor'], ['F.B.0005.sor']]
    kind, words = text(two, [['F.A.0005.sor'], ['F.B.0005.sor']])
    assert kind == 'caption'
    assert words.startswith('2 files removed in the Viewer are left out of this report.')
    assert text([[], []], [[], []]) is None
    assert text([[], []], None) is None
    # No report on screen: the note speaks for the next run, as before.
    assert text(two, None)[0] == 'caption'


def test_a_remove_after_the_run_says_the_report_is_out_of_date():
    text = _py_fn('_removed_note_text')
    kind, words = text([['F.A.0005.sor'], ['F.B.0005.sor']], [[], []],
                       again='Generate the report again')
    assert kind == 'warning'
    assert words == ('This report was made before 2 files were removed in the '
                     'Viewer, so it still has them. Generate the report again '
                     'to leave them out.')
    kind, words = text([[], []], [['F.A.0005.sor'], []])
    assert kind == 'warning' and words.startswith('This report leaves out 1 file that was put back')
    kind, words = text([['F.A.0007.sor'], []], [['F.A.0005.sor'], []])
    assert kind == 'warning' and 'changed after this report was made' in words
    for _k, w in (text([['x'], ['y']], [[], []]), text([[], []], [['x'], []])):
        assert '—' not in w and '–' not in w


def test_every_report_records_its_removed_set_when_its_run_starts():
    assert "'removed': _removed_now(_da, _db)})" in APP
    assert "manifest['_viewer_removed'] = _run.get('removed')" in APP
    assert "st.session_state['uni_run_removed'] = _removed_now(folder)" in APP
    assert "manifest['_viewer_removed'] = st.session_state.pop('uni_run_removed', None)" in APP
    assert "st.session_state['ss_run_removed'] = _removed_now(_pa, _pb)" in APP
    assert "manifest['_viewer_removed'] = st.session_state.pop('ss_run_removed', None)" in APP
    assert APP.count('_viewer_removed_report_note(') == 5   # def + SR, Uni, SS x2


def test_a_removed_fibers_cell_keeps_its_value_without_a_link():
    cell = _py_fn('_cell_markup')
    for popout in (True, False):
        html = cell(popout, 354, 44.1, 'both', '#e74c3c', 'Splice', 'F354 0.116',
                    href='?nav=viewer&fiber=354', gone=True)
        assert 'F354 0.116' in html and "title='Removed in the Viewer'" in html
        assert 'href' not in html and "class='vc'" not in html
    assert "class='vc'" in cell(True, 354, 44.1, 'both', '#000', '', 'F354', href='')
    assert APP.count('gone=c[\'fiber\'] in _gone') == 1
    assert APP.count('gone=c[\'fiber\'] in _uni_gone') == 1


def test_removed_keys_only_for_the_folders_loaded_now(monkeypatch):
    monkeypatch.setitem(TS.CONFIG, 'dir_a', '/span/A')
    monkeypatch.setitem(TS.CONFIG, 'dir_b', '/span/B')
    try:
        TS.set_viewer_state({'keys': [], 'removed': ['a-5', 'b-5', 'a-7']})
        assert TS.removed_keys() == {'a-5', 'b-5', 'a-7'}
        monkeypatch.setitem(TS.CONFIG, 'dir_b', '/span/other')
        assert TS.removed_keys() == set()
    finally:
        TS.set_viewer_state({'keys': [], 'removed': []})


# ── the Splice Report page, end to end ────────────────────────────────────
def _span(tmp_path):
    a, b = tmp_path / 'A Side', tmp_path / 'B Side'
    a.mkdir()
    b.mkdir()
    for n in range(1, 13):
        (a / f'SPAN.A.1550.{n:04d}.sor').write_bytes(b'a')
        (b / f'SPAN.B.1550.{n:04d}.sor').write_bytes(b'b')
    return str(a), str(b)


def _manifest(ran):
    cells = [{'fiber': f, 'splice': 0, 'km': 5.0, 'loss': loss, 'category': 'reburn',
              'label': 'Splice'} for f, loss in ((5, 0.116), (7, 0.120))]
    return {'ok': True, 'site_a': 'A', 'site_b': 'B', 'n_fibers': 12, 'max_fiber': 12,
            'n_splices': 1, 'span_km': 10.0, 'n_flagged': 2, 'ribbon_size': 12,
            'ribbons': [0], 'columns': [{'kind': 'splice', 'num': 1, 'km': 5.0}],
            'cells': cells, '_viewer_removed': ran}


def _sr_page(tmp_path, ran):
    a, b = _span(tmp_path)
    at = run_streamlit().run()
    at.session_state['view_dir_a_input'] = a
    at.session_state['view_dir_b_input'] = b
    at.session_state['sr_click_target_saved'] = 'This tab (Viewer page)'
    at.sidebar.radio[0].set_value('Splice Report').run()
    assert not at.exception, list(at.exception)
    # Fiber 5 removed in the Viewer (both directions), on the folders loaded.
    TS.set_dirs(a, b)
    TS.set_viewer_state({'keys': [], 'removed': ['a-5', 'b-5']})
    at.session_state['sr_result'] = _manifest(ran)
    at.session_state['sr_dirs'] = (a, b)
    at.run()
    assert not at.exception, list(at.exception)
    return at


def _grid(at):
    return next(m.value for m in at.markdown if 'F7 0.120' in m.value)


@pytest.fixture
def _forget_state():
    yield
    TS.set_viewer_state({'keys': [], 'removed': []})


def test_report_run_with_the_same_removed_set_keeps_the_note(tmp_path, _forget_state):
    at = _sr_page(tmp_path, [['SPAN.A.1550.0005.sor'], ['SPAN.B.1550.0005.sor']])
    notes = [c.value for c in at.caption if 'removed in the Viewer' in c.value]
    assert notes == ['2 files removed in the Viewer are left out of this report. '
                     'Put them back from the Viewer’s Files list (right-click).']
    assert not [w.value for w in at.warning if 'removed in the Viewer' in w.value]


def test_report_run_before_the_remove_says_it_is_out_of_date(tmp_path, _forget_state):
    at = _sr_page(tmp_path, [[], []])
    assert not [c.value for c in at.caption if 'left out of this report' in c.value]
    stale = [w.value for w in at.warning if 'removed in the Viewer' in w.value]
    assert stale == ['This report was made before 2 files were removed in the Viewer, '
                     'so it still has them. Generate the report again to leave them out.']
    grid = _grid(at)
    # F5's value stays, with no link; F7 still jumps to the Viewer.
    assert ("<span title='Removed in the Viewer' style='color:#e74c3c;font-weight:600;"
            "opacity:0.5'>F5 0.116</span>") in grid
    assert 'fiber=5&' not in grid and 'fiber=7&' in grid


# ── the Viewer: a link to a removed fiber says so ─────────────────────────
def _js_func(name):
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', SRC)
    assert m, 'viewer.html no longer defines %s()' % name
    i, depth = m.end(), 1
    while i < len(SRC) and depth:
        depth += {'{': 1, '}': -1}.get(SRC[i], 0)
        i += 1
    return SRC[m.start():i]


_STUBS = r"""
var gInfo = { dir_a: '/A', dir_b: '/B', fibers_a: [], fibers_b: [] };
for (var f = 1; f <= 432; f++) { gInfo.fibers_a.push(f); gInfo.fibers_b.push(f); }
var gRemovedFiles = new Set(), gTraces = [], gFecMode = false;
var gSourceReport = null, gUniSide = null, said = [], loaded = [], cleared = 0, box = '';
var document = { getElementById: function () { return { set value(v) { box = v; }, get value() { return box; } }; } };
function setReadout(t, extra) { said.push([t, (extra && extra.warn) || '']); }
function clearAll() { cleared++; }
function setAddDir(d) {}
async function addFibers() { loaded.push(box); }
function syncGateUI() {} function renderBackButton() {}
function zoomToKm() {} function linkDispKm(km) { return km; }
async function autoloadDefault() {}
function parseFibers(s) { return String(s).split(',').map(function (x) { return +x; }); }
"""

_CASES = r"""
(async function () {
  var out = {};
  gRemovedFiles.add('a-354'); gRemovedFiles.add('b-354');
  await applyTarget({ fiber: '354', km: '44.1', dir: 'both', src: 'sr', replace: true });
  out.both = [said.slice(), loaded.slice(), cleared];
  said = []; loaded = [];
  await applyTarget({ fiber: '354', dir: 'a', src: 'uni' });
  out.one_dir = said.slice();
  said = []; loaded = [];
  gRemovedFiles.add('a-410'); gRemovedFiles.add('a-418');
  await applyTarget({ fibers: '410,418', dir: 'a' });
  out.pair = [said.slice(), loaded.slice()];
  said = []; loaded = [];
  await applyTarget({ fiber: '20', dir: 'both' });           // not removed
  out.kept = [said.slice(), loaded.slice()];
  said = []; loaded = [];
  gRemovedFiles.delete('b-354');                             // B still there
  await applyTarget({ fiber: '354', dir: 'both' });
  out.half = [said.slice(), loaded.slice()];
  print('OUT ' + JSON.stringify(out));
})().catch(function (e) { print('ERR ' + e + '\n' + e.stack); });
"""


@pytest.fixture(scope='module')
def res(tmp_path_factory):
    funcs = '\n'.join(_js_func(n) for n in ('applyTarget', 'planFiberLoad', 'absentNote',
                                            'planOrSay', 'sayRemovedTarget'))
    path = tmp_path_factory.mktemp('removed_target') / 'rt.js'
    path.write_text(_STUBS + COPY_HELPERS_JS + funcs + '\n' + _CASES, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


@needs_jsc
def test_a_link_to_a_removed_fiber_says_it_was_removed(res):
    said, loaded, cleared = res['both']
    assert said == [['', 'F354 was removed from the Viewer. Right-click a file and '
                         'choose Put Back to see it again.']]
    assert loaded == [] and cleared == 0          # the chart stays as it was
    assert res['one_dir'] == said


@needs_jsc
def test_a_pair_link_to_removed_fibers_says_so_too(res):
    said, loaded = res['pair']
    assert said == [['', 'F410, F418 were removed from the Viewer. Right-click a file '
                         'and choose Put Back to see them again.']]
    assert loaded == []


@needs_jsc
def test_a_fiber_with_a_file_left_still_loads(res):
    assert res['kept'] == [[], ['20']]
    assert res['half'] == [[], ['354']]
