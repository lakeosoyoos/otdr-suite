"""GenParams-first fiber identity (rescue-only) for the Splice Report engine.

SOR files carry identity metadata in the Bellcore GR-196/SR-4731 GenParams
block: cable id, fiber id, originating/terminating locations — the identity
the tech typed into the OTDR, immune to filename mangling.  The engine now:

  • parses it (sor_reader324802a.parse_genparams) into gen_* fields on every
    parse_sor_full record — cheap, always on;
  • uses the INTERNAL fiber id as a RESCUE when the filename yields no fiber
    number (loader) or when every filename parses stray (runner retry before
    the tie-panel honest abort);
  • warns — capped, surfaced in manifest['warnings'] — when the filename and
    the internal id both parse but DISAGREE.  The filename number always wins;
    a successful, non-stray filename parse is never overridden.

The splice engine ships its own sor_reader324802a copy, so everything here
runs in a clean subprocess: parse-level checks via a `python -c` shim (the
test_end_region_bconfirm _run pattern) and e2e via conftest run_splicereport.
The parse assertions below are the ACTUAL observed GenParams of the committed
ELMMIL fixtures (verified against EXFO field files with the same layout).
"""
from __future__ import annotations

import shutil
import string
import subprocess
import sys
import textwrap

from conftest import (run_splicereport, SPLICEREPORT_DIR,
                      FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)

FIXTURE_SOR = FIXTURE_SPLICE_A_DIR / "ELMMIL0001_1550.sor"


def _run(body):
    """Run `body` in a clean interpreter with ONLY the splicereport engine
    folder on sys.path (single sor_reader copy — the 3-engine isolation
    rule).  The body must print 'OK' as its last line."""
    body = textwrap.dedent(body)
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n")
    p = subprocess.run([sys.executable, "-c", header + body],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


# ─────────────────────────────────────────────────────────────────────
#  parse_genparams — real fixture + synthetic blobs
# ─────────────────────────────────────────────────────────────────────

def test_parse_genparams_real_fixture():
    """Actual observed GenParams of the committed ELMMIL fixture: the whole
    'ELMMIL0001' name sits in fiber_id (span-shot convention), cable_id is a
    blank ' ' (stripped to ''), locations are the route endpoints."""
    _run(f"""
        from sor_reader324802a import parse_genparams, parse_sor_full
        fx = {str(FIXTURE_SOR)!r}
        gp = parse_genparams(fx)
        assert gp == {{'cable_id': '', 'fiber_id': 'ELMMIL0001',
                       'loc_a': 'ELMDALE', 'loc_b': 'MILER'}}, gp
        # bytes input parses identically to a path
        with open(fx, 'rb') as f:
            assert parse_genparams(f.read()) == gp
        # ...and parse_sor_full carries the wired-through gen_* fields.
        r = parse_sor_full(fx, trim=False)
        assert r['gen_cable_id'] == '' and r['gen_fiber_id'] == 'ELMMIL0001', r['gen_fiber_id']
        assert r['gen_loc_a'] == 'ELMDALE' and r['gen_loc_b'] == 'MILER'
        print('OK')
    """)


def test_parse_genparams_synthetic_blob():
    """Constructed GenParams bytes: 2nd occurrence is the block (1st is the
    block-directory map entry); blank-space fields strip to ''; the two
    binary int16s (fiber type + nominal wavelength) are skipped, not
    string-scanned; structural surprises return {} instead of raising."""
    _run("""
        import struct
        from sor_reader324802a import parse_genparams

        def blob(cable=b'CBL9', fiber=b'0042', loc_a=b'Cheyenne', loc_b=b' '):
            return (b'Map\\x00junk' + b'GenParams\\x00' + b'more-map-junk'
                    + b'GenParams\\x00' + b'EN'
                    + cable + b'\\x00' + fiber + b'\\x00'
                    + struct.pack('<hh', 652, 1550)        # BINARY, must be skipped
                    + loc_a + b'\\x00' + loc_b + b'\\x00'
                    + b' \\x00' + b'BC')                    # cable code + flag tail

        gp = blob()
        assert parse_genparams(gp) == {'cable_id': 'CBL9', 'fiber_id': '0042',
                                       'loc_a': 'Cheyenne', 'loc_b': ''}, parse_genparams(gp)
        # empty + single-space fields (EXFO writes ' ' for blank) -> ''
        gp2 = blob(cable=b' ', fiber=b'HOWLAN559', loc_a=b'', loc_b=b'')
        assert parse_genparams(gp2) == {'cable_id': '', 'fiber_id': 'HOWLAN559',
                                        'loc_a': '', 'loc_b': ''}, parse_genparams(gp2)
        # structural surprises -> {} (defensive: never raise)
        assert parse_genparams(b'') == {}
        assert parse_genparams(b'no block here') == {}
        assert parse_genparams(b'GenParams\\x00only the map entry') == {}
        assert parse_genparams(b'GenParams\\x00x' + b'GenParams\\x00EN' + b'A' * 500) == {}   # runaway
        assert parse_genparams(b'GenParams\\x00x' + b'GenParams\\x00EN' + b'trunc') == {}     # unterminated
        assert parse_genparams('/no/such/file.sor') == {}
        print('OK')
    """)


def test_internal_fiber_num_reuses_filename_rules():
    """The internal id goes through the SAME digit rules as filenames
    (rightmost run, wavelength strip, tie-panel zero-padded port)."""
    _run("""
        import splicereportmatchexfo as E
        assert E._internal_fiber_num({'gen_fiber_id': 'ELMMIL0001'}) == 1
        assert E._internal_fiber_num({'gen_fiber_id': '0145'}) == 145        # tie-panel port
        assert E._internal_fiber_num({'gen_fiber_id': 'HOWLAN559'}) == 559
        assert E._internal_fiber_num({'gen_fiber_id': 'SEANOR109_1550'}) == 109
        # blank / absent / JSON records (no gen_* keys) -> None, no rescue
        assert E._internal_fiber_num({'gen_fiber_id': ''}) is None
        assert E._internal_fiber_num({'gen_fiber_id': '  '}) is None
        assert E._internal_fiber_num({}) is None
        assert E._internal_fiber_num(None) is None
        assert E._internal_fiber_num({'gen_fiber_id': 'NODIGITS'}) is None
        print('OK')
    """)


# ─────────────────────────────────────────────────────────────────────
#  e2e — rescue, retry, disagreement, honest abort, zero ripple
# ─────────────────────────────────────────────────────────────────────

def _copy_renamed(src_dir, dst_dir, namer):
    dst_dir.mkdir(parents=True, exist_ok=True)
    for i, src in enumerate(sorted(src_dir.glob("*.sor")), start=1):
        shutil.copy(src, dst_dir / namer(i, src.name))
    return dst_dir


def test_rescue_e2e_hostile_filenames_load_via_internal_ids(tmp_path):
    """Filenames the parser CANNOT number at all ('trace_a.sor'... no digits)
    used to be skipped one by one until the run aborted with 0 fibers.  Now
    the loader falls back to each file's internal GenParams fiber id and the
    full 24-fiber report runs."""
    letters = string.ascii_lowercase
    a = _copy_renamed(FIXTURE_SPLICE_A_DIR, tmp_path / "A",
                      lambda i, _: f"trace_{letters[i - 1]}.sor")
    b = _copy_renamed(FIXTURE_SPLICE_B_DIR, tmp_path / "B",
                      lambda i, _: f"shot_{letters[i - 1]}.sor")
    rc, m, stderr = run_splicereport(a, b, tmp_path / "rep.xlsx")
    assert rc == 0, f"runner exited {rc}; stderr:\n{stderr[-1500:]}"
    assert m and m.get("ok") is True, f"rescue run aborted: {m}"
    assert m["n_fibers"] == 24, f"internal-ID rescue lost fibers: {m['n_fibers']}"


def test_all_stray_retry_rescues_via_internal_ids(tmp_path):
    """Every A-side filename parses to an absurd (stray) number — the branch
    that used to hit the tie-panel honest abort.  The runner now RETRIES by
    re-keying from internal GenParams ids and completes the 24-fiber report,
    surfacing the rescue (and the capped filename↔internal mismatches) in
    manifest['warnings']."""
    a = _copy_renamed(FIXTURE_SPLICE_A_DIR, tmp_path / "A",
                      lambda i, _: f"TRACE91111{i:02d}.sor")   # → #91111NN, all stray
    b = _copy_renamed(FIXTURE_SPLICE_B_DIR, tmp_path / "B",
                      lambda i, name: name)                    # B side healthy
    rc, m, stderr = run_splicereport(a, b, tmp_path / "rep.xlsx")
    assert rc == 0 and m and m.get("ok") is True, f"retry failed: {m}\n{stderr[-1200:]}"
    assert m["n_fibers"] == 24, m["n_fibers"]
    warns = m.get("warnings") or []
    assert any("fiber identity rescue" in w for w in warns), warns
    # 24 mismatching files but the per-run mismatch cap holds at 20.
    mismatches = [w for w in warns if "fiber identity mismatch" in w]
    assert 1 <= len(mismatches) <= 20, len(mismatches)


def test_all_stray_abort_still_honest_when_internal_ids_unusable(tmp_path):
    """When the filenames are unusable AND the files' internal GenParams
    fiber ids carry no digits either, the honest abort must still fire — now
    with the extended 'internal fiber IDs did not rescue it' message (never a
    hang / spurious-grid OOM)."""
    a = tmp_path / "A"; a.mkdir()
    for i, src in enumerate(sorted(FIXTURE_SPLICE_A_DIR.glob("*.sor")), start=1):
        data = src.read_bytes()
        # Byte-patch the internal id to a same-length, digit-free string so
        # the GenParams block stays structurally valid but yields no number.
        data = data.replace(b"ELMMIL0001", b"NODIGITSXX")
        data = data.replace(f"ELMMIL{i:04d}".encode("ascii"), b"NODIGITSXX")
        (a / f"TRACE91111{i:02d}.sor").write_bytes(data)
    b = _copy_renamed(FIXTURE_SPLICE_B_DIR, tmp_path / "B", lambda i, name: name)
    rc, m, stderr = run_splicereport(a, b, tmp_path / "rep.xlsx")
    assert m is not None and m.get("ok") is False, f"expected honest abort, got {m}"
    err = m.get("error", "")
    assert "filename pattern was not recognized" in err, err
    assert "internal fiber IDs did not rescue it" in err, err


def test_identity_mismatch_warns_but_filename_wins(tmp_path):
    """One fixture copy renamed so the filename says #30 while the file's
    internal GenParams id still says #1: the mismatch surfaces in
    manifest['warnings'] and the FILENAME number wins (n_fibers == 30 — a
    successful, non-stray filename parse is never overridden)."""
    a = _copy_renamed(FIXTURE_SPLICE_A_DIR, tmp_path / "A",
                      lambda i, name: name.replace("ELMMIL0001", "ELMMIL0030"))
    b = _copy_renamed(FIXTURE_SPLICE_B_DIR, tmp_path / "B",
                      lambda i, name: name.replace("MILELM0001", "MILELM0030"))
    rc, m, stderr = run_splicereport(a, b, tmp_path / "rep.xlsx")
    assert rc == 0 and m and m.get("ok") is True, f"run failed: {m}\n{stderr[-1200:]}"
    warns = m.get("warnings") or []
    assert any("fiber identity mismatch" in w
               and "ELMMIL0030_1550.sor" in w
               and "#30" in w and "#1" in w for w in warns), warns
    # Filename wins: fiber 30 exists (max key), internal #1 did NOT override.
    assert m["n_fibers"] == 30, m["n_fibers"]


def test_zero_ripple_on_healthy_span(tmp_path):
    """ZERO-RIPPLE LOCK: on the normal, well-named fixture pair the feature
    must be invisible.  The pinned values below (n_fibers=24, n_flagged=9,
    n_columns=8, warnings=[]) were verified BYTE-IDENTICAL between the
    pre-feature engine (git HEAD before this change) and the GenParams build
    on this fixture — identity resolution is rescue-only and never fires on
    a healthy span."""
    rc, m, stderr = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR,
                                     tmp_path / "rep.xlsx", "Elm", "Mil")
    assert rc == 0 and m and m.get("ok") is True, f"healthy run failed: {stderr[-1200:]}"
    assert m["n_fibers"] == 24
    assert m["n_flagged"] == 9, f"flag count rippled: {m['n_flagged']}"
    assert m["n_columns"] == 8, f"column layout rippled: {m['n_columns']}"
    assert m.get("warnings") == [], f"identity chatter on a healthy span: {m['warnings']}"
