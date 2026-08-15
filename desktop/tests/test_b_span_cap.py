"""Robust B-frame span cap (KANLAN F1 / F739).

A fiber's own is_end marker can overrun the real cable end — KANLAN F1's
B file marked EOF 650 m past the +4.1 dB end reflector every other fiber
stops at.  Mirroring that fiber's B events on its own corrupted span put
its repair-splice event 550 m off, spawning a phantom one-fiber bend
column; the same corruption dropped F739's real .331 cell entirely.  A
fiber can legitimately be SHORT (breaks, short lays) but can never be
LONGER than the cable, so only the long side is capped — at the
population span, with one pulse smear of slack so ordinary end-detection
jitter is never "corrected".

WITHDRAWN (2026-08-15): a companion "zero gainers + low median = bend"
closure-suppression rule was built and REMOVED after ripple testing.  It
dropped Seattle-North Bend 4.96 km and 87.18 km, which the tech's own
sheet carries as Splice 1 and Splice 13.  Zero gainers means NO LOT
CHANGE across the joint, not "not a splice" — same-reel and repair
splices legitimately show it (cf. the KANLAN 110.2 repair).  Do not
reintroduce that rule without a discriminator that separates those
cases.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout


# ── 2. B-frame span cap ───────────────────────────────────────────────────

def test_span_cap_clips_overrunning_eof_marker_only():
    """A fiber whose end marker overruns the population end is capped; a
    genuinely SHORT fiber and ordinary jitter are left alone."""
    _run("""
        def fib(eof):
            return {'events': [{'dist_km': 6.0, 'splice_loss': 0.1,
                                'is_end': False, 'is_reflective': False},
                               {'dist_km': eof, 'splice_loss': 0.0,
                                'is_end': True, 'is_reflective': True}]}
        fibers = {i: fib(116.23) for i in range(1, 60)}
        fibers[1]  = fib(116.88)   # KANLAN F1: 650 m overrun
        fibers[2]  = fib(116.28)   # ordinary jitter, 50 m
        fibers[3]  = fib(95.00)    # genuinely short (break)
        span, cap = E._population_span_cap(fibers)
        assert abs(span - 116.23) < 0.02, span
        assert cap > span, (span, cap)
        def capped(v):
            return span if (span and v > cap) else v
        assert abs(capped(116.88) - 116.23) < 0.02   # corrupted -> population
        assert abs(capped(116.28) - 116.28) < 1e-9   # jitter untouched
        assert abs(capped(95.00) - 95.00) < 1e-9     # short fiber untouched
        print('OK')
    """)


def test_span_cap_noop_without_end_markers():
    """No end markers anywhere -> (0.0, 0.0), i.e. callers keep per-fiber
    spans exactly as before (legacy behaviour preserved)."""
    _run("""
        fibers = {1: {'events': [{'dist_km': 5.0, 'splice_loss': 0.1,
                                  'is_end': False, 'is_reflective': False}]},
                  2: {'events': []},
                  3: {}}
        assert E._population_span_cap(fibers) == (0.0, 0.0)
        assert E._population_span_cap({}) == (0.0, 0.0)
        assert E._population_span_cap(None) == (0.0, 0.0)
        print('OK')
    """)
