"""The panel-span connector pass on a record that carries no reel tolerance.

_connector_positions classifies each reflective reading by role (launch
reel end, cable end, or a connector of its own) within the direction's
launch reel tolerance, which Pass 0 stamps on every record as
'_launch_reel_tol_km'.  The fallback for a record without one named
CONN_ROLE_TOL_KM, which was never defined, so such a record raised
NameError instead of reading the span.  No shipped path reaches it (the
runner stamps every A record, at 25 m or more, before its only call), but a
new caller or a stamp of 0.0 would.

The fallback is LAUNCH_REEL_TOL_KM, the launch reel matcher's own default,
and 25 m is the right scale for a reflective connector (_reel_tol_km).

Engine tests run in a clean subprocess (three engines, three sor_reader
copies, never one process).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
PANEL = REPO_ROOT / "desktop" / "tests" / "fixtures" / "panelspan"


def _run(body):
    p = subprocess.run([sys.executable, "-c", textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_a_record_without_a_tolerance_reads_at_the_launch_reel_floor():
    """Four fibres: launch reel end 6 m past the reel, a connector 40 m past
    it, the cable end 71 m past it.  At 25 m the 40 m reading is a connector
    of its own; a wider fallback (50 m) would fold it into the launch."""
    _run(f"""
        import sys
        sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})
        import splicereportmatchexfo as E

        def fibres(tol):
            out = {{}}
            for f in range(1, 5):
                r = {{'_launch_reel_km': 1.000, 'events': [
                    {{'dist_km': 0.0, 'is_reflective': True}},
                    {{'dist_km': 1.006, 'is_reflective': True}},
                    {{'dist_km': 1.040, 'is_reflective': True}},
                    {{'dist_km': 1.071, 'is_reflective': True, 'is_end': True}},
                ]}}
                if tol != 'missing':
                    r['_launch_reel_tol_km'] = tol
                out[f] = r
            return out

        def roles(tol):
            return [(c['role'], c['pos_raw'], c['n'])
                    for c in E._connector_positions(fibres(tol))]

        want = [('launch', 1.006, 4), ('at 1.040', 1.04, 4), ('far', 1.071, 4)]
        assert roles(E.LAUNCH_REEL_TOL_KM) == want, roles(E.LAUNCH_REEL_TOL_KM)
        # the fixture tells 25 m from a wider tolerance
        assert roles(0.050) != want, roles(0.050)
        for tol in ('missing', None, 0.0):
            assert roles(tol) == want, (tol, roles(tol))
        print('OK')
    """)


def test_a_real_panel_span_reads_the_same_without_the_stamp():
    """The shipped panel-span fixture, through the runner's own Pass 0: its
    short-pulse stamp IS the 25 m floor, so taking the stamp away must not
    move either panel."""
    _run(f"""
        import sys
        sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})
        import splicereportmatchexfo as E
        fa, fb = E.load_all({str(PANEL)!r}, {str(PANEL)!r})[:2]
        # the runner's Pass 0
        reels = E.reciprocal_reels(fa, fb)
        for di, d in enumerate((fa, fb)):
            reel, recv, absent, tol = reels[di]
            endmed = E._direction_end_median_km(d)
            for r in d.values():
                r['_raw_events'] = r['events']
                r['_launch_reel_km'] = reel; r['_receive_reel_km'] = recv
                r['_launch_reel_absent'] = absent; r['_launch_reel_tol_km'] = tol
                r['_trace_offset_km'] = E._trace_frame_offset_km(r, r['events'], reel, absent, tol)
                r['events'] = E._normalize_untrimmed_events(r['events'], reel, recv, absent, tol, endmed)
        assert {{r['_launch_reel_tol_km'] for r in fa.values()}} == {{E.LAUNCH_REEL_TOL_KM}}
        stamped = E._connector_positions(fa)
        # both panels; the far one is matched to the cable end within tol
        assert len(stamped) == 2 and stamped[-1]['role'] == 'far', stamped
        for tol in ('missing', None, 0.0):
            for r in fa.values():
                if tol == 'missing':
                    r.pop('_launch_reel_tol_km', None)
                else:
                    r['_launch_reel_tol_km'] = tol
            got = E._connector_positions(fa)
            assert got == stamped, (tol, got, stamped)
        print('OK')
    """)
