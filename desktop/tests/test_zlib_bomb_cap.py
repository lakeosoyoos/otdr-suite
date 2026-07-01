"""Regression: every SOR/TRC decompressor caps its output (zlib-bomb guard).

A malicious/corrupt .sor/.trc block can inflate ~1000x (DEFLATE) to multiple GB
and OOM the process — worst on the Viewer, which parses SOR in-process in the hub
thread.  Each decompressor now routes through `_zlib_decompress_capped`, which
raises zlib.error past a hard ceiling (callers already skip a raised decompress).

The boundary is exercised with a SMALL cap override (no GB allocation), in a
clean child per engine — the three sor_reader copies share the module name and
must never be imported together.
"""
import subprocess
import sys
import textwrap

from conftest import SECRETSAUCE_DIR, SPLICEREPORT_DIR, VIEWER_DIR

_CAP_BODY = r"""
    # A 100 KB payload compresses to ~100 bytes: with a tiny cap it must be
    # refused (unconsumed_tail remains), with a generous cap it round-trips.
    small = zlib.compress(b'\x00' * 100000)
    try:
        M._zlib_decompress_capped(small, max_bytes=1000)
        print('BAD: over-cap stream was not rejected'); raise SystemExit(1)
    except zlib.error:
        pass
    assert M._zlib_decompress_capped(small, max_bytes=10_000_000) == b'\x00' * 100000
    payload = b'hello world' * 500
    assert M._zlib_decompress_capped(zlib.compress(payload)) == payload
    print('OK')
"""


def _run(engine_dir, module):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(engine_dir)!r})\n"
              f"import {module} as M\n"
              "import zlib\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(_CAP_BODY)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_viewer_sor_reader_caps_decompression():
    _run(VIEWER_DIR, "sor_reader324802a")


def test_secretsauce_sor_reader_caps_decompression():
    _run(SECRETSAUCE_DIR, "sor_reader324802a")


def test_splicereport_sor_reader_caps_decompression():
    _run(SPLICEREPORT_DIR, "sor_reader324802a")


def test_trc_parser_caps_decompression():
    _run(SECRETSAUCE_DIR, "trc_parser")


def test_exfo_decoder_caps_decompression():
    _run(SECRETSAUCE_DIR, "exfo_proprietary_decoder")
