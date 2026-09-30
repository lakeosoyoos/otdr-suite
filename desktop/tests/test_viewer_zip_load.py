"""Regression: a zipped SOR span (and a stray .json) must not blank the Viewer.

Root cause the boss hit ("viewer still doesn't work"):
  • trace_server.list_fibers flipped to JSON-only mode if ANY .json was present,
    so one stray export/report/cache zeroed a folder full of .sor → "0 fibers".
  • the Viewer's folder input couldn't read a .zip at all — only the bidirectional
    "Load span" flow extracted zips, and that flow hard-rejects a single-direction
    zip.  app._resolve_viewer_dir now extracts a zip / folder-of-zips for the
    Viewer's own input.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile

from conftest import REPO_ROOT, VIEWER_DIR


def _run(body):
    header = ("import sys, os, tempfile, shutil, zipfile\n"
              f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
              f"sys.path.insert(0, {str(VIEWER_DIR)!r})\n"
              "import folder_intake as fi\n"
              "import trace_server as T\n"
              f"FXA = {str(REPO_ROOT / 'desktop/tests/fixtures/splice_A')!r}\n"
              "A = sorted(f for f in os.listdir(FXA) if f.endswith('.sor'))[:6]\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_list_fibers_ignores_a_stray_json():
    _run("""
        d = tempfile.mkdtemp()
        for f in A: shutil.copy(os.path.join(FXA, f), os.path.join(d, f))
        assert len(T.list_fibers(d)) == 6
        open(os.path.join(d, 'pairs_cache.json'), 'w').write('{}')  # stray non-trace json
        assert len(T.list_fibers(d)) == 6, 'a stray .json zeroed the .sor list'
        print('OK')
    """)


def test_real_json_export_folder_still_lists_json():
    # A folder that is genuinely JSON exports (more .json than .sor) must still
    # list the JSON — the fix picks the majority extension, not always .sor.
    _run("""
        d = tempfile.mkdtemp()
        for i in range(1, 5):
            open(os.path.join(d, f'ELMMIL{i:04d}_1550.json'), 'w').write('{}')
        # one stray .sor shouldn't win over 4 real json
        open(os.path.join(d, 'note0001_1550.sor'), 'wb').write(b'x')
        got = dict(T.list_fibers(d))
        assert len(got) == 4 and all(v.endswith('.json') for v in got.values()), got
        print('OK')
    """)


def test_zip_span_becomes_viewable():
    # The machinery app._resolve_viewer_dir uses: extract a zip (flat OR nested),
    # flatten, and the trace server lists the fibers.
    _run("""
        for nested in (False, True):
            folder = tempfile.mkdtemp()
            zp = os.path.join(folder, 'span.zip')
            with zipfile.ZipFile(zp, 'w') as z:
                for f in A:
                    arc = ('SEANOR 6.15/' + f) if nested else f
                    z.writestr(arc, open(os.path.join(FXA, f), 'rb').read())
            work = tempfile.mkdtemp()
            files = fi.find_otdr_files_with_zips(folder, os.path.join(work, 'zips'))
            flat = fi.materialize_all(files, os.path.join(work, 'all'))
            n = len(T.list_fibers(flat))
            assert n == 6, f'nested={nested}: got {n} fibers from the zip'
        print('OK')
    """)


def test_app_wires_the_zip_resolver_into_the_viewer():
    app = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    assert "def _resolve_viewer_dir(" in app, "zip resolver missing"
    # Both boxes go through the resolver in _panel_dirs, which the Viewer and
    # the report pages (_panel_traces) share since 2026-09-29.
    panel = app.split("def _panel_dirs(", 1)[1].split("\ndef ", 1)[0]
    assert "_panel_boxes()" in panel and "_resolve_viewer_dir(raw)" in panel, \
        "_panel_dirs does not resolve the A/B inputs through the zip resolver"
    viewer = app.split("def page_viewer(", 1)[1].split("\ndef ", 1)[0]
    assert "_panel_dirs()" in viewer, "page_viewer does not read _panel_dirs"
    traces = app.split("def _panel_traces(", 1)[1].split("\ndef ", 1)[0]
    assert "_panel_dirs()" in traces, "the report pages do not read _panel_dirs"
