"""A span where discovery finds NO closures must not crash the runner.

Field crash 2026-08-25 (Zach, BLDG1<->BLDG3): discover_splices() has two
exits, and only the normal one honoured `return_subgate`.  When no fiber
carries a single in-span event the early `if not pairs` exit returned a bare
[], so the caller's

    cand, subgate = E.discover_splices(fa, return_subgate=True)

died with "not enough values to unpack (expected 2, got 0)".  `pairs` goes
empty whenever every event is filtered out — below LAUNCH_SKIP_KM (the
launch connector), an end-of-fiber code, past the first EOF marker, or a
non-in-span type ('1O' out-of-range).  That is the normal shape of a short
building-to-building shot, which is why a long-haul cable never hit it.

The two-value return arrived with the B-corroboration work (PR #69,
2026-08-15); the early return predates it and was missed.  These tests pin
the ARITY CONTRACT of both exits, so a future third exit can't drift again.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"

# Pre-dedented (top-level) so it composes with a separately-dedented test
# body — see test_b_corr_promotion for why the two are dedented apart.
_FIBER_HELPERS = textwrap.dedent("""
    def ev(km, typ='0F9999LS', end=False):
        return {'dist_km': km, 'splice_loss': 0.05, 'type': typ,
                'is_end': end, 'is_reflective': False}

    def launch_only(span_km):
        '''A short shot: launch connector, then end of fiber.  Nothing in
        between survives discover_splices' per-fiber filters.'''
        return {'events': [ev(0.0, typ='1F9999LS'),
                           ev(span_km, typ='1E9999LS', end=True)]}
""")


def _run(body, helpers=False):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    src = header + (_FIBER_HELPERS if helpers else "") + textwrap.dedent(body)
    p = subprocess.run([sys.executable, "-c", src],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout


def test_no_inspan_events_still_unpacks_two_values():
    """THE regression: the runner's own call shape on a launch+end-only
    span.  Pre-fix this raises ValueError inside the unpack."""
    _run(helpers=True, body="""
        fa = {f: launch_only(0.180) for f in range(1, 25)}
        cand, subgate = E.discover_splices(fa, return_subgate=True)
        assert cand == [], cand
        assert subgate == [], subgate
        print('OK')
    """)


def test_no_inspan_events_without_subgate_returns_bare_list():
    """The provenance check (run_splicereport.py's closure-count compare)
    calls the SAME function without return_subgate and does len() on it —
    that exit must stay a plain list, not a tuple, or the count silently
    becomes 2."""
    _run(helpers=True, body="""
        fa = {f: launch_only(0.180) for f in range(1, 25)}
        out = E.discover_splices(fa)
        assert out == [], out
        assert isinstance(out, list), type(out)
        assert len(out) == 0, len(out)
        print('OK')
    """)


def test_every_filter_that_empties_pairs_keeps_the_contract():
    """`pairs` empties four different ways.  Each must return the same
    shape — a bend-only span that clears none of them is still a valid
    run, not a crash."""
    _run(helpers=True, body="""
        cases = {
            # every event below LAUNCH_SKIP_KM (20 m)
            'sub_launch': {'events': [ev(0.004), ev(0.011),
                                      ev(2.0, typ='1E9999LS', end=True)]},
            # every event at/past the first end-of-fiber marker
            'post_eof': {'events': [ev(2.0, typ='1E9999LS', end=True),
                                    ev(2.4), ev(2.9)]},
            # '1O' out-of-range: a short shot that never reached the far end
            'out_of_range': {'events': [ev(1.5, typ='1O9999LS'),
                                        ev(2.0, typ='1E9999LS', end=True)]},
            # no events at all
            'no_events': {'events': []},
        }
        for name, proto in cases.items():
            fa = {f: {'events': list(proto['events'])} for f in range(1, 25)}
            cand, subgate = E.discover_splices(fa, return_subgate=True)
            assert cand == [] and subgate == [], (name, cand, subgate)
            assert isinstance(E.discover_splices(fa), list), name
        print('OK')
    """)


def test_populated_span_still_returns_both_values():
    """Guard the OTHER exit: a real closure population must keep returning
    a 2-tuple, so a fix to the empty path can't be made by deleting the
    flag."""
    _run(helpers=True, body="""
        fa = {}
        for f in range(1, 101):
            fa[f] = {'events': [ev(0.0, typ='1F9999LS'), ev(12.4),
                                ev(30.0, typ='1E9999LS', end=True)]}
        cand, subgate = E.discover_splices(fa, return_subgate=True)
        assert len(cand) == 1, cand
        assert isinstance(subgate, list), type(subgate)
        assert isinstance(E.discover_splices(fa), list)
        print('OK')
    """)
