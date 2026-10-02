"""Splice Report FEC: the facility-entrance tool (Robert 2026-10-01).

FEC shots are short traces from EACH END of a span: launch reel, the building
panel connector at ~1 km, often a pigtail splice tens of metres behind it.
The two ends never see the same glass, so the tool grades each folder alone.

The rule comes from two techs' FEC OOS lists on one span:
  * loss = panel connector + every event within 150 m behind it ("COMBINE"),
    each event as the table prints it (3 dp), then added;
  * reflectance = the panel connector's, fails above -50.0 (1 dp);
  * a loss of exactly 0.500 fails for tech A, passes for tech C (the
    default, Robert 2026-10-01).
The event values below are that span's real numbers (no names, no files).
"""
import json
import subprocess
import sys
import textwrap

from conftest import (FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR,
                      SPLICEREPORT_DIR)


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import run_splicereport as R\n"
              "def ev(pos_m, loss, refl=None, reflective=False, end=False):\n"
              "    return (pos_m, loss, refl, reflective, end)\n"
              "PORT = ev(0.0, 0.0, -56.0, True)\n"
              "END = ev(4993.0, 0.0, None, False, True)\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_connector_alone_fails_on_loss():
    _run("""
        g = R.fec_grade([PORT, ev(1001.9, 0.8037, -57.8, True), END])
        assert g['found'] and g['loss'] == 0.804 and g['fail_loss'], g
        assert not g['fail_refl'] and g['combined'] == [], g
        print('OK')
    """)


def test_connector_and_the_splice_behind_it_are_combined():
    """Tech sheet: 'COMBINE' .502 = connector .315 + splice .187, 48 m on."""
    _run("""
        g = R.fec_grade([PORT, ev(1006.0, 0.3150, -51.3, True),
                         ev(1054.0, 0.1872), END])
        assert g['loss'] == 0.502 and g['fail_loss'], g
        assert [round(p) for p, _ in g['combined']] == [1054], g
        print('OK')
    """)


def test_the_printed_numbers_are_added_not_the_raw_ones():
    """.3057 + .2434 prints .306 + .243 = .549 (the tech's number); the raw
    sum would print .548."""
    _run("""
        g = R.fec_grade([PORT, ev(1005.0, 0.3057, -50.2, True),
                         ev(1054.0, 0.2434), END])
        assert g['loss'] == 0.549, g
        print('OK')
    """)


def test_events_past_the_reach_are_not_combined():
    """The first FEC splice ~2.16 km on is NOT part of the connector."""
    _run("""
        g = R.fec_grade([PORT, ev(1006.0, 0.4712, -54.7, True),
                         ev(3166.0, 0.2882), END])
        assert g['loss'] == 0.471 and not g['fail_loss'], g
        g = R.fec_grade([PORT, ev(1006.0, 0.4712, -54.7, True),
                         ev(3166.0, 0.2882), END], combine_m=2500)
        assert g['loss'] == 0.759 and g['fail_loss'], g
        print('OK')
    """)


def test_exactly_half_a_db_fails_for_a_and_passes_for_c():
    _run("""
        evs = [PORT, ev(1002.0, 0.500007, -53.9, True), END]
        assert R.fec_grade(evs, strict=0)['fail_loss']
        assert not R.fec_grade(evs, strict=1)['fail_loss']
        # 0.4995 prints .500, so it is graded as .500
        evs = [PORT, ev(1005.0, 0.4995, -53.2, True), END]
        assert R.fec_grade(evs, strict=0)['loss'] == 0.5
        assert R.fec_grade(evs, strict=0)['fail_loss']
        print('OK')
    """)


def test_reflectance_is_graded_on_the_printed_value():
    _run("""
        fail = R.fec_grade([PORT, ev(1006.0, 0.238, -49.6, True), END])
        assert fail['fail_refl'] and not fail['fail_loss'], fail
        # -49.99 prints -50.0: not above the gate
        ok = R.fec_grade([PORT, ev(1006.0, 0.19, -49.99, True), END])
        assert not ok['fail_refl'], ok
        print('OK')
    """)


def test_the_port_is_not_the_panel_and_a_trace_without_one_is_reported():
    _run("""
        g = R.fec_grade([PORT, ev(3141.0, 0.31), END])
        assert g == {'found': False}, g
        print('OK')
    """)


def test_rows_use_the_tech_sheet_columns():
    _run("""
        side = {'side': 'A', 'prefix': 'ABCDEF', 'traces': [
            {'file': 'ABCDEFsh0104_1550.sor', 'stem': 'ABCDEFsh0104_1550',
             'fiber': 104, 'pulse_ns': 10.0,
             'events': [PORT, ev(1005.0, 0.4995, -53.2, True), END]},
            {'file': 'ABCDEFsh0324_1550.sor', 'stem': 'ABCDEFsh0324_1550',
             'fiber': 324, 'pulse_ns': 10.0,
             'events': [PORT, ev(1006.0, 0.3150, -51.3, True),
                        ev(1054.0, 0.1872), END]},
            {'file': 'ABCDEFsh0985_1550.sor', 'stem': 'ABCDEFsh0985_1550',
             'fiber': 985, 'pulse_ns': 5.0,
             'events': [PORT, ev(1006.0, 0.1, -60.0, True), END]},
            {'file': 'ABCDEFsh0986_1550.sor', 'stem': 'ABCDEFsh0986_1550',
             'fiber': 986, 'pulse_ns': 5.0, 'events': [PORT, END]},
        ]}
        rows, no_conn, pulses = R.fec_rows(side)
        got = [(r['fiber_id'], r['failing_at'], r['distance'], r['side'])
               for r in rows]
        # the default (tech C): a printed .500 passes
        assert got == [('ABCDEF0324', '0.502', 'COMBINE', 'A')], got
        R.FEC_LOSS_STRICT = 0.0                     # tech A: .500 fails
        got = [(r['fiber_id'], r['failing_at'], r['distance'], r['side'])
               for r in R.fec_rows(side)[0]]
        assert got == [('ABCDEF0104', '0.500', '1.005km', 'A'),
                       ('ABCDEF0324', '0.502', 'COMBINE', 'A')], got
        assert no_conn == ['ABCDEFsh0986_1550'], no_conn
        assert [(p['pulse_ns'], p['fibers']) for p in pulses] == \\
            [(10.0, '104, 324'), (5.0, '985-986')], pulses
        assert R._fec_prefix(['ABCDEFsh0104_1550', 'ABCDEFsh0324_1550']) == 'ABCDEF'
        print('OK')
    """)


def test_runner_fec_mode_end_to_end(tmp_path):
    """Two folders in, one workbook and one manifest out; never paired."""
    out = tmp_path / 'FEC_OOS.xlsx'
    p = subprocess.run(
        [sys.executable, str(SPLICEREPORT_DIR / 'run_splicereport.py'), '--fec',
         '--dir-a', str(FIXTURE_SPLICE_A_DIR), '--dir-b', str(FIXTURE_SPLICE_B_DIR),
         '--out', str(out), '--overrides', json.dumps({'FEC_LOSS_STRICT': 1})],
        capture_output=True, text=True, timeout=600)
    man = json.loads(p.stdout.strip().splitlines()[-1])
    assert man['ok'], man
    assert out.is_file()
    fec = man['fec']
    assert fec['gates']['FEC_LOSS_STRICT'] == 1.0
    assert [s['side'] for s in fec['sides']] == ['A', 'B']
    for s in fec['sides']:
        assert s['n_traces'] > 0 and s['n_traces'] == s['n_files']
    import openpyxl
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ['FEC OOS', 'Acquisition']
    head = [c.value for c in wb['FEC OOS'][4]]
    assert head[:4] == ['Fiber Number', 'FAILING @', 'Distance', 'Side (A or B)']


def test_runner_fec_mode_refuses_a_missing_folder(tmp_path):
    p = subprocess.run(
        [sys.executable, str(SPLICEREPORT_DIR / 'run_splicereport.py'), '--fec',
         '--dir-a', str(tmp_path / 'nope'), '--out', str(tmp_path / 'x.xlsx')],
        capture_output=True, text=True, timeout=120)
    man = json.loads(p.stdout.strip().splitlines()[-1])
    assert not man['ok'] and 'not found' in man['error']


# ── hub ───────────────────────────────────────────────────────────────────

def test_profiles_a_and_c_carry_the_two_tech_styles():
    import app as hub
    a = hub._fec_settings_from_profile('A')
    c = hub._fec_settings_from_profile('C')
    assert a == {'FEC_LOSS_GATE': 0.5, 'FEC_LOSS_STRICT': 0.0,
                 'FEC_REFL_GATE': -50.0, 'FEC_COMBINE_M': 150.0}
    assert c == dict(a, FEC_LOSS_STRICT=1.0)
    # every other profile runs FEC at the defaults: tech C's style
    assert hub._fec_settings_from_profile('Default (engine baseline)') == hub.FEC_DEFAULTS == c
    # and A / C run the other tools at the engine baseline, like Default
    base = hub._overrides_from_settings(hub._otdr_settings_from_profile(
        'Default (engine baseline)'))
    for name in ('A', 'C'):
        assert hub._overrides_from_settings(
            hub._otdr_settings_from_profile(name)) == base, name


def test_fec_cmd_runs_the_splice_report_runner():
    import app as hub
    cmd = hub.fec_cmd('/a', '/b', '/out.xlsx', {'FEC_LOSS_STRICT': 1.0})
    assert cmd[1].endswith('run_splicereport.py')
    assert cmd[2:8] == ['--fec', '--dir-a', '/a', '--out', '/out.xlsx', '--dir-b']
    assert json.loads(cmd[cmd.index('--overrides') + 1]) == {'FEC_LOSS_STRICT': 1.0}
    assert '--dir-b' not in hub.fec_cmd('/a', '', '/o.xlsx')


def test_the_fec_page_draws_and_asks_for_a_folder():
    from conftest import run_streamlit, go_tab
    at = run_streamlit().run()
    go_tab(at, 'Splice Report FEC')
    assert not at.exception, at.exception
    assert any('Splice Report FEC' in m.value for m in at.markdown)
    assert any('A end FEC folder' in i.value for i in at.info)
    labels = [t.label for t in at.text_input]
    assert 'A End FEC (Folder or .zip)' in labels
    assert 'B End FEC (Optional) (Folder or .zip)' in labels


# ── Viewer FEC tool (Robert 2026-10-01) ─────────────────────────────────

def _viewer_src(at):
    return next(e.proto.src for e in at.get('iframe')
                if '127.0.0.1' in (e.proto.src or '') and 'host=' not in e.proto.src)


def test_the_fec_viewer_is_the_viewer_locked_in_fec_mode():
    from conftest import run_streamlit, go_tab
    at = run_streamlit().run()
    go_tab(at, 'Viewer FEC')
    assert not at.exception, at.exception
    # the heading at half the old #### size (Robert 2026-10-01)
    assert any('font-size:0.75rem' in m.value and 'Viewer FEC</p>' in m.value
               for m in at.markdown)
    assert 'fec=1' in _viewer_src(at)
    pop = [e.proto.srcdoc for e in at.get('iframe') if 'vpop2' in (e.proto.srcdoc or '')]
    assert pop and '/?fec=1"' in pop[0]       # the pop-out window stays in FEC mode
    go_tab(at, 'Viewer')
    assert not at.exception, at.exception
    assert 'fec=1' not in _viewer_src(at)


def test_only_the_fec_link_turns_fec_mode_on():
    from conftest import VIEWER_DIR
    html = (VIEWER_DIR / 'viewer.html').read_text(encoding='utf-8')
    assert "gFecMode = new URLSearchParams(location.search).get('fec') === '1';" in html
    assert 'id="set-fec"' not in html


# ── FEC report rows link into Viewer FEC ────────────────────────────────

def test_a_fec_row_click_opens_viewer_fec_on_that_fibre_and_end(tmp_path):
    from conftest import run_streamlit
    fa, fb = tmp_path / 'endA', tmp_path / 'endB'
    fa.mkdir(); fb.mkdir()
    at = run_streamlit()
    at.query_params['nav'] = 'viewerfec'
    at.query_params['fiber'] = '324'
    at.query_params['km'] = '1.006'
    at.query_params['dir'] = 'b'
    at.query_params['fa'] = str(fa)
    at.query_params['fb'] = str(fb)
    at.query_params['ra'] = str(fa) + '.zip'
    at.run()
    assert not at.exception, at.exception
    ss = at.session_state
    assert ss['nav_radio'] == 'Viewer FEC'
    assert ss['view_dir_a_input'] == str(fa) and ss['view_dir_b_input'] == str(fb)
    assert ss['fec_dir_a'] == str(fa) + '.zip' and ss['fec_dir_b'] == str(fb)
    src = _viewer_src(at)
    assert 'fec=1' in src and 'fiber=324' in src and 'dir=b' in src and 'km=1.006' in src
    assert any(b.label == '← Back to Splice Report FEC' for b in at.button)


def test_fec_rows_link_each_fibre_from_its_own_end():
    import app as hub
    rows = [{'side': 'B', 'fiber': 324, 'fiber_id': 'ABCDEF0324', 'conn_km': 1.006,
             'failing_at': '0.620', 'distance': 'COMBINE', 'kind': 'loss',
             'conn_loss': 0.2, 'combined': [{'km': 1.0809, 'loss': 0.42}]},
            {'side': 'A', 'fiber': 123, 'fiber_id': 'GHIJKL0123', 'conn_km': 1.005,
             'failing_at': '-35.3', 'distance': '1.005km', 'kind': 'refl',
             'conn_loss': 0.205, 'combined': []}]
    html = hub._fec_rows_html(rows, '/x/A end', '/x/B end')
    # Every cell of a row is the row's link: a click anywhere on it jumps.
    assert html.count("target='_self'") == 2 * 7
    for f in (324, 123):
        assert html.count(f'fiber={f}&amp;') == 7
    assert '?nav=viewerfec&amp;fiber=324&amp;km=1.006&amp;dir=b&amp;fa=%2Fx%2FA%20end' in html
    assert '?nav=viewerfec&amp;fiber=123&amp;km=1.005&amp;dir=a&amp;' in html
    assert '0.420 @ 1.081 km' in html


def test_fec_links_keep_the_traces_own_km():
    from conftest import VIEWER_DIR
    html = (VIEWER_DIR / 'viewer.html').read_text(encoding='utf-8')
    body = html.split('function linkDispKm(km, src, dir, fiber) {', 1)[1]
    assert body.lstrip().startswith('if (gFecMode) return km;')
    assert 'gFecMode ? 0.3 : 2.5' in html


def test_the_fec_boxes_show_values_set_on_an_earlier_run():
    """A Viewer FEC link sets the FEC folders on the Viewer FEC run, and a
    carried save folder lands on the first run.  Streamlit sends a keyed
    box's value to the browser only when it was set in the run that draws
    it, so the page re-assigns all three right before drawing them (the
    browser showed three empty boxes over a report built from them)."""
    import inspect
    import app as hub
    row = inspect.getsource(hub._fec_folder_row)
    assert row.index('_fec_resync(slot)') < row.index('st.text_input(')
    page = inspect.getsource(hub.page_splice_report_fec)
    assert (page.index("_fec_resync('fec_report_dest')")
            < page.index("_report_dest_row('fec_report_dest'"))
    assert 'fec_report_dest' in hub._CARRIED_SETTINGS


# ── every trace type: .sor, .trc and .json, each from the OTDR port ──────

def test_a_json_export_is_measured_from_the_port():
    """A .json puts SpanStart (the panel) at 0 and the launch reel at minus
    its length; read raw, the panel sat inside the port skip and the rule
    found no connector (a real export: panel 0 m, first sample -1006.956 m)."""
    _run("""
        rec = {'_json_first_pos_m': -1006.956, 'events': [
            {'dist_km': 0.0, 'splice_loss': 0.395, 'reflection': -54.9,
             'is_reflective': True, 'is_end': False},
            {'dist_km': 0.075, 'splice_loss': 0.2, 'reflection': 0.0,
             'is_reflective': False, 'is_end': False},
            {'dist_km': 59.877, 'splice_loss': 0.0, 'reflection': -15.3,
             'is_reflective': True, 'is_end': True}]}
        g = R.fec_grade(R._fec_events(rec))
        assert g['found'] and abs(g['conn_m'] - 1006.956) < 1e-6, g
        assert g['loss'] == 0.595 and g['fail_loss'], g
        print('OK')
    """)


def test_a_declared_span_trc_is_measured_from_the_port():
    """A .trc whose span start was set on the panel (1.006 km) stores its
    events from there: the panel at 0, its pigtail splice at 31 m.  Raw, the
    rule skipped both and graded an event 1 km down the cable."""
    from conftest import FIXTURE_DIR as FIXTURES_DIR
    p = FIXTURES_DIR / 'trc' / 'TRCDECL0001_155016251310.trc'
    _run(f"""
        rec = R._fec_parser({str(p)!r})({str(p)!r})
        g = R.fec_grade(R._fec_events(rec))
        assert g['found'] and abs(g['conn_m'] - 1005.92) < 0.01, g
        assert [round(m) for m, _ in g['combined']] == [1037], g
        print('OK')
    """)


def test_a_folder_of_mixed_types_reads_each_fiber_once(tmp_path):
    """.sor, .trc and .json are all read; a fiber shot in two types is read
    once, from the type the folder holds most of."""
    import shutil
    from conftest import FIXTURE_DIR as FIXTURES_DIR
    sor = sorted((FIXTURES_DIR / 'splice_A').glob('*.sor'))[0]
    trc = FIXTURES_DIR / 'trc' / 'TRCDECL0001_155016251310.trc'
    for n in (1, 2):
        shutil.copy(sor, tmp_path / f'ABCDEFsh{n:04d}_1550.sor')
    shutil.copy(trc, tmp_path / 'ABCDEFsh0002_1550.trc')      # a reshoot
    shutil.copy(trc, tmp_path / 'ABCDEFsh0003_1550.trc')      # only a .trc
    _run(f"""
        sd = R._fec_side({str(tmp_path)!r}, 'A', log=lambda *a: None)
        got = sorted((t['fiber'], t['file'][-4:]) for t in sd['traces'])
        assert got == [(1, '.sor'), (2, '.sor'), (3, '.trc')], got
        assert not sd['unreadable'], sd['unreadable']
        print('OK')
    """)


def test_viewer_fec_grades_every_trace_type():
    from conftest import VIEWER_DIR
    src = (VIEWER_DIR / 'trace_server.py').read_text(encoding='utf-8')
    body = src.split('def fec_tables(fibers', 1)[1].split('\ndef ', 1)[0]
    assert "endswith(('.sor', '.trc', '.json'))" in body
    assert "endswith('.sor')" not in body


def test_the_report_goes_into_a_new_save_folder(tmp_path):
    """'Save reports to' may name a folder that does not exist yet under one
    that does; the FEC run makes it, as the Splice Report's run does."""
    import shutil
    from conftest import FIXTURE_DIR
    src = sorted((FIXTURE_DIR / 'splice_A').glob('*.sor'))[0]
    a = tmp_path / 'A'
    a.mkdir()
    shutil.copy(src, a / 'ABCDEFsh0001_1550.sor')
    out = tmp_path / 'new folder' / 'FEC_OOS.xlsx'
    _run(f"""
        res = R._fec_payload([('A', {str(a)!r})], {str(out)!r}, log=lambda *a: None)
        assert res['ok'], res
        import os
        assert os.path.isfile({str(out)!r})
        print('OK')
    """)


def test_the_engine_default_is_tech_cs_style():
    """With no profile and no override, a printed 0.500 passes (tech C);
    the hub's defaults and the engine's agree."""
    import app as hub
    _run("""
        evs = [PORT, ev(1002.0, 0.500, -53.9, True), END]
        assert R.FEC_LOSS_STRICT == 1.0
        assert not R.fec_grade(evs)['fail_loss']
        assert R.fec_grade([PORT, ev(1002.0, 0.501, -53.9, True), END])['fail_loss']
        print('OK')
    """)
    assert hub.FEC_DEFAULTS['FEC_LOSS_STRICT'] == 1


# ── the page after a run (2026-10-02 audit) ─────────────────────────────

_GATES = {'FEC_LOSS_GATE': 0.5, 'FEC_LOSS_STRICT': 1.0,
          'FEC_REFL_GATE': -50.0, 'FEC_COMBINE_M': 150.0}


def _fec_page(tmp_path, monkeypatch):
    """The FEC page with an A end folder in its box and nothing run.  The
    folder holds traces: an empty one is warned about (no trace files)."""
    import os
    import shutil
    from conftest import run_streamlit
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    a = tmp_path / 'endA'
    if not a.exists():
        shutil.copytree(FIXTURE_SPLICE_A_DIR, a)
    at = run_streamlit(default_timeout=180).run()
    at.sidebar.radio[0].set_value('Splice Report FEC').run()
    next(t for t in at.text_input if t.label.startswith('A End FEC')).set_value(str(a)).run()
    assert not at.exception, at.exception
    return at, os.path.abspath(str(a))


def _a_report(dir_a, gates=_GATES):
    """What a run hands the page, for a span with one failing fiber."""
    row = {'side': 'A', 'fiber': 6, 'fiber_id': 'ABCDEF0006', 'conn_km': 1.002,
           'failing_at': '0.576', 'distance': 'COMBINE', 'kind': 'loss',
           'conn_loss': 0.2, 'conn_refl': -55.0, 'pulse_ns': 10.0,
           'combined': [{'km': 1.05, 'loss': 0.376}]}
    return {'ok': True, 'xlsx': '', '_dirs': [dir_a, ''],
            'fec': {'gates': dict(gates), 'sides': [
                {'side': 'A', 'label': 'ABCDEF', 'n_traces': 1,
                 'n_fail_fibers': 1, 'rows': [row]}]}}


def test_a_second_run_into_the_same_folder_keeps_the_first_report(tmp_path, monkeypatch):
    """The workbook was always FEC_OOS.xlsx: a second span run into the same
    Save Reports To folder wrote over the first, and the first span's page
    then offered the second span's file."""
    import shutil
    from conftest import finish_engine_run, run_streamlit
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    a = tmp_path / 'endA'
    shutil.copytree(FIXTURE_SPLICE_A_DIR, a)
    dest = tmp_path / 'reports'
    dest.mkdir()
    (dest / 'FEC_OOS.xlsx').write_bytes(b'the first span')
    at = run_streamlit(default_timeout=180).run()
    at.sidebar.radio[0].set_value('Splice Report FEC').run()
    next(t for t in at.text_input if t.label.startswith('A End FEC')).set_value(str(a)).run()
    next(t for t in at.text_input if t.label == 'Save Reports To').set_value(str(dest)).run()
    next(b for b in at.button if b.label == 'Run FEC Report').click().run()
    finish_engine_run(at, 'fec')
    assert not at.exception, at.exception
    assert (dest / 'FEC_OOS.xlsx').read_bytes() == b'the first span'
    assert at.session_state['fec_result']['xlsx'] == str(dest / 'FEC_OOS (2).xlsx')
    assert any(c.value == f"Saved: {dest / 'FEC_OOS (2).xlsx'}" for c in at.caption)


def test_a_settings_edit_after_a_run_says_the_report_is_old(tmp_path, monkeypatch):
    """The report on screen is found by its folders; an FEC Settings edit
    left its fails under the new rule's caption with nothing said."""
    at, a = _fec_page(tmp_path, monkeypatch)
    at.session_state['fec_result'] = _a_report(a)
    at.run()
    assert not at.exception, at.exception
    assert not at.warning and at.success
    gate = next(n for n in at.number_input if n.label == 'Loss Gate (dB)')
    gate.set_value(0.3).run()
    said = [w.value for w in at.warning]
    assert len(said) == 1 and said[0].startswith(
        'This report was made with other FEC Settings: loss > 0.500 dB'), said
    assert 'Run FEC Report again' in said[0]
    assert any(c.value.startswith('Fails: loss > 0.300 dB') for c in at.caption)
    next(n for n in at.number_input if n.label == 'Loss Gate (dB)').set_value(0.5).run()
    assert not at.warning
    next(s for s in at.selectbox if s.label == 'Loss Fails When').set_value(
        'At or Over the Gate').run()
    assert len(at.warning) == 1


def test_the_same_gates_are_judged_as_the_engine_gets_them():
    import app as hub
    assert hub._fec_same_gates(_GATES, dict(_GATES))
    assert not hub._fec_same_gates(_GATES, dict(_GATES, FEC_LOSS_GATE=0.501))
    assert not hub._fec_same_gates(_GATES, dict(_GATES, FEC_REFL_GATE=-45.0))
    assert not hub._fec_same_gates(_GATES, dict(_GATES, FEC_COMBINE_M=100.0))
    assert not hub._fec_same_gates(_GATES, dict(_GATES, FEC_LOSS_STRICT=0.0))
    # a report with no record of its gates is not questioned
    assert hub._fec_same_gates(None, _GATES) and hub._fec_same_gates({}, _GATES)
    assert hub._fec_same_gates({'FEC_LOSS_GATE': 0.5}, _GATES)


def _clear_traces(at):
    next(b for b in at.sidebar.button if b.label == 'Clear Traces').click().run()
    next(b for b in at.button if b.key == 'clear_traces_allow').click().run()
    assert not at.exception, at.exception


def test_clear_traces_clears_the_fec_page(tmp_path, monkeypatch):
    """The dialog says every tool; the FEC page kept its folders, its report
    and the report's saved copy."""
    import json as _json
    import os
    import app as hub
    at, a = _fec_page(tmp_path, monkeypatch)
    at.session_state['fec_result'] = _a_report(a)
    at.run()
    saved = hub._hub_cache_path('fec_result_cache.json', a, '')
    with open(saved, 'w', encoding='utf-8') as fh:
        _json.dump(_a_report(a), fh)
    assert at.success
    _clear_traces(at)
    at.run()
    assert not os.path.exists(saved)
    assert next(t for t in at.text_input if t.label.startswith('A End FEC')).value == ''
    assert not at.success and 'fec_result' not in at.session_state
    # the same folder back in the box needs a fresh run
    next(t for t in at.text_input if t.label.startswith('A End FEC')).set_value(a).run()
    assert not at.success


def test_clear_traces_after_a_row_click_forgets_the_fec_report(tmp_path, monkeypatch):
    """A row click starts a new session on Viewer FEC; the report's saved
    copy is filed under the run's folders, which the link brings."""
    import json as _json
    import os
    import app as hub
    from conftest import run_streamlit
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    fa = tmp_path / 'endA'
    fa.mkdir()
    saved = hub._hub_cache_path('fec_result_cache.json', str(fa), '')
    with open(saved, 'w', encoding='utf-8') as fh:
        _json.dump(_a_report(str(fa)), fh)
    at = run_streamlit()
    at.query_params['nav'] = 'viewerfec'
    at.query_params['fiber'] = '6'
    at.query_params['km'] = '1.002'
    at.query_params['dir'] = 'a'
    at.query_params['fa'] = str(fa)
    at.query_params['ra'] = str(fa) + '.zip'     # the box held the zip
    at.run()
    assert not at.exception, at.exception
    _clear_traces(at)
    assert not os.path.exists(saved)
    at.sidebar.radio[0].set_value('Splice Report FEC').run()
    assert next(t for t in at.text_input if t.label.startswith('A End FEC')).value == ''
