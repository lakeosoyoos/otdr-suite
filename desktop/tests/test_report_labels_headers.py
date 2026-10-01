"""Splice Report and Unidirectional report labels and headers (group 2).

Display only: every test here checks what a workbook or the hub prints, never
what the engine finds.  Shaped on a 432-fiber span whose two directions'
files store the same GenParams pair, with the direction in EXFO's own
LocationsDirection field.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import json
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"

_HELPERS = textwrap.dedent("""
    import json, os, tempfile
    import openpyxl
    import splicereportmatchexfo as E

    def ev(km, loss=0.05, end=False, refl=None):
        return {'dist_km': km, 'splice_loss': loss, 'is_end': end,
                'is_reflective': refl is not None, 'reflection': refl,
                'type': '1E9999LS' if end else '0F9999LS'}

    def header_rows(path, sheet='Splice Report'):
        ws = openpyxl.load_workbook(path)[sheet]
        return [[ws.cell(r, c).value for c in range(1, ws.max_column + 1)]
                for r in range(1, 4)]
""")


def _run(body: str) -> dict:
    code = _HELPERS + "\n" + textwrap.dedent(body)
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(SPLICEREPORT_DIR),
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── #7 No negative header ──────────────────────────────────────────────────

def test_far_connector_header_is_never_negative():
    """F354 alone: the far connector sits at 55.003 km from A on a 55.00 km
    span.  The B->A header is the span minus the A->B value, and that read
    "-0.01km, -33'"; it is floored at 0.00.  The A->B header is the column's
    own display distance, as before."""
    out = _run("""
        sp = [{'position_km': 55.003, 'position_km_refined': 55.003,
               'position_km_display': 55.0, 'column_kind': 'connector'}]
        p = os.path.join(tempfile.mkdtemp(), 'sr.xlsx')
        E.write_xlsx({}, sp, 354, 12, p, 'SITEA', 'NET-XX-SITEB-0001', 54.99)
        rows = header_rows(p)
        print(json.dumps({'b': rows[0][2], 'a': rows[1][2]}))
    """)
    assert out['a'].startswith('55.00km'), out
    assert out['b'] == "0.00km, 0'", out


# ── #22 Unidirectional B workbook direction ────────────────────────────────

RUNNER = SPLICEREPORT_DIR / "run_splicereport.py"


def _uni(folder, out, *extra):
    proc = subprocess.run([sys.executable, str(RUNNER), "--uni", "--dir-a",
                           str(folder), "--out", str(out), *extra],
                          cwd=str(SPLICEREPORT_DIR), capture_output=True,
                          text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_uni_header_names_the_shot_from_its_own_direction(tmp_path):
    """Both ends' files store GenParams (ELMDALE, MILL[E]R) in the same order;
    EXFO's LocationsDirection says 1 for the A shots and 2 for the B shots.
    The header prints the stored names in full, in the direction of the shot:
    the B folder's distances run from MILLER.  It read "ELM→MIL:" for both."""
    import openpyxl
    from conftest import FIXTURE_A_DIR, FIXTURE_B_DIR
    got = {}
    for side, folder in (("A", FIXTURE_A_DIR), ("B", FIXTURE_B_DIR)):
        out = tmp_path / f"uni_{side}.xlsx"
        m = _uni(folder, out)
        got[side] = (openpyxl.load_workbook(out)["Unidir Events"]["A1"].value,
                     m["uni"].get("direction_label"))
    assert got["A"] == ("ELMDALE → MILER:", "ELMDALE → MILER"), got
    assert got["B"] == ("MILLER → ELMDALE:", "MILLER → ELMDALE"), got


def test_uni_b_why_flagged_says_b_side():
    """A bend/damage row of a B shot says B-side, not A-side."""
    out = _run("""
        cols = [{'kind': 'bend_damage', 'position_km_refined': 8.0,
                 'position_km_display': 8.0, 'fiber_count': 3}]
        grid = {(0, 0): [(2, 0.42)]}
        try:
            rows = E.uni_flagged_event_rows(grid, cols, side='B')
        except TypeError:                      # before the fix: no side
            rows = E.uni_flagged_event_rows(grid, cols)
        print(json.dumps({'why': rows[0]['reason']}))
    """)
    assert "B-side event" in out["why"] and "A-side" not in out["why"], out


# ── #3 Site names ──────────────────────────────────────────────────────────

def test_uni_typed_site_names_print_in_the_direction_of_the_shot(tmp_path):
    """The names typed for the A end and the B end (WEST, EAST) print in the
    workbook and in the hub's summary line, ordered by the shot: a B shot
    reads "EAST → WEST"."""
    import openpyxl
    from conftest import FIXTURE_A_DIR, FIXTURE_B_DIR
    got = {}
    for side, folder in (("A", FIXTURE_A_DIR), ("B", FIXTURE_B_DIR)):
        out = tmp_path / f"uni_{side}.xlsx"
        m = _uni(folder, out, "--site-a", "WEST", "--site-b", "EAST")
        got[side] = (openpyxl.load_workbook(out)["Unidir Events"]["A1"].value,
                     m["uni"].get("direction_label"))
    assert got["A"] == ("WEST → EAST:", "WEST → EAST"), got
    assert got["B"] == ("EAST → WEST:", "EAST → WEST"), got


def test_sr_typed_site_names_print_everywhere_the_site_appears(tmp_path):
    """Pin: the Splice Report's two typed names reach the A-End / B-End
    header cells, the acquisition sheet, the Viewer table's end columns and
    the manifest the hub prints and names the file from.  (This already
    held before group 2; the test keeps it.)"""
    import openpyxl
    from conftest import FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR
    out = tmp_path / "sr.xlsx"
    vt = tmp_path / "vt.json"
    proc = subprocess.run([sys.executable, str(RUNNER),
                           "--dir-a", str(FIXTURE_SPLICE_A_DIR),
                           "--dir-b", str(FIXTURE_SPLICE_B_DIR),
                           "--out", str(out), "--site-a", "WEST",
                           "--site-b", "EAST", "--viewer-table", str(vt)],
                          cwd=str(SPLICEREPORT_DIR), capture_output=True,
                          text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-3000:]
    m = json.loads(proc.stdout.strip().splitlines()[-1])
    assert (m["site_a"], m["site_b"]) == ("WEST", "EAST")
    wb = openpyxl.load_workbook(out)
    ws = wb["Splice Report"]
    assert ws.cell(3, 2).value == "A-End ILA: WEST"
    assert ws.cell(3, ws.max_column).value == "B-End ILA: EAST"
    acq = [c.value for row in wb["Acquisition Parameters"].iter_rows()
           for c in row if isinstance(c.value, str)]
    assert "A-dir WEST" in acq and "B-dir EAST" in acq
    cols = json.loads(vt.read_text())["columns"]
    assert cols[0]["title"] == "A-End ILA: WEST"
    assert cols[-1]["title"] == "B-End ILA: EAST"


# ── #6 End cells name the direction of each reading ────────────────────────

def test_end_cell_names_the_direction_of_each_reflectance():
    """F180 of the 432-fiber span fails at the B end both ways: B reads its own launch
    connector at -48.3 dB, A reads the same connector at its far end at
    -48.7 dB.  The cell printed '180 REFL-48.3dB 180 REFL-48.7dB'."""
    out = _run("""
        issues = {180: {'a_tags': ['REFL-42.3dB'],
                        'b_tags': ['REFL-48.3dB', 'REFL-48.7dB'],
                        'refl_rules': {'A': ['tailbox'],
                                       'B': ['launch', 'tailbox']},
                        'severity': 'HIGH'}}
        cells, lca, lcb = E.build_ribbon_data({}, 432, 12, 0,
                                              launch_issues=issues)
        print(json.dumps({'a': lca[14]['text'], 'b': lcb[14]['text']}))
    """)
    assert out['b'] == '180 B→A REFL-48.3dB 180 A→B REFL-48.7dB', out
    assert out['a'] == '180 B→A REFL-42.3dB', out      # B's far end, at A


# ── #4 Legends: only the colors used, named for what they are ─────────────

def test_sr_legend_lists_only_the_colors_used_in_plain_words():
    """a 432-fiber span paints two colors: reburn pink and the orange cable-end
    cells, which hold end-connector reflectances.  The Legend listed eleven
    colors and called the orange ones "Launch / RESHOOT_DEAD_TRACE /
    BREAK_AT_PANEL"."""
    out = _run("""
        sp = [{'position_km': 21.86, 'position_km_refined': 21.86,
               'column_kind': 'splice', 'splice_display_num': 1}]
        cells = {(0, 0): {'text': '7 .172', 'is_break': False,
                          'is_broke': False}}
        lcb = {14: {'text': '180 B→A REFL-48.3dB 180 A→B REFL-48.7dB',
                    'severity': 'HIGH'}}
        p = os.path.join(tempfile.mkdtemp(), 'sr.xlsx')
        E.write_xlsx(cells, sp, 432, 12, p, 'SITEA', 'NET-XX-SITEB-0001', 55.03,
                     launch_cells_b=lcb)
        ws = openpyxl.load_workbook(p)['Legend']
        print(json.dumps([[c.value for c in r] for r in ws.iter_rows()]))
    """)
    assert out == [['Color', 'Meaning'],
                   ['Pink', 'Reburn (A and B average)'],
                   ['Orange', 'End connector reflectance']], out


def test_uni_legend_gives_connector_and_cable_end_their_own_rows():
    """The connector header shared the bend gold (and its cells the bend
    yellow), and the gray Cable End cells had no Legend row.  Each kind
    now has its own color and only the colors used are listed."""
    out = _run("""
        cols = [{'kind': 'bend_damage', 'position_km_refined': 8.0,
                 'position_km_display': 8.0, 'fiber_count': 3},
                {'kind': 'connector', 'position_km_refined': 20.0,
                 'position_km_display': 20.0, 'conn_all': {2: 0.6},
                 'conn_members': {2: 0.6}},
                {'kind': 'end', 'position_km_refined': 30.0,
                 'position_km_display': 30.0, 'end_members': {2: -45.0}}]
        grid = {(0, 0): [(2, 0.42)], (0, 1): [(2, 0.6)], (0, 2): [(2, -45.0)]}
        p = os.path.join(tempfile.mkdtemp(), 'uni.xlsx')
        E.uni_write_xlsx(grid, cols, 12, 12, 30.0, p, site_a='SITEA',
                         site_b='NET-XX-SITEB-0001')
        wb = openpyxl.load_workbook(p)
        ws = wb['Unidir Events']
        hdr = {ws.cell(3, c).value: ws.cell(3, c).fill.start_color.rgb[-6:]
               for c in range(2, 5)}
        cell = {ws.cell(3, c).value: ws.cell(4, c).fill.start_color.rgb[-6:]
                for c in range(2, 5)}
        leg = [[c.value for c in r] for r in wb['Legend'].iter_rows()]
        print(json.dumps({'hdr': hdr, 'cell': cell, 'leg': leg}))
    """)
    hdr, cell, leg = out['hdr'], out['cell'], out['leg']
    assert hdr['Connector 1'] != hdr['Bend/Damage 1'], hdr
    assert cell['Connector 1'] != cell['Bend/Damage 1'], cell
    names = [r[1] for r in leg[1:]]
    assert names == ['Bend/Damage', 'Bend/Damage', 'Connector', 'Connector',
                     'Cable End', 'Cable End'], leg
    assert ['Light Gray (cell)', 'Cable End'] in leg, leg
