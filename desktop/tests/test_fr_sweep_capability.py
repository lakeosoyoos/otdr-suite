"""Glass-sweep discovery — the capability FastReporter structurally cannot match.

FR prints what the FIRMWARE detected at acquisition time: it reads its event
values out of `ExfoNewProprietaryBlock`, not the samples.  Proven here by
controlled poke: a +0.5 dB step injected into the DataPts is PLOTTED by FR
while its event table still prints the stored value, and "Analyze OTDR
Measurement" reproduces the stored tables exactly on real traces.  So a gentle
loss the unit's detector missed is invisible to FR permanently.

`fr_sweep_pass` re-measures the raw backscatter instead.  Per A fiber it
sweeps the trace for real loss steps, discards anything a stored table already
owns, then demands TWO independent confirmations before flagging: a near-field
raw-median measure reaching FR_SWEEP_CONFIRM_FRAC of the swept step (kills
two-window side lobes), AND the B-direction mirror showing the same feature at
the mirrored position within FR_SWEEP_MIRROR_TOL_KM, with its own near-field
measure.  On the 2026-08-15 PLACHE 1152 shoot this produced 116 of 122 flagged
cells — i.e. nearly the whole report was glass discoveries FR cannot produce.

These tests exist because that capability is easy to erode silently: any
change to the clustering/attribution radii can reroute a sweep discovery into
a splice column, where the higher reburn gate unflags it (observed on
Seattle↔North Bend when the fold radius grew to the pulse width — see
test_sweep_inside_pulse_smear_is_judged_at_the_splice_gate, which pins that
tradeoff as deliberate rather than accidental).

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"

# Pre-dedented so it composes with a separately-dedented test body: dedenting
# the concatenation once would leave the body nested inside rec()'s block and
# silently skip every assert.
_HELPERS = textwrap.dedent("""
    import numpy as np
    SP = 5e-08
    RES = 299_792_458.0 * SP / 2.0 / 1.468 / 1000.0      # km per sample

    def rec(step_km, step_db, eof_km=60.0, launch_km=1.0, stored=()):
        '''A synthetic record whose TRACE carries a real loss step.  Anything
        passed in `stored` also appears in the event table; the step itself
        does NOT unless you put it there — that is the FR-blind case.'''
        n = int(eof_km / RES) + 400
        x = np.arange(n) * RES
        y = 10.0 + 0.19 * x                  # accumulated dB, ascending
        if step_db:
            y[x >= step_km] += step_db
        y[x >= eof_km] = 63.0                # past end-of-fiber
        evs = [{'dist_km': 0.0, 'splice_loss': 0.0, 'is_end': False,
                'is_reflective': True, 'type': '1F9999LS'},
               {'dist_km': launch_km, 'splice_loss': 0.2, 'is_end': False,
                'is_reflective': True, 'type': '1F9999LS'}]
        for k, l in stored:
            evs.append({'dist_km': k, 'splice_loss': l, 'is_end': False,
                        'is_reflective': False, 'type': '0F9999LS',
                        'tot_start_curr': 0, 'tot_end_curr': 0})
        evs.append({'dist_km': eof_km, 'splice_loss': 0.0, 'is_end': True,
                    'is_reflective': True, 'type': '1E9999LS'})
        return {'trace': y, 'events': evs, '_raw_events': evs,
                'exfo_sampling_period': SP, '_trace_offset_km': 0.0}

    # The B mirror of an A-frame position: launch_b + eof_a - d.
    def mirror(d, eof_a=60.0, launch_b=1.0):
        return launch_b + eof_a - d
""")


def _run(body, splices=False):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n"
              "E.FR_MODE = True\n")
    # Each block is dedented on its own and only THEN concatenated; dedenting
    # the concatenation would find a 0-space common prefix and leave the test
    # body indented, which Python parses as part of the preceding block.
    src = header + _HELPERS + (_SPLICES if splices else "") + textwrap.dedent(body)
    p = subprocess.run([sys.executable, "-c", src],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout


_SPLICES = textwrap.dedent("""
    splices = [{'position_km': 12.0, 'position_km_refined': 12.0,
                'column_kind': 'splice'},
               {'position_km': 45.0, 'position_km_refined': 45.0,
                'column_kind': 'splice'}]
""")


def test_glass_only_loss_is_discovered():
    """THE capability: a 0.26 dB loss present in BOTH directions' glass and in
    NEITHER stored table is found, bidirectionally confirmed, and flagged as a
    'glass' cell.  FR shows nothing here — no stored event, no event row."""
    _run(splices=True, body="""
        A = rec(30.0, 0.26, stored=[(12.0, 0.08), (45.0, 0.07)])
        B = rec(mirror(30.0), 0.26, stored=[(16.0, 0.08), (44.0, 0.07)])
        out = E.fr_sweep_pass({1: A}, {1: B}, splices, {}, 60.0)
        assert len(out) == 1, out
        cell = list(out.values())[0]
        assert cell['event_source'] == 'sweep', cell
        assert cell['is_flagged'] is True, cell
        assert cell['event_type'] == 'GLASS', cell
        assert abs(cell['bidir_loss'] - 0.26) < 0.03, cell
        assert abs(cell['bidir_dist'] - 30.0) < 0.05, cell
        assert 'glass' in cell['label'], cell
        print('OK')
    """)


def test_stored_table_event_is_not_rediscovered():
    """Additive only: when the A table already marks the loss, the sweep must
    not manufacture a duplicate cell (Phases 1-2 own table-driven events)."""
    _run(splices=True, body="""
        A = rec(30.0, 0.26, stored=[(12.0, 0.08), (30.0, 0.26), (45.0, 0.07)])
        B = rec(mirror(30.0), 0.26, stored=[(16.0, 0.08), (44.0, 0.07)])
        assert E.fr_sweep_pass({1: A}, {1: B}, splices, {}, 60.0) == {}
        print('OK')
    """)


def test_single_direction_step_is_rejected():
    """One-sided evidence is not enough: without the B-direction mirror the
    candidate is dropped, so trace artifacts cannot become flags."""
    _run(splices=True, body="""
        A = rec(30.0, 0.26, stored=[(12.0, 0.08), (45.0, 0.07)])
        B = rec(0, 0.0, stored=[(16.0, 0.08), (44.0, 0.07)])   # clean B glass
        assert E.fr_sweep_pass({1: A}, {1: B}, splices, {}, 60.0) == {}
        print('OK')
    """)


def test_existing_cell_is_never_overwritten():
    """A discovery that collides with a cell an earlier pass already produced
    is skipped — the sweep only ADDS."""
    _run(splices=True, body="""
        A = rec(30.0, 0.26, stored=[(12.0, 0.08), (45.0, 0.07)])
        B = rec(mirror(30.0), 0.26, stored=[(16.0, 0.08), (44.0, 0.07)])
        probe = E.fr_sweep_pass({1: A}, {1: B}, splices, {}, 60.0)
        key = list(probe)[0]
        blocked = E.fr_sweep_pass({1: A}, {1: B}, splices, {key: {'x': 1}}, 60.0)
        assert blocked == {}, blocked
        print('OK')
    """)


def test_sweep_inside_pulse_smear_is_judged_at_the_splice_gate():
    """DELIBERATE tradeoff, pinned so it cannot change silently.

    A discovery closer to a splice column than the instrument can resolve IS
    that splice's own loss, so it is judged by REBURN_THRESHOLD instead of the
    bend gate.  A 0.10 dB discovery 210 m from a column therefore does NOT
    flag once the fold radius carries the 2500 ns pulse floor (~255 m), while
    the same loss far from any column still flags as a bend.  This is what
    unflagged two Seattle-North Bend sweep cells; it is correct, but it is the
    mechanism by which glass discoveries can vanish, so it is asserted here.
    """
    _run("""
        splices = [{'position_km': 30.0, 'position_km_refined': 30.0,
                    'column_kind': 'splice'}]
        E._RUN_PULSE_SMEAR_KM = 0.2553          # 2500 ns
        # 210 m from the column, below the reburn gate -> folded, not flagged
        A = rec(30.21, 0.10, stored=[(12.0, 0.08)])
        B = rec(mirror(30.21), 0.10, stored=[(16.0, 0.08)])
        assert E.fr_sweep_pass({1: A}, {1: B}, splices, {}, 60.0) == {}

        # same loss 3 km away from any column -> off-splice, flags as a bend
        far = [{'position_km': 12.0, 'position_km_refined': 12.0,
                'column_kind': 'splice'}]
        A2 = rec(30.0, 0.10, stored=[(12.0, 0.08)])
        B2 = rec(mirror(30.0), 0.10, stored=[(16.0, 0.08)])
        out = E.fr_sweep_pass({1: A2}, {1: B2}, far, {}, 60.0)
        assert len(out) == 1, out
        assert list(out.values())[0]['is_bend'] is True, out
        print('OK')
    """)
