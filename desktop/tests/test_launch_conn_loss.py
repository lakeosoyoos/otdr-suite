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
        assert E.LAUNCH_CONN_UNI_MIN_DB == 0.65, E.LAUNCH_CONN_UNI_MIN_DB
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


def test_single_direction_failure_is_no_longer_masked():
    """THE change.  A purely bidirectional gate cannot see a one-sided
    failure: one direction reading as a gainer drags the pair under the line
    however bad the other is.  Field report: "If bidi passes it won't flag uni
    that are failing above .65 ... This is why Denver was missed."

    Measured on Defuniak connector X, 144 pairs at 0.65: min flags 0 and the
    average flags 0, yet F34 reads B=1.090 and F98 B=1.108 dB."""
    _run(_FIXTURE, """
        # F34-shaped: bidi min 0.188, average 0.639 — both under 0.65.
        assert issues({34: (0.188, 1.090)})[34]['a_tags'] == ['1.09 LAUNCH 1-WAY B']
        # mirror orientation must behave identically
        assert issues({7: (1.090, 0.188)})[7]['a_tags'] == ['1.09 LAUNCH 1-WAY A']
        print('OK')
    """)


def test_bkfdel_f402_now_flags_and_that_is_accepted():
    """Known, accepted cost of the bare single-direction threshold.  F402
    reads A=0.766 with a healthy B=0.499; the old bidirectional-only gate
    excluded it and the reviewer did not hand-type it.  Under the uni gate it
    flags.  The field chose a bare threshold over a population-relative one
    with this trade-off on the table."""
    _run(_FIXTURE, """
        assert issues({402: (0.766, 0.499)})[402]['a_tags'] == ['.76 LAUNCH 1-WAY A']
        print('OK')
    """)


def test_gate_boundary_inclusive():
    """Both gates are inclusive at their threshold, and a pair under BOTH
    stays silent."""
    _run(_FIXTURE, """
        assert issues({1: (0.65, 0.65)})[1]['a_tags'] == ['.65 LAUNCH']   # bidi
        assert issues({2: (0.619, 0.619)}) == {}                          # neither
        assert issues({3: (0.60, 0.66)})[3]['a_tags'] == ['.66 LAUNCH 1-WAY B']
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


def test_zero_threshold_turns_off_that_gate_and_only_that_gate():
    """The panel's unticked value (0.0) must disable the check it belongs to,
    not flag every fiber — the gate is a MINIMUM, so 0.0 would otherwise
    catch all.

    It must also not disable the OTHER gates.  This test used to assert that
    zeroing the min gate silenced the connector check entirely, which is what
    the code did: the loop's guard tested the min gate alone, so unticking
    'connector loss (bidirectional)' in the settings panel switched off the
    1-direction gate with it.  One knob must not turn off another."""
    _run(_FIXTURE, """
        # min gate off, uni gate still on -> the uni gate still speaks
        E.LAUNCH_CONN_LOSS_MIN_DB = 0.0
        assert issues({118: (0.763, 0.716)})[118]['a_tags'] == ['.76 LAUNCH 1-WAY A']
        # …and a pair under the uni gate stays silent
        assert issues({1: (0.42, 0.30)}) == {}

        # every gate off -> nothing at all
        E.LAUNCH_CONN_UNI_MIN_DB = 0.0
        E.LAUNCH_CONN_AVG_MIN_DB = 0.0
        assert issues({118: (0.763, 0.716), 1: (0.42, 0.30)}) == {}

        E.LAUNCH_CONN_LOSS_MIN_DB, E.LAUNCH_CONN_UNI_MIN_DB = 0.62, 0.65
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
def test_panel_rows_live_in_the_connector_knobs_panel():
    """The two gates moved out of the EXFO threshold table into the
    'Connector & launch' knobs panel, which carries help text and holds the
    rest of the connector path beside them.  They must be there exactly once,
    at the engine's own defaults, and reach the right globals."""
    rows = {r["key"]: r for r in hub._CONN_ROWS}
    assert rows["conn_bidi"]["globals"] == {"value": "LAUNCH_CONN_LOSS_MIN_DB"}
    assert rows["conn_bidi"]["defaults"]["value"] == 0.620
    assert rows["conn_uni"]["globals"] == {"value": "LAUNCH_CONN_UNI_MIN_DB"}
    assert rows["conn_uni"]["defaults"]["value"] == 0.650
    assert rows["conn_bidi"]["label"] == "Connector loss (bidirectional)"
    assert rows["conn_uni"]["label"] == "Connector loss (1 direction)"

    # …and they are NOT still in the EXFO table, or two controls would write
    # one global and whichever rendered last would win.
    assert not any(r[0].startswith("launch_conn") for r in hub.OTDR_ROWS)
    assert "LAUNCH_CONN_LOSS_MIN_DB" not in hub._OTDR_KEY_TO_ENGINE_GLOBAL.values()
    assert "LAUNCH_CONN_UNI_MIN_DB" not in hub._OTDR_KEY_TO_ENGINE_GLOBAL.values()


def test_connector_knob_defaults_are_the_engine_defaults():
    """Out of the box the hub must match the CLI / engine, so an untouched
    run through the panel is the run the engine would have done alone."""
    assert hub._CONN_DEFAULTS["LAUNCH_CONN_LOSS_MIN_DB"] == 0.620
    assert hub._CONN_DEFAULTS["LAUNCH_CONN_UNI_MIN_DB"] == 0.650
    # 0.0 is the engine's explicit "off" for these gates — never the 1e9
    # sentinel, which would show the tech a nonsense number in the panel.
    assert hub._CONN_ROWS[0]["min"] == 0.0


def test_engine_global_exists_for_the_runner_hasattr_check():
    """run_splicereport applies overrides only for globals that already exist
    on the engine — a rename would silently no-op the panel row."""
    eng = (SPLICEREPORT_DIR / "splicereportmatchexfo.py").read_text(encoding="utf-8")
    assert "\nLAUNCH_CONN_LOSS_MIN_DB" in eng, "engine global renamed/removed"
    assert "\nLAUNCH_CONN_CONFIRM_TOL_DB" in eng, "engine global renamed/removed"
    assert "\nLAUNCH_CONN_UNI_MIN_DB" in eng, "uni gate global missing"
    # Two INDEPENDENT gates — either firing flags the fiber.
    assert "min(a_loss, b_loss) >= LAUNCH_CONN_LOSS_MIN_DB" in eng
    assert "max(a_loss, b_loss) >= LAUNCH_CONN_UNI_MIN_DB" in eng
    assert "_bidi_fires or _uni_fires" in eng
    # Truncated 2-dp display, not rounded — now on whichever value fired.
    assert "math.floor(shown * 100) / 100.0" in eng
    assert "' LAUNCH 1-WAY '" in eng


# ── Panel/engine drift lock ─────────────────────────────────────────────────

def test_panel_defaults_match_the_engine_for_every_ticked_row():
    """A ticked panel row sends its value to the engine on EVERY run, so a
    panel default that disagrees with the engine SILENTLY OVERRIDES it — the
    engine constant becomes dead code and a threshold change never reaches a
    report.  That is exactly what happened when LAUNCH_CONN_LOSS_MIN_DB moved
    to 0.65 while the panel row stayed at 0.62.

    Engine constants are read from SOURCE, not imported: other test modules
    put viewer/ on sys.path, and that directory carries its own deliberately
    divergent sor_reader324802a, so importing the engine here resolves the
    wrong copy and dies at its import line.
    """
    import ast
    import re

    app_src = (SPLICEREPORT_DIR.parent / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(app_src)

    def lit(name):
        return next(ast.literal_eval(n.value) for n in ast.walk(tree)
                    if isinstance(n, ast.Assign)
                    and any(getattr(t, 'id', '') == name for t in n.targets))

    eng_src = (SPLICEREPORT_DIR / "splicereportmatchexfo.py").read_text(encoding="utf-8")

    def engine_const(name):
        m = re.search(rf'^{name}\s*=\s*(-?[\d.]+)', eng_src, re.M)
        assert m, f'{name} not found in the engine'
        return float(m.group(1))

    rows = lit("OTDR_ROWS")
    ticked = lit("OTDR_DEFAULT_APPLY")
    to_global = lit("_OTDR_KEY_TO_ENGINE_GLOBAL")
    to_warn = lit("_OTDR_KEY_TO_WARN_GLOBAL")
    warn_default = lit("_OTDR_WARN_DEFAULT")

    drift = []
    for key, _label, fail, _unit, _sup in rows:
        if key not in ticked or key not in to_global:
            continue
        eng = engine_const(to_global[key])
        if float(fail) != eng:
            drift.append((key, 'fail', fail, eng))
        if key in to_warn:
            w = float(warn_default.get(key, fail))
            ew = engine_const(to_warn[key])
            if w != ew:
                drift.append((key, 'warning', w, ew))
    assert not drift, f"panel default silently overrides the engine: {drift}"


def test_printed_number_is_the_one_that_fired():
    """A flag whose own number is below the threshold reads as a bug.  The
    bidirectional case keeps FastReporter's convention (truncated average,
    what the reviewer hand-types); the single-direction case prints the
    failing direction and says which, because there the average PASSED."""
    _run(_FIXTURE, """
        # bidi gate fires -> truncated bidirectional average, unchanged
        assert issues({118: (0.763, 0.716)})[118]['a_tags'] == ['.73 LAUNCH']
        # uni gate only -> the failing direction, marked, with its side
        assert issues({34: (0.188, 1.090)})[34]['a_tags'] == ['1.09 LAUNCH 1-WAY B']
        assert issues({7:  (1.090, 0.188)})[7]['a_tags']  == ['1.09 LAUNCH 1-WAY A']
        print('OK')
    """)
