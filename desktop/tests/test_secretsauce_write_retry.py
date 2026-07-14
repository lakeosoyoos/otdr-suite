"""Regression (prod issue #7): Secret Sauce must not throw away a completed
multi-minute analysis when its output dir vanishes mid-run.

out_dir is <folder>/SecretSauce_reports — it lives INSIDE the analyzed folder
and is created once, up front, but the SOR analysis then runs for minutes and
cloud-sync / AV can remove or quarantine that dir before the report is written.
The runner now writes through _write_report(), which (re)creates the parent dir
immediately before each write.

HARD RULE — namespace isolation (see test_secretsauce_runner.py): this test
process must NEVER import run_secretsauce (it puts the Secret Sauce copy of
sor_reader324802a on sys.path, colliding with the viewer's).  We exercise the
helper in a SUBPROCESS, exactly as the hub runs the whole runner.
"""
from __future__ import annotations

import subprocess
import sys

from conftest import SECRETSAUCE_DIR, REPO_ROOT

# Subprocess body: import the runner in a clean interpreter, delete the output
# dir AFTER it was created (the mid-run cloud-sync/AV case), then write through
# the helper and prove the bytes landed.  Prints one sentinel on success.
_PROBE = r"""
import os, sys, shutil, tempfile
sys.path.insert(0, r"{ss}")
sys.path.insert(0, r"{root}")
import run_secretsauce as R

td = tempfile.mkdtemp()
out_dir = os.path.join(td, "SecretSauce_reports")
os.makedirs(out_dir)                     # created up front, as the runner does
shutil.rmtree(out_dir)                   # ...then it vanishes mid-analysis
assert not os.path.isdir(out_dir)

outp = os.path.join(out_dir, "report.pdf")
R._write_report(outp, b"%PDF-1.4 secret sauce")
assert os.path.isfile(outp), "report not written"
assert open(outp, "rb").read() == b"%PDF-1.4 secret sauce", "bytes mismatch"
print("WRITE_RETRY_OK")
"""


def test_write_report_recreates_vanished_out_dir():
    probe = _PROBE.format(ss=str(SECRETSAUCE_DIR), root=str(REPO_ROOT))
    p = subprocess.run([sys.executable, "-c", probe],
                       capture_output=True, text=True)
    assert p.returncode == 0, (
        f"probe exited {p.returncode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}")
    assert "WRITE_RETRY_OK" in p.stdout, (
        f"helper did not recover a deleted out_dir\nstdout:\n{p.stdout}\n"
        f"stderr:\n{p.stderr}")
