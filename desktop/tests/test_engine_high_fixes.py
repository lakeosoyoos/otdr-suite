"""Regression tests for two confirmed-HIGH engine issues (Fable audit).

Both engines ship their own sor_reader copy, so each is exercised in a CLEAN
child process (never imported in-process) — same isolation the other suites use.

  • Splice Report: scan_b_side_breaks crashed with "min() arg is an empty
    sequence" on any zero-closure span (guaranteed for <MIN_POP_SPLICE fibers) —
    the un-fixed twin of the scan_a_standalone zero-closure bug.
  • Secret Sauce: the hub writes pairs_cache.json into <folder>/SecretSauce_reports/;
    _inventory's recursive walk counted it as a .json acquisition, so a 2nd run on
    a pure-SOR folder aborted with a bogus "Mixed file types."
"""
import subprocess
import sys
import textwrap

from conftest import SECRETSAUCE_DIR, SPLICEREPORT_DIR


def _run(engine_dir, body):
    header = "import sys\n" f"sys.path.insert(0, {str(engine_dir)!r})\n"
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_scan_b_side_breaks_no_closures_does_not_crash():
    """splices == [] (zero closures) must not raise; the nearest-closure min()
    over an empty range used to kill the whole report."""
    _run(SPLICEREPORT_DIR, """
        import splicereportmatchexfo as E
        def _ev(k, end=False):
            return {'dist_km': k, 'splice_loss': (0.0 if end else 0.3),
                    'type': ('1E' if end else '0F'),
                    'reflection': (-40.0 if end else -60.0),
                    'is_end': end, 'is_reflective': end, 'time_of_travel': 0.0}
        def _fiber(kms, eol):
            evs = [_ev(k) for k in kms] + [_ev(eol, end=True)]
            return {'_source': 'sor', '_trace_offset_km': 0.0,
                    'events': sorted(evs, key=lambda e: e['dist_km'])}
        fa = {f: _fiber([10.0, 30.0], 60.0) for f in range(1, 3)}
        fb = {1: _fiber([10.0, 30.0], 60.0), 2: _fiber([10.0], 50.0)}  # fiber 2 ends short
        out = E.scan_b_side_breaks(fa, fb, [], {}, 60.0)   # splices=[] -> used to ValueError
        assert isinstance(out, dict), type(out)
        print('OK')
    """)


def test_inventory_excludes_secretsauce_reports_cache(tmp_path):
    """A stray pairs_cache.json under SecretSauce_reports/ must NOT be inventoried
    as a .json acquisition (that turned every 2nd run into 'Mixed file types')."""
    d = tmp_path / "span"
    d.mkdir()
    (d / "AAA0001_1550.sor").write_bytes(b"x")
    (d / "AAA0002_1550.sor").write_bytes(b"x")
    rpt = d / "SecretSauce_reports"
    rpt.mkdir()
    (rpt / "pairs_cache.json").write_text("{}")
    _run(SECRETSAUCE_DIR, f"""
        import run_secretsauce as R
        sor, trc, jsn = R._inventory({str(d)!r})
        assert len(sor) == 2, sor
        assert jsn == [], jsn        # the hub's own cache must be pruned, not counted
        print('OK')
    """)
