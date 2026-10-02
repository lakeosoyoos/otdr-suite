"""The hub's slips from the Viewer polish for a 432-fiber span (audit #39).

5. The browser tab read "Streamlit" while the hub reran: Streamlit's page
   resets the title at the start of every run and only sets it back when the
   script reaches st.set_page_config.  A script on the hub page now turns
   that reset into "OTDR Suite".
6. The sidebar said "A: 1 fibers".  One is "1 fiber".
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

from conftest import (APP_PATH, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR,
                      run_streamlit)
from test_viewer_overview_failures import JSC, needs_jsc

APP = APP_PATH.read_text(encoding="utf-8")


def _jsc(prog, tmp_path, name='prog.js'):
    path = tmp_path / name
    path.write_text(prog, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-3000:]
    line = [ln for ln in out.splitlines() if ln.startswith('OUT ')][-1]
    return json.loads(line[4:])


# ─── 5. the tab keeps its title through a rerun ─────────────────────────────

def _title_js():
    m = re.search(r"TITLE_KEEP_JS = \"\"\"\n<script>\n(.*?)</script>\n\"\"\"", APP, re.S)
    assert m, 'app.py no longer has TITLE_KEEP_JS'
    return m.group(1).replace('__TITLE__', 'OTDR Suite')


@needs_jsc
def test_streamlits_reset_writes_the_app_title(tmp_path):
    prog = r"""
function Document() {}
var store = { t: 'OTDR Suite' };
Object.defineProperty(Document.prototype, 'title', {
  configurable: true,
  get: function () { return store.t; }, set: function (v) { store.t = String(v); } });
var doc = new Document();
var window = { parent: { document: doc, Document: Document } };
""" + _title_js() + r"""
var out = [];
doc.title = 'Streamlit';                  // Streamlit's reset at a run's start
out.push(doc.title);
doc.title = 'OTDR Suite';                 // set_page_config
out.push(doc.title);
doc.title = 'Something else';             // any other title passes through
out.push(doc.title);
var again = window.parent.__otdrTitleKeep;
print('OUT ' + JSON.stringify({ seen: out, installed: again }));
"""
    out = _jsc(prog, tmp_path)
    assert out == {'seen': ['OTDR Suite', 'OTDR Suite', 'Something else'], 'installed': True}


def test_the_title_keep_is_installed_on_every_run():
    assert "\n_install_title_keep()\n" in APP
    assert "st.set_page_config(page_title='OTDR Suite', layout='wide'," in APP
    assert "PAGE_TITLE = 'OTDR Suite'" in APP


# ─── 6. one fiber ───────────────────────────────────────────────────────────

def _one_file_folder(src, dest):
    dest.mkdir()
    first = sorted(f for f in os.listdir(src) if f.lower().endswith('.sor'))[0]
    shutil.copy2(os.path.join(src, first), dest / first)
    return str(dest)


def test_the_sidebar_says_one_fiber(tmp_path):
    a = _one_file_folder(FIXTURE_SPLICE_A_DIR, tmp_path / 'A')
    b = _one_file_folder(FIXTURE_SPLICE_B_DIR, tmp_path / 'B')
    at = run_streamlit(default_timeout=180).run()
    next(t for t in at.sidebar.text_input if t.label == 'A Folder').input(a).run()
    next(t for t in at.sidebar.text_input if t.label == 'B Folder').input(b).run()
    assert not at.exception, at.exception
    caps = ' '.join(str(c.value) for c in at.sidebar.caption)
    assert 'A: 1 fiber · B: 1 fiber' in caps, caps
    assert '1 fibers' not in caps
