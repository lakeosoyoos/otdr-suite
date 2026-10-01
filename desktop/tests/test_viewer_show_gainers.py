"""The Viewer's "Show gainers" switch.

Asked for by Robert 2026-09-29: an option beside the other "Show..." view
switches to hide gainers.  A gainer is ONE direction's reading with a
negative loss (FastReporter's "Positive" type).  With the switch off:

  * its loss cell prints blank, in the single-direction grid and in the A->B
    and B->A rows of both two-direction tables (FastReporter and OTDR Suite);
  * it is not judged, so it cannot turn its cell red or yellow or put a ✗ on
    its row;
  * it leaves the single-direction grid's Minimum / Maximum / Average;
  * the bidirectional Average row is never hidden: it decides pass/fail.

The audit case: a 1152-fibre span, A only, first 24 fibres.  Fibre 7's event
10 at 63.9685 km reads -0.249 dB (FR type 1) and failed at the 0.160 gate;
with the switch off it must be blank and F7 must not show ✗ for it.

Plain JS, no runtime here: the rules are PARSED OUT OF viewer.html and a
Python mirror applies them, with a teeth check that the same mirror without
the rules (viewer.html before the change) still fails F7.  The mirror's
answer for F7 was checked once against the real Viewer in a browser.
"""
from __future__ import annotations

import re

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def _body(src, head, end="\n}\n"):
    return src.split(head, 1)[1].split(end, 1)[0]


RULE_NAMES = (
    'hide_rule',            # gainerHidden: switch off and a negative loss
    'single_drops',         # single-direction grid: evLoss drops a hidden gainer
    'single_blanks',        # ... and its loss cell prints blank
    'fr_leg_unjudged',      # FR two-direction table: a hidden leg never fails / warns
    'fr_leg_blanks',        # ... and its loss cell prints blank
    'suite_leg_unjudged',   # OTDR Suite table: the same
    'suite_leg_blanks',
)
MAIN_RULES = dict.fromkeys(RULE_NAMES, False)      # viewer.html before the change


def rules_from_source(src=None):
    src = src if src is not None else SRC
    grid = _body(src, "function renderFastReporterGrid(")
    fr = _body(src, "function paintFrBidiGrid(")
    suite = _body(src, "function paintSuiteBidiGrid(")
    return {
        'hide_rule': "const gainerHidden = v => !gShowGainers && v != null && !isNaN(v) && v < 0;" in src,
        'single_drops': re.search(
            r"const evLoss = e => \{ const L = evLossRaw\(e\); return gainerHidden\(L\) \? null : L; \};",
            grid) is not None,
        'single_blanks': re.search(
            r"if \(gainerHidden\(evLossRaw\(e\)\)\) \{[^\n]*\n\s*cells\.push\(`<td data-col=\"\$\{i\}\" data-km=\"\$\{e\.dist_km\}\"></td>`",
            grid) is not None,
        'fr_leg_unjudged': "if (!legOk(leg) || gainerHidden(leg.loss)) return false;" in fr
            and "return legOk(leg) && !gainerHidden(leg.loss) && clearsAt(leg.loss, warnFor(isRefl(x), true));" in fr,
        'fr_leg_blanks': "+ (gainerHidden(leg.loss) ? `<td${at}></td>`" in fr,
        'suite_leg_unjudged': "if (!leg || gainerHidden(leg.loss)) return false;" in suite
            and "return legOk(leg) && !gainerHidden(leg.loss) && clearsAt(leg.loss, warnFor(!!x.reflective, true));" in suite
            and "x[which].flag && !gainerHidden(x[which].loss)" in suite,
        'suite_leg_blanks': "if (leg && gainerHidden(v)) return `<td${attrs}></td>`;" in suite,
    }


# ── The mirror ──────────────────────────────────────────────────────────

GATE_1DIR = 0.160      # the report's single-direction gate (the audit's)
GATE_BIDI = 0.160


def clears(v, gate):
    """clearsGate / clearsAt: round to what is printed, then test |loss|."""
    return v is not None and round(abs(v) * 1000) / 1000 >= gate


def _hidden(rules, show, v):
    return rules['hide_rule'] and not show and v is not None and v < 0


def single_grid(traces, show, rules):
    """renderFastReporterGrid over pre-aligned columns: traces[ti][col] is an
    event {loss, launch?, end?} or None.  Returns (rows, agg): each row the
    loss cells as (text, class) plus its ✗; agg the Minimum / Maximum /
    Average per column."""
    def raw(e):
        return None if (e is None or e.get('launch') or e.get('end')) else e['loss']

    def ev_loss(e):
        L = raw(e)
        return None if (rules['single_drops'] and _hidden(rules, show, L)) else L

    rows = []
    for evs in traces:
        cells = []
        for e in evs:
            if rules['single_blanks'] and _hidden(rules, show, raw(e)):
                cells.append(('', ''))
                continue
            v = ev_loss(e)
            cells.append(('-' if v is None else f'{v:.3f}',
                          'fr-hi' if clears(v, GATE_1DIR) else ''))
        rows.append({'cells': cells, 'fail': any(clears(ev_loss(e), GATE_1DIR) for e in evs)})
    agg = []
    for ci in range(len(traces[0])):
        ls = [ev_loss(t[ci]) for t in traces]
        ls = [x for x in ls if x is not None]
        agg.append((min(ls), max(ls), sum(ls) / len(ls)) if ls else (None, None, None))
    return rows, agg


def bidi_table(fibre, show, rules, table='fr'):
    """One fibre's A->B / B->A / Average rows in a two-direction table:
    fibre is a list of columns {a: {loss, flag?}, b: {...}, avg}."""
    unjudged = rules['fr_leg_unjudged' if table == 'fr' else 'suite_leg_unjudged']
    blanks = rules['fr_leg_blanks' if table == 'fr' else 'suite_leg_blanks']
    out = {}
    for w in ('a', 'b', 'avg'):
        cells, fail = [], False
        for col in fibre:
            if w == 'avg':
                v = col['avg']
                bad = clears(v, GATE_BIDI)
            else:
                leg = col[w]
                v = leg['loss']
                hid = _hidden(rules, show, v)
                bad = (not (unjudged and hid)) and (bool(leg.get('flag')) or clears(v, GATE_1DIR))
                if blanks and hid:
                    cells.append(('', ''))
                    fail = fail or bad
                    continue
            cells.append((f'{v:.3f}', 'fr-hi' if bad else ''))
            fail = fail or bad
        out[w] = {'cells': cells, 'fail': fail}
    return out


# The audit's F7, A only: a splice, the gainer at 63.9685 km, a splice.
F7 = [{'loss': 0.0, 'launch': True}, {'loss': 0.045}, {'loss': -0.249}, {'loss': 0.061}]
F8 = [{'loss': 0.0, 'launch': True}, {'loss': 0.030}, {'loss': 0.080}, {'loss': 0.052}]


# ── The rules are in viewer.html ──────────────────────────────────────────

def test_every_rule_is_present_in_viewer_html():
    rules = rules_from_source()
    assert all(rules.values()), [k for k, v in rules.items() if not v]


def test_the_gate_rule_the_mirror_uses_is_the_viewers():
    fn = _body(SRC, "function clearsGate(loss) {")
    assert "return Math.round(Math.abs(loss) * 1000) / 1000 >= activeGateDb();" in fn


# ── Single-direction grid ─────────────────────────────────────────────────

def test_f7_gainer_blank_and_its_row_passes_when_gainers_are_hidden():
    rows, _ = single_grid([F7, F8], show=False, rules=rules_from_source())
    assert rows[0]['cells'][2] == ('', '')
    assert rows[0]['fail'] is False
    assert rows[0]['cells'][1] == ('0.045', '')          # the rest still print


def test_f7_still_fails_with_gainers_shown():
    rows, _ = single_grid([F7, F8], show=True, rules=rules_from_source())
    assert rows[0]['cells'][2] == ('-0.249', 'fr-hi')
    assert rows[0]['fail'] is True


def test_teeth_without_the_change_f7_fails_with_the_switch_off():
    rows, _ = single_grid([F7, F8], show=False, rules=MAIN_RULES)
    assert rows[0]['cells'][2] == ('-0.249', 'fr-hi')
    assert rows[0]['fail'] is True


def test_min_max_average_leave_out_a_hidden_gainer():
    _, agg = single_grid([F7, F8], show=False, rules=rules_from_source())
    assert agg[2] == (0.080, 0.080, 0.080)
    _, agg_on = single_grid([F7, F8], show=True, rules=rules_from_source())
    assert agg_on[2][0] == -0.249
    # the grid's statistics and verdicts all read evLoss, the one that drops it
    grid = _body(SRC, "function renderFastReporterGrid(")
    assert "const ls = c.ev.map(evLoss).filter(v => v != null && !isNaN(v));" in grid
    # overGate is clearsGate, or the one-direction gate for a mixed load's one-way fibres
    assert "const rowFails = traces.map((_t, ti) => cols.some(c => overGate(evLoss(c.ev[ti]))));" in grid
    assert "const L = evLoss(e);" in grid                 # the per-row statistics block


# ── The two-direction tables ──────────────────────────────────────────────

# A gainer on A (fails its single-direction gate by size), B a loser, the
# Average small; and a column whose Average is itself negative.
FIBRE = [
    {'a': {'loss': -0.249}, 'b': {'loss': 0.300}, 'avg': 0.026},
    {'a': {'loss': 0.020}, 'b': {'loss': -0.120}, 'avg': -0.050},
]


def test_fr_table_hides_the_direction_gainer_and_keeps_the_average():
    rules = rules_from_source()
    t = bidi_table(FIBRE, show=False, rules=rules, table='fr')
    assert t['a']['cells'][0] == ('', '') and t['a']['fail'] is False
    assert t['b']['cells'][1] == ('', '')
    assert t['b']['cells'][0] == ('0.300', 'fr-hi')       # a loser still judged
    assert t['avg']['cells'] == [('0.026', ''), ('-0.050', '')]   # never hidden
    main = bidi_table(FIBRE, show=False, rules=MAIN_RULES, table='fr')
    assert main['a']['cells'][0] == ('-0.249', 'fr-hi') and main['a']['fail'] is True   # teeth


def test_suite_table_hides_a_flagged_gainer_leg_too():
    rules = rules_from_source()
    fibre = [{'a': {'loss': -0.249, 'flag': True}, 'b': {'loss': 0.050}, 'avg': -0.100}]
    t = bidi_table(fibre, show=False, rules=rules, table='suite')
    assert t['a']['cells'] == [('', '')] and t['a']['fail'] is False
    assert t['avg']['cells'] == [('-0.100', '')]
    main = bidi_table(fibre, show=False, rules=MAIN_RULES, table='suite')
    assert main['a']['fail'] is True                     # teeth


def test_the_average_row_is_never_blanked_for_a_gainer():
    fr = _body(SRC, "function paintFrBidiGrid(")
    avg = fr.split("} else if (which === 'avg') {", 1)[1].split("} else {", 1)[0]
    assert "gainerHidden" not in avg
    assert "if (which === 'avg') return clearsAt(x.row.loss, gateFor(isRefl(x), false));" in fr
    suite = _body(SRC, "function paintSuiteBidiGrid(")
    # the Suite lossCell's guard needs a leg: the Average row has none
    assert "const leg = which === 'avg' ? null : x[which];" in suite


# ── The switch ────────────────────────────────────────────────────────────

def test_default_on_and_remembered():
    assert "let gShowGainers = true;" in SRC
    assert "try { gShowGainers = localStorage.getItem('otdr_viewer_gainers') !== '0'; } catch (e) {}" in SRC
    tog = _body(SRC, "function toggleView(which) {")
    assert "else if (which === 'gainers') {" in tog
    assert "try { localStorage.setItem('otdr_viewer_gainers', gShowGainers ? '1' : '0'); } catch (e) {}" in tog
    assert "if (gb) gb.checked = gShowGainers;" in tog      # menu and box are one switch


def test_in_the_gear_and_the_view_menu_and_lights_the_gear_when_off():
    menu = SRC.split('id="evt-view-menu"', 1)[1].split("</span>", 1)[0]
    assert '<input id="set-gainers" type="checkbox" checked> Show Gainers</label>' in menu
    item = _body(SRC, "function viewItems() {")
    assert "`<button data-view=\"gainers\">${gShowGainers ? '✓ ' : ''}Show Gainers</button>`" in item
    sync = _body(SRC, "function syncViewBtn() {")
    assert "!gShowGainers && 'gainers hidden'" in sync
    assert "if (e.target.checked !== gShowGainers) toggleView('gainers');" in SRC


def test_the_hint_and_the_report_say_gainers_are_hidden():
    # Every table's hint is a list of what is ticked, joined by " · " (the
    # Suite caption trimmed in #384, the other two since 2026-09-30), so the
    # note is an item in each.
    for painter in ("renderFastReporterGrid", "paintFrBidiGrid", "paintSuiteBidiGrid"):
        assert "gShowGainers ? '' : 'gainers hidden'," in _body(SRC, "function " + painter + "("), painter
    rep = _body(SRC, "async function reportPayload(")
    assert "if (!gShowGainers) meta.push(['Gainers'," in rep
    # the report prints the table as shown: its rows come from the same rowHtml
    assert SRC.count("gTableExport = { table, rows: () => descs.map(rowHtml) };") == 2
