"""Suite mode: a declared span start is metadata; the report must not change.

A tech, FastReporter or our Viewer's span editor can declare the span start
on the launch connector.  The file then stores every event relative to that
connector while the samples still start at the OTDR port, a reel length
earlier, and GenParams carries the difference (the user offset).  The glass
is the same glass, so Suite mode has to print the same cells.

It did not.  The re-measure gate behind every one-direction cell
(_local_step_from_event) finds an event's raw-trace position through its twin
in the as-shot table, keyed off the port + launch pair at the top of that
table.  A declared start leaves no such pair, so the gate read the trace a
reel upstream, found flat glass there and refuted real losses.  On a
1152-fiber span with a 1.0 km launch reel and no receive reel, declaring the
start on the A files dropped the six A-only cells on its broken fibers
(469 .841 among them) and changed nothing else.

Fixture declared_start/ = 25 fiber pairs of that span as shot: the five
broken fibers that carry those cells, plus 20 healthy fibers with an event at
every closure so discovery finds the full cable's columns.  The test declares
the start on each A file the way the Viewer writes it (trace_server.set_span
on the launch connector) and runs the Suite-mode runner on both copies.

The Viewer and the Splice Report ship their own sor_reader324802a copies, so
each engine runs in its own subprocess, never one process.
"""
import json
import subprocess
import sys
import textwrap

from conftest import FIXTURE_DIR, VIEWER_DIR, run_splicereport

FX = FIXTURE_DIR / "declared_start"

# What the as-shot pair prints for its broken fibers: A sees each closure
# upstream of the break, B's trace cannot reach it.
A_ONLY = {(232, "232 .200 (A)"), (240, "240 .231 (A)"), (469, "469 .841 (A)"),
          (808, "808 .542 (A)"), (808, "808 .248 (A)"), (1152, "1152 .204 (A)")}


def _declare_span_start(src, dst):
    """Copy every .sor in `src` to `dst` with the span start declared on the
    launch connector (the event 0.9-1.1 km from the OTDR port).  Returns the
    declared starts in km."""
    code = textwrap.dedent(f"""
        import json, os, sys
        sys.path.insert(0, {str(VIEWER_DIR)!r})
        import trace_server as T
        src, dst = {str(src)!r}, {str(dst)!r}
        os.makedirs(dst)
        starts = []
        for fn in sorted(os.listdir(src)):
            data = open(os.path.join(src, fn), 'rb').read()
            _, bl = T.split(data)
            evs, _ = T._kev_parse(T._find(bl, b'KeyEvents').body)
            m_per_tot = T._TOT_M_PER_UNIT / T.read_ior(data)
            km = next(e['tot'] * m_per_tot / 1000.0 for e in evs
                      if 0.9 <= e['tot'] * m_per_tot / 1000.0 <= 1.1)
            with open(os.path.join(dst, fn), 'wb') as f:
                f.write(T.set_span(data, start_km=km))
            starts.append(km)
        print(json.dumps(starts))
    """)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


def _cells(man):
    # Positions are stored to 4 dp km.  The as-shot table is re-based by
    # subtracting two rounded positions and the declared copy rounds once, so
    # a km may differ by 0.1 m; everything the report prints must be identical.
    return sorted((c["fiber"], c["splice"], c["category"], c["label"],
                   None if c["loss"] is None else round(c["loss"], 3),
                   c["is_flagged"], c["borderline"]) for c in man["cells"])


def test_declared_span_start_leaves_the_suite_report_unchanged(tmp_path):
    a_declared = tmp_path / "A_declared"
    starts = _declare_span_start(FX / "A", a_declared)
    assert len(starts) == 25 and all(0.99 < s < 1.01 for s in starts), starts

    rc, as_shot, err = run_splicereport(FX / "A", FX / "B", tmp_path / "as_shot.xlsx")
    assert rc == 0 and as_shot and as_shot.get("ok"), err
    rc, declared, err = run_splicereport(a_declared, FX / "B", tmp_path / "declared.xlsx")
    assert rc == 0 and declared and declared.get("ok"), err

    assert {(c["fiber"], c["label"]) for c in as_shot["cells"]
            if c["category"] == "a_only"} == A_ONLY
    assert _cells(declared) == _cells(as_shot)
    km = {(c["fiber"], c["splice"]): c["km"] for c in as_shot["cells"]}
    for c in declared["cells"]:
        assert abs(c["km"] - km[(c["fiber"], c["splice"])]) <= 1.5e-4, c
    assert ([c["kind"] for c in declared["columns"]]
            == [c["kind"] for c in as_shot["columns"]])
    for c, ref in zip(declared["columns"], as_shot["columns"]):
        assert abs(c["km"] - ref["km"]) <= 1.5e-4, (c, ref)
