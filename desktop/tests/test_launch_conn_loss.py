"""Launch-connector high-loss flag — badly-mated connector at the A panel.

BKF↔DEL (432 fibers, 2026-07): three fibers were coupled badly at the BKF
panel.  The loss sits in the stored launch-connector KeyEvent — first 1F at
the launch offset in the A file, last 1F before EOF in the B file (B shoots
back through the same connector one launch-reel length from its far end).
The engine read that event for reflectance and distance only; its loss was
never consulted (LAUNCH_HIGH_LOSS_DB is None), so a connector that mates
badly but still reflects cleanly was invisible.

Why the gate is min(A, B) and not A: every mated connector costs real loss
(BKF↔DEL median 0.42 dB, 405/432 over 0.3), so ranking on A alone false-fires
— F402 reads A=0.766, ABOVE F118's 0.763, on a healthy 0.499 dB B side.  The
bidirectional minimum separates cleanly: 0.716 / 0.690 / 0.645 for the three
bad fibers, 0.587 for the next one.

Cell text is the FastReporter display convention the reviewer hand-typed:
truncated (not rounded) to 2 dp with the leading zero dropped — "118 .73
LAUNCH".  Severity HIGH.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

import app as hub

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def _run(*bodies):
    """Run source blocks against a freshly imported engine.  Each block is
    dedented on its own (the shared fixture and the per-test body are written
    at different indents)."""
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    src = header + "".join(textwrap.dedent(b) for b in bodies)
    p = subprocess.run([sys.executable, "-c", src],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert (p.stdout.strip().splitlines() or [""])[-1] == "OK", p.stdout


# Synthetic A/B pair carrying ONLY a launch-connector event, so the tag under
# test is the only one detect_launch_issues can emit (spans_have_tailbox=False
# skips the tailbox + duration blocks; a non-reflective events[0] keeps the
# launch-reflectance rule out of it).
_FIXTURE = """
    A_OFF, B_BACK, EOF_B = 1.03, 1.08, 80.29

    def _a(loss):
        raw = [{'dist_km': 0.0, 'time_of_travel': 0, 'is_reflective': True,
                'is_end': False, 'type': '1F', 'splice_loss': 0.0,
                'reflection': -60.0},
               {'dist_km': A_OFF, 'time_of_travel': 10000, 'is_reflective': True,
                'is_end': False, 'type': '1F', 'splice_loss': loss,
                'reflection': -53.0},
               {'dist_km': 40.0, 'time_of_travel': 400000, 'is_reflective': False,
                'is_end': False, 'type': '0F', 'splice_loss': 0.05,
                'reflection': 0.0},
               {'dist_km': 79.2, 'time_of_travel': 800000, 'is_reflective': False,
                'is_end': True, 'type': '1E', 'splice_loss': 0.0,
                'reflection': -45.0}]
        return {'_raw_events': raw, 'duration_sec': 15.0, 'trace': None,
                'events': [{'dist_km': 0.0, 'is_reflective': False, 'is_end': False,
                            'splice_loss': 0.0, 'reflection': 0.0},
                           {'dist_km': 78.17, 'is_reflective': False, 'is_end': True,
                            'splice_loss': 0.0, 'reflection': -45.0}]}

    def _b(loss, back=None, reflective=True):
        back = B_BACK if back is None else back
        raw = [{'dist_km': 0.0, 'time_of_travel': 0, 'is_reflective': True,
                'is_end': False, 'type': '1F', 'splice_loss': 0.0,
                'reflection': -60.0},
               {'dist_km': 1.02, 'time_of_travel': 10000, 'is_reflective': True,
                'is_end': False, 'type': '1F', 'splice_loss': 0.21,
                'reflection': -53.0},
               {'dist_km': EOF_B - back, 'time_of_travel': 780000,
                'is_reflective': reflective, 'is_end': False,
                'type': '1F' if reflective else '0F', 'splice_loss': loss,
                'reflection': -53.0},
               {'dist_km': EOF_B, 'time_of_travel': 800000, 'is_reflective': False,
                'is_end': True, 'type': '1E', 'splice_loss': 0.0,
                'reflection': -45.0}]
        return {'_raw_events': raw, 'duration_sec': 15.0, 'trace': None,
                'events': [{'dist_km': 0.0, 'is_reflective': False, 'is_end': False,
                            'splice_loss': 0.0, 'reflection': 0.0},
                           {'dist_km': 78.17, 'is_reflective': False, 'is_end': True,
                            'splice_loss': 0.0, 'reflection': -45.0}]}

    def issues(pairs):
        fa = {f: _a(a) for f, (a, b) in pairs.items()}
        fb = {}
        for f, (a, b) in pairs.items():
            fb[f] = _b(b) if not isinstance(b, tuple) else _b(*b)
        return E.detect_launch_issues(fa, fb, spans_have_tailbox=False)
"""


def test_constants_locked():
    _run(_FIXTURE, """
        assert E.LAUNCH_CONN_LOSS_MIN_DB == 0.62, E.LAUNCH_CONN_LOSS_MIN_DB
        assert E.LAUNCH_CONN_CONFIRM_TOL_DB == 0.05, E.LAUNCH_CONN_CONFIRM_TOL_DB
        print('OK')
    """)


def test_bkfdel_three_fibers_flag_with_truncated_bidir_text():
    """The shipped ground truth: the boss's reviewer hand-typed 118 .73,
    121 .74, 426 .68 — (A+B)/2 TRUNCATED to 2 dp, leading dot."""
    _run(_FIXTURE, """
        out = issues({118: (0.763, 0.716),
                      121: (0.800, 0.690),
                      426: (0.645, 0.719)})
        got = {f: v['a_tags'] for f, v in out.items()}
        assert got == {118: ['.73 LAUNCH'],
                       121: ['.74 LAUNCH'],
                       426: ['.68 LAUNCH']}, got
        assert all(v['severity'] == 'HIGH' for v in out.values()), out
        assert all(v['b_tags'] == [] for v in out.values()), out
        print('OK')
    """)


def test_gate_is_bidirectional_minimum_not_a_side():
    """BKF↔DEL F402: A=0.766 is the HIGHEST A reading on the span — above
    F118's 0.763 — but its B side is a healthy 0.499.  Ranking on A alone
    flags it; the minimum must not."""
    _run(_FIXTURE, """
        assert issues({402: (0.766, 0.499)}) == {}
        # ...and the mirror case: a big B with a healthy A is equally silent.
        assert issues({7: (0.499, 0.766)}) == {}
        print('OK')
    """)


def test_gate_boundary_inclusive():
    _run(_FIXTURE, """
        assert issues({1: (0.62, 0.62)})[1]['a_tags'] == ['.62 LAUNCH']
        assert issues({1: (0.619, 0.90)}) == {}
        assert issues({1: (0.90, 0.619)}) == {}
        print('OK')
    """)


def test_truncates_never_rounds():
    """0.7495 displays .74, not .75 — FastReporter truncates."""
    _run(_FIXTURE, """
        assert issues({1: (0.800, 0.699)})[1]['a_tags'] == ['.74 LAUNCH']
        print('OK')
    """)


def test_no_single_direction_fallback():
    """Both sides must exist.  A B file whose last pre-EOF event is at the
    wrong distance (its own tailbox, not A's launch connector) or isn't
    reflective yields NO flag, however bad the A reading."""
    _run(_FIXTURE, """
        # mirror 0.4 km back — outside the +/-0.3 km launch-reel window
        assert issues({1: (0.90, (0.90, 0.4))}) == {}
        # mirror at the right distance but non-reflective (a fusion splice)
        assert issues({1: (0.90, (0.90, None, False))}) == {}
        print('OK')
    """)


def test_confirm_pass_refutes_unreproducible_stored_loss():
    """Phantom-proofing: the flag is driven by a stored KeyEvents number, so
    the trace's own marker LSA must reproduce it within
    LAUNCH_CONN_CONFIRM_TOL_DB on BOTH sides."""
    _run(_FIXTURE, """
        # both sides reproduce (BKF/DEL agrees to ~0.003 dB) -> flags
        E.measure_grey_loss_from_sor_event = lambda r, e, **k: e['splice_loss'] + 0.003
        assert issues({118: (0.763, 0.716)})[118]['a_tags'] == ['.73 LAUNCH']

        # A side unreproducible -> no flag
        E.measure_grey_loss_from_sor_event = (
            lambda r, e, **k: 0.30 if e['dist_km'] < 2.0 else e['splice_loss'])
        assert issues({118: (0.763, 0.716)}) == {}

        # B side unreproducible -> no flag
        E.measure_grey_loss_from_sor_event = (
            lambda r, e, **k: 0.30 if e['dist_km'] > 2.0 else e['splice_loss'])
        assert issues({118: (0.763, 0.716)}) == {}

        # just outside tolerance on one side -> no flag; just inside -> flag
        E.measure_grey_loss_from_sor_event = lambda r, e, **k: e['splice_loss'] - 0.051
        assert issues({118: (0.763, 0.716)}) == {}
        E.measure_grey_loss_from_sor_event = lambda r, e, **k: e['splice_loss'] - 0.049
        assert issues({118: (0.763, 0.716)})[118]['a_tags'] == ['.73 LAUNCH']
        print('OK')
    """)


def test_confirm_fails_safe_when_unmeasurable():
    """No trace / no per-event markers -> treat as confirmed, the convention
    the other re-measure gates use.  Never hide a real defect because the
    confirmation could not run."""
    _run(_FIXTURE, """
        E.measure_grey_loss_from_sor_event = lambda r, e, **k: None
        assert issues({118: (0.763, 0.716)})[118]['a_tags'] == ['.73 LAUNCH']
        E.measure_grey_loss_from_sor_event = lambda r, e, **k: 1 / 0
        assert issues({118: (0.763, 0.716)})[118]['a_tags'] == ['.73 LAUNCH']
        print('OK')
    """)


def test_trimmed_span_is_inert():
    """Spans shot with start/stop already picked carry no launch-connector
    event (HOWLAN, Span 3 V2, ELMMIL, ...) — the gate can never fire."""
    _run(_FIXTURE, """
        fa = {1: _a(0.90)}
        fb = {1: _b(0.90)}
        fa[1]['_raw_events'] = fa[1]['_raw_events'][2:]   # launch pair stripped
        assert E.detect_launch_issues(fa, fb, spans_have_tailbox=False) == {}
        print('OK')
    """)


def test_zero_threshold_turns_it_off():
    """The panel's unticked value (0.0) must disable the check, not flag
    every fiber — the gate is a MINIMUM, so 0.0 would otherwise catch all."""
    _run(_FIXTURE, """
        E.LAUNCH_CONN_LOSS_MIN_DB = 0.0
        assert issues({118: (0.763, 0.716), 1: (0.42, 0.30)}) == {}
        E.LAUNCH_CONN_LOSS_MIN_DB = 0.62
        assert 118 in issues({118: (0.763, 0.716)})
        print('OK')
    """)


def test_threshold_read_at_call_time_for_overrides():
    """A run_splicereport --overrides setattr must change behavior."""
    _run(_FIXTURE, """
        E.LAUNCH_CONN_LOSS_MIN_DB = 0.50      # simulate the override setattr
        assert 87 in issues({87: (0.587, 0.597)})
        E.LAUNCH_CONN_LOSS_MIN_DB = 0.62
        assert issues({87: (0.587, 0.597)}) == {}
        print('OK')
    """)


def test_cell_text_survives_the_ribbon_writer_join():
    """build_ribbon_data renders '<fiber> <tag>' after stripping at '@'/'+',
    so the tag must contain neither.  Locks the exact ILA:A cell text."""
    _run(_FIXTURE, """
        li = {118: {'a_tags': ['.73 LAUNCH'], 'b_tags': [], 'severity': 'HIGH',
                    'summary': '118 LAUNCH(A) .73 LAUNCH'}}
        cells, lca, lcb = E.build_ribbon_data({}, 432, 12, 0, launch_issues=li)
        assert lca[9] == {'text': '118 .73 LAUNCH', 'severity': 'HIGH'}, lca
        assert lcb == {}, lcb
        print('OK')
    """)


# ── Panel plumbing ────────────────────────────────────────────────────────
def test_panel_row_maps_and_unchecked_sends_zero_not_sentinel():
    s = hub._otdr_settings_from_profile("Default (engine baseline)")
    ov = hub._overrides_from_settings(s)
    assert ov["LAUNCH_CONN_LOSS_MIN_DB"] == 0.620          # checked default

    s["launch_conn_loss"]["fail"] = 0.700                  # tech edit flows
    assert hub._overrides_from_settings(s)["LAUNCH_CONN_LOSS_MIN_DB"] == 0.700

    # Unchecked = 0.0 ("off" — the engine's explicit disable), never 1e9:
    # this gate is a MINIMUM, so the sentinel would also disable it but would
    # show the tech a nonsense number in the panel.
    s["launch_conn_loss"]["apply"] = False
    assert hub._overrides_from_settings(s)["LAUNCH_CONN_LOSS_MIN_DB"] == 0.0


def test_panel_row_present_and_supported():
    row = next(r for r in hub.OTDR_ROWS if r[0] == "launch_conn_loss")
    assert row[1] == "Launch connector loss", row
    assert row[2] == 0.620 and row[3] == "dB" and row[4] is True, row
    # Ticked out of the box, so the hub matches the CLI / engine default.
    assert "launch_conn_loss" in hub.OTDR_DEFAULT_APPLY
    assert hub._otdr_settings_from_profile(
        "Default (engine baseline)")["launch_conn_loss"]["apply"] is True


def test_engine_global_exists_for_the_runner_hasattr_check():
    """run_splicereport applies overrides only for globals that already exist
    on the engine — a rename would silently no-op the panel row."""
    eng = (SPLICEREPORT_DIR / "splicereportmatchexfo.py").read_text(encoding="utf-8")
    assert "\nLAUNCH_CONN_LOSS_MIN_DB" in eng, "engine global renamed/removed"
    assert "\nLAUNCH_CONN_CONFIRM_TOL_DB" in eng, "engine global renamed/removed"
    # The gate itself, in the shape the analysis above justifies.
    assert "min(a_loss, b_loss) >= LAUNCH_CONN_LOSS_MIN_DB" in eng
    # Truncated 2-dp display, not rounded.
    assert "math.floor(bidir * 100) / 100.0" in eng
