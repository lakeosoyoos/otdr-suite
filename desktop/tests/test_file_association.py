"""File associations: .iss registers .zfc/.zdb/.otdrproject; launcher hands
the double-clicked path to the hub via ~/.otdrSuite/open_request.json."""
import ast
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import launcher  # noqa: E402


def _iss():
    return (HERE / "OTDRSuite.iss").read_text(encoding="utf-8")


def test_iss_changes_associations_and_is_per_user():
    s = _iss()
    assert "ChangesAssociations=yes" in s
    assert "PrivilegesRequired=lowest" in s
    assert "[Registry]" in s


def test_iss_registers_each_extension_with_uninstall_cleanup():
    s = _iss()
    for ext, prog in ((".zfc", "OTDRSuite.zfc"), (".zdb", "OTDRSuite.zdb"),
                      (".otdrproject", "OTDRSuite.zdb")):
        assert f'Subkey: "Software\\Classes\\{ext}"; ValueType: string; ValueName: ""; ValueData: "{prog}"; Flags: uninsdeletevalue' in s
        assert f'Subkey: "Software\\Classes\\{ext}\\OpenWithProgids"; ValueType: string; ValueName: "{prog}"' in s
    for prog, name in (("OTDRSuite.zfc", "OTDR Suite Field Capture"),
                       ("OTDRSuite.zdb", "OTDR Suite Project")):
        assert f'ValueData: "{name}"; Flags: uninsdeletekey' in s
        assert f'Software\\Classes\\{prog}\\shell\\open\\command' in s
    assert '""%1""' in s
    assert "HKCR" not in s and "HKLM" not in s


def test_file_arg_picks_associated_file_only():
    assert launcher._file_arg(["exe"]) == ""
    assert launcher._file_arg(["exe", "--flag"]) == ""
    assert launcher._file_arg(["exe", "x.sor"]) == ""
    got = launcher._file_arg(["exe", '"C:/a b/Job.ZFC"'])
    assert got.lower().endswith("job.zfc") and os.path.isabs(got)
    assert launcher._file_arg(["exe", "p.zdb"]).endswith("p.zdb")
    assert launcher._file_arg(["exe", "old.otdrproject"]).endswith(".otdrproject")


def test_write_open_request_atomic(tmp_path):
    t = tmp_path / "sub" / "open_request.json"
    assert launcher._write_open_request("", t) is False and not t.exists()
    assert launcher._write_open_request("C:/x/p.zdb", t) is True
    d = json.loads(t.read_text(encoding="utf-8"))
    assert d["path"] == "C:/x/p.zdb" and abs(d["ts"] - time.time()) < 60
    assert not (tmp_path / "sub" / "open_request.json.tmp").exists()


def _consume_fn():
    """Load app._consume_open_request without running the Streamlit script."""
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    node = next(n for n in ast.parse(src).body
                if isinstance(n, ast.FunctionDef) and n.name == "_consume_open_request")
    ns = {"os": os, "json": json, "time": time}
    exec(compile(ast.Module([node], []), "app.py", "exec"), ns)
    return ns["_consume_open_request"]


def test_hub_consumes_once_and_drops_stale(tmp_path):
    consume = _consume_fn()
    req = tmp_path / "open_request.json"
    assert consume(str(req)) is None
    launcher._write_open_request("C:/x/job.zfc", req)
    assert consume(str(req)) == "C:/x/job.zfc"
    assert consume(str(req)) is None               # claimed, gone
    assert list(tmp_path.iterdir()) == []
    launcher._write_open_request("C:/x/old.zdb", req)
    assert consume(str(req), now=time.time() + 3600) is None


def test_hub_hands_the_request_to_open_share_file_after_it_is_defined():
    # Streamlit runs app.py top to bottom: the request is claimed early but
    # opened only once open_share_file (and what it calls) exists.
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    claim = src.index("st.session_state['_share_open_pending'] = _open_req")
    define = src.index("def open_share_file(path):")
    use = src.index("open_share_file(_share_pending)")
    assert claim < define < use
