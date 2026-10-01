"""
.trc through the Splice Report and Unidirectional engines.

The loaders pick one trace type per direction folder and read a .trc at the
graded wavelength through sor_reader324802a.parse_trc_wavelength.  The proof
that a .trc is graded exactly like a .sor: a .sor's EXFO block IS a
one-trace .trc container, so the same shots are rewrapped as .trc and the
real runners are run on both.  Every cell of every sheet must match, except
the Acquisition sheet's fiber-type note: a .sor's GenParams states G.652, a
.trc stores no fiber type, and the reader leaves it blank rather than assume.
"""
import json
import os
import subprocess
import sys

import openpyxl
import pytest

from conftest import APP_PATH, SPLICEREPORT_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, 'fixtures')
TRC_HEAD_SRC = os.path.join(FIX, 'trc', 'TRCSPAN0001_131015501625.trc')

sys.path.insert(0, str(SPLICEREPORT_DIR))
import sor_reader324802a as sr                            # noqa: E402  the engine's copy
import splicereportmatchexfo as E                         # noqa: E402

FIBER_TYPE_CELLS = {('Acquisition Parameters', 'B23'), ('Acquisition Parameters', 'D23')}


def _rewrap(src_dir, dst_dir):
    wrap = open(TRC_HEAD_SRC, 'rb').read()
    head = wrap[:wrap.find(b'AppReg Format Ex', 1)]
    os.makedirs(dst_dir, exist_ok=True)
    for fn in sorted(os.listdir(src_dir)):
        if not fn.endswith('.sor'):
            continue
        data = open(os.path.join(src_dir, fn), 'rb').read()
        blocks = sr._parse_block_directory(data)
        k = next(x for x in blocks if 'ExfoNewProprietaryBlock' in x)
        body = data[blocks[k]['body']:blocks[k]['offset'] + blocks[k]['size']]
        with open(os.path.join(dst_dir, fn[:-4] + '.trc'), 'wb') as fh:
            fh.write(head + body)


def _cells(xlsx):
    wb = openpyxl.load_workbook(xlsx, data_only=True)
    return {(ws.title, c.coordinate): c.value for ws in wb.worksheets
            for row in ws.iter_rows() for c in row if c.value is not None}


def _same_report(a, b):
    ca, cb = _cells(a), _cells(b)
    diff = []
    for k in sorted(set(ca) | set(cb)):
        va, vb = ca.get(k), cb.get(k)
        if va == vb or k in FIBER_TYPE_CELLS:
            continue
        if isinstance(va, str) and isinstance(vb, str) and va.replace('.sor', '.trc') == vb:
            continue                       # a file name, by extension
        diff.append((k, va, vb))
    return diff


# ── the type a folder is read as ─────────────────────────────────────

@pytest.mark.parametrize('names, first', [
    (['A0001.sor', 'A0002.sor'], '.sor'),
    (['A0001.trc', 'A0002.trc'], '.trc'),
    (['A0001.sor', 'A0002.sor', 'A0003.sor', 'A0001.trc'], '.sor'),   # reshoots
    (['A0001.json', 'A0001.sor'], '.json'),                          # json wins a tie
    (['A0001.json', 'A0001.trc', 'A0002.trc'], '.trc'),
])
def test_trace_type_choice(names, first):
    order, _n = E._trace_ext_order(names)
    assert order[0] == first


# ── the report twin ──────────────────────────────────────────────────

@pytest.fixture(scope='module')
def twin(tmp_path_factory):
    d = tmp_path_factory.mktemp('trc_twin')
    _rewrap(os.path.join(FIX, 'splice_A'), str(d / 'A'))
    _rewrap(os.path.join(FIX, 'splice_B'), str(d / 'B'))
    return d


@pytest.mark.parametrize('analysis', ['suite', 'fr'])
def test_splice_report_on_trc_matches_sor(twin, analysis):
    out_s, out_t = twin / f'sor_{analysis}.xlsx', twin / f'trc_{analysis}.xlsx'
    for da, db, out in ((os.path.join(FIX, 'splice_A'), os.path.join(FIX, 'splice_B'), out_s),
                        (str(twin / 'A'), str(twin / 'B'), out_t)):
        rc, _m, err = _run(['--analysis', analysis, '--dir-a', da, '--dir-b', db,
                            '--out', str(out)])
        assert rc == 0, err
    assert _same_report(out_s, out_t) == []


def test_unidirectional_on_trc_matches_sor(twin):
    out_s, out_t = twin / 'uni_sor.xlsx', twin / 'uni_trc.xlsx'
    for d, out in ((os.path.join(FIX, 'splice_A'), out_s), (str(twin / 'A'), out_t)):
        rc, _m, err = _run(['--uni', '--dir-a', d, '--out', str(out)])
        assert rc == 0, err
    assert _same_report(out_s, out_t) == []


def test_fr_table_on_trc_pairs_matches_sor(twin):
    a = sorted(f for f in os.listdir(os.path.join(FIX, 'splice_A')) if f.endswith('.sor'))[:2]
    b = sorted(f for f in os.listdir(os.path.join(FIX, 'splice_B')) if f.endswith('.sor'))[:2]
    sor = [[i, os.path.join(FIX, 'splice_A', x), os.path.join(FIX, 'splice_B', y)]
           for i, (x, y) in enumerate(zip(a, b), 1)]
    trc = [[i, str(twin / 'A' / (x[:-4] + '.trc')), str(twin / 'B' / (y[:-4] + '.trc'))]
           for i, (x, y) in enumerate(zip(a, b), 1)]
    s, t = _fr_table(sor), _fr_table(trc)
    assert not s['errors'] and not t['errors']
    assert t['tables'] == s['tables'] and all(t['tables'].values())


def _run(args):
    p = subprocess.run([sys.executable, str(SPLICEREPORT_DIR / 'run_splicereport.py')] + args,
                       capture_output=True, text=True)
    return p.returncode, None, p.stderr[-800:]


def _fr_table(pairs):
    p = subprocess.run([sys.executable, str(SPLICEREPORT_DIR / 'run_splicereport.py'),
                        '--fr-table', json.dumps(pairs)], capture_output=True, text=True)
    return json.loads(p.stdout.strip().splitlines()[-1])


# ── the hub's upload boxes ───────────────────────────────────────────

def test_hub_upload_boxes_take_trc():
    app = APP_PATH.read_text(encoding='utf-8')
    assert "type=['zip', 'bdr', 'sor', 'json', 'trc']" in app       # Splice Report
    assert "type=['sor', 'json', 'trc', 'zip']" in app              # Unidirectional
    assert "_ext(u, '.sor', '.json', '.trc')" in app                # dropped span


def test_span_site_names_come_from_a_trc_folder(tmp_path):
    """The Splice Report titles a span with the sites the files store; a
    folder of .trc is read like a folder of .sor, in the case typed."""
    import shutil
    sys.path.insert(0, str(APP_PATH.parent))
    import app
    for i in (1, 2):
        shutil.copy(os.path.join(FIX, 'trc', 'TRCDECL0001_155016251310.trc'),
                    tmp_path / f'TRCDECL{i:04d}_155016251310.trc')
    assert app._derive_ila(str(tmp_path)) == ('SITA', 'SITB')
