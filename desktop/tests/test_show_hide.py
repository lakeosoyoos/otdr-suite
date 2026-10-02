"""Show / hide in report: the engine finds the same things, the switches
only decide which flagged cells reach the workbook, and a Display sheet
says what was left out.  Runs the real runner on the splice fixtures."""
import json
import subprocess
import sys

import openpyxl

from conftest import REPO_ROOT

RUNNER = REPO_ROOT / 'splicereport' / 'run_splicereport.py'
FIX = REPO_ROOT / 'desktop' / 'tests' / 'fixtures'


def _run(tmp_path, name, *args):
    out = tmp_path / f'{name}.xlsx'
    p = subprocess.run([sys.executable, str(RUNNER), *args, '--out', str(out)],
                       capture_output=True, text=True, cwd=str(RUNNER.parent))
    return json.loads(p.stdout.strip().splitlines()[-1]), out


def _bidir(tmp_path, name, show=None):
    # longpulse: a reburn and five real bends (the splice_A/B fixture's bends
    # were closures a 24-fibre job could not find; it now finds them all).
    # 31 fibres: under 80 a job lists events and calls no bend (Robert
    # 2026-10-01), so the run lowers the cutoff to keep its closure layout.
    args = ['--dir-a', str(FIX / 'longpulse' / 'A'), '--dir-b', str(FIX / 'longpulse' / 'B'),
            '--site-a', 'A', '--site-b', 'B',
            '--overrides', json.dumps({'EVENT_JOB_MAX_FIBERS': 0})]
    if show:
        args += ['--show', json.dumps(show)]
    return _run(tmp_path, name, *args)


def test_bidir_loss_hidden_keeps_bends(tmp_path):
    full, full_x = _bidir(tmp_path, 'full')
    cats = [c['category'] for c in full['cells']]
    assert 'reburn' in cats and 'bend' in cats
    assert 'Display' not in openpyxl.load_workbook(full_x).sheetnames
    hid, hid_x = _bidir(tmp_path, 'hid', {'loss': False})
    assert [c for c in hid['cells'] if c['category'] == 'reburn'] == []
    assert ([c for c in hid['cells'] if c['category'] == 'bend']
            == [c for c in full['cells'] if c['category'] == 'bend'])
    rows = [[c.value for c in r]
            for r in openpyxl.load_workbook(hid_x)['Display'].iter_rows(max_row=4)]
    assert rows == [['Category', 'Shown'], ['Splice loss', 'N'],
                    ['Bend/Damage', 'Y'], ['Breaks', 'Y']]


def test_uni_bend_hidden_drops_column_keeps_splices(tmp_path):
    # A 24-fibre uni job lists events (UNI_EVENT_JOB_MAX 79) and calls no
    # bend; the switch is about a closure layout, so this run keeps one.
    base = ['--uni', '--dir-a', str(FIX / 'splice_A'),
            '--overrides', json.dumps({'UNI_EVENT_JOB_MAX': 1})]
    full, _ = _run(tmp_path, 'u_full', *base)
    hid, _ = _run(tmp_path, 'u_hid', *base, '--show', json.dumps({'bend': False}))
    kinds = lambda d: [c['kind'] for c in d['uni']['grid_columns']]
    assert 'bend_damage' in kinds(full) and 'bend_damage' not in kinds(hid)
    splice = lambda d: [(c['fiber'], c['loss']) for c in d['uni']['cells']
                        if c['kind'] == 'splice']
    assert splice(hid) == splice(full)


def _sheet(path, name):
    return [[c.value for c in r] for r in openpyxl.load_workbook(path)[name].iter_rows()]


def test_bidir_loss_hidden_still_counts_reburns(tmp_path):
    # The Display sheet says hidden splice loss was found, so the Reburn
    # Summary counts it: 1 reburn cell, not 0 (audit 2026-10-02).
    _, full_x = _bidir(tmp_path, 'full')
    _, hid_x = _bidir(tmp_path, 'hid', {'loss': False})
    full = _sheet(full_x, 'Reburn Summary')
    assert ['Cells with at least one reburn', 1] in [r[:2] for r in full]
    assert _sheet(hid_x, 'Reburn Summary') == full


def test_uni_loss_hidden_still_counts_reburns(tmp_path):
    base = ['--uni', '--dir-a', str(FIX / 'splice_A'),
            '--overrides', json.dumps({'UNI_EVENT_JOB_MAX': 1})]
    _, full_x = _run(tmp_path, 'u_full', *base)
    hid, hid_x = _run(tmp_path, 'u_hid', *base, '--show', json.dumps({'loss': False}))
    assert [c for c in hid['uni']['cells'] if c['kind'] == 'splice'] == []
    full = _sheet(full_x, 'Reburn Percentage')
    assert full[3][0] != '0.00%'
    assert _sheet(hid_x, 'Reburn Percentage') == full
