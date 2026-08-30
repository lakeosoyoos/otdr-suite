"""The count the user reads must be the count that was analysed.

`run_sor_bytes` and `run_sor_xlsx_bytes` recomputed their own file count with
a glob over the staged folder AFTER rendering, so it disagreed with the header
of the very report they were returning:

    ELMDALE TO MILER   glob 1152 / 662,976   analysed 1151 / 661,825
                       (ELMMIL0231_1550 ends at 22,288 m against a 69,567 m
                        median - a real break)
    DURANC 1-144       glob  144 /  10,296   analysed  141 /   9,870

The glob number reached the download-button label (app.py) while the workbook's
own Summary sheet printed the smaller one.  Same folder, two numbers, no
explanation offered for either.

The glob was also case-sensitive on a path that is not.  `_inventory` matches
on a lowercased name, so a file saved as `.SOR` was inventoried and staged but
missed by the glob.  On POSIX that made the count too LOW and the trace was
silently dropped; on Windows `ntpath.normcase` lowercases, so the same folder
behaved differently in the field than on the dev machine.  Reading the analysis
removes the second parser entirely rather than teaching it the same rules.

Separately, the engine has always reported excluded traces in the manifest
(`short_traces`) and app.py never rendered them, so the shortfall had no
explanation on screen either.
"""
from __future__ import annotations

import os
import shutil

from conftest import FIXTURE_A_DIR, FIXTURE_B_DIR, SECRETSAUCE_DIR, run_secretsauce


def _folder(tmp_path):
    d = tmp_path / "span"
    d.mkdir()
    for src in list(FIXTURE_A_DIR.glob("*.sor")) + list(FIXTURE_B_DIR.glob("*.sor")):
        shutil.copy(src, d / src.name)
    return d


def test_manifest_count_matches_what_was_analysed(tmp_path):
    d = _folder(tmp_path)
    n_on_disk = len(list(d.glob("*.sor")))

    rc, m, err = run_secretsauce(d, tmp_path / "out", "xlsx")
    assert rc == 0 and m and m.get("ok"), f"runner failed: {(err or '')[-800:]}"
    w = m["written"][0]
    # Nothing is excluded in this fixture, so the two agree - the point is
    # that the number now COMES FROM the analysis, not from a second glob.
    assert w["n_files"] == n_on_disk, (w["n_files"], n_on_disk)
    assert w["n_pairs"] == n_on_disk * (n_on_disk - 1) // 2


def test_the_count_comes_from_the_analysis_not_a_second_glob():
    """Source lock.  A second parser over the same folder is what let the two
    numbers drift, and it is what made the result platform-dependent."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    for fn in ("def run_sor_bytes(", "def run_sor_xlsx_bytes("):
        i = src.index(fn)
        body = src[i:i + 2600]
        assert "glob.glob(os.path.join(folder, '*.sor'))" not in body, (
            f"{fn} recomputes its own file count again")
        assert "_meta.get('n_files'" in body, f"{fn} must read the analysed count"
        assert "_meta.get('n_pairs'" in body

    # Both builders have to publish the counts or the readers above get 0.
    assert src.count("meta['n_files'] = len(analysis['files'])") == 2
    assert src.count("meta['n_pairs'] = len(analysis['pairs'])") == 2


def test_uppercase_extension_is_no_longer_a_platform_coin_flip(tmp_path):
    """`_inventory` lowercases, so a .SOR file is found and staged.  The old
    glob was case-sensitive: on macOS it missed the file (count too low, trace
    silently dropped), on Windows it matched.  Reading the analysis makes both
    platforms agree, whatever the engine actually did with the file."""
    d = _folder(tmp_path)
    src = next(FIXTURE_A_DIR.glob("*.sor"))
    shutil.copy(src, d / "ELMMIL9001_1550.SOR")

    rc, m, err = run_secretsauce(d, tmp_path / "out", "xlsx")
    assert rc == 0 and m and m.get("ok"), f"runner failed: {(err or '')[-800:]}"
    w = m["written"][0]
    # counts = what was INVENTORIED; n_files = what was ANALYSED.  Whatever
    # the engine decided, the label and the workbook must not disagree.
    assert m["counts"]["sor"] == len(list(d.glob("*.[sS][oO][rR]")))
    assert w["n_pairs"] == w["n_files"] * (w["n_files"] - 1) // 2, (
        "pairs must be consistent with the file count the report prints")


def test_excluded_traces_are_surfaced_in_the_ui():
    """The engine has always emitted `short_traces`; app.py rendered nothing,
    so a folder could report on fewer fibers than it found and say nothing.
    DURANC 1-144 is the real case: 144 found, 141 compared, 3 suspected breaks."""
    from conftest import REPO_ROOT
    app = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    i = app.index("res = st.session_state.get('ss_result')")
    block = app[i:i + 2200]
    assert "res.get('short_traces')" in block, "excluded traces still not rendered"
    assert "excluded" in block
    assert "window_warnings" in block, "the inconsistent-folder guard is also silent"
    # "processed" claimed more than the tool did; the inventory count is what
    # this line actually holds.
    assert "JSON found." in block and "JSON processed." not in block
