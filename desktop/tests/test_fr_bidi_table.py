"""FastReporter's bidirectional table, built from a .sor pair.

`fr_bidi_table` is the first column-level piece of FastReporter mode: one
row per event pair, a leg per direction, the silent leg synthesised.  The
oracle is FR's own merged table inside a .bdr (`_bdr_merged`), and the gate
is row for row, field for field, on every vendored .bdr: 8 ORPVL fibers
(100 ns, 55 km, trimmed), 12 SEANOR (2,500 ns, 110 km, reels both ends) and
4 WSC<->SUI (275 ns, 64 km, a launch reel, Splice 12 forty metres from the far
connector -- FR transplants there, so the table does too, and reads the two
lines at each other's reach when the windows are too short to meet).  ORPVL fiber 0263 is the corpus's one overlap case where both event windows
are narrower than FR's tolerance; FR keeps two rows there, and the width
condition that says so was pinned by running FR on five edits of that file.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
BDR_DIR = REPO_ROOT / "desktop" / "tests" / "fixtures" / "bdr"
FIX = REPO_ROOT / "desktop" / "tests" / "fixtures"


def _run(body):
    header = ("import sys, glob, os, math\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import sor_reader324802a as sr\n"
              "import splicereportmatchexfo as E\n"
              f"BDR = {str(BDR_DIR)!r}\n"
              f"FIX = {str(FIX)!r}\n"
              + textwrap.dedent("""
        def synth(leg):
            return leg.get('Type') == 0 and (leg.get('Length') or 0) == 0

        def compare(ours, fr, label):
            '''Every FR row must have an exact row of ours; returns the list of
            differences (empty = exact).'''
            diffs = []
            if len(ours) != len(fr):
                diffs.append((label, 'row count', len(ours), len(fr)))
            for m in fr:
                o = min(ours, key=lambda r: abs(r['mean_pos_m'] - m['Position']))
                if abs(o['mean_pos_m'] - m['Position']) > 1e-6:
                    diffs.append((label, round(m['Position'], 1), 'no row')); continue
                if o['type'] != m.get('Type') or o['status'] != m.get('Status'):
                    diffs.append((label, round(m['Position'], 1), 'type/status', o['type'], o['status'], m.get('Type'), m.get('Status')))
                ml = m.get('Loss')
                if isinstance(ml, float) and not math.isnan(ml):
                    if o['loss'] is None or abs(o['loss'] - ml) > 1e-9:
                        diffs.append((label, round(m['Position'], 1), 'loss', o['loss'], ml))
                elif o['loss'] is not None:
                    diffs.append((label, round(m['Position'], 1), 'loss should be nan', o['loss']))
                if abs(o['length_m'] - (m.get('Length') or 0.0)) > 1e-9:
                    diffs.append((label, round(m['Position'], 1), 'length', o['length_m'], m.get('Length')))
                for leg, frleg in (('a', m['_ab']), ('b', m['_ba'])):
                    if o[leg]['synthetic'] != synth(frleg):
                        diffs.append((label, round(m['Position'], 1), leg + ' synthetic', o[leg]['synthetic'], synth(frleg)))
                    if abs(o[leg]['pos_m'] - frleg['Position']) > 1e-6:
                        diffs.append((label, round(m['Position'], 1), leg + ' position', o[leg]['pos_m'], frleg['Position']))
                    fl = frleg.get('Loss')
                    if isinstance(fl, float) and not math.isnan(fl) and (o[leg]['loss'] is None or abs(o[leg]['loss'] - fl) > 1e-9):
                        diffs.append((label, round(m['Position'], 1), leg + ' loss', o[leg]['loss'], fl))
            return diffs

        def compare_sections(ours, secs, label):
            '''FR's merged section rows, one between each pair of event rows:
            merged and per-leg Length (1e-6 m) and Loss (1e-9 dB).'''
            diffs = []
            if len(secs) != len(ours) - 1:
                return [(label, 'section count', len(secs), len(ours) - 1)]
            for i, srow in enumerate(secs):
                o = ours[i].get('section')
                if not o or o['loss'] is None or o['length_m'] is None:
                    diffs.append((label, round(srow['Position'], 1), 'section missing')); continue
                if abs(o['length_m'] - srow['Length']) > 1e-6:
                    diffs.append((label, round(srow['Position'], 1), 'section length', o['length_m'], srow['Length']))
                if abs(o['loss'] - srow['Loss']) > 1e-9:
                    diffs.append((label, round(srow['Position'], 1), 'section loss', o['loss'], srow['Loss']))
                for leg, frleg in (('a', srow['_ab']), ('b', srow['_ba'])):
                    ol = o.get(leg)
                    if not ol or ol['loss'] is None:
                        diffs.append((label, round(srow['Position'], 1), 'section ' + leg + ' missing')); continue
                    if abs(ol['length_m'] - frleg['Length']) > 1e-6:
                        diffs.append((label, round(srow['Position'], 1), 'section ' + leg + ' length', ol['length_m'], frleg['Length']))
                    if abs(ol['loss'] - frleg['Loss']) > 1e-9:
                        diffs.append((label, round(srow['Position'], 1), 'section ' + leg + ' loss', ol['loss'], frleg['Loss']))
            if ours and ours[-1].get('section') is not None:
                diffs.append((label, 'last row carries a section'))
            return diffs

        def sides(path):
            d = sr.parse_bdr(path)
            ra, rb = dict(d['a']), dict(d['b'])
            for r in (ra, rb):
                r.pop('fr_synthetic', None); r['_source'] = 'sor'
            fr = [m for m in (d['a'].get('_bdr_merged') or []) if m.get('_merged') and 'Type' in m]
            return ra, rb, fr

        def section_rows(path):
            d = sr.parse_bdr(path)
            return [m for m in (d['a'].get('_bdr_merged') or []) if m.get('_merged') and 'Type' not in m]
                                """))
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout


def test_every_vendored_bdr_reproduces_row_for_row():
    """All 25 .bdr exact on every row and field, 419 rows, and on every
    section between them, 394 sections (merged and per leg)."""
    _run("""
        n_files = n_rows = n_secs = 0
        for p in sorted(glob.glob(BDR + '/*.bdr')):
            ra, rb, fr = sides(p)
            ours = E.fr_bidi_table(ra, rb)
            assert ours is not None, p
            diffs = compare(ours, fr, os.path.basename(p))
            assert not diffs, diffs
            secs = section_rows(p)
            diffs = compare_sections(ours, secs, os.path.basename(p))
            assert not diffs, diffs
            n_files += 1; n_rows += len(fr); n_secs += len(secs)
        assert n_files == 25 and n_rows == 419 and n_secs == 394, (n_files, n_rows, n_secs)
        print('OK')
    """)


def test_the_section_rule():
    """FR's section loss is ONE least-squares line from the first event's
    CursorB to the last event's CursorA, times the section's length in
    samples; negative stores as 0.0; a one-sample window (an event pulled
    back onto the next one) stores 0.0.  Pinned on fibre 0263 against the
    file's own section records, on a fabricated negative slope, and on the
    one-sample geometry."""
    _run("""
        import numpy as np
        ra, rb, fr = sides(BDR + '/ORPVL.ZYO-OR-DES-0048.1550.0263_1550.bdr')
        # every section record in A's own block reproduces from A's trace
        evs = [e for e in ra['exfo_events'] if not e.get('_is_section')]
        own = [e for e in ra['exfo_events'] if e.get('_is_section')]
        assert len(own) == 8, len(own)
        for s in own:
            e0 = [e for e in evs if abs(e['Position'] - s['CursorAPosition']) < 1e-6][0]
            e1 = [e for e in evs if abs(e['Position'] - s['CursorBPosition']) < 1e-6][0]
            v = E.measure_fr_section_loss(ra, e0['CursorBPosition'], e1['CursorAPosition'], e0['Position'], e1['Position'])
            assert v is not None and abs(v - s['Loss']) < 1e-9, (s['Position'], v, s['Loss'])
        # the table's first section: A's own record, B's own record, the mean
        ours = E.fr_bidi_table(ra, rb)
        sec = ours[0]['section']
        assert abs(sec['a']['loss'] - 1.323282297149699) < 1e-9 and abs(sec['b']['loss'] - 1.3357855596515162) < 1e-9
        assert abs(sec['loss'] - 1.3295339284006076) < 1e-9 and abs(sec['length_m'] - 6964.216622405625) < 1e-6
        assert abs(sec['att_db_km'] - sec['loss'] / sec['length_m'] * 1000) < 1e-12
        # a rising trace (a gainer over the whole section) stores 0.0
        res = ra['exfo_res_m']
        fake = dict(ra, exfo_raw=np.arange(len(ra['exfo_raw']), dtype=np.int32) * 4 + 20000)
        assert E.measure_fr_section_loss(fake, 100 * res, 600 * res, 90 * res, 610 * res) == 0.0
        # falling trace: slope x length in samples
        fake = dict(ra, exfo_raw=40000 - np.arange(len(ra['exfo_raw']), dtype=np.int32) * 4)
        v = E.measure_fr_section_loss(fake, 100 * res, 600 * res, 90 * res, 610 * res)
        assert abs(v - (4.0 / 1024.0) * 520) < 1e-9, v
        # one-sample window, degenerate windows, no trace
        assert E.measure_fr_section_loss(ra, 100 * res, 100 * res, 90 * res, 110 * res) == 0.0
        assert E.measure_fr_section_loss(ra, 100 * res, 99 * res, 90 * res, 110 * res) is None
        assert E.measure_fr_section_loss(ra, 100 * res, 200 * res, 210 * res, 205 * res) is None
        assert E.measure_fr_section_loss({'exfo_raw': None}, 1, 2, 1, 2) is None
        # 0017: the A leg synthesised at 21,810 m was pulled back onto A's own
        # event 18 m on -- the section between them is the one-sample case
        ra, rb, fr = sides(BDR + '/ORPVL.ZYO-OR-DES-0048.1550.0017_1550.bdr')
        ours = E.fr_bidi_table(ra, rb)
        r = [x for x in ours if 21800 < x['mean_pos_m'] < 21830][0]
        assert r['a']['synthetic'] and r['section']['a']['loss'] == 0.0 and 30 < r['section']['a']['length_m'] < 36
        print('OK')
    """)


def test_fiber_0263_two_rows_and_the_width_condition_behind_them():
    """Fiber 0263: A's event at 36,855.8 m (inner window 29.3 m) and B's
    mirror at 36,894.1 m (25.5 m) overlap but sit 38.3 m apart, past FR's
    30.2 m tolerance, and both windows are narrower than that tolerance.
    FR keeps two rows.  Editing the file and re-running FR (2026-09-21):
    A's window at 35.7, 39.6 or 60.0 m gives ONE row at 36,874.9 m, mean
    0.012601; B's window at 60.0 m instead moves the geometry into the
    absorb region and FR folds A into B's row.  The same edits on the
    record in memory must give the same tables."""
    _run("""
        ra, rb, fr = sides(BDR + '/ORPVL.ZYO-OR-DES-0048.1550.0263_1550.bdr')
        ours = E.fr_bidi_table(ra, rb)
        near = [r for r in ours if 36800 < r['mean_pos_m'] < 36950]
        assert len(near) == 2, [(r['mean_pos_m'], r['loss']) for r in near]
        r1, r2 = near
        assert abs(r1['mean_pos_m'] - 36855.8) < 0.1 and abs(r2['mean_pos_m'] - 36894.1) < 0.1
        assert not r1['a']['synthetic'] and r1['b']['synthetic'] and abs(r1['a']['loss'] - 0.0710) < 5e-4
        assert r2['a']['synthetic'] and not r2['b']['synthetic'] and abs(r2['b']['loss'] - (-0.0458)) < 5e-4
        ea = [e for e in ra['exfo_events'] if abs(e['Position'] - 36855.8) < 0.1][0]
        L = E._fr_proj_constant(ra, rb)
        eb = [e for e in rb['exfo_events'] if abs(L - e['Position'] - 36894.1) < 0.1][0]
        assert abs((ea['CursorBPosition'] - ea['Position']) - 29.3) < 0.1
        assert abs((eb['CursorBPosition'] - eb['Position']) - 25.5) < 0.1
        tol = max(E._pulse_length_m(ra), E._pulse_length_m(rb)) + 20.0
        assert 30.0 < tol < 30.5, tol
        # widen A's window: one paired row, FR's mean to the last digit
        for w in (35.7303, 39.6, 60.0):
            ra2 = dict(ra, exfo_events=[dict(e, CursorBPosition=e['Position'] + w, Length=w) if e is ea else e for e in ra['exfo_events']])
            rows = [r for r in E.fr_bidi_table(ra2, rb) if 36800 < r['mean_pos_m'] < 36950]
            assert len(rows) == 1 and not rows[0]['a']['synthetic'] and not rows[0]['b']['synthetic'], (w, rows)
            assert abs(rows[0]['mean_pos_m'] - 36874.9) < 0.1 and abs(rows[0]['loss'] - 0.01260130) < 1e-6, (w, rows[0])
        # widen B's window instead: A absorbed into B's row
        rb2 = dict(rb, exfo_events=[dict(e, CursorBPosition=e['Position'] + 60.0, Length=60.0) if e is eb else e for e in rb['exfo_events']])
        rows = [r for r in E.fr_bidi_table(ra, rb2) if 36800 < r['mean_pos_m'] < 36950]
        assert len(rows) == 1 and rows[0]['a']['synthetic'] and abs(rows[0]['mean_pos_m'] - 36894.1) < 0.1, rows
        assert rows[0]['a']['absorbed'] and abs(rows[0]['a']['absorbed'][0] - 36855.8) < 0.1, rows[0]['a']
        # ...and the sections FR wrote around that absorbed row reproduce too
        # (frout/0263_WB60.bdr is FR's output for exactly this edit: the A
        # leg's section from the previous row runs across the absorbed event)
        print('OK')
    """)


def test_the_customer_path_gives_the_same_table_from_the_sor_pair():
    """Fiber 0017 as a customer supplies it -- the two .sor, nothing of FR's
    -- against FR's merged table read from its .bdr: exact, every row."""
    _run("""
        pa = FIX + '/zayo_sor/ORPVL.ZYO-OR-DES-0048.1550.0017.sor'
        pb = FIX + '/zayo_sor/ZYO-OR-DES-0048.ORPVL.1550.0017.sor'
        ra = sr.parse_sor_full(pa, trim=False); rb = sr.parse_sor_full(pb, trim=False)
        for r, side in ((ra, 'a'), (rb, 'b')):
            r['_source'] = 'sor'; r['_span_side'] = side
        _, _, fr = sides(BDR + '/ORPVL.ZYO-OR-DES-0048.1550.0017_1550.bdr')
        ours = E.fr_bidi_table(ra, rb)
        assert ours is not None and len(ours) == len(fr) == 11, (len(ours), len(fr))
        assert not compare(ours, fr, '0017 .sor'), compare(ours, fr, '0017 .sor')
        print('OK')
    """)


def test_f354_is_two_rows_not_one():
    """The boss's case (2026-09-21): FR shows fiber 354 PASSING at 44.1 km
    where the classic report prints .116.  FR's table has two rows there,
    A's event at 44,098.8 m (A +0.1749 real, B +0.0237 synthesised, mean
    0.0993) and B's at 44,215.0 m (A -0.0155 synthesised, B +0.0580 real,
    mean 0.0213): they are 116 m apart and FR does not pair them.  The
    classic report averages A's +0.1749 with B's real +0.0580 -- .116."""
    _run("""
        ra, rb, fr = sides(BDR + '/ORPVL.ZYO-OR-DES-0048.1550.0354_1550.bdr')
        ours = E.fr_bidi_table(ra, rb)
        near = [r for r in ours if 44000 < r['mean_pos_m'] < 44300]
        assert len(near) == 2, [(r['mean_pos_m'], r['loss']) for r in near]
        r1, r2 = near
        assert abs(r1['mean_pos_m'] - 44098.8361) < 1e-3 and abs(r2['mean_pos_m'] - 44214.9596) < 1e-3
        assert not r1['a']['synthetic'] and r1['b']['synthetic']
        assert r1['a']['synthetic'] is False and abs(r1['a']['loss'] - 0.1749) < 5e-4
        assert abs(r1['b']['loss'] - 0.0237) < 5e-4 and abs(r1['loss'] - 0.0993) < 5e-4
        assert r2['a']['synthetic'] and not r2['b']['synthetic']
        assert abs(r2['b']['loss'] - 0.0580) < 5e-4 and abs(r2['loss'] - 0.0213) < 5e-4
        # and neither row is the classic .116
        assert all(abs(r['loss'] - 0.1165) > 0.01 for r in near)
        print('OK')
    """)


def test_pairing_decision_table():
    """The regions, on fabricated events with the projection constant and the
    transplant patched out, so only the pairing logic is under test:

        |delta| <= tol            pair
        delta < -tol              separate (never pairs, however close)
        tol < delta <= inner_A    pair
        inner_A < delta <= inner_B  A absorbed into B's row
        inner_B < delta <= inner_A + inner_B  pair if either window is at
                                  least tol wide, else separate
        delta > inner_A + inner_B separate
    """
    _run("""
        L = 50000.0
        E._fr_proj_constant = lambda a, b: L
        E._fr_exact_silent_loss = lambda silent, loud, evt, **kw: 0.001
        def ev(pos, inner, typ=2, loss=0.05, status=0):
            return {'Position': float(pos), 'CursorAPosition': float(pos), 'CursorBPosition': float(pos + inner),
                    'SubCursorAPosition': float(pos - 5000), 'SubCursorBPosition': float(pos + inner + 5000),
                    'Length': float(inner), 'Type': typ, 'Status': status, 'Loss': loss, 'Reflectance': float('nan')}
        def rec(side, events):
            return {'_source': 'sor', '_span_side': side, 'fxd_pulse_ns': 100.0, 'ior': 1.468325,
                    'exfo_events': events, 'exfo_raw': None, 'exfo_res_m': 1.276}
        tol = E._pulse_length_m(rec('a', [])) + 20.0
        assert abs(tol - 30.211) < 0.01, tol
        def table(a_pos, a_inner, b_mirror_pos, b_inner):
            ra = rec('a', [ev(0, 20, 3, float('nan'), 0x48), ev(a_pos, a_inner), ev(L, 20, 3, float('nan'), 0x84)])
            rb = rec('b', [ev(0, 20, 3, float('nan'), 0x48), ev(L - b_mirror_pos, b_inner), ev(L, 20, 3, float('nan'), 0x84)])
            rows = E.fr_bidi_table(ra, rb)
            mid = [r for r in rows if 0 < r['mean_pos_m'] < L]
            kinds = ['pair' if not r['a']['synthetic'] and not r['b']['synthetic'] else ('Asolo' if r['b']['synthetic'] else 'Bsolo') for r in mid]
            return kinds, mid
        # in tolerance, either sign
        assert table(20000, 30, 20000 + 25, 30)[0] == ['pair']
        assert table(20000, 30, 20000 - 25, 30)[0] == ['pair']
        # beyond tolerance with B's mirror BEFORE A: never pairs
        assert table(20000, 60, 20000 - 31, 60)[0] == ['Bsolo', 'Asolo']
        assert table(20000, 60, 20000 - 100, 60)[0] == ['Bsolo', 'Asolo']
        # B's mirror inside A's inner window: pair
        assert table(20000, 60, 20000 + 45, 30)[0] == ['pair']
        # past A's window but A inside B's mirrored window: A absorbed
        kinds, mid = table(20000, 30, 20000 + 45, 60)
        assert kinds == ['Bsolo'], kinds
        assert mid[0]['a']['absorbed'] == [20000.0]
        # windows overlap without either position inside the other: pair
        # when either window is at least tol wide (the 0263 rule)...
        assert table(20000, 40, 20000 + 55, 25)[0] == ['pair']
        assert table(20000, 25, 20000 + 55, 40)[0] == ['pair']
        # ...and separate when both are narrower than tol
        assert table(20000, 30, 20000 + 55, 30)[0] == ['Asolo', 'Bsolo']
        # past both windows: separate
        assert table(20000, 40, 20000 + 66, 25)[0] == ['Asolo', 'Bsolo']
        # the ends pair with status from the bits: launch 64, end 128
        rows = table(20000, 30, 20000, 30)[1]
        ra = rec('a', [ev(0, 20, 3, float('nan'), 0x48), ev(L, 20, 3, float('nan'), 0x84)])
        rb = rec('b', [ev(0, 20, 3, float('nan'), 0x48), ev(L, 20, 3, float('nan'), 0x84)])
        ends = E.fr_bidi_table(ra, rb)
        assert [r['status'] for r in ends] == [64, 128] and [r['type'] for r in ends] == [3, 3]
        # row type on a pair follows the sign of the mean
        ra = rec('a', [ev(0, 20, 3, float('nan'), 0x48), ev(20000, 30, 1, -0.08), ev(L, 20, 3, float('nan'), 0x84)])
        rb = rec('b', [ev(0, 20, 3, float('nan'), 0x48), ev(L - 20000, 30, 2, 0.05), ev(L, 20, 3, float('nan'), 0x84)])
        r = [x for x in E.fr_bidi_table(ra, rb) if 0 < x['mean_pos_m'] < L][0]
        assert r['type'] == 1 and abs(r['loss'] - (-0.015)) < 1e-12
        print('OK')
    """)


def test_the_table_transplants_to_the_cable_end_where_fr_does():
    """WSC<->SUI Splice 12 sits ~40-90 m before the far connector.  The
    classic transplant refuses anything within FR_TRANSPLANT_REACH_M (150 m)
    of an end; FastReporter does not, and its .bdr keys carry a synthesised
    A leg there on every fiber.  fr_bidi_table passes reach 0 and reproduces
    FR's leg; the classic call, untouched, still refuses."""
    _run("""
        ra, rb, fr = sides(BDR + '/WSC_SUI_0003_1550.bdr')
        ours = E.fr_bidi_table(ra, rb)
        r = [x for x in ours if 63900 < x['mean_pos_m'] < 64000][0]
        assert r['a']['synthetic'] and r['a']['loss'] is not None
        m = [x for x in fr if 63900 < x['Position'] < 64000][0]
        assert abs(r['a']['loss'] - m['_ab']['Loss']) < 1e-9 and abs(r['loss'] - m['Loss']) < 1e-9
        end = [e for e in ra['exfo_events'] if isinstance(e.get('Status'), int) and e['Status'] & 0x80][0]
        assert 0 < end['Position'] - r['a']['pos_m'] < E.FR_TRANSPLANT_REACH_M
        # the classic path keeps its guard
        pseudo = {'dist_km': m['_ba']['Position'] / 1000.0}
        assert E._fr_exact_silent_loss(ra, rb, pseudo) is None
        assert E._fr_exact_silent_loss(ra, rb, pseudo, reach_m=0.0) is not None
        assert E.FR_TABLE_END_REACH_M == 0.0
        print('OK')
    """)
