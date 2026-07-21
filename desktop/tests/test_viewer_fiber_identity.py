"""Viewer fiber identity — keep it in lockstep with the Splice Report.

Boss 2026-07-21: clicking a Splice Report cell opened the Viewer but the
fiber's trace never loaded.  Root cause: the viewer's private
extract_fiber_num never received the June-13 filename-sweep fixes, and it
had no GenParams fallback — so spans the report can grid (via its fixed
parser + PR#17 GenParams identity) contained fibers the viewer could not
resolve at all (324 real files on disk, e.g.
``DNW1DNW50007withstartstop.sor`` → report 7, viewer None).

Locks under test:
  * behavior-lock: viewer and splicereport extractors agree on the whole
    documented filename catalog (both loaded via AST — no engine imports)
  * source-lock: the viewer's parse_genparams is byte-identical to the
    splicereport copy it was ported from
  * rescue: a .sor with an unusable filename resolves via its internal
    GenParams fiber id (subprocess — the two sor_reader copies share a
    module name and must never meet in one process)
"""
import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

CATALOG = [
    ('LAGDUR0001.sor', 1),
    ('Norsea001_1550.sor', 1),
    ('Seattle to Spokane d.0431.sor', 431),
    ('20260520_LAGDUR0001.sor', 1),
    ('DURSAN001_1550 .json', 1),
    ('VERSLK001_131015501625 .json', 1),
    ('TEST0001_155016251310.trc', 1),
    ('CHC-HCH-LS-089.trc', 89),
    ('._STRROM0001_1550.sor', None),
    ('PTL1PTL60145.sor', 145),
    ('DNW1DNW50148.sor', 148),
    ('DNW1DNW50007withstartstop.sor', 7),
    ('STRROM0064_1550.sor', 64),
    ('ELMMIL1152_1550.sor', 1152),
]


def _load_extractor(path, fnname):
    src = open(os.path.join(ROOT, path), encoding='utf-8').read()
    ns = {'re': re, 'os': os}
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, 'id', '').startswith('_FIBER') for t in node.targets):
            exec(compile(ast.Module([node], []), path, 'exec'), ns)
        if isinstance(node, ast.FunctionDef) and node.name == fnname:
            exec(compile(ast.Module([node], []), path, 'exec'), ns)
    return ns[fnname]


def _fn_source(path, fnname):
    src = open(os.path.join(ROOT, path), encoding='utf-8').read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == fnname:
            return ast.get_source_segment(src, node)
    raise AssertionError(f'{fnname} not found in {path}')


def test_extractors_agree_on_catalog():
    viewer = _load_extractor('viewer/trace_server.py', 'extract_fiber_num')
    sr = _load_extractor('splicereport/splicereportmatchexfo.py',
                         '_extract_fiber_num')
    for fn, want in CATALOG:
        assert viewer(fn) == want, (fn, viewer(fn), want)
        assert sr(fn) == want, (fn, sr(fn), want)


def test_extractor_bodies_identical():
    v = _fn_source('viewer/trace_server.py', 'extract_fiber_num')
    s = _fn_source('splicereport/splicereportmatchexfo.py',
                   '_extract_fiber_num')
    assert v.replace('extract_fiber_num', '_extract_fiber_num') == s


def test_parse_genparams_source_identical():
    v = _fn_source('viewer/sor_reader324802a.py', 'parse_genparams')
    s = _fn_source('splicereport/sor_reader324802a.py', 'parse_genparams')
    assert v == s


def test_genparams_rescue_resolves_unusable_filename():
    fixture = None
    fdir = os.path.join(ROOT, 'desktop', 'tests', 'fixtures', 'splice_A')
    for fn in sorted(os.listdir(fdir)):
        if fn.endswith('.sor'):
            fixture = os.path.join(fdir, fn)
            break
    assert fixture
    td = tempfile.mkdtemp()
    try:
        shutil.copy(fixture, os.path.join(td, 'no_digits_here.sor'))
        code = (
            "import sys; sys.path.insert(0, r'%s')\n"
            "import trace_server as T\n"
            "print(T.list_fibers(r'%s'))\n" % (os.path.join(ROOT, 'viewer'), td))
        p = subprocess.run([sys.executable, '-c', code],
                           capture_output=True, text=True, timeout=120)
        assert p.returncode == 0, p.stderr[-800:]
        out = eval(p.stdout.strip().splitlines()[-1])
        assert out and out[0][1] == 'no_digits_here.sor'
        assert isinstance(out[0][0], int)
    finally:
        shutil.rmtree(td, ignore_errors=True)
