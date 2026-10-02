"""The hub's report pages on a small load: they count the fibers loaded
(not the highest fiber number), say "1 fiber" for one, and draw only the
ribbons that hold a loaded fiber (a 432-fiber span: ribbon 30 alone said "360
fibers" over thirty rows; fiber 354 alone said "354 fibers"; Uni on fiber
354 said "1 fibers")."""
from __future__ import annotations

import shutil

from conftest import (run_streamlit, finish_engine_run, FIXTURE_SPLICE_A_DIR,
                      FIXTURE_SPLICE_B_DIR)


def _copy(src, dest, fibers):
    dest.mkdir()
    for p in sorted(src.glob("*.sor")):
        if int(p.name[6:10]) in fibers:
            shutil.copy(p, dest / p.name)
    return str(dest)


def _hub(a, b=''):
    at = run_streamlit(default_timeout=180).run()
    next(t for t in at.sidebar.text_input if t.label == 'A Folder').input(a).run()
    if b:
        next(t for t in at.sidebar.text_input if t.label == 'B Folder').input(b).run()
    assert not at.exception, at.exception
    return at


def test_uni_one_fiber_says_one_fiber(tmp_path):
    a = _copy(FIXTURE_SPLICE_A_DIR, tmp_path / 'A', {20})
    at = _hub(a)
    at.session_state['uni_report_dest'] = str(tmp_path)
    at.sidebar.radio[0].set_value('Unidirectional').run()
    next(b for b in at.main.button if b.label == 'Run Unidirectional Report').click().run()
    finish_engine_run(at, 'uni')
    assert not at.exception, at.exception
    done = [s.value for s in at.success if s.value.startswith('Done:')]
    assert done and done[0].startswith('Done: 1 fiber ·'), done


def test_sr_one_ribbon_counts_its_fibers_and_draws_one_row(tmp_path):
    fibers = set(range(13, 25))
    a = _copy(FIXTURE_SPLICE_A_DIR, tmp_path / 'A', fibers)
    b = _copy(FIXTURE_SPLICE_B_DIR, tmp_path / 'B', fibers)
    at = _hub(a, b)
    at.session_state['sr_report_dest'] = str(tmp_path)
    at.sidebar.radio[0].set_value('Splice Report').run()
    next(x for x in at.main.button if x.label.startswith('Generate')).click().run()
    finish_engine_run(at, 'sr')
    assert not at.exception, at.exception
    line = next(s.value for s in at.success if '·' in s.value and 'fiber' in s.value)
    assert '  ·  12 fibers  ·  ' in line, line
    grid = ''.join(m.value for m in at.markdown if 'Ribbon</th>' in m.value)
    if not grid:          # the grid may be drawn in its own frame instead
        grid = ''.join(str(getattr(e.proto, 'srcdoc', '') or '')
                       for e in at.get('iframe'))
    assert 'F13–24' in grid and 'F1–12' not in grid
