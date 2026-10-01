"""A hub test leaves the trace server's CONFIG as it found it.

The trace server is one module for the whole test process, and every hub run
writes into its CONFIG.  A hub test that plays the installed build
(sys.frozen) left the frozen engine command, [python, '--run-splicereport'],
which a plain python refuses, so the next test that had the server run the
report (test_viewer_tie_panel_ends) got no verdicts.  In one CI part of four
no other hub test ran in between to write the dev command back.

The two tests run in file order: the first opens a frozen hub, the second
reads the CONFIG it left (conftest._no_span_left_loaded puts it back).
"""
from __future__ import annotations

import json

from conftest import import_trace_server, run_streamlit
from test_engine_self_verify import _load_launcher
from test_update_needs_install import _arm, _fake_manifest

_SEEN = {}


def test_a_frozen_hub_writes_the_frozen_engine_command(monkeypatch, tmp_path):
    tv = import_trace_server()
    _SEEN["before"] = dict(tv.CONFIG)
    L = _load_launcher()
    _arm(monkeypatch, tmp_path, _fake_manifest(658, L.ENGINE_FILES),
         list(L.ENGINE_FILES))
    at = run_streamlit().run()
    assert not at.exception, f"page raised: {list(at.exception)}"
    assert tv.CONFIG["engine_argv"][1:] == ["--run-splicereport"], tv.CONFIG["engine_argv"]
    _SEEN["frozen"] = True


def test_the_next_test_finds_the_config_the_hub_found():
    tv = import_trace_server()
    if not _SEEN.get("frozen"):
        return   # run on its own: nothing came before it
    assert tv.CONFIG.get("engine_argv") == _SEEN["before"].get("engine_argv")
    assert json.dumps(tv.CONFIG, sort_keys=True, default=str) == json.dumps(
        _SEEN["before"], sort_keys=True, default=str)
