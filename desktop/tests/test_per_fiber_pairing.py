"""A fibre's A/B pairing is the same however many fibres are loaded.

Robert, 2026-09-30: "why can't we be exact with F350 regardless of how many
fibers we load?"  One rule at every job size, reading only that fibre's two
traces: a B reading pairs with an A reading when, after the fibre's OWN A-to-B
frame offset (the median offset of its events the two ends read within
PAIR_CONFIDENT_KM), they sit within a closure cluster gap (floored at the
fibre's pulse smear).  Otherwise A's leg takes B's measured (grey) value, read
at A's reading, as FastReporter does.  The population decides which columns
exist and what they are called, never a pair or a paired number.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import json
import shutil
import subprocess
import sys
import textwrap

from conftest import FIXTURE_DIR, REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
RUNNER = SPLICEREPORT_DIR / "run_splicereport.py"


def _engine(body):
    src = ("import sys\n"
           f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
           "import splicereportmatchexfo as E\n" + textwrap.dedent(body))
    p = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True)
    assert p.returncode == 0, f"{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_the_fibre_s_own_frame_offset():
    _engine("""
    def ev(km, end=False):
        return {'dist_km': km, 'splice_loss': 0.05, 'type': '0F9999LS', 'is_end': end}
    SPAN = 55.0
    ra = {'events': [ev(0.0), ev(7.0), ev(21.0), ev(44.10), ev(55.0, True)]}
    # B reads 7.0 and 21.0 twelve metres further along than A, and a feature
    # 850 m from A's 44.10 reading
    rb = {'events': [ev(0.0), ev(SPAN - 7.012), ev(SPAN - 21.012),
                     ev(SPAN - 44.95), ev(SPAN, True)]}
    off = E._fiber_ab_offset(ra, rb, SPAN)
    assert abs(off - 0.012) < 1e-9, off
    # fewer than two confident pairs: no offset
    rb1 = {'events': [ev(0.0), ev(SPAN - 7.012), ev(SPAN, True)]}
    assert E._fiber_ab_offset(ra, rb1, SPAN) == 0.0
    ea = ra['events'][3]
    tol = 0.25
    assert not E._b_pairs_with_a(ea, 44.95, off, tol)      # another feature
    assert E._b_pairs_with_a(ea, 44.214, off, tol)         # one splice, 115 m
    # the window is centred on the fibre's own offset
    assert E._b_pairs_with_a(ea, 44.10 + 0.30, 0.10, tol)
    assert not E._b_pairs_with_a(ea, 44.10 + 0.30, 0.0, tol)
    print('OK')
    """)


def _job(tmp_path, fibres):
    for side in ("A", "B"):
        d = tmp_path / side
        d.mkdir(parents=True)
        for src in sorted((FIXTURE_DIR / f"splice_{side}").glob("*.sor")):
            if int(src.name[6:10]) in fibres:
                shutil.copy(src, d / src.name)
    return tmp_path / "A", tmp_path / "B"


def _pairs(tmp_path, a, b):
    tbl = tmp_path / "t.json"
    p = subprocess.run([sys.executable, str(RUNNER), "--dir-a", str(a), "--dir-b", str(b),
                        "--out", str(tmp_path / "r.xlsx"), "--viewer-table", str(tbl)],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-2000:]
    t = json.loads(tbl.read_text(encoding="utf-8"))
    out = {}
    for f, cells in t["fibers"].items():
        for c in cells:
            a_, b_ = c.get("a") or {}, c.get("b") or {}
            if c.get("category") == "end" or (a_.get("km") is None and b_.get("km") is None):
                continue
            key = (a_.get("km") if a_.get("km") is not None else "grey" if a_ else None,
                   b_.get("km") if b_.get("km") is not None else "grey" if b_ else None)
            out.setdefault(f, {})[key] = None if c.get("loss") is None else round(c["loss"], 4)
    return out


def _by_leg(pairs):
    legs = {}
    for (ak, bk), avg in pairs.items():
        if ak not in (None, "grey"):
            legs[("a", ak)] = ((ak, bk), avg)
        if bk not in (None, "grey"):
            legs[("b", bk)] = ((ak, bk), avg)
    return legs


def test_pairing_is_the_same_alone_and_in_the_cable(tmp_path):
    """Fibres 1-4 of the 67.5 km fixture, each loaded alone and in all 24:
    every stored reading that both runs print has the same partner and the
    same average."""
    full = _pairs(tmp_path / "full", FIXTURE_DIR / "splice_A", FIXTURE_DIR / "splice_B")
    shared = 0
    for f in (1, 2, 3, 4):
        a, b = _job(tmp_path / f"f{f}", {f})
        alone = _by_leg(_pairs(tmp_path / f"f{f}", a, b)[str(f)])
        whole = _by_leg(full[str(f)])
        for leg in set(alone) & set(whole):
            assert alone[leg] == whole[leg], (f, leg, alone[leg], whole[leg])
            shared += 1
    assert shared >= 40, shared
