"""A mid-span connector is flagged when either direction or its average fails.

The boss, 2026-09-29: "We can't average connector losses A and B.  We have
to see them separately.  Flag them if over their threshold.  But then we also
give a bidi average."  Robert: the Splice Report flags a connector when a
direction fails OR the average fails, at every connector.

The two cable ends already had a one-direction gate (detect_launch_issues).
A mid-span connector (a reflective event) was graded on its A+B average
only (apply_connector_loss_rule, "Bidir connector loss" 0.500), so 0.90 from
A and -0.10 from B (average 0.40) passed.  Now either direction at or over
"Connector loss (1 direction)" (LAUNCH_CONN_UNI_MIN_DB, 0.649) flags it too:
one-sided (a gainer is not a bad connector), on the printed value, only
when both directions read it, and off on a panel span (the report grades a
tie between reels on the pair).  The label keeps the average it always
printed and names each failing direction with its own reading.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
from test_launch_conn_loss import _run

_CELLS = """
    def cell(a, b, etype='1F9999LS', bidir='avg', label='13 REFL {} (-18dB)'):
        if bidir == 'avg':
            bidir = None if (a is None or b is None) else (a + b) / 2.0
        shown = E._format_loss(bidir if bidir is not None else (a if a is not None else b))
        return {'fiber': 13, 'splice_idx': 4, 'bidir_loss': bidir,
                'a_loss': a, 'b_loss': b, 'event_type': etype,
                'is_ref': True, 'is_flagged': True, 'label': label.format(shown)}

    def run(**cells_and_kw):
        kw = {k: cells_and_kw.pop(k) for k in ('panel_span', 'uni_threshold')
              if k in cells_and_kw}
        res = {(i, 0): c for i, c in enumerate(cells_and_kw.values())}
        n = E.apply_connector_loss_rule(res, E.BIDIR_CONNECTOR_LOSS, **kw)
        return n, dict(zip(cells_and_kw, res.values()))
"""


def test_one_direction_over_the_gate_flags_a_passing_average():
    _run(_CELLS, """
        n, r = run(x=cell(0.90, -0.10))
        c = r['x']
        assert n == 1 and c['is_high_connector_loss'] and c['conn_fail_sides'] == ['A']
        assert c['label'] == '13 REFL .400 (-18dB) ⚠ conn .900 A side', c['label']
        # mirror
        n, r = run(x=cell(-0.10, 0.90))
        assert r['x']['label'] == '13 REFL .400 (-18dB) ⚠ conn .900 B side', r['x']['label']
        print('OK')
    """)


def test_both_directions_and_the_average_each_show():
    _run(_CELLS, """
        n, r = run(x=cell(0.802, 0.834))
        assert r['x']['label'] == '13 REFL .818 (-18dB) ⚠ conn .802 A side .834 B side', r['x']['label']
        assert r['x']['conn_fail_sides'] == ['A', 'B']
        print('OK')
    """)


def test_the_average_alone_still_flags_as_before():
    _run(_CELLS, """
        n, r = run(x=cell(0.60, 0.50))
        assert n == 1 and r['x']['label'] == '13 REFL .550 (-18dB) ⚠ conn', r['x']['label']
        assert 'conn_fail_sides' not in r['x']
        print('OK')
    """)


def test_healthy_gainer_and_non_connector_stay_quiet():
    _run(_CELLS, """
        n, r = run(ok=cell(0.30, 0.20),
                   gain=cell(-0.90, 0.10),                  # a gainer is not a bad connector
                   splice=cell(0.90, -0.10, etype='0F9999LS'))
        assert n == 0, r
        for k in ('ok', 'gain', 'splice'):
            assert not r[k].get('is_high_connector_loss'), (k, r[k])
            assert '⚠ conn' not in r[k]['label'], (k, r[k]['label'])
        print('OK')
    """)


def test_the_gate_compares_the_printed_value_inclusively():
    _run(_CELLS, """
        n, r = run(at=cell(0.649, 0.0), under=cell(0.6484, 0.0), rounds_up=cell(0.6486, 0.0))
        assert r['at']['conn_fail_sides'] == ['A']
        assert r['rounds_up']['conn_fail_sides'] == ['A']       # prints .649
        assert not r['under'].get('is_high_connector_loss')     # prints .648
        print('OK')
    """)


def test_one_direction_only_is_not_a_direction_check():
    """Both directions must read the connector for the one-direction check;
    a single reading keeps the old fallback (its loss against the average
    gate), exactly as before."""
    _run(_CELLS, """
        n, r = run(a_only=cell(0.90, None), b_only_small=cell(None, 0.30))
        assert r['a_only']['label'] == '13 REFL .900 (-18dB) ⚠ conn', r['a_only']['label']
        assert 'conn_fail_sides' not in r['a_only']
        assert not r['b_only_small'].get('is_high_connector_loss')
        print('OK')
    """)


def test_off_on_a_panel_span_and_when_the_gate_is_zero():
    _run(_CELLS, """
        n, r = run(x=cell(0.90, -0.10), y=cell(0.80, 0.83), panel_span=True)
        assert not r['x'].get('is_high_connector_loss')          # average .40 passes
        assert r['y']['label'] == '13 REFL .815 (-18dB) ⚠ conn', r['y']['label']
        n, r = run(x=cell(0.90, -0.10), uni_threshold=0.0)
        assert not r['x'].get('is_high_connector_loss')
        # the gate is read at call time, so an --overrides setattr moves it
        E.LAUNCH_CONN_UNI_MIN_DB = 1.0
        n, r = run(x=cell(0.90, -0.10))
        assert not r['x'].get('is_high_connector_loss')
        print('OK')
    """)


def test_a_rebuilt_cell_text_names_the_failing_side_too():
    """Cells whose text build_ribbon_data rebuilds (a generic reburn at a
    reflective event) get the same side reading after '⚠ conn'."""
    _run(_CELLS, """
        c = cell(0.90, -0.10, label='13 {}')
        c.update(is_ref=False, is_bend=False, is_break=False, is_broke=False,
                 is_a_only=False, is_b_only=False, is_bfill=False,
                 event_source='bidir', bidir_dist=12.0)
        res = {(13, 0): c}
        E.apply_connector_loss_rule(res, E.BIDIR_CONNECTOR_LOSS)
        cells, _a, _b = E.build_ribbon_data(res, 24, 12, 1)
        text = ' '.join(str(v.get('text') if isinstance(v, dict) else v)
                        for v in (cells.values() if isinstance(cells, dict) else cells))
        assert '⚠ conn .900 A side' in text, text
        print('OK')
    """)


def test_the_runner_and_the_engine_pass_the_panel_span():
    """Both callers hand the rule the span's panel decision, so a tie between
    reels keeps the report's pair-only grading."""
    from conftest import REPO_ROOT
    runner = (REPO_ROOT / "splicereport" / "run_splicereport.py").read_text(encoding="utf-8")
    engine = (REPO_ROOT / "splicereport" / "splicereportmatchexfo.py").read_text(encoding="utf-8")
    assert ("E.apply_connector_loss_rule(all_results, E.BIDIR_CONNECTOR_LOSS,\n"
            "                                        panel_span=E._is_panel_span(fa))") in runner
    assert ("apply_connector_loss_rule(all_results, BIDIR_CONNECTOR_LOSS,\n"
            "                                            panel_span=_is_panel_span(fibers_a))") in engine
