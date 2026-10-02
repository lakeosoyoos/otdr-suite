"""The Viewer's trace-settings dialog shows FastReporter's Test Parameters and
Test Settings panel for the clicked file, row for row (Robert 2026-09-30:
"we need to include all of these displayed").

The values are read from the fields FR reads; a missing one stays None and
prints '---', never a default.
"""
import os
import shutil
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'viewer'))

import trace_server as TS                       # noqa: E402
read_test_panel = TS.read_test_panel
from test_sor_writer import make_sor            # noqa: E402

FIX = os.path.join(HERE, 'fixtures')


def _first_sor(*parts):
    d = os.path.join(FIX, *parts)
    return os.path.join(d, sorted(f for f in os.listdir(d) if f.endswith('.sor'))[0])


LONG = _first_sor('splice_A')                 # a 500 ns span shot
TIE = _first_sor('strayreshoot', 'B')         # a 5 ns tie-panel shot


def _panel(path):
    with open(path, 'rb') as f:
        return read_test_panel(f.read())


def test_long_shot_reads_every_fr_row():
    p = _panel(LONG)
    assert p['wavelength_nm'] == 1550.0
    assert p['range_km'] == pytest.approx(100.0)
    assert p['pulse_ns'] == 500.0
    assert p['duration_s'] == 60.0
    assert p['ior'] == pytest.approx(1.47)
    assert p['backscatter_db'] == pytest.approx(-83.0)
    assert p['helix_pct'] == pytest.approx(0.0)
    assert p['splice_thr_db'] == pytest.approx(0.02)
    assert p['splitter_on'] is False
    assert p['splitter_thr_db'] == pytest.approx(2.5)
    assert p['refl_thr_db'] == pytest.approx(-78.0)
    assert p['eof_thr_db'] == pytest.approx(5.0)
    assert p['fiber_type'] == 652


def test_resolution_is_frs_sample_pitch():
    # FR's panel: 3.125 ns sampling at IOR 1.47 prints 0.319 m.  Same formula
    # on the fixtures: 25 ns -> 2.549 m, 0.78125 ns -> 0.080 m.
    assert round(_panel(LONG)['resolution_m'], 3) == 2.549
    assert round(_panel(TIE)['resolution_m'], 3) == 0.080
    assert round(299_792_458.0 * 3.125e-9 / 2 / 1.47, 3) == 0.319


def test_tie_panel_shot_range_and_pulse():
    p = _panel(TIE)
    assert p['range_km'] == pytest.approx(2.5)
    assert p['pulse_ns'] == 5.0
    assert p['duration_s'] == 30.0


def test_a_file_without_the_exfo_block_never_invents_settings():
    p = read_test_panel(make_sor(ior=1.47))
    for k in ('backscatter_db', 'helix_pct', 'splice_thr_db', 'refl_thr_db',
              'eof_thr_db', 'range_km', 'resolution_m'):
        assert p[k] is None, k


def test_garbage_bytes_give_an_empty_panel():
    p = read_test_panel(b'not a sor file')
    assert all(v is None for v in p.values())


def test_trace_settings_carries_the_panel(tmp_path):
    d = tmp_path / 'PNAPNB A'
    d.mkdir()
    shutil.copy(LONG, d / 'PNAPNB0001.sor')
    s = TS.trace_settings('a', 1, dir_a=str(d))
    assert s['panel']['eof_thr_db'] == pytest.approx(5.0)
    assert s['panel']['range_km'] == pytest.approx(100.0)


def test_a_json_export_has_no_panel(tmp_path):
    d = tmp_path / 'j'
    d.mkdir()
    (d / 'DNN1DNN20001.json').write_text('{}', encoding='utf-8')
    assert TS.trace_settings('a', 1, dir_a=str(d))['panel'] is None


def test_the_dialog_prints_every_fr_row_in_frs_order():
    with open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8') as f:
        h = f.read()
    body = h.split('function frPanelHtml', 1)[1].split('function closeEditDialog', 1)[0]
    assert 'Test Parameters</th>' in body and 'Test Settings</th>' in body
    rows = ["'Wavelength'", "'Range'", "'Pulse'", "'Duration'",
            "'Resolution'", "'IOR'", "'Backscatter'",
            "'Helix Factor'", "'Splice Loss Detection Threshold'",
            'Splitter Loss (SM Only)', "'Reflectance Detection Threshold'",
            "'End-of-Fiber Detection Threshold'", "'Fiber Core Size'"]
    at = [body.find(r) for r in rows]
    assert all(i >= 0 for i in at), [r for r, i in zip(rows, at) if i < 0]
    assert at == sorted(at)
    # IOR stays the editable input the save path reads.
    assert 'id="edit-ior"' in body
