"""Build the sample span that Home -> View Sample Span opens.

Everything here is made up except the traces, which are the repository's
own 24-fibre test span (desktop/tests/fixtures/splice_A and splice_B).  The
production sheet is written to match them: its four splice vaults sit where
the Splice Report engine finds the four closures on those traces (14.56,
21.29, 47.05 and 61.51 km of a 67.55 km span), so the FQA package can take
its distances from the traces.  The photos are drawn placeholders, the GPS
points lie on a straight line between the two huts, and one splice has no
phone fix (the sample types one of them in by hand), so the GPS tab has
something to show.  Two more photos stand for ones added by hand.

Run from the repository root to regenerate demo/:

    python3 demo/build_demo_assets.py
"""
from __future__ import annotations

import io
import json
import math
import os
import shutil
import sys
from datetime import date

import openpyxl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, 'demo')
FIX = os.path.join(ROOT, 'desktop', 'tests', 'fixtures')
FT = 0.3048

SITE_A, SITE_Z = 'ELMDALE', 'MILLER'
SPAN_M = 67550
ENTRY_M = 60
SPLICES_M = [14559, 21286, 47053, 61508]            # where the traces put them
JOB_ID = 'demo0001'
A_FIX = (39.10, -100.50)
Z_FIX = (39.10, -100.50 + SPAN_M / 1000 / (111.32 * math.cos(math.radians(39.10))))


def _along(m, wiggle=0.0):
    f = m / SPAN_M
    return (round(A_FIX[0] + (Z_FIX[0] - A_FIX[0]) * f + wiggle, 6),
            round(A_FIX[1] + (Z_FIX[1] - A_FIX[1]) * f, 6))


def _dms(lat, lon):
    def part(v):
        v = abs(v)
        d = int(v)
        m = int((v - d) * 60)
        return f'{d} {m} {((v - d) * 60 - m) * 60:.2f}'
    return f"{part(lat)} N {part(lon)} W"


def _termination(ws, building, address):
    ws['C3'] = 'Termination Location Worksheet'
    ws['C5'] = 'Location Information'
    ws['C6'], ws['G6'] = 'Customer', 'Demo'
    ws['C7'], ws['G7'] = 'Building', building
    ws['C8'], ws['G8'] = 'Address', address
    ws['C10'], ws['G10'] = 'Bay/Shelf', 'RR 100.01'
    ws['C13'] = 'Fiber Distribution Frame Information'
    ws['C14'], ws['H14'] = 'Manufacturer', 'Commscope'
    ws['C15'], ws['H15'] = 'Model/RU', 'NG4'
    ws['C16'], ws['H16'] = 'Capacity Ports', '48'
    ws['C19'] = 'Installer Information'
    ws['C20'], ws['H20'] = 'Company', 'Demo Splicing'
    ws['C23'], ws['H23'] = 'Work Order', 'Sample span'
    ws['C25'] = 'Cable Information'
    ws['C26'], ws['O26'], ws['V26'], ws['AC26'] = (
        'Cable Part Number', 'Sequential at', 'Sequential at', 'From')
    ws['O27'], ws['V27'], ws['AC27'] = 'Frame', 'Duct', 'Location'
    ws['C28'] = 'Commscope 24f 04/25'
    ws['N28'], ws['U28'], ws['AB28'] = '00100ft', '00150ft', 'Cable 1'
    ws['C31'] = 'Termination Information'
    ws['C32'], ws['I32'], ws['R32'], ws['X32'], ws['AC32'] = (
        'Mounting Postion', 'From Cable (Direction)',
        'From Fibers (colors or numbers)', 'To Jack Number(s) in Frame',
        'Connector Type (SC,ST,FC,ect.)')
    ws['C34'], ws['I34'], ws['R34'], ws['X34'], ws['AC34'] = (
        'RMU 8', 'Cable 1', '1-24', '1-24', 'LC/UPC')


def _splice(ws, name, address, west_ft, east_ft):
    ws['C3'] = 'Splice Location Worksheet'
    ws['C5'] = 'Location Information'
    ws['C6'], ws['G6'] = 'Name', name
    ws['C7'], ws['G7'] = 'Address', address
    ws['C9'], ws['G9'] = 'Placement', 'MH'
    ws['C15'] = 'Enclosure Information'
    ws['C16'], ws['G16'] = 'Manufacturer', 'Commscope'
    ws['C21'] = 'Installer Information'
    ws['C22'], ws['G22'] = 'Company', 'Demo Splicing'
    ws['C23'], ws['G23'] = 'Technician(s)', 'Demo'
    ws['C24'], ws['G24'] = 'Date', date(2026, 5, 6)
    ws['C27'] = 'Cable Information'
    ws['D28'], ws['M28'], ws['S28'], ws['Y28'] = (
        'Cable Part Number', 'Sequential at', 'Sequential at', 'From')
    ws['M29'], ws['S29'], ws['Y29'] = 'Enclosure', 'Duct', 'Direction'
    row = 30
    for mark, heading in ((west_ft, 'West'), (east_ft, 'East')):
        if mark is None:
            continue
        ws[f'C{row}'] = 'Corning 24f 07/26'
        ws[f'L{row}'] = mark
        ws[f'R{row}'] = mark + 50
        ws[f'X{row}'] = heading
        row += 1
    ws['C37'] = 'Splice Information'


def production_sheet(path):
    """One cable with sequential footage marks: each location's mark is its
    distance from the A hut, so the marks agree with the traces."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    _termination(wb.create_sheet(f'{SITE_A.title()} Hut'), f'{SITE_A.title()} Hut',
                 '100 Demo Road')
    ft = lambda m: int(round(m / FT))
    _splice(wb.create_sheet(f'{SITE_A.title()} Entry'), f'{SITE_A.title()} Entry',
            '100 Demo Road', None, ft(ENTRY_M))
    n = len(SPLICES_M)
    for i, m in enumerate(SPLICES_M):
        vault = n - i                          # vaults count down from A, as on real spans
        _splice(wb.create_sheet(f'Splice {vault}'), f'Splice {vault}',
                _dms(*_along(m)), ft(m), ft(m))
    _splice(wb.create_sheet(f'{SITE_Z.title()} Entry'), f'{SITE_Z.title()} Entry',
            '200 Demo Road', ft(SPAN_M - ENTRY_M), None)
    _termination(wb.create_sheet(f'{SITE_Z.title()} Hut'), f'{SITE_Z.title()} Hut',
                 '200 Demo Road')
    wb.save(path)


def _photo(text, color):
    from PIL import Image, ImageDraw
    im = Image.new('RGB', (1280, 960), color)
    d = ImageDraw.Draw(im)
    d.rectangle([80, 80, 1200, 880], outline=(255, 255, 255), width=8)
    d.text((120, 120), text, fill=(255, 255, 255))
    d.text((120, 820), 'SAMPLE PHOTO', fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=80)
    return buf.getvalue()


def capture_package(path):
    """What Field Capture would send: both huts' photos and fixes, and a GPS
    fix at every splice point but one."""
    sys.path.insert(0, ROOT)
    import folder_intake
    photos = {
        'photos/A-1-1.jpg': _photo(f'{SITE_A} rack label  RR 100.01', (44, 91, 138)),
        'photos/A-1-2.jpg': _photo(f'{SITE_A} panel  RMU 8  fibers 1-24', (60, 110, 70)),
        'photos/Z-1-1.jpg': _photo(f'{SITE_Z} rack label  RR 100.01', (140, 70, 50)),
        'photos/Z-1-2.jpg': _photo(f'{SITE_Z} panel  RMU 8  fibers 1-24', (110, 60, 120)),
    }
    ok = lambda k: {'key': k, 'status': 'ok'}
    site = lambda end, fix: {
        'site': end, 'floor': '001', 'room': '0001', 'aisle': '100', 'bay': '001',
        'panel': {'rmu': '8', 'termination': 'LC', 'panelType': 'NG4'},
        'photos': [n for n in photos if n.startswith(f'photos/{end}-')],
        'gps': {'lat': fix[0], 'lon': fix[1], 'acc': 5},
        'checks': [ok('rack'), ok('rmu'), ok('toward'), ok('fibers')]}
    events = [ENTRY_M] + SPLICES_M + [SPAN_M - ENTRY_M]
    splices = []
    for no, m in enumerate(events, 1):
        lat, lon = _along(m, wiggle=0.002 * math.sin(no))
        splices.append({'event': no, 'gps': None if no in (4, 5) else {'lat': lat, 'lon': lon, 'acc': 6}})
    capture = {'format': 'otdr-capture', 'v': 1, 'job': JOB_ID, 'created': '2026-05-07T16:20:00Z',
               'initials': 'DEMO', 'sites': [site('A', A_FIX), site('Z', Z_FIX)],
               'splices': splices}
    files = {'capture.json': json.dumps(capture, indent=1).encode('utf-8'), **photos}
    tmp = os.path.join(DEMO, '_zfc')
    shutil.rmtree(tmp, ignore_errors=True)
    for name, data in files.items():
        p = os.path.join(tmp, *name.split('/'))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'wb') as fh:
            fh.write(data)
    folder_intake.share_write(path, 'field-capture',
                              {n: os.path.join(tmp, *n.split('/')) for n in files},
                              {'job': JOB_ID})
    shutil.rmtree(tmp, ignore_errors=True)


def main():
    for d in ('A', 'B'):
        dest = os.path.join(DEMO, 'traces', d)
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(os.path.join(FIX, f'splice_{d}'), dest)
    production_sheet(os.path.join(DEMO, 'Demo Production Sheet.xlsx'))
    pics = os.path.join(DEMO, 'pictures')
    os.makedirs(pics, exist_ok=True)
    for name, text, color in (('Splice 3 closure.jpg', 'Splice 3 closure, trays 1-2', (90, 90, 90)),
                              ('Vault lid.jpg', 'Vault lid, Splice 2', (120, 100, 60))):
        with open(os.path.join(pics, name), 'wb') as fh:
            fh.write(_photo(text, color))
    capture_package(os.path.join(DEMO, 'Demo Field Capture.zfc'))
    print('demo assets written to', DEMO)


if __name__ == '__main__':
    main()


def reports():
    """Real engine output on the sample traces, for the Sample Span's Reports
    tab: a Splice Report, a Unidirectional report and a Secret Sauce run,
    plus the Splice Report's closures (the FQA distances)."""
    import subprocess
    import tempfile
    sys.path.insert(0, ROOT)
    import app
    out = os.path.join(DEMO, 'reports')
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    a, b = os.path.join(DEMO, 'traces', 'A'), os.path.join(DEMO, 'traces', 'B')
    proc = app.run_engine(app.splicereport_cmd(
        a, b, os.path.join(out, f'{SITE_A}_to_{SITE_Z}_SpliceReport.xlsx'), SITE_A, SITE_Z,
        analysis='suite'))
    manifest = app._parse_manifest(proc.stdout)
    assert manifest and manifest.get('ok'), proc.stderr[-500:]
    manifest.pop('xlsx', None)              # a local path: not for the repository
    with open(os.path.join(DEMO, 'closures.json'), 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh)
    proc = app.run_engine(app.uni_cmd(a, os.path.join(out, 'unidirectional_events.xlsx')))
    assert os.path.isfile(os.path.join(out, 'unidirectional_events.xlsx')), proc.stderr[-500:]
    with tempfile.TemporaryDirectory() as tmp:
        both = os.path.join(tmp, 'both')
        shutil.copytree(a, os.path.join(both, 'A'))
        shutil.copytree(b, os.path.join(both, 'B'))
        ss_out = os.path.join(out, 'Secret Sauce')
        proc = app.run_engine(app.secretsauce_cmd(both, ss_out, 'xlsx'))
        assert os.listdir(ss_out), proc.stderr[-500:]
    print('reports written to', out)


if __name__ == '__main__' and '--reports' in sys.argv:
    reports()
