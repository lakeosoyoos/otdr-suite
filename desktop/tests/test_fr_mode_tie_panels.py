"""FastReporter mode on a Lumen tie panel shows what FastReporter shows there.

Before, the FR-mode grid printed only FR's merged LOSS, and only on ordinary
rows, so a tie panel lost everything else FR paints red with the Lumen
template (Reflectance Fail -50.0, IncludeSpanStart/End True):

  * reflectance, judged per direction the way FR judges it -- Red Rock East
    F74 -49.8 dB and F126 -47.0 dB (tech-confirmed out of spec 2026-09-17),
    LSC1<->LSC6 F128 -49.8 dB on its end marker (FR Event 2, red, 2026-09-24);
  * the panels on a span whose markers sit ON them: FR's .bdr carries their
    Average (FTH01<->FTH06 F140: 0.290 and 0.239), graded by the connector gate.

Breaks stay out: FR passes RDR4<->RDR6 F46 (broken at the panel) in both
directions, taking the break as the span end.  So does the receive reel's far
end on a shot with no markers: FR under Lumen paints it red (Tucson West A,
-48.0 dB at 2.1309 km) but the files' own setting leaves the span end out and
no tech ever graded it.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT, FIXTURE_DIR

RUNNER = REPO_ROOT / "splicereport" / "run_splicereport.py"


def _fr(name, tmp_path):
    d = FIXTURE_DIR / name
    p = subprocess.run([sys.executable, str(RUNNER), "--analysis", "fr",
                        "--dir-a", str(d / "A"), "--dir-b", str(d / "B"),
                        "--out", str(tmp_path / f"{name}.xlsx")],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-1500:]
    m = json.loads(p.stdout.strip().splitlines()[-1])
    return m, sorted(c["label"] for c in m["cells"] if c["is_flagged"])


def test_reflectance_fails_print_in_fr_mode(tmp_path):
    _, cells = _fr("panelreels_east", tmp_path)
    assert cells == ["126 REFL-47.0dB", "74 REFL-49.8dB"], cells


def test_far_panel_on_the_end_marker_prints_in_fr_mode(tmp_path):
    _, cells = _fr("panelfarrefl", tmp_path)
    assert cells == ["128 REFL-49.8dB"], cells


def test_marked_panels_are_columns_where_they_stand(tmp_path):
    m, cells = _fr("paneljumper", tmp_path)
    km = [c["km"] for c in m["columns"]]
    assert len(km) == 2 and abs(km[0]) < 0.001 and abs(km[1] - 0.0624) < 0.002, km
    assert cells == [], cells                    # every FTH panel averages under 0.5


def test_no_column_past_the_cable_on_a_reel_shot(tmp_path):
    m, _ = _fr("panelreels_east", tmp_path)
    assert all(c["km"] < 0.1 for c in m["columns"]), m["columns"]


def test_a_marked_panel_is_graded_on_fr_s_average():
    """Fabricated table: the declared span start carries FR's Average, and it
    fails the connector gate; an undeclared launch row with the same numbers
    is the OTDR port and stays out, and so does a declared start on a long
    cable (Miller->Topeka's launch connector reads the B shot's open end at
    -15.5 dB on every fibre)."""
    body = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(REPO_ROOT / 'splicereport')!r})
        import splicereportmatchexfo as E
        def leg(loss, refl, status):
            return {{'loss': loss, 'refl': refl, 'status': status, 'pos_m': 0.0}}
        def table(_ra, _rb):
            return [{{'mean_pos_m': 0.0, 'loss': 0.62, 'type': 3, 'status': 64,
                     'refl': -55.0, 'a': leg(0.30, -55.0, 64), 'b': leg(0.94, -56.0, 128),
                     'section': None, 'length_m': 0.0}}]
        E.fr_bidi_table = table
        E._pulse_length_m = lambda r: 5.0
        marked = {{1: {{'_trace_offset_km': 1.04, 'user_offset_km': 1.04}}}}
        bare = {{1: {{'_trace_offset_km': 1.04, 'user_offset_km': 0.0}}}}
        b = {{1: {{}}}}
        E._is_panel_span = lambda fa: True
        cols, res = E.fr_report_grid(marked, b, 0.15)
        assert [c['position_km'] for c in cols] == [0.0], cols
        assert res[(1, 0)]['label'] == '1 .620 REFL-55.0dB', res
        cols, res = E.fr_report_grid(bare, b, 0.15)
        assert cols == [] and res == {{}}, (cols, res)
        E._is_panel_span = lambda fa: False
        cols, res = E.fr_report_grid(marked, b, 0.15)
        assert cols == [] and res == {{}}, (cols, res)
        print('OK')
    """)
    p = subprocess.run([sys.executable, "-c", body], capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip().endswith("OK"), p.stdout + p.stderr
