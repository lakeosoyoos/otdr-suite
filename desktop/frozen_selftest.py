"""Frozen self-test: use the built OTDR Suite exe the way a tech does.

The boot self-test in CI only proves that the exe answers /_stcore/health.
That stays green when the bundle is missing a library that only a report or
a page needs.  This script goes further:

  1. Reports.  The exe runs every engine on the 24-fibre test pair in
     tests/fixtures: Splice Report (OTDR Suite and FastReporter analysis,
     both with the Viewer table), Unidirectional, Duplicate Check (Excel and
     pairs) and the FastReporter table for one fibre.  Each run must print a
     JSON manifest with "ok": true and write the files it names.
  2. Same answers as the source.  Both Splice Report runs are repeated from
     source with this Python.  Every cell of every sheet, the manifests and
     the Viewer table must match, paths aside.  A library that is missing
     from the bundle, or a different version of it, shows up here.
  3. First page.  The exe starts as the hub, the script waits for
     /_stcore/health, then plays one browser visit over the websocket.  The
     page must finish its run with no exception box on it.  Then the exe and
     everything it started are stopped.

Usage, from the desktop folder, with the Python the build used:

    python frozen_selftest.py dist\\OTDRApp\\OTDRApp.exe [--work DIR]

Exit code 0 when everything passed, 1 otherwise; the reasons are printed.

Everything runs with OTDR_SUITE_NO_UPDATE=1 and a scratch home folder, so
the tech's (or the runner's) own settings and engine cache are never used.
HTTP and HTTPS proxies point at a closed local port, so nothing reaches the
network: no update check, and a failure here is never posted to the team's
error channel.  Every call has a time limit.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent
REPO = DESKTOP.parent
FIXTURES = DESKTOP / "tests" / "fixtures"
WINDOWS = os.name == "nt"

# Time limits, in seconds.
ENGINE_TIMEOUT = 600        # one engine run (Duplicate Check is the slowest)
HEALTH_TIMEOUT = 180        # exe start until /_stcore/health says ok
RENDER_TIMEOUT = 240        # one page run over the websocket
STOP_TIMEOUT = 30           # stopping the hub and its children

# Engine stderr lines that mean something broke even if the manifest says ok
# (the engines catch some errors, print them and carry on).
BAD_STDERR = ("Traceback (most recent call last)", "No module named",
              "ImportError", "DLL load failed")

# A proxy on a port nothing listens on: any HTTP(S) request the exe makes to
# the outside world fails at once.  Local requests skip it (NO_PROXY).
DEAD_PROXY = "http://127.0.0.1:9"

failures: list[str] = []
steps: list[tuple[str, str, float]] = []      # (name, PASS/FAIL, seconds)


def fail(msg: str) -> None:
    failures.append(msg)
    print("  FAIL: " + msg, flush=True)


def note(msg: str) -> None:
    print("  " + msg, flush=True)


def hub_port() -> int:
    """The port the launcher serves the hub on (launcher.PORT)."""
    text = (DESKTOP / "launcher.py").read_text(encoding="utf-8")
    m = re.search(r"^PORT\s*=\s*(\d+)", text, re.MULTILINE)
    return int(m.group(1)) if m else 8510


# ── Environment ──────────────────────────────────────────────────────────
def make_env(work: Path) -> dict:
    home = work / "home"
    tmp = work / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # Never let a value from a parent hub or a dev shell pick the engine.
    for k in ("OTDR_SUITE_HOME", "OTDR_SUITE_SOURCE", "OTDR_SUITE_RESTART_FROM",
              "SS_ERROR_WEBHOOK"):
        env.pop(k, None)
    # The launcher opens a browser once the hub is up.  Point it at a
    # program that does nothing, so no real browser starts a second session.
    if WINDOWS:
        noop = work / "no_browser.cmd"
        noop.write_text("@exit /b 0\r\n", encoding="ascii")
        browser = str(noop)
    else:
        browser = shutil.which("true") or "true"
    env.update({
        "OTDR_SUITE_NO_UPDATE": "1",
        "HOME": str(home),
        "USERPROFILE": str(home),          # Path.home() on Windows
        "TMPDIR": str(tmp), "TEMP": str(tmp), "TMP": str(tmp),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "BROWSER": browser,
        "HTTP_PROXY": DEAD_PROXY, "HTTPS_PROXY": DEAD_PROXY,
        "http_proxy": DEAD_PROXY, "https_proxy": DEAD_PROXY,
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
    })
    return env


# ── Running one engine ───────────────────────────────────────────────────
def last_json(stdout: str):
    """The hub reads the LAST JSON line of an engine's stdout; so do we."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def run_engine(label: str, argv: list, env: dict, work: Path,
               must_write: tuple = ()):
    """Run one engine command; return its manifest, or None after a FAIL."""
    argv = [str(a) for a in argv]
    logs = work / "logs"
    logs.mkdir(exist_ok=True)
    t0 = time.perf_counter()
    try:
        p = subprocess.run(argv, env=env, cwd=str(work / "tmp"),
                           capture_output=True, timeout=ENGINE_TIMEOUT)
    except subprocess.TimeoutExpired:
        dt = time.perf_counter() - t0
        fail(f"{label}: no answer within {ENGINE_TIMEOUT} s")
        steps.append((label, "FAIL", dt))
        return None
    dt = time.perf_counter() - t0
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    (logs / f"{label}.stdout.txt").write_text(out, encoding="utf-8")
    (logs / f"{label}.stderr.txt").write_text(err, encoding="utf-8")

    n_before = len(failures)
    manifest = last_json(out)
    if p.returncode != 0:
        fail(f"{label}: exit code {p.returncode}")
    if manifest is None:
        fail(f"{label}: printed no JSON manifest")
    elif manifest.get("ok") is not True:
        fail(f"{label}: manifest says ok={manifest.get('ok')!r}, "
             f"error: {manifest.get('error')!r}")
    for bad in BAD_STDERR:
        if bad in err:
            fail(f"{label}: stderr contains {bad!r}")
            break
    for path in must_write:
        path = Path(path)
        if not path.is_file() or path.stat().st_size == 0:
            fail(f"{label}: did not write {path.name}")
    ok = len(failures) == n_before
    if not ok:
        tail = err.strip().splitlines()[-15:]
        if tail:
            note(f"{label}: last lines of stderr:")
            for line in tail:
                note("    " + line[:300])
    steps.append((label, "PASS" if ok else "FAIL", dt))
    note(f"{label}: {'ok' if ok else 'FAILED'} in {dt:.1f} s")
    return manifest if ok else None


# ── Comparing frozen and source outputs ──────────────────────────────────
def normalise(obj, swaps):
    """Replace run-specific folders in every string with a fixed tag."""
    if isinstance(obj, str):
        for old, new in swaps:
            obj = obj.replace(old, new)
        return obj
    if isinstance(obj, list):
        return [normalise(x, swaps) for x in obj]
    if isinstance(obj, dict):
        return {k: normalise(v, swaps) for k, v in obj.items()}
    return obj


def path_swaps(folder: Path, tag: str = "<OUT>"):
    s = str(folder)
    return [(s, tag), (folder.as_posix(), tag)]


def same_value(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b


def first_diff(a, b, where="") -> str | None:
    """Where two JSON values first differ, in words, or None if equal."""
    if type(a) is not type(b):
        return f"{where or 'top'}: {type(a).__name__} vs {type(b).__name__}"
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                return f"{where}/{k}: only in {'source' if k not in a else 'frozen'}"
            d = first_diff(a[k], b[k], f"{where}/{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{where or 'top'}: {len(a)} items vs {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_diff(x, y, f"{where}[{i}]")
            if d:
                return d
        return None
    if not same_value(a, b):
        return f"{where or 'top'}: {a!r} vs {b!r}"
    return None


def _colour(c):
    if c is None:
        return None
    kind = getattr(c, "type", None)
    if kind == "rgb":
        return c.rgb
    if kind == "theme":
        return f"theme{c.theme}/{c.tint}"
    if kind == "indexed":
        return f"indexed{c.indexed}"
    return None


# The build stamp the Acquisition Parameters sheet carries, for example
# "OTDR Suite · app build 54 (2026-07-14 10:00 PDT) · engine: bundled".  It
# names the code that ran, so it is meant to differ: the exe says "engine:
# bundled", a source run says "dev" or "engine: dev".  It is compared on its
# own (see compare_workbooks), not as a number.
# The App's engines say "OTDR App" (the edition its launcher hands down).
STAMP_RE = re.compile(r"^OTDR (Suite|App) \u00b7 ")
STAMP = "<BUILD STAMP>"


def workbook_cells(path: Path, swaps, stamps: list | None = None) -> dict:
    """{sheet: {"merged": [...], "cells": {coord: (value, look)}}}.
    Build stamps are replaced by STAMP and their text appended to `stamps`."""
    import openpyxl
    wb = openpyxl.load_workbook(path)
    out = {}
    for ws in wb.worksheets:
        cells = {}
        for row in ws.iter_rows():
            for c in row:
                fill = _colour(c.fill.fgColor) if c.fill.fill_type else None
                look = (c.number_format, fill, _colour(c.font.color), bool(c.font.b))
                value = normalise(c.value, swaps)
                if isinstance(value, str) and STAMP_RE.match(value):
                    if stamps is not None:
                        stamps.append(value)
                    value = STAMP
                if value is None and look == ("General", None, None, False):
                    continue
                cells[c.coordinate] = (value, look)
        out[ws.title] = {"merged": sorted(str(r) for r in ws.merged_cells.ranges),
                         "cells": cells}
    return out


def compare_workbooks(label: str, frozen: Path, source: Path, fdir: Path, sdir: Path):
    stamps: list = []
    try:
        a = workbook_cells(frozen, path_swaps(fdir), stamps)
        b = workbook_cells(source, path_swaps(sdir))
    except Exception as exc:  # noqa: BLE001
        fail(f"{label}: could not read the workbooks ({type(exc).__name__}: {exc})")
        return
    # The exe must say it ran its own bundled engine (auto-update is off).
    for stamp in stamps:
        if "engine: bundled" not in stamp:
            fail(f"{label}: the exe's build stamp does not say 'engine: bundled': {stamp!r}")
    if list(a) != list(b):
        fail(f"{label}: sheets differ: frozen {list(a)} vs source {list(b)}")
        return
    n_cells = 0
    n_bad = 0
    for sheet in a:
        if a[sheet]["merged"] != b[sheet]["merged"]:
            fail(f"{label}: sheet {sheet!r} merged cells differ")
        ca, cb = a[sheet]["cells"], b[sheet]["cells"]
        for coord in sorted(set(ca) | set(cb)):
            n_cells += 1
            va, vb = ca.get(coord), cb.get(coord)
            if va is None or vb is None or not same_value(va[0], vb[0]) or va[1] != vb[1]:
                n_bad += 1
                if n_bad <= 5:
                    fail(f"{label}: {sheet}!{coord} frozen {va!r} vs source {vb!r}")
    if n_bad > 5:
        fail(f"{label}: ... {n_bad} differing cells in all")
    if n_bad == 0:
        note(f"{label}: {len(a)} sheets, {n_cells} cells, all the same "
             f"(build stamp {stamps[0] if stamps else 'not present'!r})")


def compare_json(label: str, a, b, fdir: Path, sdir: Path):
    d = first_diff(normalise(a, path_swaps(fdir)), normalise(b, path_swaps(sdir)))
    if d:
        fail(f"{label}: frozen and source differ at {d}"[:600])
    else:
        note(f"{label}: same")


# ── The hub and its first page ───────────────────────────────────────────
def port_answers(port: int) -> bool:
    s = socket.socket()
    s.settimeout(1)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def health_ok(port: int) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/_stcore/health", timeout=2) as r:
            return r.status == 200 and r.read().strip() == b"ok"
    except Exception:  # noqa: BLE001
        return False


def first_render(port: int, timeout: float, linger_s: float = 3.0) -> dict:
    """Play one browser visit: open the websocket, ask for a script run, and
    collect what the server sends until the run finishes (plus `linger_s`
    for anything late).  Uses Streamlit's own message classes."""
    from streamlit.proto.Alert_pb2 import Alert
    from streamlit.proto.BackMsg_pb2 import BackMsg
    from streamlit.proto.ForwardMsg_pb2 import ForwardMsg
    from websockets.sync.client import connect

    finished_names = {v.number: v.name for v in ForwardMsg.DESCRIPTOR.fields_by_name[
        "script_finished"].enum_type.values}
    error_box = Alert.Format.Value("ERROR")
    res = {"finished": [], "exceptions": [], "errors_shown": [], "elements": 0,
           "seconds": None, "error": None}
    t0 = time.perf_counter()
    done_at = None
    kwargs = dict(subprotocols=["streamlit"], open_timeout=30, max_size=None,
                  close_timeout=2)
    try:
        try:
            ws_cm = connect(f"ws://127.0.0.1:{port}/_stcore/stream", proxy=None, **kwargs)
        except TypeError:            # websockets older than 15 has no proxy=
            ws_cm = connect(f"ws://127.0.0.1:{port}/_stcore/stream", **kwargs)
        with ws_cm as ws:
            back = BackMsg()
            back.rerun_script.query_string = ""
            back.rerun_script.page_script_hash = ""
            ws.send(back.SerializeToString())
            while True:
                now = time.perf_counter()
                if now - t0 > timeout:
                    res["error"] = f"page run did not finish within {timeout:.0f} s"
                    break
                if done_at is not None and now - done_at > linger_s:
                    break
                try:
                    raw = ws.recv(timeout=0.5)
                except TimeoutError:
                    continue
                msg = ForwardMsg()
                msg.ParseFromString(raw if isinstance(raw, bytes) else raw.encode())
                kind = msg.WhichOneof("type")
                if kind == "delta" and msg.delta.WhichOneof("type") == "new_element":
                    el = msg.delta.new_element
                    res["elements"] += 1
                    what = el.WhichOneof("type")
                    if what == "exception":
                        res["exceptions"].append(
                            f"{el.exception.type}: {el.exception.message}"[:400])
                    elif what == "alert" and el.alert.format == error_box:   # st.error
                        res["errors_shown"].append(el.alert.body[:200])
                elif kind == "script_finished":
                    name = finished_names.get(msg.script_finished, str(msg.script_finished))
                    res["finished"].append(name)
                    if name in ("FINISHED_SUCCESSFULLY", "FINISHED_WITH_COMPILE_ERROR"):
                        done_at = time.perf_counter()
                        res["seconds"] = round(done_at - t0, 2)
    except Exception as exc:  # noqa: BLE001
        res["error"] = f"{type(exc).__name__}: {exc}"
    return res


def stop_tree(proc: subprocess.Popen) -> None:
    """Stop the hub and every process it started."""
    if WINDOWS:
        if proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass
    else:
        # The exe was started in its own session, so its process group holds
        # it and everything it started (engines, trace server).
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(STOP_TIMEOUT / 2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        fail(f"hub: process {proc.pid} still running after the stop")


def check_hub(exe: Path, env: dict, work: Path) -> None:
    label = "hub first page"
    print(f"\n[3] {label}", flush=True)
    port = hub_port()
    t0 = time.perf_counter()
    n_before = len(failures)
    if port_answers(port):
        fail(f"hub: something already answers on port {port}; "
             "is an earlier copy of the app still running?")
        steps.append((label, "FAIL", 0.0))
        return
    flags = {}
    if WINDOWS:
        flags["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        flags["start_new_session"] = True
    proc = subprocess.Popen([str(exe)], env=env, cwd=str(work / "tmp"),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, **flags)
    try:
        up = None
        while time.perf_counter() - t0 < HEALTH_TIMEOUT:
            if proc.poll() is not None:
                break
            if health_ok(port):
                up = time.perf_counter() - t0
                break
            time.sleep(0.5)
        if up is None:
            if proc.poll() is not None:
                fail(f"hub: the exe exited with code {proc.returncode} before it served health")
            else:
                fail(f"hub: /_stcore/health never said ok within {HEALTH_TIMEOUT} s")
        else:
            note(f"hub: health ok after {up:.1f} s")
            r = first_render(port, RENDER_TIMEOUT)
            if r["error"]:
                fail(f"hub: {r['error']}")
            if "FINISHED_SUCCESSFULLY" not in r["finished"]:
                fail(f"hub: the page never finished its run (statuses: {r['finished']})")
            for e in r["exceptions"]:
                fail(f"hub: exception on the page: {e}")
            for e in r["errors_shown"]:
                note(f"hub: error box on the page (not failing on it): {e}")
            note(f"hub: page ran in {r['seconds']} s, {r['elements']} elements, "
                 f"{len(r['exceptions'])} exceptions")
    finally:
        stop_tree(proc)
    deadline = time.time() + STOP_TIMEOUT
    while port_answers(port) and time.time() < deadline:
        time.sleep(0.5)
    if port_answers(port):
        fail(f"hub: port {port} still answers after the exe was stopped")
    ok = len(failures) == n_before
    if not ok:
        log = work / "home" / ".otdrSuite" / "otdrsuite.log"
        if log.is_file():
            note("hub: last lines of otdrsuite.log:")
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]:
                note("    " + line[:300])
    steps.append((label, "PASS" if ok else "FAIL", time.perf_counter() - t0))


# ── The whole test ───────────────────────────────────────────────────────
def fixture_pair():
    """The 24-fibre bidirectional test pair (24 .sor files each way)."""
    a, b = FIXTURES / "splice_A", FIXTURES / "splice_B"
    na, nb = len(list(a.glob("*.sor"))), len(list(b.glob("*.sor")))
    if na != 24 or nb != 24:
        raise SystemExit(f"frozen self-test: expected 24 .sor files in each of "
                         f"{a} and {b}, found {na} and {nb}")
    return a, b


def splice_report_args(mode: str, a: Path, b: Path, out: Path):
    xlsx = out / f"splice_{mode}.xlsx"
    table = out / f"splice_{mode}.viewer.json"
    args = ["--dir-a", a, "--dir-b", b, "--analysis", mode,
            "--out", xlsx, "--viewer-table", table]
    # FastReporter mode writes no Viewer table (by design), so only the
    # OTDR Suite run must write one.
    must = (xlsx, table) if mode == "suite" else (xlsx,)
    return args, xlsx, table, must


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("exe", help="path to the built OTDRSuite exe")
    ap.add_argument("--work", default=None,
                    help="folder for outputs and logs (kept); default: a temp "
                         "folder, removed when everything passed")
    a = ap.parse_args(argv)

    exe = Path(a.exe).resolve()
    if not exe.is_file():
        print(f"FROZEN SELF-TEST FAILED: {exe} does not exist")
        return 1
    work = Path(a.work).resolve() if a.work else Path(tempfile.mkdtemp(prefix="otdr_selftest_"))
    work.mkdir(parents=True, exist_ok=True)
    env = make_env(work)
    dir_a, dir_b = fixture_pair()
    t_all = time.perf_counter()
    print(f"frozen self-test of {exe}\nwork folder {work}\npython {sys.version.split()[0]}",
          flush=True)

    # 1. Reports from the frozen exe.
    print("\n[1] reports from the frozen exe", flush=True)
    fdir = work / "frozen"
    fdir.mkdir(exist_ok=True)
    frozen = {}
    for mode in ("suite", "fr"):
        args, xlsx, table, must = splice_report_args(mode, dir_a, dir_b, fdir)
        frozen[mode] = run_engine(f"frozen splice report {mode}",
                                  [exe, "--run-splicereport"] + args, env, work, must)
    uni = fdir / "uni.xlsx"
    run_engine("frozen unidirectional", [exe, "--run-splicereport", "--uni",
                                         "--dir-a", dir_a, "--out", uni], env, work, (uni,))
    m = run_engine("frozen duplicate check xlsx",
                   [exe, "--run-secretsauce", "--folder", dir_a,
                    "--out-dir", fdir / "dup_xlsx", "--format", "xlsx"], env, work)
    if m is not None:
        written = [w.get("path") for w in m.get("written", [])]
        if not written:
            fail("frozen duplicate check xlsx: the manifest lists no written file")
        for w in written:
            if not w or not Path(w).is_file() or Path(w).stat().st_size == 0:
                fail(f"frozen duplicate check xlsx: did not write {w}")
    m = run_engine("frozen duplicate check pairs",
                   [exe, "--run-secretsauce", "--folder", dir_a,
                    "--out-dir", fdir / "dup_pairs", "--format", "pairs"], env, work)
    if m is not None and not m.get("pairs"):
        fail("frozen duplicate check pairs: the manifest lists no pairs")
    pair = [[1, str(sorted(dir_a.glob("*.sor"))[0]), str(sorted(dir_b.glob("*.sor"))[0])]]
    m = run_engine("frozen fr table", [exe, "--run-splicereport", "--fr-table",
                                       json.dumps(pair), "--analysis", "fr"], env, work)
    if m is not None and not (m.get("tables") or {}).get("1"):
        fail("frozen fr table: no table for fibre 1 in the manifest")

    # 2. The same Splice Report runs from source; the answers must match.
    print("\n[2] the same splice reports from source, compared", flush=True)
    sdir = work / "source"
    sdir.mkdir(exist_ok=True)
    runner = REPO / "splicereport" / "run_splicereport.py"
    for mode in ("suite", "fr"):
        args, xlsx, table, must = splice_report_args(mode, dir_a, dir_b, sdir)
        src = run_engine(f"source splice report {mode}",
                         [sys.executable, runner] + args, env, work, must)
        if src is None or frozen[mode] is None:
            fail(f"compare {mode}: skipped, one of the two runs failed")
            continue
        t0 = time.perf_counter()
        n_before = len(failures)
        compare_workbooks(f"compare {mode} workbook", fdir / xlsx.name, xlsx, fdir, sdir)
        compare_json(f"compare {mode} manifest", frozen[mode], src, fdir, sdir)
        if mode == "suite":
            try:
                ta = json.loads((fdir / table.name).read_text(encoding="utf-8"))
                tb = json.loads(table.read_text(encoding="utf-8"))
                compare_json(f"compare {mode} viewer table", ta, tb, fdir, sdir)
            except Exception as exc:  # noqa: BLE001
                fail(f"compare {mode} viewer table: {type(exc).__name__}: {exc}")
        steps.append((f"compare {mode}", "PASS" if len(failures) == n_before else "FAIL",
                      time.perf_counter() - t0))

    # 3. The hub and its first page.
    check_hub(exe, env, work)

    # Summary.
    total = time.perf_counter() - t_all
    print("\nsummary", flush=True)
    for name, status, sec in steps:
        print(f"  {status}  {sec:7.1f} s  {name}")
    print(f"  total {total:.1f} s")
    if failures:
        print(f"\nFROZEN SELF-TEST FAILED ({len(failures)} problem(s)):")
        for f in failures:
            print("  - " + f)
        print(f"outputs and logs kept in {work}")
        return 1
    print("\nFROZEN SELF-TEST PASSED")
    if not a.work:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
