"""A ribbon broken at one spot prints one entry, not twelve (boss, 2026-09-17).

The reburn cell read

    1 broke@30.9k (B-only) 2 broke@30.9k (B-only) ... 12 broke@30.9k (B-only)

— the same sentence twelve times.  Every fiber whose printed reading is
identical now shares a single entry:

    1,2,3,4,5,6,7,8,9,10,11,12 broke@30.9k (B-only)

Fibers broken at DIFFERENT places, or carrying different damage losses, keep
their own entries — the collapse keys on the printed text, so nothing that
reads differently is merged away.

Engine runs in a clean subprocess (single sor_reader copy — the 3-engine
isolation rule).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n"
              "def broke(f, label, **kw):\n"
              "    r = {'fiber': f, 'splice_idx': 0, 'bidir_loss': None,\n"
              "         'a_loss': None, 'b_loss': None, 'bidir_dist': 30.9,\n"
              "         'is_break': False, 'is_broke': True, 'is_bend': False,\n"
              "         'is_bfill': False, 'is_dead_zone': False,\n"
              "         'is_a_only': False, 'is_b_only': False,\n"
              "         'is_flagged': True, 'event_source': 'broke_b',\n"
              "         'event_type': 'BROKE_B', 'label': label}\n"
              "    r.update(kw)\n"
              "    return r\n"
              "def build(results):\n"
              "    return E.build_ribbon_data(results, 12, 12, 1)[0]\n"
              "def text(results):\n"
              "    return build(results)[(0, 0)]['text']\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_whole_ribbon_broken_at_one_spot_collapses():
    _run("""
        res = {(f, 0): broke(f, '%d broke@30.9k (B-only)' % f)
               for f in range(1, 13)}
        got = text(res)
        want = '1,2,3,4,5,6,7,8,9,10,11,12 broke@30.9k (B-only)'
        assert got == want, got
        print('OK')
    """)


def test_two_different_spots_stay_separate():
    _run("""
        res = {}
        for f in (1, 2, 3):
            res[(f, 0)] = broke(f, '%d broke@30.9k (B-only)' % f)
        for f in (4, 5):
            res[(f, 0)] = broke(f, '%d broke@41.2k (B-only)' % f)
        got = text(res)
        want = '1,2,3 broke@30.9k (B-only) 4,5 broke@41.2k (B-only)'
        assert got == want, got
        print('OK')
    """)


def test_different_damage_losses_keep_their_own_numbers():
    _run("""
        res = {
            (1, 0): broke(1, '1 3.384 (A) broke@30.9k', a_loss=3.384,
                          damage_loss=3.384, event_source='broke'),
            (2, 0): broke(2, '2 3.384 (A) broke@30.9k', a_loss=3.384,
                          damage_loss=3.384, event_source='broke'),
            (3, 0): broke(3, '3 1.200 (A) broke@30.9k', a_loss=1.200,
                          damage_loss=1.200, event_source='broke'),
        }
        got = text(res)
        want = '1,2 3.384 (A) broke@30.9k 3 1.200 (A) broke@30.9k'
        assert got == want, got
        print('OK')
    """)


def test_single_broke_fiber_is_unchanged():
    _run("""
        res = {(7, 0): broke(7, '7 broke@30.9k (B-only)')}
        got = text(res)
        assert got == '7 broke@30.9k (B-only)', got
        print('OK')
    """)


def test_broke_cell_still_colours_red_and_counts():
    _run("""
        res = {(f, 0): broke(f, '%d broke@30.9k (B-only)' % f)
               for f in range(1, 13)}
        c = build(res)[(0, 0)]
        assert c['is_broke'] is True, c
        assert c['is_break'] is False, c
        assert c['is_flagged'] is True, c
        print('OK')
    """)


def test_tech_compare_still_reads_every_collapsed_fiber():
    """The comparison workbook parses our cell text back into per-fiber
    entries; a collapsed list must expand to the same twelve fibers."""
    import os
    import types

    src = open(os.path.join(REPO_ROOT, 'app.py'), encoding='utf-8').read()
    start = src.index('# ─── tech_compare: begin ───')
    end = src.index('# ─── tech_compare: end ───')
    mod = types.ModuleType('tech_compare_block_collapse')
    sys.modules['tech_compare_block_collapse'] = mod
    exec(compile('from __future__ import annotations\nimport os, re\n' + src[start:end],
                 'app.py[tech_compare]', 'exec'), mod.__dict__)

    got = mod.tc_parse_cell('1,2,3,4,5,6,7,8,9,10,11,12 broke@30.9k (B-only)', 1, 12)
    assert set(got) == set(range(1, 13)), sorted(got)
    assert all(e.tag == 'broke' for e in got.values()), got
