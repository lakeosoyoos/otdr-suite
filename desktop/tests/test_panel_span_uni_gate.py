"""On a panel tie between reels the single-direction connector gate stands down.

Redrock RDR4<->RDR6, 2026-09-16.  144 fibers, a 31 m tie between two panels
shot through 1 km launch and receive reels.  The team's OOS note for the span
is five lines: four dead fibers and "F6 .640" — the (A+B)/2 at the RDR4
connector.  Our report agreed on all five and then added 83 more: the far
connector reads +0.65..+0.89 from A against -0.25 from B on every fiber
(launch-reel glass against tie glass — a backscatter step, not loss; the
pair averages ~0.20), and the uni gate wrote 82 of them into the B-end ILA
column plus F69 at the A end.

The field asked for the 0.65 uni gate "on long traces".  A tie between panels
is not one.  Below PANEL_SPAN_MAX_KM the uni gate is off; the pair gates and
the connector column still grade the connector, which is how F6 is reported.

Fixture: Redrock West fibers 1, 6 and 11, both directions.  F6 is the team's
finding; F11 (A +0.734 / B -0.292 at the far connector) is the artifact.
Engine runs in a clean subprocess (3-engine sor_reader isolation).
"""
from __future__ import annotations

import re

import openpyxl

from conftest import run_splicereport, FIXTURE_DIR

PANEL_A = FIXTURE_DIR / "panelreels" / "A"
PANEL_B = FIXTURE_DIR / "panelreels" / "B"


def _grid(tmp_path):
    out = tmp_path / "panelreels.xlsx"
    rc, m, stderr = run_splicereport(PANEL_A, PANEL_B, out, "RDR4", "RDR6")
    assert rc == 0 and m and m.get("ok"), f"runner failed: {stderr[-1200:]}"
    ws = openpyxl.load_workbook(out)["Splice Report"]
    hdr = list(next(ws.iter_rows(min_row=3, max_row=3, values_only=True)))
    ila = {}
    for ci, h in enumerate(hdr, 1):
        if h and "ILA" in str(h):
            ila[str(h)] = " ".join(str(ws.cell(r, ci).value or "")
                                   for r in range(4, ws.max_row + 1))
    assert len(ila) == 2, hdr
    return m, ila


def test_fixture_is_a_panel_tie_between_reels(tmp_path):
    """Premise guard: reels normalized away, 31 m of span, no closures."""
    m, _ = _grid(tmp_path)
    assert m["n_splices"] == 0
    assert 0 < float(m["span_km"]) < 0.1, m["span_km"]
    assert [c["kind"] for c in m["columns"]] == ["connector", "section", "connector"]


def test_uni_gate_is_silent_on_a_panel_span(tmp_path):
    """THE regression.  Before: 'B-end ILA: RDR6' read
    '6 .68 LAUNCH A side 11 .73 LAUNCH A side'."""
    _, ila = _grid(tmp_path)
    for name, text in ila.items():
        assert not re.search(r"LAUNCH [AB] side", text), (name, text)


def test_the_teams_finding_still_prints(tmp_path):
    """F6 is graded on the pair — .640 at the RDR4 connector — and stays
    flagged in the connector column.  F1 and F11 are clean."""
    m, _ = _grid(tmp_path)
    flagged = {(c["fiber"], c["splice"]): c for c in m["cells"] if c["is_flagged"]}
    assert set(f for f, _ in flagged) == {6}, sorted(flagged)
    cell = flagged[(6, 0)]
    assert abs(float(cell["loss"]) - 0.640) < 0.0015, cell
    assert cell["label"].startswith("6 .640"), cell["label"]


def test_gate_still_fires_on_a_cable():
    """Scope guard: the field's 'on long traces' request is untouched.  A
    60 km span with a one-way .78 connector still flags via the uni gate
    (this is test_b_end_connector's F939 shape, run against the
    PANEL_SPAN_MAX_KM constant directly)."""
    import subprocess
    import sys
    import textwrap
    from conftest import REPO_ROOT
    body = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(REPO_ROOT / 'splicereport')!r})
        import splicereportmatchexfo as E
        def rec(own, far, span):
            return {{'events': [
                {{'dist_km': 0.0, 'splice_loss': 0.0, 'is_end': False,
                  'is_reflective': True, 'time_of_travel': 0, 'type': '1F9999LS'}},
                {{'dist_km': 1.01, 'splice_loss': own, 'is_end': False,
                  'is_reflective': True, 'time_of_travel': 5000, 'type': '1F9999LS'}},
                {{'dist_km': span - 1.01, 'splice_loss': far, 'is_end': False,
                  'is_reflective': True, 'type': '1F9999LS'}},
                {{'dist_km': span, 'splice_loss': 0.0, 'is_end': True,
                  'is_reflective': True, 'type': '1E9999LS'}},
            ]}}
        assert E.PANEL_SPAN_MAX_KM == 1.0
        assert not E._is_panel_span({{1: rec(0.05, 0.22, 60.0)}})
        assert E._is_panel_span({{1: rec(0.05, 0.22, 0.031)}})
        # three dead fibers inside the tie do not make a cable look like a tie
        long = {{i: rec(0.05, 0.22, 60.0) for i in range(1, 9)}}
        for i in (2, 5, 7):
            long[i]['events'][-1]['dist_km'] = 0.02
        assert not E._is_panel_span(long)
        issues = E.detect_launch_issues({{1: rec(0.05, 0.22, 60.0)}},
                                        {{1: rec(0.78, 0.05, 60.0)}})
        b = list((issues.get(1) or {{}}).get('b_tags') or [])
        assert any('LAUNCH' in t for t in b), b
        print('OK')
    """)
    p = subprocess.run([sys.executable, "-c", body], capture_output=True, text=True)
    assert p.returncode == 0, f"{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK"
