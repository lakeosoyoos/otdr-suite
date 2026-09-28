"""FR mode on a pair whose span END the tech declared.

A tech can set the span end on any event: the receive reel's connector, or an
event short of it.  EXFO keeps everything past it in the file (the connector,
the reel, the fibre end), flags the chosen event as the span end (Status 0x80)
and leaves the end-of-fibre bit (0x04) on the real fibre end.

FastReporter's bidirectional table for such a pair, read off 432 of its own
.bdr files for a 432-fibre, 62.6 km, 500 ns span shot with a 1 km reel at each
end, the start declared on each launch connector and the end on the receive
side:

  * only the events inside each direction's own span make rows: nothing past
    A's end marker;
  * and only what lies within the matching tolerance (pulse + 20 m) of the
    OTHER direction's span.  B's launch mirrors 71.4 to 96.9 m past A's end on
    35 fibres (tolerance 71.0 m): it pairs with nothing and has no row, and on
    31 of them A's end row gets a synthesised B leg.  The mirror image drops
    A's end where it lies past B's launch (3 fibres);
  * a span marker is never folded into another row, and only the last row is
    the span end: where B's launch is within the tolerance but A's end pairs
    with a nearer B event, FR prints both rows, and A's is an ordinary row
    that the report grades (fibre 39, .345);
  * the synthesised leg's before-window never opens upstream of the silent
    direction's span start (FR stores SubCursorA 0.0), and a section whose
    fit window is empty stores 0.0.

FR mode used to pair A's end with B's launch through A's end-event window:
end rows 36 to 48 m from FastReporter's (fibre 1 at 62.551 km, .112, where FR
prints 62.515 km, .875), rows past the end that FR does not print, and the
fibre 39 cell missing from the report.  4,490 of 4,528 rows matched; now all
4,528 rows and 4,096 sections do.

Fixture declared_end/ = three of those pairs AS SHOT, identifiers scrubbed:
fibre 1 (B's launch 71.4 m past A's end), fibre 34 (A's end 71.4 m past B's
launch) and fibre 39 (A's end set on an event 71.4 m short of the connector,
B's launch 66.3 m past it, inside the tolerance).  The test declares the
tech's markers with the Viewer's set_span, which reproduces the tech's own
files (every event position, status and cursor to the micrometre), and
compares FR mode's table with FastReporter's.  The same pairs as shot keep
the table FastReporter printed for them before any marker was set, and OTDR
Suite mode keeps its own reading of the declared pairs.

Each engine runs in its own subprocess: the Viewer and the Splice Report ship
their own sor_reader324802a copies, never one process.
"""
import json
import subprocess
import sys
import textwrap

from conftest import FIXTURE_DIR, REPO_ROOT, VIEWER_DIR

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
FX = FIXTURE_DIR / "declared_end"

# The tech's markers, raw km from each OTDR port: (span start, span end).
# Starts on the launch connectors; ends on the receive-reel connectors,
# except fibre 39's A shot, whose end sits on an event 71.4 m short of it.
SPANS = {1: {"A": (1.0019, 63.5173), "B": (1.0044, 63.5912)},
         34: {"A": (1.0019, 63.5861), "B": (1.0044, 63.5173)},
         39: {"A": (0.9993, 63.5096), "B": (1.0044, 63.5810)}}
NAMES = {"A": "DSADSB%04d_1550.sor", "B": "DSBDSA%04d_1550.sor"}

# FastReporter's own table for each declared pair, from its .bdr: per row
# (MeanPosition m, Loss dB, Status & 0xC0, the synthesised leg), then the
# merged Loss of every section between consecutive rows.
FR_DECLARED = {
    1: ([(0.0, 0.170095766, 64, ""),
         (13638.517435, 0.013174069, 0, "a"),
         (20702.504689, 0.044333025, 0, "b"),
         (28067.304104, 0.025871332, 0, "b"),
         (35207.769026, 0.025469963, 0, "a"),
         (42200.377124, 0.025131999, 0, "a"),
         (49981.979828, 0.118115694, 0, ""),
         (55461.60473, 0.00787207, 0, ""),
         (56511.898035, 0.018901477, 0, "a"),
         (62515.394962, 0.87545858, 128, "b")],
        [2.545393693, 1.300490343, 1.363014369, 1.31395185, 1.301031804,
         1.461777765, 1.028154955, 0.198930177, 1.112760623]),
    34: ([(0.0, 0.031172468, 64, ""),
          (6886.813991, 0.012765939, 0, ""),
          (13597.729345, 0.02563965, 0, "a"),
          (20646.421066, 0.033729014, 0, "a"),
          (35164.431681, 0.019932768, 0, ""),
          (42116.251689, 0.014674536, 0, "a"),
          (49978.155945, 0.019328439, 0, "b"),
          (55448.858452, 0.029259296, 0, "b"),
          (62512.845706, 0.064639571, 128, "a")],
         [1.270421618, 1.232968162, 1.293534232, 2.683248694, 1.281065807,
          1.472811586, 1.022954476, 1.316014109]),
    39: ([(0.0, 0.1065938, 64, ""),
          (6899.560269, 0.008123622, 0, ""),
          (13681.85478, 0.016324553, 0, "a"),
          (20705.053945, 0.025120835, 0, "a"),
          (28048.184687, 0.029196777, 0, ""),
          (35172.079448, 0.01717291, 0, "a"),
          (42201.651751, 0.028308365, 0, ""),
          (49967.958922, 0.009593995, 0, ""),
          (55443.759941, 0.029946358, 0, "b"),
          (56485.130851, 0.038913285, 0, ""),
          (62498.824801, 0.344770658, 0, ""),
          (62576.577096, 0.237088697, 128, "a")],
         [1.26366665, 1.240063515, 1.292906734, 1.34949648, 1.306441538,
          1.302366719, 1.453754061, 1.032220478, 0.190459414, 1.114589654,
          0.07434082]),
}

# ...and for fibre 1 as shot, no markers: here FR pairs A's receive-reel
# connector with B's launch connector 94.3 m away, inside A's event window,
# and ends on the fibre end.  A declared end changes FR's table; its absence
# must not change ours.
FR_AS_SHOT_1 = (
    [(0.0, None, 64, ""),
     (1013.329099, 0.170095766, 0, ""),
     (14663.318184, 0.013235139, 0, "a"),
     (21704.362138, 0.044291855, 0, "b"),
     (29069.161552, 0.02594373, 0, "b"),
     (36232.569775, 0.025522158, 0, "a"),
     (43225.177873, 0.0251097, 0, "a"),
     (50995.308927, 0.118115694, 0, ""),
     (56474.933829, 0.00787207, 0, ""),
     (57536.698785, 0.019559858, 0, "a"),
     (63564.413639, 0.111549866, 0, ""),
     (64610.883061, None, 128, "")],
    [0.176254907, 2.547482084, 1.296188921, 1.362978312, 1.318260527,
     1.300928022, 1.459677914, 1.028154955, 0.200175357, 1.116050644,
     0.193779558])


def _declare(dst):
    """Copy the fixture pairs into dst/A, dst/B with the tech's markers set,
    the way the Viewer writes them."""
    code = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(VIEWER_DIR)!r})
        import trace_server as T
        FX, DST, SPANS, NAMES = {str(FX)!r}, {str(dst)!r}, {SPANS!r}, {NAMES!r}
        for side in ('A', 'B'):
            os.makedirs(os.path.join(DST, side))
            for fnum, span in SPANS.items():
                name = NAMES[side] % fnum
                data = open(os.path.join(FX, side, name), 'rb').read()
                start_km, end_km = span[side]
                out = T.set_span(data, start_km=start_km, end_km=end_km)
                with open(os.path.join(DST, side, name), 'wb') as f:
                    f.write(out)
        print('OK')
    """)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip().endswith("OK"), p.stderr


def _tables(dir_a, dir_b, fibres, mode="fr"):
    """fr_bidi_table for each pair, in the given analysis mode: its rows and
    section losses."""
    code = textwrap.dedent(f"""
        import json, os, sys
        sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})
        import sor_reader324802a as sr
        import splicereportmatchexfo as E
        E.ANALYSIS_MODE = {mode!r}
        DA, DB, NAMES = {str(dir_a)!r}, {str(dir_b)!r}, {NAMES!r}
        out = {{}}
        for fnum in {list(fibres)!r}:
            recs = []
            for side, d in (('A', DA), ('B', DB)):
                r = sr.parse_sor_full(os.path.join(d, NAMES[side] % fnum), trim=False)
                r['_source'] = 'sor'
                r['_span_side'] = side.lower()
                recs.append(r)
            rows = E.fr_bidi_table(*recs)
            out[fnum] = {{
                'rows': [(r['mean_pos_m'], r['loss'], int(r['status'] or 0) & 0xC0,
                          'a' if r['a']['synthetic'] else ('b' if r['b']['synthetic'] else ''))
                         for r in rows],
                'sections': [r['section']['loss'] if r['section'] else None for r in rows[:-1]],
                'last_section': rows[-1]['section'],
            }}
        print(json.dumps(out))
    """)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return {int(k): v for k, v in json.loads(p.stdout.strip().splitlines()[-1]).items()}


def _diffs(fnum, got, want):
    """Every place FR mode's table differs from FastReporter's: position to
    1e-6 m, losses to 1e-9 dB (the answer keys are quoted to 6 and 9 dp)."""
    rows, secs = want
    out = []
    if len(got["rows"]) != len(rows):
        return [(fnum, "rows", [round(r[0], 3) for r in got["rows"]], [r[0] for r in rows])]
    for (pos, loss, st, syn), (fpos, floss, fst, fsyn) in zip(got["rows"], rows):
        if abs(pos - fpos) > 2e-6:
            out.append((fnum, fpos, "position", pos))
        if (loss is None) != (floss is None) or (floss is not None and abs(loss - floss) > 2e-9):
            out.append((fnum, fpos, "loss", loss, floss))
        if (st, syn) != (fst, fsyn):
            out.append((fnum, fpos, "status / synthesised leg", (st, syn), (fst, fsyn)))
    for i, (s, fs) in enumerate(zip(got["sections"], secs)):
        if s is None or abs(s - fs) > 2e-9:
            out.append((fnum, rows[i][0], "section loss", s, fs))
    if got["last_section"] is not None:
        out.append((fnum, "the last row carries a section"))
    return out


def test_declared_span_end_table_is_fastreporters(tmp_path):
    _declare(tmp_path)
    got = _tables(tmp_path / "A", tmp_path / "B", sorted(SPANS))
    diffs = [d for fnum in sorted(SPANS) for d in _diffs(fnum, got[fnum], FR_DECLARED[fnum])]
    assert not diffs, diffs
    # the pieces the report reads off it: fibre 1 ends on A's own marker with
    # B synthesised; fibre 39's A end is an ordinary, gradable row
    assert got[1]["rows"][-1][2:] == [128, "b"], got[1]["rows"][-1]
    assert [r[2] for r in got[39]["rows"][-2:]] == [0, 128], got[39]["rows"][-2:]


def test_suite_mode_keeps_reading_the_ends_as_one_connector(tmp_path):
    """FR's end row on a declared end is its own artefact (A's end and B's
    launch are one connector the two directions place 71 m apart; the B leg
    FR synthesises there is fitted across B's launch connector).  OTDR Suite
    mode's table keeps pairing them, exactly as before."""
    _declare(tmp_path)
    got = _tables(tmp_path / "A", tmp_path / "B", [1], mode="suite")
    pos, loss, status, synth = got[1]["rows"][-1]
    assert (round(pos, 3), round(loss, 4), status, synth) == (62551.085, 0.1115, 128, ""), got[1]["rows"][-1]


def test_the_same_pairs_as_shot_keep_their_table():
    got = _tables(FX / "A", FX / "B", [1])
    diffs = _diffs(1, got[1], FR_AS_SHOT_1)
    assert not diffs, diffs


def test_only_a_moved_span_end_counts_as_declared(tmp_path):
    """The declared copies read as declared and the same files as shot do
    not.  A window cut off at the acquisition limit also has an end marker
    without the fibre-end bit, but nothing past it: not a declared end.  A
    tie panel with the end on its far panel is one."""
    _declare(tmp_path)
    code = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})
        import sor_reader324802a as sr
        import splicereportmatchexfo as E
        FIX, FX, DEC, NAMES = {str(FIXTURE_DIR)!r}, {str(FX)!r}, {str(tmp_path)!r}, {NAMES!r}
        for fnum in {sorted(SPANS)!r}:
            for side in ('A', 'B'):
                name = NAMES[side] % fnum
                shot = sr.parse_sor_full(os.path.join(FX, side, name), trim=False)
                declared = sr.parse_sor_full(os.path.join(DEC, side, name), trim=False)
                assert not E._fr_span_end_declared(shot), name
                assert E._fr_span_end_declared(declared), name
        cd = os.path.join(FIX, 'continuous')
        cut = sr.parse_sor_full(os.path.join(cd, sorted(os.listdir(cd))[0]), trim=False)
        end = [e for e in cut['exfo_events'] if not e.get('_is_section') and int(e.get('Status') or 0) & 0x80]
        assert end and not int(end[0]['Status']) & 0x04, end
        assert not E._fr_span_end_declared(cut)
        pj = os.path.join(FIX, 'paneljumper', 'A')
        panel = sr.parse_sor_full(os.path.join(pj, sorted(os.listdir(pj))[0]), trim=False)
        assert E._fr_span_end_declared(panel)
        print('OK')
    """)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip().endswith("OK"), p.stderr
