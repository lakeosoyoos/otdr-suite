"""Viewer FEC mode (Robert 2026-10-01).

FEC shots are short traces from each END of a span, so A and B never see the
same glass.  In FEC mode the Viewer neither mirrors B nor pairs it with A, and
its event table grades each trace's panel connector on its own with the
Splice Report FEC tool's rule, through /api/fec_table -> the runner's
--fec-table (the server never imports the engine).
"""
import os
import re
import shutil
import subprocess
import sys
import textwrap

import pytest

from conftest import (FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, REPO_ROOT,
                      VIEWER_DIR)

RUNNER = REPO_ROOT / "splicereport" / "run_splicereport.py"
HTML = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def _run(body, tmp_path):
    da, db = tmp_path / "A", tmp_path / "B"
    da.mkdir(); db.mkdir()
    # one fibre from each end, copied under neutral names (fibre 17)
    shutil.copy(sorted(FIXTURE_SPLICE_A_DIR.glob("*.sor"))[16], da / "ENDA0017_1550.sor")
    shutil.copy(sorted(FIXTURE_SPLICE_B_DIR.glob("*.sor"))[16], db / "ENDB0017_1550.sor")
    header = ("import sys, os, json, subprocess\n"
              f"sys.path.insert(0, {str(VIEWER_DIR)!r})\n"
              "import trace_server as T\n"
              f"T.CONFIG['dir_a'] = {str(da)!r}\n"
              f"T.CONFIG['dir_b'] = {str(db)!r}\n"
              f"T.CONFIG['engine_argv'] = [sys.executable, {str(RUNNER)!r}]\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_each_side_is_graded_on_its_own_and_cached(tmp_path):
    _run("""
        res = T.fec_tables([17, 5])
        assert res['error'] is None, res['error']
        a, b = res['grades']['A']['17'], res['grades']['B']['17']
        for g in (a, b):
            assert g['found'] and 'loss' in g and 'fail_loss' in g, g
            assert g['conn_km'] > 0.05, g          # past the OTDR port
        assert '5' not in res['grades']['A']        # no such fibre
        assert res['gates']['FEC_LOSS_GATE'] == 0.5
        real = subprocess.run
        def boom(*a, **k): raise AssertionError('engine run twice for an unchanged file')
        subprocess.run = boom
        try:
            again = T.fec_tables([17])
        finally:
            subprocess.run = real
        assert again['grades']['A']['17'] == a and again['grades']['B']['17'] == b
        print('OK')
    """, tmp_path)


def test_the_profile_gates_reach_the_engine(tmp_path):
    """A gate change is a new cache key and is what the engine grades by."""
    _run("""
        base = T.fec_tables([17])['grades']['A']['17']
        T.set_fec_gates({'FEC_LOSS_GATE': 0.0, 'FEC_LOSS_STRICT': 0,
                         'FEC_REFL_GATE': -50.0, 'FEC_COMBINE_M': 150.0})
        res = T.fec_tables([17])
        g = res['grades']['A']['17']
        assert res['gates']['FEC_LOSS_GATE'] == 0.0, res['gates']
        assert g['loss'] == base['loss'] and g['fail_loss'] == (g['loss'] >= 0.0)
        T.set_fec_gates(None)
        assert T.CONFIG['fec_gates'] is None
        print('OK')
    """, tmp_path)


def test_fec_mode_switches_the_mirror_off():
    flip = re.search(r"function isFlipped\(t\) \{([^}]*)\}", HTML).group(1)
    assert '!gFecMode' in flip
    yoff = re.search(r"function yOffsetFor\(t\) \{([^}]*)\}", HTML).group(1)
    assert '!gFecMode' in yoff
    assert 'if (gFecMode || !gStacked || !gHaveA) return;' in HTML


def test_fec_mode_has_its_own_table_ahead_of_the_pairing_grids():
    body = HTML[HTML.index('function renderEventTable() {'):]
    body = body[:body.index('\n}\n')]
    assert body.index('renderFecGrid') < body.index('renderSuiteBidiGrid')
    assert "fetch(`/api/fec_table?fibers=" in HTML
    # the side is the folder a trace came from, never a pairing
    assert "t.src === 'b' ? 'B' : 'A'" in HTML


def test_fec_mode_comes_only_from_the_viewer_fec_link():
    """No switch on the gear (Robert 2026-10-01): only the Viewer FEC tool's
    ?fec=1 address turns it on, so nothing is remembered between visits."""
    assert 'id="set-fec"' not in HTML
    assert 'otdr_viewer_fec' not in HTML
    assert "gFecMode = new URLSearchParams(location.search).get('fec') === '1';" in HTML
    assert "'FEC mode'].filter(Boolean)" not in HTML


def test_the_short_shot_warning_points_to_fec_mode():
    assert 'For facility-entrance (FEC) shots, use the Viewer FEC tool.' in HTML


def test_fec_table_ends_with_min_max_average_that_pins():
    """Robert 2026-10-01: the FEC table had no Min/Max/Average strip, so
    "Pin Min/Max/Average" did nothing there.  It now ends in a <tfoot> like
    the other grids (the sticky/unpinned CSS works on any fr-table tfoot)."""
    body = HTML[HTML.index('function paintFecGrid('):]
    body = body[:body.index('\n}\n')]
    for label in ("'Minimum'", "'Maximum'", "'Average'"):
        assert label in body
    assert '<tfoot>${aggRows.join' in body
    assert 'class="fr-agg"' in body
    # Min/Max name their trace and a click finds the row; centred in the
    # window the pinned footer leaves
    assert 'class="fr-own"' in body and 'pinnedFootH(table)' in body
    assert 'table.fr-table tfoot { position: sticky; bottom: 0;' in HTML
    assert '#event-panel.agg-unpinned table.fr-table tfoot { position: static; }' in HTML


def test_viewer_script_still_parses():
    jsc = ('/System/Library/Frameworks/JavaScriptCore.framework/Versions/'
           'Current/Helpers/jsc')
    if not os.path.exists(jsc):
        pytest.skip('JavaScriptCore shell not available')
    scripts = re.findall(r'<script>(.*?)</script>', HTML, re.S)
    src = max(scripts, key=len)
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as fh:
        fh.write(src)
        path = fh.name
    try:
        p = subprocess.run([jsc, '-e', f"checkSyntax('{path}')"],
                           capture_output=True, text=True, timeout=60)
        assert p.returncode == 0 and 'Error' not in (p.stdout + p.stderr), \
            p.stdout + p.stderr
    finally:
        os.remove(path)
