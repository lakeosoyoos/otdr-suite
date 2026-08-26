"""Launch/tail box presence detection (Robert 2026-07-21).

Real-world matrix validated: Span 7 quick shots = launch reel, NO tail box
(shot ends at a 1O range marker); WINNIL finals = NO launch box, tail box
present (tail far-end 1F sits AFTER the 1E EOL event); SEANOR = both.
When a tail box is absent the BAD_TAILBOX_REFL checks are suppressed for
that direction; the panel rows 'launch_box_detection' / 'tail_box_detection'
(both default ON) disable each half (assume that box present, no notes).

Engine tests run in a clean subprocess (3-engine sor_reader isolation): a
module-level `import splicereportmatchexfo` here resolves viewer/'s
deliberately divergent sor_reader324802a whenever another test module has
already put viewer/ on sys.path, and dies at the engine's import line.
The source-level tests below need no engine and read the files directly.
"""
import os
import subprocess
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SPLICEREPORT_DIR = os.path.join(ROOT, 'splicereport')

# Pre-dedented (top-level) so it composes with a separately-dedented test
# body — concatenating two indented blocks and dedenting once would leave the
# body nested inside the helper and silently skip every assert.
_FIBER_HELPERS = textwrap.dedent("""
    def _f(evs):
        return {0: {'events': evs}}

    def _ev(km, typ='0F9999LS', refl=0.0, end=False):
        return {'dist_km': km, 'splice_loss': 0.05, 'reflection': refl,
                'type': typ, 'is_reflective': typ.startswith('1'),
                'is_end': end}
""")


def _run(body):
    src = ("import sys\n"
           f"sys.path.insert(0, {SPLICEREPORT_DIR!r})\n"
           "import splicereportmatchexfo as E\n"
           + _FIBER_HELPERS + textwrap.dedent(body))
    p = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout


def test_quickshot_launch_no_tail():
    """Span 7 shape: launch reel, shot ends at a 1O range marker (no is_end),
    no tail box."""
    _run("""
        evs = [_ev(0.0, '1F9999LS', -68), _ev(1.01, '1F9999LS', -54),
               _ev(4.99, '1O9999LS')]
        r = E.detect_box_presence(_f(evs), {})['a']
        assert r['launch'] is True and r['tail'] is False, r
        print('OK')
    """)


def test_winnil_no_launch_tail_after_eol():
    """WINNIL shape: no launch reel, and the tail reel's far-end 1F sits
    AFTER the 1E EOL — so the tail zone must look BOTH sides of the end
    anchor."""
    _run("""
        evs = [_ev(0.0, '1F9999LS', -61), _ev(15.0), _ev(84.2),
               _ev(87.58, '1E9999LS', -55, end=True),
               _ev(88.62, '1F9999LS', -42)]
        r = E.detect_box_presence(_f(evs), {})['a']
        assert r['launch'] is False and r['tail'] is True, r
        print('OK')
    """)


def test_full_span_both_present():
    """SEANOR shape: both boxes in use."""
    _run("""
        evs = [_ev(0.0, '1F9999LS', -57), _ev(1.0, '1F9999LS', -54), _ev(50.0),
               _ev(107.5, '1F9999LS', -55), _ev(108.5, '1E9999LS', -56, end=True)]
        r = E.detect_box_presence(_f(evs), {})['a']
        assert r['launch'] is True and r['tail'] is True, r
        print('OK')
    """)


def test_tailbox_suppression_accepts_pair():
    src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    assert 'isinstance(_tb, (tuple, list))' in src


def test_runner_wires_detection_and_manifest():
    src = open(os.path.join(ROOT, 'splicereport', 'run_splicereport.py'),
               encoding='utf-8').read()
    assert 'detect_box_presence' in src
    assert "'box_detection': box_info" in src
    assert 'no tail box in use' in src


def test_two_independent_panel_rows():
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    assert '"launch_box_detection",      "Launch box detection"' in src
    assert '"tail_box_detection",        "Tail box detection"' in src
    assert '"launch_box_detection": "LAUNCH_BOX_DETECTION"' in src
    assert '"tail_box_detection":   "TAIL_BOX_DETECTION"' in src
    assert '"launch_box_detection": 0.0' in src
    assert '"tail_box_detection": 0.0' in src


def test_runner_gates_each_switch_independently():
    src = open(os.path.join(ROOT, 'splicereport', 'run_splicereport.py'),
               encoding='utf-8').read()
    assert "getattr(E, 'LAUNCH_BOX_DETECTION', True)" in src
    assert "getattr(E, 'TAIL_BOX_DETECTION', True)" in src
    # a disabled switch forces that box 'present' (no note, no suppression)
    assert "box_info[_d]['launch'] = True" in src
    assert "box_info[_d]['tail'] = True" in src


def test_report_header_notes_wired():
    eng = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    assert 'no A tail box in use' in eng          # xlsx last-column note
    assert 'no A launch box' in eng               # xlsx first-column note
    app = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    assert 'no A tail box in use' in app          # in-app grid note
    assert '_box_note' in app
