"""The one-direction event grid prints what FastReporter prints (2026-10-02).

Robert: "drive FR and make sure we are handling FECs correctly in viewer ...
we want to make sure we are showing the right thing and have the right event
columns".  Checked cell by cell against FR 3 on twelve FEC shots from each
end of one cable.  What did not match, and is pinned here:

  * Sections.  FR prints each trace's section from its own event to its OWN
    next event, in the Section right after that event, wherever the next one
    sits; the grid only filled a section when the trace had events in both
    neighbour columns, so 11 of 12 fibers lost the section after the panel.
    The figures are FR's own Section record (Loss, Length) from EXFO's block.
  * Rounding.  The server rounded reflectance to 2 dp and slope to 3 dp, and
    the grid rounded again: -56.948 -> -56.95 -> -57.0 where FR prints -56.9.
  * Averages.  FR averages the figures it prints and rounds half away from
    zero: sections 0.170 and 0.783 average 0.477 (unrounded mean 0.47637).
  * Columns.  A column of one non-reflective kind is Loss alone; a column of
    mixed kinds names none and gives each row its own Type; an event is named
    by FR's own Type (a reflective end prints "Reflective").
  * FEC P/F.  FEC shots are graded by the FEC rule only, never by the
    Viewer's splice gate.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser

import pytest

from test_viewer_one_dir_grid_runs import JSC, SHIMS, _script, needs_jsc

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'viewer'))

import trace_server as TS      # noqa: E402

VIEWER = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()


# ─── the server: FR's section records, reflectance unrounded ─────────────

def test_load_trace_carries_fr_sections_and_full_reflectance(tmp_path):
    """A 5 km short shot (panel at 1.002 km, a splice 33 m behind it whose
    section FR stores as 0.000 dB)."""
    folder = os.path.join(HERE, 'fixtures', 'continuous')
    src = os.path.join(folder, sorted(f for f in os.listdir(folder) if f.endswith('.sor'))[0])
    shutil.copy(src, tmp_path / os.path.basename(src))
    fiber = int(re.search(r'(\d+)\.sor$', src).group(1))
    saved = TS.CONFIG.get('dir_a')
    TS.CONFIG['dir_a'] = str(tmp_path)
    try:
        t = TS.load_trace('a', fiber)
    finally:
        TS.CONFIG['dir_a'] = saved
        TS._LIST_CACHE.clear()
        TS._FRAME_CACHE.clear()
        TS._load_trace_cached.cache_clear()
    ev = t['events']
    assert [e['number'] for e in ev] == [1, 2, 3, 4]
    # not rounded on the way out: the grid rounds once, as FR does
    assert ev[1]['reflection'] == pytest.approx(-58.51362212443154, abs=1e-9)
    assert ev[1]['slope'] == pytest.approx(0.18438720013369905, abs=1e-12)
    # the section that STARTS at each event, as FR stores it
    assert ev[0]['sec_loss'] == pytest.approx(0.18476395150582428, abs=1e-12)
    assert ev[0]['sec_len_km'] == pytest.approx(1.0020432620694498, abs=1e-12)
    assert ev[1]['sec_loss'] == 0.0                       # FR prints 0.000
    assert ev[2]['sec_loss'] == pytest.approx(0.7519579155065487, abs=1e-12)
    assert ev[3]['sec_loss'] is None and ev[3]['sec_len_km'] is None   # the last event
    # FR's position, unrounded, for the column headings
    assert ev[1]['fr_pos_km'] == pytest.approx(1.0020432620694498, abs=1e-12)
    assert ev[1]['dist_km'] == 1.002


# ─── the grid, in JavaScriptCore ──────────────────────────────────────────

DRIVER = r"""
// Server-shaped events (see trace_server load_trace): FR Type 3 reflective
// (tot 0 = Launch Level), 2 non-reflective, 1 positive, 5 continuous fiber.
function E(n, km, type, loss, refl, secLoss, secLen) {
  return {number: n, dist_km: km, splice_loss: loss, reflection: refl || 0, slope: 0.19,
          type: type === 3 ? '1F9999LS' : (type === 5 ? '1O9999LS' : '0F9999LS'),
          is_reflective: type === 3 || type === 5, is_end: false,
          time_of_travel: km === 0 ? 0 : Math.round(km * 49000), fr_type: type, fr_status: 0,
          sec_loss: secLoss, sec_len_km: secLen, fr_pos_km: null};
}
function T(fiber, events) {
  var xs = [], ys = [];
  for (var i = 0; i <= 500; i++) { xs.push(i / 100); ys.push(-i / 500); }
  return {key: 'a-' + fiber, dir: 'a', src: 'a', fiber: fiber, visible: true,
          name: 'F' + fiber + '.sor', color: '#2f6fb3',
          data: {dist_km: xs, trace_db: ys, wavelength_nm: 1550, events: events}};
}
// F1: launch, panel, a splice 2.13 km on, continuous fiber.  F3: no splice.
// F8: a pigtail splice 49 m behind its panel, then a gainer.
var F1 = T(1, [E(1, 0, 3, null, -56.947826899396496, 0.18612075792458183, 1.0050440184226189),
               E(2, 1.005, 3, 0.142, -50.42392514929309, 0.38078621125291956, 2.1337269332142855),
               E(3, 3.1388, 2, 0.123, 0, 0.34311900104749093, 1.8542647885863094),
               E(4, 4.993, 5, null, 0, null, null)]);
var F3 = T(3, [E(1, 0, 3, null, -57.0526868402515, 0.16952, 1.0050440184226189),
               E(2, 1.005, 3, 0.097, -51.15000818077605, 0.78322, 3.988),
               E(3, 4.993, 5, null, 0, null, null)]);
var F8 = T(8, [E(1, 0, 3, null, -57.037497745190436, 0.19564, 1.0050440184226189),
               E(2, 1.005, 3, 0.002, -51.495780990060574, 0.00371, 0.0491),
               E(3, 1.0541, 2, 0.211, 0, 0.38143, 2.0815),
               E(4, 3.1356, 1, -0.070, 0, 0.34512, 1.8574),
               E(5, 4.993, 5, null, 0, null, null)]);
F8.data.events[1].splice_loss = 0.234;      // over the Viewer's 0.160 gate
// FR's unrounded positions, where the headings read them
F1.data.events[2].fr_pos_km = 3.1387709516369046;
F1.data.events[3].fr_pos_km = 4.993035740223214;
F8.data.events[3].fr_pos_km = 3.135584382142857;
var out = {};
function grab() {
  return {head: gTableExport.table.innerHTML, rows: gTableExport.rows()};
}
renderFastReporterGrid([F1, F3, F8], document.createElement('div'), document.createElement('div'));
out.plain = grab();
// handed over in the order they finished loading, not fibre order
renderFastReporterGrid([F8, F3, F1], document.createElement('div'), document.createElement('div'));
out.shuffled = grab();
gFecMode = true;
var g = function (km, failLoss) {
  return {found: true, conn_km: km, conn_loss: 0.1, conn_refl: -51, combined: [], loss: 0.1,
          refl: -51, fail_loss: failLoss, fail_refl: false};
};
renderFastReporterGrid([F1, F3, F8], document.createElement('div'), document.createElement('div'),
  {fec: {grades: {A: {'1': g(1.005, false), '3': g(1.005, true), '8': g(1.005, false)}},
         gates: {FEC_LOSS_GATE: 0.5, FEC_REFL_GATE: -50, FEC_COMBINE_M: 150, FEC_LOSS_STRICT: 1}},
   fecColumn: true});
out.fec = grab();
gFecMode = false;
print(JSON.stringify(out));
"""


class _Cells(HTMLParser):
    """Every row's cells as (text, class), header rows from <thead>."""

    def __init__(self):
        super().__init__()
        self.rows, self._cell = [], None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.rows.append([])
        elif tag in ('td', 'th'):
            a = dict(attrs)
            self._cell = ['', a.get('class') or '', int(a.get('colspan') or 1)]

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self._cell is not None:
            self.rows[-1].append((self._cell[0].strip(), self._cell[1], self._cell[2]))
            self._cell = None

    def handle_data(self, d):
        if self._cell is not None:
            self._cell[0] += d


def _cells(html):
    p = _Cells()
    p.feed(html)
    return p.rows


@pytest.fixture(scope='module')
def grid(tmp_path_factory):
    if not JSC.exists():
        pytest.skip('no JavaScriptCore shell here')
    p = tmp_path_factory.mktemp('grid') / 'grid.js'
    p.write_text(SHIMS + '\n' + _script() + '\n' + DRIVER, encoding='utf-8')
    res = subprocess.run([str(JSC), str(p)], capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr + res.stdout
    return json.loads(res.stdout.strip().splitlines()[-1])


def _row(out, fiber):
    for h in out['rows']:
        if f'data-fiber="{fiber}"' in h:
            return [c[0] for c in _cells('<table>' + h + '</table>')[0]]
    raise AssertionError(f'no row for F{fiber}')


@needs_jsc
def test_sections_run_to_the_traces_own_next_event(grid):
    f1, f3, f8 = (_row(grid['plain'], f) for f in (1, 3, 8))
    # F1: launch | sec | panel | sec 1.0050->3.1388 = FR's 2.1337 km, 0.381 dB
    assert f1[4:12] == ['---', '-56.9', '1.0050', '0.186', '0.185', '0.142', '-50.4', '2.1337']
    assert f1[12:14] == ['0.381', '0.178']
    # F3 has no splice: its panel->end section is right after the panel
    assert f3[11:14] == ['3.9880', '0.783', '0.196']
    # F8's pigtail section, then its own 2.08 km section after the splice
    assert f8[11:14] == ['0.0491', '0.004', '0.076']
    assert f8[14:18] == ['0.211', '2.0815', '0.381', '0.183']


@needs_jsc
def test_reflectance_rounds_once(grid):
    f1, f3 = _row(grid['plain'], 1), _row(grid['plain'], 3)
    assert f1[5] == '-56.9'                  # -56.948; twice-rounded was -57.0
    assert f3[5] == '-57.1' and f3[10] == '-51.2'   # -51.150008; was -51.1


@needs_jsc
def test_statistics_average_the_printed_figures(grid):
    f3 = _row(grid['plain'], 3)
    # Section Loss min / max / average: 0.170, 0.783 -> 0.477 (FR), not 0.476
    i = f3.index('0.783', 14)
    assert f3[i - 1:i + 2] == ['0.170', '0.783', '0.477']
    # Connector Refl. average: -57.1 and -51.2 -> -54.2 (FR), not -54.1
    assert '-54.2' in f3


@needs_jsc
def test_columns_are_laid_out_as_fr_lays_them_out(grid):
    head = _cells(grid['plain']['head'])
    kinds = [c[0] for c in head[1]]
    units = [c[0] for c in head[2]]
    # one row of kinds per column: Launch Level, Reflective, the 1.0541
    # Non-reflective, then the 3.14 km column holding a Non-reflective (F1)
    # and a Positive (F8), which names no kind
    assert kinds[0].startswith('Launch Level')
    assert kinds[2].startswith('Reflective')
    assert kinds[4].startswith('Non-reflective')
    assert re.match(r'^\d', kinds[6]), kinds[6]           # just its km
    assert kinds[8].startswith('Continuous Fiber')
    flat = ' '.join(units)
    # Launch | Sec | Refl | Sec | NR (Loss only) | Sec | mixed (Type Loss Refl) | Sec | CF (Loss only)
    assert flat.startswith('Loss(dB) Refl.(dB) Length(km) Loss(dB) Att.(dB/km) '
                           'Loss(dB) Refl.(dB) Length(km) Loss(dB) Att.(dB/km) '
                           'Loss(dB) Length(km) Loss(dB) Att.(dB/km) '
                           'Type Loss(dB) Refl.(dB) Length(km) Loss(dB) Att.(dB/km) '
                           'Loss(dB) Min'), flat
    # each row says its own kind in the mixed column
    assert 'Non-reflective' in _row(grid['plain'], 1) and 'Positive' in _row(grid['plain'], 8)


@needs_jsc
def test_fec_shots_are_graded_by_the_fec_rule_only(grid):
    pf = {f: _row(grid['fec'], f)[1] for f in (1, 3, 8)}
    assert pf == {1: '✓', 3: '✗', 8: '✓'}, pf
    # F8's panel 0.234 is over the Viewer's 0.160 gate and is not red here
    h8 = next(h for h in grid['fec']['rows'] if 'data-fiber="8"' in h)
    assert 'fr-hi' not in h8
    # outside FEC the same row is judged by the gate
    h8p = next(h for h in grid['plain']['rows'] if 'data-fiber="8"' in h)
    assert 'fr-hi' in h8p
    # F3's graded connector carries its FEC fail
    h3 = next(h for h in grid['fec']['rows'] if 'data-fiber="3"' in h)
    assert re.search(r'class="fr-hi" data-col="1"', h3), h3


def test_fec_ends_are_never_one_table():
    fn = VIEWER.split('function renderEventTable(', 1)[1].split('\nfunction ', 1)[0]
    i = fn.index('if (gFecMode) {')
    block = fn[i:i + 2400]
    assert 'new Set(visible.map(t => t.dir)).size > 1' in block
    assert 'FastReporter' in block and 'renderFastReporterGrid' in block
    assert block.index('size > 1') < block.index('renderFastReporterGrid')
    # the grades are read whether or not the Combined column shows
    assert 'fec: fecGradesFor(visible), fecColumn: gFecCombined' in block


def test_fec_gates_are_in_the_boxes_whenever_fec_shots_are_loaded():
    sync = VIEWER.split('function syncGateUI(', 1)[1].split('\nfunction ', 1)[0]
    assert 'if (gFecMode) { syncFecGateUI(); return; }' in sync


@needs_jsc
def test_a_heading_prints_its_first_rows_event_as_fr_does(grid):
    """Robert 2026-10-02, "6c follow FR": FR heads a column with the event of
    the first row holding one (F1's 3.1388 km, not the 3.1372 average with
    F8's 3.1356), at FR's unrounded position, so the Section heading after it
    is FR's 1.8543 km (4-dp positions give 1.8542)."""
    heads = [c[0] for c in _cells(grid['plain']['head'])[1]]
    assert heads[6].startswith('3.1388km'), heads[6]
    assert heads[7] == '1.8543 km', heads[7]
    assert heads[3] == '0.0491 km', heads[3]           # 1.0541 - 1.0050
    assert heads[5] == '2.0847 km', heads[5]           # 3.1388 - 1.0541


@needs_jsc
def test_headings_count_the_rows_with_an_event_or_section(grid):
    """Robert 2026-10-02: FR's heading says how many rows have the event:
    "Event 5 (6/12)", and the section after it: "Section (6/12)"."""
    top = [c[0] for c in _cells(grid['plain']['head'])[0]][4:]
    assert top[:9] == ['Event 1 (3/3)⋯', 'Section (3/3)', 'Event 2 (3/3)⋯', 'Section (3/3)',
                       'Event 3 (1/3)⋯', 'Section (1/3)', 'Event 4 (2/3)⋯', 'Section (2/3)',
                       'Event 5 (3/3)⋯'], top


def test_the_two_direction_tables_count_their_sections_too():
    for fn in ('function paintFrBidiGrid(', 'function paintSuiteBidiGrid('):
        body = VIEWER.split(fn, 1)[1].split('\nfunction ', 1)[0]
        assert 'const nSec = have.filter((_p, fi) => secOf(fi, i)).length;' in body, fn
        assert 'Section (${nSec}/${have.length})</th>' in body, fn


# ─── the FastReporter-mode A+B table's sections ───────────────────────────

BIDI_DRIVER = r"""
function T(dir, fiber) {
  var xs = [], ys = [];
  for (var i = 0; i <= 200; i++) { xs.push(i / 10); ys.push(-i / 50); }
  return {key: dir + '-' + fiber, dir: dir, src: dir, fiber: fiber, visible: true,
          name: 'F' + fiber + '.sor', color: '#2f6fb3',
          data: {dist_km: xs, trace_db: ys, wavelength_nm: 1550, events: []}};
}
function leg(km, type, loss, refl) { return {pos_m: km * 1000, type: type, loss: loss, refl: refl}; }
function sec(len, la, lb) {
  return {length_m: len * 1000, loss: (la + lb) / 2, att_db_km: (la + lb) / 2 / len,
          a: {loss: la, att_db_km: la / len}, b: {loss: lb, att_db_km: lb / len}};
}
function row(km, type, loss, s) {
  return {mean_pos_m: km * 1000, type: type, loss: loss,
          a: leg(km, type, loss, type === 3 ? -50 : null), b: leg(km, type, loss, type === 3 ? -50 : null),
          section: s};
}
var res = {tables: {
  t1: [row(0, 3, null, sec(5, 0.931, 0.925)), row(5, 2, 0.05, sec(5, 0.94, 0.93)), row(10, 2, 0.04, null)],
  t4: [row(0, 3, null, sec(10, 1.889, 1.880)), row(10, 2, 0.039, null)]}};
var pairs = [{fiber: 1, tkey: 't1', ta: T('a', 1), tb: T('b', 1)},
             {fiber: 4, tkey: 't4', ta: T('a', 4), tb: T('b', 4)}];
var out = {};
try {
  paintFrBidiGrid(pairs, [], res, document.createElement('div'), document.createElement('div'));
  out.head = gTableExport.table.innerHTML; out.rows = gTableExport.rows();
} catch (e) { out.err = String(e) + '\n' + e.stack; }
print(JSON.stringify(out));
"""


@needs_jsc
def test_the_ab_table_prints_a_section_after_its_own_row(tmp_path):
    """Robert 2026-10-03, "fix the bidi sections too".  FastReporter prints a
    fibre's section under the column of the row it starts at, wherever the
    fibre's next row sits: on a 4-fibre span fibre 4's A->B prints 0.039 then
    1.889 / 0.188 after Event 2 with Event 3 blank.  The table needed the next
    row in the very next column and printed "---" there."""
    p = tmp_path / 'bidi.js'
    p.write_text(SHIMS + '\n' + _script() + '\n' + BIDI_DRIVER, encoding='utf-8')
    res = subprocess.run([str(JSC), str(p)], capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr + res.stdout
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert 'err' not in out, out.get('err')
    rows = {}
    for h in out['rows']:
        cells = [c[0] for c in _cells('<table>' + h + '</table>')[0]]
        rows[(cells[0], cells[3])] = cells
    # fibre 1 has a row in every column: unchanged (a Non-reflective column
    # is Loss alone, as FR lays it out)
    assert rows[('F1', 'A→B')][4:12] == ['---', '-50.0', '0.931', '0.186', '0.050', '0.940', '0.188', '0.040']
    # fibre 4 has no 5 km row: its 0 -> 10 km section prints after column 1,
    # and the column and section it has nothing in are BLANK, as FR prints
    # them (Robert 2026-10-03, "fix the blank vs --- cells to follow FR")
    assert rows[('F4', 'A→B')][4:12] == ['---', '-50.0', '1.889', '0.189', '', '', '', '0.039']
    assert rows[('F4', 'B→A')][6:8] == ['1.880', '0.188']
    assert rows[('F4', 'Average')][4:8] == ['---', '---', '1.885', '0.188']
    # the heading counts both fibres' sections there
    top = [c[0] for c in _cells(out['head'])[0]]
    assert 'Section (2/2)' in top and 'Section (1/2)' in top, top


@needs_jsc
def test_blank_where_there_is_nothing_dashes_where_a_reading_is_missing(grid):
    """Robert 2026-10-03, "fix the blank vs --- cells to follow FR".  FR
    prints "---" where the trace HAS the event but no reading of it (a Launch
    Level's loss, a Continuous Fiber's loss, a non-reflective event's
    reflectance) and leaves the cell BLANK where the trace has no event or
    section in the column, and where a statistic has nothing to summarise."""
    f1, f3 = _row(grid['plain'], 1), _row(grid['plain'], 3)
    # F3: launch, panel, then nothing until its continuous fiber
    assert f3[4:6] == ['---', '-57.1']                    # the launch has no loss of its own
    # 1.0541 (Loss) + its Section + 3.14 (Type, Loss, Refl.) + its Section
    assert f3[14:24] == [''] * 10
    assert f3[24] == '---'                                # the Continuous Fiber's loss
    assert f3[25:28] == ['', '', '']                      # Splice Loss Min / Max / Avg: no splice
    # F1 has the 3.14 km event: its missing reflectance is "---"
    assert f1[18:21] == ['Non-reflective', '0.123', '---']
    # a Minimum over a column nobody has a figure in is blank: the launch's Loss
    mins = [r for r in _cells('<table>' + grid['plain']['head'] + '</table>') if r and r[0][0] == 'Minimum']
    assert mins and mins[0][4][0] == '' and mins[0][5][0] == '-57.1', mins


@needs_jsc
def test_rows_are_in_fibre_order_whatever_order_the_traces_arrive_in(grid):
    """Robert 2026-10-03: the single-direction table listed its rows in the
    order the traces finished loading (after a restore F1-F4, F7, F8, F9,
    F5 ...).  FR lists its files in order, and a column heading prints its
    FIRST row's event (6c), so the order picked the heading's km too."""
    import re as _re
    order = [_re.search(r'data-fiber="(\d+)"', h).group(1) for h in grid['shuffled']['rows']]
    assert order == ['1', '3', '8'], order
    assert grid['shuffled']['rows'] == grid['plain']['rows']
    heads = [c[0] for c in _cells(grid['shuffled']['head'])[1]]
    assert heads[6].startswith('3.1388km'), heads[6]     # F1's, not F8's 3.1356
