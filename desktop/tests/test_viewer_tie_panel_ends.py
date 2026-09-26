"""The Viewer's end connectors take the report's verdict.

The report's launch / tailbox reflectance rule is population-based (the panel
span decision, each direction's median), so a handful of fibres in the Viewer
cannot reproduce it.  The run hands its verdicts over in the manifest, and the
Viewer marks the one reading each verdict is about.

Ground truth: Redrock East (RDR4<->RDR5, 31 m tie between 1 km reels), tech-
confirmed 2026-09-17: F74 -49.8 and F126 -47.0 fail on the B shot at RDR4; F2
is clean.  The single-direction connector gate is off on a panel span, so the
0.53-0.72 one-sided steps at the panels (reel/tie backscatter mismatch) pass.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
EAST = os.path.join(HERE, 'fixtures', 'panelreels_east')
SRC = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))


def _fn(name):
    i = SRC.index('function ' + name + '(')
    return SRC[i:SRC.index('\n}\n', i) + 3]


def test_a_verdict_is_filed_under_the_direction_that_read_it():
    from run_splicereport import _end_refl_verdicts
    issues = {7: {'a_tags': ['REFL-45.0dB', 'REFL-48.0dB'], 'b_tags': ['REFL-47.0dB'],
                  'refl_rules': {'A': ['launch', 'tailbox'], 'B': ['tailbox']}},
              9: {'a_tags': ['HIGH_LAUNCH_LOSS'], 'b_tags': [],
                  'refl_rules': {'A': [], 'B': []}}}
    assert _end_refl_verdicts(issues) == [
        {'fiber': 7, 'dir': 'A', 'refl': -45.0},   # A's own launch
        {'fiber': 7, 'dir': 'B', 'refl': -48.0},   # B's far end, AT end A
        {'fiber': 7, 'dir': 'A', 'refl': -47.0},   # A's far end, AT end B
    ]
    assert _end_refl_verdicts(None) == []


def test_redrock_east_run_hands_over_the_tech_s_fails(tmp_path):
    out = subprocess.run(
        [sys.executable, os.path.join(ROOT, 'splicereport', 'run_splicereport.py'),
         '--dir-a', os.path.join(EAST, 'A'), '--dir-b', os.path.join(EAST, 'B'),
         '--out', str(tmp_path / 'east.xlsx')],
        capture_output=True, text=True, timeout=600).stdout
    man = json.loads(out.strip().splitlines()[-1])
    assert man['ok'], man
    assert man['panel_span'] is True
    assert sorted((v['fiber'], v['dir'], v['refl']) for v in man['end_refl']) == [
        (74, 'B', -49.8), (126, 'B', -47.0)]
    assert man['thresholds']['LAUNCH_CONN_UNI_MIN_DB'] == 0.649


def test_the_server_passes_the_verdicts_and_the_panel_decision():
    srv = open(os.path.join(ROOT, 'viewer', 'trace_server.py'), encoding='utf-8').read()
    assert "'end_refl': CONFIG.get('end_refl')," in srv
    assert "'panel_span': CONFIG.get('panel_span')," in srv
    assert "'connector_uni': 'LAUNCH_CONN_UNI_MIN_DB'" in srv
    app = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    assert app.count("trace_server.set_end_refl(res.get('end_refl'))") == 2
    assert app.count("trace_server.set_panel_span(res.get('panel_span'))") == 2


def test_the_viewer_marks_the_nearest_reading_and_judges_in_the_report_frame():
    body = _fn('paintFrBidiGrid')
    assert "if (endBad[which].has(x)) return true;" in body
    assert "let best = null, bestD = 0.5;" in body
    assert "reflFails(leg.refl, leg.pos_m / 1000 - own," in body
    assert "eof != null ? eof - own - far : eof);" in body
    gate = _fn('gateFor')
    assert "(gInfo && gInfo.panel_span) || !(g > 0) ? Infinity : g;" in gate
