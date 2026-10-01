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
