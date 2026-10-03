"""The Splice Report page's clickable grid colors a cell by its manifest
category, and 'reburn' (red) has to mean what the Excel's pink Reburn fill
means: the cell cleared the run's bidirectional gate.

run_splicereport._category used a fixed 0.160.  With a profile whose
bidirectional splice loss gate is 0.10 dB, a 24-fiber span flagged 12 cells;
the Excel filled all 12 pink, but the grid showed only the 3 at 0.160 or more
in red and the other 9 (0.108 to 0.159) black, like unremarkable events.

The category now asks the engine's own bidir gate (_clears_splice_threshold)
at the run's threshold, so the two cannot disagree.  Display only: nothing
here moves which cells flag, any number, or the workbook.

Namespace rule: the engine ships its own sor_reader324802a.py copy, so every
check runs in a clean child interpreter.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def _run(body: str):
    """Run `body` in a clean child interpreter with the runner as R and the
    engine as E; it must print OK last."""
    src = ("import sys\n"
           f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
           "import run_splicereport as R\n"
           "import splicereportmatchexfo as E\n"
           + textwrap.dedent(body))
    p = subprocess.run([sys.executable, "-c", src],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout or p.stderr


def test_low_gate_flags_are_reburn_on_the_grid():
    """A 0.10 dB gate: every flag from 0.108 up to 0.159 is a reburn."""
    _run("""
        for loss in (0.108, 0.125, 0.159, 0.160, 0.300):
            assert R._category({'bidir_loss': loss}, 0.10) == 'reburn', loss
        print("OK")
    """)


def test_under_the_gate_stays_event():
    """Under the run's gate a cell is still the generic event."""
    _run("""
        assert R._category({'bidir_loss': 0.095}, 0.10) == 'event'
        assert R._category({'bidir_loss': 0.15}, 0.160) == 'event'
        assert R._category({'bidir_loss': 0.19}, 0.20) == 'event'
        assert R._category({'bidir_loss': None}, 0.10) == 'event'
        print("OK")
    """)


def test_default_reads_the_engine_threshold():
    """No threshold passed: the engine's REBURN_THRESHOLD (as an override
    leaves it), not a number of the runner's own."""
    _run("""
        assert E.REBURN_THRESHOLD == 0.160
        assert R._category({'bidir_loss': 0.15}) == 'event'
        assert R._category({'bidir_loss': 0.160}) == 'reburn'
        E.REBURN_THRESHOLD = 0.10
        assert R._category({'bidir_loss': 0.15}) == 'reburn'
        print("OK")
    """)


def test_category_agrees_with_the_engine_gate_at_the_edges():
    """Rounding edges land where the engine's flag gate puts them, under
    both boundary rules."""
    _run("""
        edges = (0.0994, 0.0995, 0.0999, 0.1, 0.1004, 0.1005, -0.1005,
                 0.1594, 0.1595, 0.2, 0.2005)
        for strict in (0, 1):
            E.SPLICE_STRICT_BOUNDARY = strict
            for thr in (0.08, 0.10, 0.15, 0.160, 0.20):
                for loss in edges:
                    want = ('reburn' if E._clears_splice_threshold(loss, thr)
                            else 'event')
                    got = R._category({'bidir_loss': loss}, thr)
                    assert got == want, (strict, thr, loss, got, want)
        E.SPLICE_STRICT_BOUNDARY = 0
        # 0.0995 prints ".100", so it flags at 0.10 and is a reburn
        assert R._category({'bidir_loss': 0.0995}, 0.10) == 'reburn'
        assert R._category({'bidir_loss': 0.0994}, 0.10) == 'event'
        print("OK")
    """)


def test_other_categories_are_untouched():
    """The threshold only decides reburn vs event; every flag kind ahead of
    it keeps its own color key."""
    _run("""
        for key, cat in (('is_break', 'break'), ('is_broke', 'broke'),
                         ('is_dead_zone', 'deadzone'), ('is_bend', 'bend'),
                         ('is_ref', 'ref'), ('is_gainer', 'gainer'),
                         ('is_bfill', 'bfill'), ('is_a_only', 'a_only'),
                         ('is_b_only', 'b_only')):
            assert R._category({key: True, 'bidir_loss': 0.30}, 0.10) == cat
        assert R._category({'event_source': 'dirty_connector',
                            'bidir_loss': 0.30}, 0.10) == 'dirty_connector'
        assert R._category({'event_source': 'sweep',
                            'bidir_loss': 0.30}, 0.10) == 'sweep'
        print("OK")
    """)


def test_the_grid_passes_the_runs_threshold():
    """main() hands the category the same `threshold` local the engine
    passes were given (and the manifest's thresholds carry)."""
    src = (SPLICEREPORT_DIR / "run_splicereport.py").read_text(encoding="utf-8")
    assert "'category': _category(res, threshold)," in src
