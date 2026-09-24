"""OTDR Suite mode on a tie-panel span shot with a DECLARED span start.

When the tech sets the span start on the launch connector, EXFO writes the
event table relative to that connector (entry panel at table 0, far panel at
31.5 m, receive-reel end at 1.04 km) while the samples start at the OTDR
port.  The panel-span pass converted table positions with the full trace
offset, which includes the declared start the table never contained, so
every position shifted a reel-length upstream: Las Cruces published its
receive-reel end as a panel at 0.008 km and dropped the real far panel as
reel furniture, and Fort Worth West Panel B put a column at -0.968 km and
flagged 30 fibers on their one-sided A readings where the bidirectional
pair (FastReporter's own row) is 0.108-0.24 dB.  Fixed: 288/288 Las Cruces
fibers and every kept Fort Worth cell equal FR's bidirectional rows, and
neither set flags a fiber (FR's method flags none).

The fixture is Las Cruces fiber 2, which also tables an event 4.7 m before
the entry panel -- the pair has to be read on the panel, not on that.

Engine tests run in a clean subprocess (three engines, three sor_reader
copies, never one process).
"""
import os
import shutil
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
PANEL = REPO_ROOT / "desktop" / "tests" / "fixtures" / "lsc_panel"
BDR = REPO_ROOT / "desktop" / "tests" / "fixtures" / "bdr"


def test_declared_start_panel_span_reads_both_panels_where_they_are(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir(); b.mkdir()
    shutil.copy(PANEL / "LSC1LSC60002_1550.sor", a)
    shutil.copy(PANEL / "LSC6LSC10002_1550.sor", b)
    body = f"""
        import sys
        sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})
        import sor_reader324802a as sr
        import splicereportmatchexfo as E
        fa, fb = E.load_all({str(a)!r}, {str(b)!r})[:2]
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
        r = fa[2]
        assert r['user_offset_km'] > 1.0 and abs(E._table_offset_km(r)) < 1e-9, (r['user_offset_km'], E._table_offset_km(r))
        cols, res = E.discover_span_structure(fa, fb)
        kinds = [(round(c['position_km'], 3), c['column_kind']) for c in cols]
        assert kinds == [(0.0, 'connector'), (0.016, 'section'), (0.032, 'connector')], kinds
        assert all(c['position_km'] >= 0 for c in cols)
        # FastReporter's rows for this pair (its own .bdr key)
        key = sr.parse_bdr_side({str(BDR / 'LSC1LSC60002_1550.bdr')!r}, 'a')
        fr = [m for m in key['_bdr_merged'] if 'Type' in m]
        launch = [m['Loss'] for m in fr if m.get('Status') == 64][0]
        end = [m['Loss'] for m in fr if m.get('Status') == 128][0]
        entry, far = res[(2, 0)], res[(2, 2)]
        assert entry['b_loss'] is not None and far['b_loss'] is not None      # paired, not A-only
        # equal at the printed digit: Suite mode averages each direction's own
        # table loss, FR its full-precision legs (0.18323 vs 0.18316 here)
        assert round(entry['bidir_loss'], 3) == round(launch, 3), (entry['bidir_loss'], launch)
        assert round(far['bidir_loss'], 3) == round(end, 3), (far['bidir_loss'], end)
        assert not any(v.get('is_flagged') for v in res.values())
        print('OK')
    """
    p = subprocess.run([sys.executable, "-c", textwrap.dedent(body)], capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout
