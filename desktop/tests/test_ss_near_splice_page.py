"""The Secret Sauce page: the splice behind the panel, a two-fibre check, a
Splice column on the mating ranking, and the fibres shot out of order.

The tech's question that started this was "are 350 and 351 duplicates?".  On a
5 km FEC span at 10 ns the duplicate detectors cannot say, but the splice a few
tens of metres behind the panel is glass: two shots of one fibre read it within
a small wobble, so two files that read it far apart are different fibres.  A
match proves nothing.  So the page must:

  1. show it only where the engine measured it (manifest key absent otherwise,
     so every other folder's manifest and page are unchanged);
  2. answer a two-fibre check from the manifest, with a binary result: over the
     clearing line is "Different fibres", anything else is "Not cleared" and
     says that a match is not a duplicate;
  3. put the gap on the mating ranking, the list a tech checks against the port
     log on a folder the fingerprint cannot measure;
  4. show it in BOTH result modes, like the competence banner.

Namespace isolation: the engine and the hub are only exercised in subprocesses.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys

from conftest import FIXTURE_A_DIR, FIXTURE_B_DIR, REPO_ROOT, SECRETSAUCE_DIR, run_secretsauce

CONTINUOUS = REPO_ROOT / "desktop" / "tests" / "fixtures" / "continuous"


def _stage(tmp_path, sources):
    d = tmp_path / "span"
    d.mkdir()
    for src in sources:
        shutil.copy(src, d / src.name)
    return d


def test_manifest_carries_the_reading_where_the_splice_exists(tmp_path):
    d = _stage(tmp_path, sorted(CONTINUOUS.glob("*.sor")))
    rc, m, err = run_secretsauce(d, tmp_path / "out", "xlsx")
    assert rc == 0 and m and m.get("ok"), (err or "")[-1500:]
    ns = m.get("near_splice")
    assert isinstance(ns, list) and len(ns) == 1, m.keys()
    rec = ns[0]
    assert rec["group"] == "report" and rec["clear_sd"] == 4.0
    assert len(rec["loss"]) == 6 and rec["sd_pair_db"] > 0
    # "15" must resolve to the file the tech means
    assert rec["fibres"]["15"] == "WSC_SUIsh_0015"
    tail = [p for _, p in rec["tail"]]
    assert tail[0] == 100.0 and all(b <= a for a, b in zip(tail, tail[1:])), tail[:6]


def test_manifest_is_unchanged_where_it_is_not(tmp_path):
    d = _stage(tmp_path, sorted(FIXTURE_A_DIR.glob("*.sor")) + sorted(FIXTURE_B_DIR.glob("*.sor")))
    rc, m, err = run_secretsauce(d, tmp_path / "out", "xlsx")
    assert rc == 0 and m and m.get("ok"), (err or "")[-1500:]
    assert "near_splice" not in m and "fill_ins" not in m
    assert all("splice_sd" not in r for r in m.get("mating_top") or [])


_CHECK = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
import app
tail = [[k / 4.0, max(0.0, 100.0 - 30.0 * k / 4.0)] for k in range(0, 41)]
tail = [[k, round(p if k < 3 else 100.0 * 0.5 ** (k * 2), 4)] for k, p in tail]
ns = {'sd_pair_db': 0.019, 'clear_sd': 4.0, 'tail': tail,
      'loss': {'GOOMONsh0350_1550': -0.060, 'GOOMONsh0351_1550': 0.014,
               'GOOMONsh0352_1550': 0.018, 'GOOMONsh0353_1550': 0.080,
               'OTHERsh0350_1550': 0.2},
      'fibres': {'350': 'GOOMONsh0350_1550', '351': 'GOOMONsh0351_1550',
                 '352': 'GOOMONsh0352_1550', '353': 'GOOMONsh0353_1550'}}
out = {}
out['clear'] = app._near_splice_check(ns, '352', '353')           # 0.062 dB = 3.3 sd
out['far'] = app._near_splice_check(ns, '350', '353')             # 0.140 dB = 7.4 sd
out['close'] = app._near_splice_check(ns, '351', '352')           # 0.004 dB
out['unknown'] = app._near_splice_check(ns, '350', '999')
out['same'] = app._near_splice_check(ns, '350', 'GOOMONsh0350_1550')
out['by_name'] = app._near_splice_check(ns, 'GOOMONsh0351', '350')
out['ambiguous'] = app._near_splice_check(ns, 'sh0350', '351')
out['pct_mid'] = app._near_splice_pct(ns, 0.125)
out['pct_hi'] = app._near_splice_pct(ns, 99)
out['cell_far'] = app._splice_cell({'splice_sd': 7.4}, 4.0)
out['cell_close'] = app._splice_cell({'splice_sd': 1.2}, 4.0)
out['cell_none'] = app._splice_cell({}, 4.0)
print(json.dumps(out))
"""


def _check():
    p = subprocess.run([sys.executable, "-c", _CHECK, str(REPO_ROOT)],
                       capture_output=True, text=True, timeout=300, cwd=str(REPO_ROOT))
    assert p.returncode == 0, p.stderr[-3000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_two_fibre_check_is_binary_and_plain():
    r = _check()
    assert r["far"]["ok"] and r["far"]["cleared"] is True
    assert r["far"]["text"].startswith("**Different fibres.**")
    for k in ("clear", "close"):
        assert r[k]["ok"] and r[k]["cleared"] is False, k
        assert r[k]["text"].startswith("**Not cleared.**")
        assert "would not make them duplicates" in r[k]["text"]
        # the rate is always there, so 3.9x never reads as "they match"
        assert "of the time" in r[k]["text"] and "match within" not in r[k]["text"]
    assert abs(r["far"]["sd"] - 0.140 / 0.019) < 0.01


def test_two_fibre_check_says_why_it_cannot_answer():
    r = _check()
    assert r["unknown"]["ok"] is False and "999" in r["unknown"]["text"]
    assert r["same"]["ok"] is False and "same file" in r["same"]["text"]
    assert r["by_name"]["ok"] is True
    assert r["ambiguous"]["ok"] is False and "matches 2 files" in r["ambiguous"]["text"]


def test_tail_and_column_read_the_engine_numbers():
    r = _check()
    assert abs(r["pct_mid"] - 96.25) < 1e-6
    assert r["pct_hi"] >= 0.0
    assert "different fibres (7.4x)" in r["cell_far"]
    assert ">1.2x<" in r["cell_close"] and "different" not in r["cell_close"]
    assert r["cell_none"].endswith("></td>")


def test_both_result_modes_show_it():
    src = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    for fn in ("_render_near_splice(res)", "_render_fill_ins(res)"):
        assert src.count(fn) == 3, f"{fn}: the def plus both result modes"
    i = src.index("def _render_mating_top(res):")
    body = src[i:src.index("\ndef ", i + 10)]
    assert "if has_splice else ''" in body, "the Splice column only when a pair carries it"
    run = (SECRETSAUCE_DIR / "run_secretsauce.py").read_text(encoding="utf-8")
    assert run.count("payload['near_splice'] = near_splice_all") == 2
    assert run.count("payload['fill_ins'] = fill_ins_all") == 2
