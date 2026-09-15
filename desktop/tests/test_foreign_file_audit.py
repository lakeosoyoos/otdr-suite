"""Foreign-file audit: acquisitions from ANOTHER job mixed into a span folder
are flagged and excluded, and the tech is told.

Origin: Lumen Span 2 Tooele<->Knolls (2026-09-12) shipped with five HH3WES
short shots (HH3->West, 10 ns, range 156,250) among 864 KNOLLS<->TOOELE
traces (275 ns, range 1,250,000).  The rule is header-only: a file is foreign
when its GenParams location pair disagrees with the folder majority AND its
pulse width or acquisition range disagrees too.
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
import folder_intake as fi  # noqa: E402

FX = REPO_ROOT / "desktop" / "tests" / "fixtures"


def _span():
    # ELMDALE<->MILER on A, ELMDALE<->MILLER on B: a real mistyped location.
    return (fi.find_otdr_files(str(FX / "splice_A")) +
            fi.find_otdr_files(str(FX / "splice_B")))


def _strays():
    # Two different jobs: DNN1<->DNN2 (5 ns, 39,063) and WSC<->SUIsh (10 ns, 156,250).
    return (fi.find_otdr_files(str(FX / "frspan")) +
            fi.find_otdr_files(str(FX / "continuous")))


def test_sor_header_reads_the_three_fields():
    h = fi.sor_header(_span()[0])
    assert h["loc_pair"] == ("ELMDALE", "MILER")
    assert h["pulse_ns"] == 500
    assert h["acq_range"] == 1250000


def test_strays_from_other_jobs_are_excluded_with_a_reason():
    paths = _span() + _strays()
    kept, foreign = fi.audit_foreign_files(paths)
    assert len(kept) == 48
    assert {f["name"] for f in foreign} == {os.path.basename(p) for p in _strays()}
    for f in foreign:
        assert "ns pulse" in f["reason"] and "range" in f["reason"]
    msg = fi.foreign_files_message(foreign)
    assert "EXCLUDED" in msg and "8 file(s)" in msg
    assert "ELMDALE" in msg           # tells the tech what the span IS


def test_mistyped_location_alone_is_not_foreign():
    # MILER vs MILLER split the location vote 24/24 but pulse + range agree:
    # nothing is excluded — a typo is not another job.
    kept, foreign = fi.audit_foreign_files(_span())
    assert len(kept) == 48 and foreign == []


def test_two_real_camps_are_left_alone():
    # 6 WSC/SUI files vs 4 DNN files: the minority is 40% of the folder, far
    # above FOREIGN_MAX_SHARE — no majority to trust, nothing excluded.
    paths = (fi.find_otdr_files(str(FX / "continuous")) +
             fi.find_otdr_files(str(FX / "frspan")) +
             fi.find_otdr_files(str(FX / "panelspan")))
    kept, foreign = fi.audit_foreign_files(paths)
    assert len(kept) == len(paths) and foreign == []


def test_json_and_unreadable_files_are_kept(tmp_path):
    j = tmp_path / "ELMMIL0099_1550.json"
    j.write_text("{}", encoding="utf-8")
    bad = tmp_path / "ELMMIL0098_1550.sor"
    bad.write_bytes(b"not a sor file")
    paths = _span() + [str(j), str(bad)]
    kept, foreign = fi.audit_foreign_files(paths)
    assert str(j) in kept and str(bad) in kept and foreign == []


def test_message_is_empty_when_nothing_is_foreign():
    assert fi.foreign_files_message([]) == ""


# ── The hub pages tell the tech and run on the cleaned copy ──────────────────

def _mixed_folder(tmp_path):
    """A one-direction folder with two strays from other jobs mixed in."""
    import shutil
    d = tmp_path / "shots"
    d.mkdir()
    for p in fi.find_otdr_files(str(FX / "splice_A")):
        shutil.copy(p, d)
    for p in fi.find_otdr_files(str(FX / "frspan")):
        shutil.copy(p, d)
    return d


def _warnings(at):
    return " ".join(w.value for w in at.warning)


def test_uni_page_warns_and_runs_on_the_cleaned_copy(tmp_path):
    from conftest import run_streamlit
    d = _mixed_folder(tmp_path)
    at = run_streamlit().run()
    at.session_state["nav_radio"] = "Unidirectional"
    at.session_state["uni_folder_input"] = str(d)
    at.run()
    assert "EXCLUDED" in _warnings(at) and "DNN" in _warnings(at)
    # The engine reads the CLEANED copy; the workbook still lands next to
    # the tech's original folder (checked on the page's own record).
    rec = at.session_state["foreign_excluded"]
    assert rec["src"] == str(d) and rec["staged"] != str(d)
    assert len(rec["foreign"]) == 2
    assert len(fi.find_otdr_files(rec["staged"])) == 24


def test_secret_sauce_page_warns(tmp_path):
    from conftest import run_streamlit
    d = _mixed_folder(tmp_path)
    at = run_streamlit().run()
    at.session_state["nav_radio"] = "Secret Sauce"
    at.session_state["ss_folder_input"] = str(d)
    at.run()
    assert "EXCLUDED" in _warnings(at)


def test_span_loader_excludes_strays_and_names_the_real_span(tmp_path):
    """The boss's report named the A site 'HH3' because _derive_ila reads the
    first .sor alphabetically and HH3WES sorts before TOOKNO.  With the strays
    excluded before the split, the span names itself from its own files."""
    import shutil
    from conftest import run_streamlit
    d = tmp_path / "span"
    d.mkdir()
    for p in (fi.find_otdr_files(str(FX / "splice_A")) +
              fi.find_otdr_files(str(FX / "splice_B")) +
              fi.find_otdr_files(str(FX / "frspan"))):      # DNN* sorts before ELM*
        shutil.copy(p, d)
    at = run_streamlit().run()
    at.session_state["span_folder"] = str(d)
    [b for b in at.sidebar.button if "Load into all tools" in b.label][0].click().run()
    span = at.session_state["span_loaded"]
    assert len(span["foreign"]) == 2
    assert span["a_count"] == 24 and span["b_count"] == 24
    assert "ELMDALE" in (span["ila_a"] + span["ila_b"]).upper()
    assert "DNN" not in (span["ila_a"] + span["ila_b"]).upper()
    assert "EXCLUDED" in _warnings(at)
