"""The in-app pairs list makes the same call as the workbook: over 50% or not.

Reported 2026-09-13 on Goodland to Monument FEC Finals (1,152 files, 5 km,
10 ns): the header said "0 at >=50% likelihood" while all 500 rows shown read
"Possible duplicate".  The runner's pairs mode had a 10-50% 'Possible
duplicate' band that the workbook never had: its Confirmed duplicates sheet,
its per-file verdict and n_flagged all use p_dup > 0.5.  58 of those rows were
pairs a physical or speckle gate had held at exactly 0.5 (LEN_CAP), which the
workbook calls unique.

Robert, 2026-09-14: drop the band.  A pair over 50% is a duplicate and anything
else is unique, so the list uses the same line as the header count and the
workbook.

Namespace isolation rule: the Secret Sauce engine and runner only ever run in
subprocesses, never imported into the test process.
"""
from __future__ import annotations

import json
import subprocess
import sys

from conftest import SECRETSAUCE_DIR


def _run(script, *args, timeout=240):
    p = subprocess.run([sys.executable, "-c", script, str(SECRETSAUCE_DIR),
                        *[str(a) for a in args]],
                       capture_output=True, text=True, timeout=timeout)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


# The loader is stubbed; everything after it (pair metrics, regime, the
# physical and speckle gates, the runner's pairs mode) runs for real.
_TWIN_SCRIPT = r"""
import sys, os, json, hashlib, contextlib
import numpy as np
sys.path.insert(0, sys.argv[1])
import report_sor
import run_secretsauce as R

def _stub_load(path):
    # file body: "eof_m,serial,glass_seed_name,extra_noise_db"
    name = os.path.splitext(os.path.basename(path))[0]
    eof, serial, glass, extra = open(path).read().strip().split(',')
    n = int(float(eof))
    rng = np.random.RandomState(int(hashlib.md5(glass.encode()).hexdigest()[:8], 16))
    trace = 20.0 - 0.0002 * np.arange(n, dtype=np.float64) + rng.randn(n) * 0.05
    if float(extra) > 0:
        own = np.random.RandomState(int(hashlib.md5(name.encode()).hexdigest()[:8], 16))
        trace = trace + own.randn(n) * float(extra)
    return {'name': name, 'filepath': path, 'trace': trace.astype(np.float32),
            'pos': np.arange(n, dtype=np.float64), 'length': float(eof), 'loss': None,
            'max_splice_dB': None, 'timestamp': None, 'wavelength': 1550,
            'serial_number': serial, 'events': []}

report_sor.load_sor_file = _stub_load
_analyze = report_sor._analyze_sor
engine_twin = {}

def _recording_analyze(folder):
    a = _analyze(folder)
    for p in a['pairs']:
        if {p['a'], p['b']} == {'AAABBB0005', 'AAABBB0013'}:
            engine_twin.update(p_dup=p['p_dup'], p_dup_raw=p['p_dup_raw'],
                               serial_mismatch=p.get('serial_mismatch'),
                               raw_identical=p.get('raw_identical'))
    return a

report_sor._analyze_sor = _recording_analyze

def run_pairs(tag, twin_serial):
    # 12 distinct fibres shot on SN1, and file 13: a second shot of fibre 5
    # (same glass, fresh noise, so not a raw-identical copy) on twin_serial.
    d = os.path.join(sys.argv[2], tag)
    os.makedirs(d)
    for i in range(1, 13):
        nm = 'AAABBB%04d' % i
        with open(os.path.join(d, nm + '.sor'), 'w') as fh:
            fh.write('%s,SN1,%s,0' % (2190.0 + 2 * i, nm))
    with open(os.path.join(d, 'AAABBB0013.sor'), 'w') as fh:
        fh.write('2200.0,%s,AAABBB0005,0.005' % twin_serial)
    paths = sorted(os.path.join(d, f) for f in os.listdir(d))
    engine_twin.clear()
    got = []
    with contextlib.redirect_stdout(sys.stderr):
        R._emit_pairs(paths, d, {'sor': len(paths)}, got.append)
    m = got[-1]
    rows = [p for p in m['pairs'] if {p['fileA'], p['fileB']} == {'AAABBB0005', 'AAABBB0013'}]
    return {'n_flagged': m['n_flagged'], 'truncated': m['pairs_truncated'],
            'row': rows[0] if rows else None, 'engine': dict(engine_twin),
            'dup_rows': sum(1 for p in m['pairs'] if p['verdict'] != 'Unique'),
            'verdicts': sorted({p['verdict'] for p in m['pairs']})}

print(json.dumps({'capped': run_pairs('capped', 'SN2'), 'kept': run_pairs('kept', 'SN1')}))
"""


def test_a_pair_held_at_the_cap_reads_unique_and_the_list_matches_the_header(tmp_path):
    out = _run(_TWIN_SCRIPT, tmp_path)
    capped, kept = out["capped"], out["kept"]

    # The engine scored the twin as a duplicate and the serial gate held it at 0.5.
    assert capped["engine"]["p_dup_raw"] > 0.5, capped["engine"]
    assert capped["engine"]["serial_mismatch"] == "SN1 != SN2", capped["engine"]
    assert not capped["engine"]["raw_identical"], capped["engine"]
    assert capped["row"]["p_dup"] == 0.5, capped["row"]

    # Unique, like the workbook, and the list flags exactly what the header counts.
    assert capped["row"]["verdict"] == "Unique", capped["row"]
    assert not capped["truncated"]
    assert capped["n_flagged"] == 0 and capped["dup_rows"] == 0, capped

    # Control: the same twin on the same OTDR is not capped and stays confirmed.
    assert kept["engine"]["serial_mismatch"] is None, kept["engine"]
    assert kept["row"]["p_dup"] == 1.0, kept["row"]
    assert kept["row"]["verdict"] == "CONFIRMED duplicate", kept["row"]
    assert kept["n_flagged"] == 1 and kept["dup_rows"] == 1, kept

    for side in (capped, kept):
        assert "Possible duplicate" not in side["verdicts"], side["verdicts"]


def test_the_verdict_uses_the_workbook_line():
    out = _run(r"""
import sys, json
sys.path.insert(0, sys.argv[1])
import run_secretsauce as R
grid = [i / 1000 for i in range(1001)]
print(json.dumps({
    'wrong_side_of_line': [p for p in grid if (R._verdict(p) != 'Unique') != (p > 0.5)],
    'words': sorted({R._verdict(p) for p in grid}),
    'on_the_line': R._verdict(0.5),
    'just_over': R._verdict(0.5001),
    'below': R._verdict(0.3),
    'top_of_likely': R._verdict(0.99),
    'confirmed': R._verdict(0.9901),
}))
""")
    assert out["wrong_side_of_line"] == []
    assert out["words"] == ["CONFIRMED duplicate", "Likely duplicate", "Unique"]
    assert out["on_the_line"] == "Unique"
    assert out["just_over"] == "Likely duplicate"
    assert out["below"] == "Unique"
    assert out["top_of_likely"] == "Likely duplicate"
    assert out["confirmed"] == "CONFIRMED duplicate"
