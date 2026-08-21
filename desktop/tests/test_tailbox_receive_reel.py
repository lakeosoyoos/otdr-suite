"""A receive spool's bare end is not the cable's tailbox connector.

`_fiber_tailbox_refl` walks back from the fiber's end marker looking for a
`1F` connector.  When it finds none it falls back to the END MARKER's own
reflectance — right for a cable that ends in bare glass (KANLAN F336 reads
-15.6 dB, the glass/air Fresnel), wrong for a span shot into a receive spool,
where the end marker is the far end of the SPOOL, a reel length past the
cable's terminating connector.

KAN<->LAN A is what that costs.  12 of 864 A fibers table a reflective `1E`
at 118.2527 km -- the spool's own end, -45.89 to -46.30 dB.  Six of them also
happen to have no stored `1F`, because the firmware typed their tailbox
connector `0F` (non-reflective, refl 0.0) at 117.28-117.43 km instead of
`1F`.  Those six -- and only those six -- were then measured against a
population median built almost entirely (187/193) from `1F` CONNECTOR
readings at -58.93 dB, and flagged a tailbox REFL-46.0dB for being
12.8 dB "worse".  A firmware type byte decided which fibers flagged, and the
comparison was connector-against-spool-end.  The other six carry the
identical -46 dB and stay silent only because they do have a `1F`.

The guard reads back a decision Pass 0 already made per fiber:
_normalize_untrimmed_events pulls the end marker to the cable's far end iff
the fiber carries the reel, and the short-fiber guard leaves broken fibers
alone.  So it needs no threshold of its own -- the measured pull-back is
either exactly 0.0 or a whole reel length, nothing in between.

What must NOT change: a genuinely bare-glass end, on a fiber with no reel,
still flags.  That is the finding the population rule was built for (SANDUR,
a span shot with no receive jumper at all).

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"

_HELPERS = textwrap.dedent("""
    CABLE_END = 100.0     # cable's terminating connector, A frame
    REEL      = 1.05      # receive-reel length

    def ev(km, refl=0.0, reflective=False, typ='0F9999LS', end=False):
        return {'dist_km': km, 'splice_loss': 0.0, 'is_end': end,
                'is_reflective': reflective, 'reflection': refl,
                'time_of_travel': 0, 'type': typ}

    def normal_fiber():
        '''The population: a 1F tailbox connector at the cable end.'''
        evts = [ev(0.0, -55.0, True, '1F9999LS'), ev(50.0),
                ev(CABLE_END - 0.02, -58.9, True, '1F9999LS'),
                ev(CABLE_END, 0.0, False, '1E9999LS', end=True)]
        r = {'events': evts, '_raw_events': evts, '_trace_offset_km': 0.0,
             'duration_sec': 60, 'exfo_calibration': {}}
        return r

    def reel_fiber():
        '''Carries the receive reel: firmware typed the tailbox 0F so there
        is no 1F to find, and the raw end marker is the SPOOL's end, a reel
        length past the cable.  Pass 0 pulled the normalized end marker back
        to the cable end -- that pull-back is the reel evidence.'''
        raw = [ev(0.0, -55.0, True, '1F9999LS'), ev(50.0),
               ev(CABLE_END - 0.02, 0.0, False, '0F9999LS'),
               ev(CABLE_END + REEL, -46.0, True, '1E9999LS', end=True)]
        nrm = [ev(0.0, -55.0, True, '1F9999LS'), ev(50.0),
               ev(CABLE_END - 0.02, 0.0, False, '0F9999LS'),
               ev(CABLE_END, -46.0, True, '1E9999LS', end=True)]
        return {'events': nrm, '_raw_events': raw, '_trace_offset_km': 0.0,
                'duration_sec': 60, 'exfo_calibration': {}}

    def bare_glass_fiber():
        '''No reel (raw IS normalized, no pull-back) and no 1F: the cable
        really does end in air.  This must still flag.'''
        evts = [ev(0.0, -55.0, True, '1F9999LS'), ev(50.0),
                ev(CABLE_END, -46.0, True, '1E9999LS', end=True)]
        return {'events': evts, '_raw_events': evts, '_trace_offset_km': 0.0,
                'duration_sec': 60, 'exfo_calibration': {}}

    def tags_for(build_special):
        '''The TAILBOX-rule reflectance tags on fiber 7's A direction.

        Both reflectance rules now print the same bare 'REFL-46.0dB' -- the
        cell names no place -- so the rule cannot be read off the tag text.
        Ask the engine instead: 'refl_rules' records which rule produced each
        REFL tag, in append order, and is internal (never printed, never
        coloured on).  This is strictly no weaker than the old 'TAILBOX' in t
        match: a LAUNCH-rule REFL tag does not satisfy it.'''
        fa = {}
        for n in range(1, 41):
            fa[n] = normal_fiber()
        fa[7] = build_special()
        fb = {n: normal_fiber() for n in range(1, 41)}
        out = E.detect_launch_issues(fa, fb)
        info = out.get(7) or {}
        tags = list(info.get('a_tags') or [])
        rules = list(((info.get('refl_rules') or {}).get('A')) or [])
        refl = [t for t in tags if t.startswith('REFL')]
        assert len(refl) == len(rules), (refl, rules)
        return [t for t, rule in zip(refl, rules) if rule == 'tailbox']
""")


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c", header + _HELPERS + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_receive_reel_end_is_not_a_tailbox_reading():
    """The KANLAN A defect: -46 dB spool end, no 1F, must not flag."""
    _run("""
        tags = tags_for(reel_fiber)
        assert tags == [], tags
        print("OK")
    """)


def test_bare_glass_end_still_flags():
    """The finding the population rule exists for: no reel, no connector,
    the cable ends in air.  Unchanged."""
    _run("""
        tags = tags_for(bare_glass_fiber)
        assert len(tags) == 1 and tags[0] == 'REFL-46.0dB', tags
        print("OK")
    """)


def test_guard_reads_pass_0s_pullback_not_a_threshold():
    """No reel -> no pull-back -> the 1E fallback still applies; a reel
    length of pull-back refuses it.  Nothing in between exists."""
    _run("""
        before = tags_for(bare_glass_fiber)
        after  = tags_for(reel_fiber)
        assert before and not after, (before, after)
        print("OK")
    """)
