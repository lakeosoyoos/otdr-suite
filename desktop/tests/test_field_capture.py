"""Field Capture page: the server that shows the web app inside the hub, the
email draft it writes, and the packaging that ships it.

The web app itself (fieldcapture/web) is JavaScript and there is no Node on
the build machines, so its behaviour was verified in a browser; these tests
pin the Python around it and the contract between the two.
"""
import email
import json
import re
import sys
import threading
import urllib.error
import urllib.request
from email import policy
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fieldcapture import email_draft, server  # noqa: E402

WEB = REPO_ROOT / 'fieldcapture' / 'web'


@pytest.fixture()
def fc(tmp_path, monkeypatch):
    """A Field Capture server on a free port, saving into tmp_path, with the
    'open in the mail program' step recorded instead of run."""
    opened = []
    monkeypatch.setattr(email_draft, 'open_with_default_app', lambda p: (opened.append(Path(p)) or (True, '')))
    monkeypatch.setattr(email_draft, 'reveal', lambda p: (opened.append(Path(p)) or (True, '')))
    monkeypatch.setitem(server.CONFIG, 'dest_dir', str(tmp_path))
    server._saved.clear()
    srv, port = server.find_free_port(8900, 100)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{port}'

    def get(path):
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, dict(r.headers), r.read()

    def post(path, data, headers=None):
        req = urllib.request.Request(base + path, data=data, method='POST', headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                return e.code, json.loads(body or b'{}')
            except ValueError:
                return e.code, {}

    yield type('FC', (), {'get': staticmethod(get), 'post': staticmethod(post), 'opened': opened, 'dest': tmp_path})
    srv.shutdown()
    srv.server_close()


# ── serving the page ─────────────────────────────────────────────────────

def test_the_page_and_its_code_are_served_with_types_a_browser_will_run(fc):
    code, h, body = fc.get('/')
    assert code == 200 and h['Content-Type'].startswith('text/html')
    assert b'OTDR Field Capture' in body
    for path in ('/app.js', '/fqa.js', '/labels.js', '/vendor/jszip.min.js', '/vendor/tesseract/tesseract.min.js'):
        code, h, _ = fc.get(path)
        assert code == 200 and h['Content-Type'].startswith('application/javascript'), path


def test_the_reader_language_data_is_not_sent_as_gzip_encoded(fc):
    """Tesseract fetches eng.traineddata.gz and un-gzips it itself.  A
    Content-Encoding: gzip header would make the browser un-gzip it first and
    the reader would then choke on plain bytes."""
    code, h, body = fc.get('/vendor/tesseract/lang/eng.traineddata.gz')
    assert code == 200 and 'Content-Encoding' not in h
    assert body[:2] == b'\x1f\x8b'


@pytest.mark.parametrize('path', ['/../app.py', '/%2e%2e/app.py', '/..%2fapp.py', '/vendor/../../server.py', '/nope.js'])
def test_nothing_outside_the_web_app_is_served(fc, path):
    with pytest.raises(urllib.error.HTTPError) as e:
        fc.get(path)
    assert e.value.code == 404


def test_the_blank_form_is_the_fqa_builders_own_template(fc):
    """One copy of Lumen's form in the repo: the FQA Builder's.  A fix to it
    (like the namespace-prefix repair) reaches this page too."""
    code, _, body = fc.get('/api/blank-form')
    assert code == 200
    assert body == (REPO_ROOT / 'fqa' / 'templates' / 'FQA_Site_Survey_v1_1.xlsm').read_bytes()


# ── saving and emailing ──────────────────────────────────────────────────

def test_save_writes_into_the_chosen_folder_and_never_overwrites(fc):
    c1, r1 = fc.post('/api/save?name=Span%204%20FQA.xlsm', b'first')
    c2, r2 = fc.post('/api/save?name=Span%204%20FQA.xlsm', b'second')
    assert c1 == c2 == 200
    assert Path(r1['path']) == fc.dest / 'Span 4 FQA.xlsm'
    assert Path(r2['path']) == fc.dest / 'Span 4 FQA (2).xlsm'
    assert Path(r1['path']).read_bytes() == b'first' and Path(r2['path']).read_bytes() == b'second'


def test_a_saved_name_cannot_climb_out_of_the_folder(fc):
    code, r = fc.post('/api/save?name=..%2F..%2Fevil.txt', b'x')
    assert code == 200
    p = Path(r['path'])
    assert p.parent == fc.dest and p.name == 'evil.txt.xlsm'


def test_email_opens_a_draft_with_the_saved_fqa_attached(fc):
    _, saved = fc.post('/api/save?name=FQA.xlsm', b'PK\x03\x04 workbook bytes')
    code, r = fc.post('/api/email', json.dumps({
        'path': saved['path'], 'to': 'office@example.com',
        'subject': 'FQA Site Survey section 1.2: Flagler to Bethune',
        'body': 'Section 1.2 filled in the field.\nLabel check: all match.'}).encode(),
        {'Content-Type': 'application/json'})
    assert code == 200 and r['opened'] is True
    eml = Path(r['eml'])
    assert eml == fc.dest / 'FQA.eml' and fc.opened == [eml]
    msg = email.message_from_bytes(eml.read_bytes(), policy=policy.default)
    assert msg['X-Unsent'] == '1'                       # Outlook opens it as a new draft
    assert msg['To'] == 'office@example.com'
    assert msg['Subject'] == 'FQA Site Survey section 1.2: Flagler to Bethune'
    assert 'Label check: all match.' in msg.get_body(('plain',)).get_content()
    (att,) = list(msg.iter_attachments())
    assert att.get_filename() == 'FQA.xlsm'
    assert att.get_content_type() == 'application/vnd.ms-excel.sheet.macroenabled.12'
    assert att.get_content() == b'PK\x03\x04 workbook bytes'


def test_email_refuses_a_file_this_page_did_not_save(fc, tmp_path):
    other = tmp_path / 'secret.xlsx'
    other.write_bytes(b'x')
    code, r = fc.post('/api/email', json.dumps({'path': str(other)}).encode(), {'Content-Type': 'application/json'})
    assert code == 400 and fc.opened == []


def test_a_cross_site_page_cannot_post(fc):
    code, _ = fc.post('/api/save?name=a.xlsm', b'x', {'Origin': 'https://evil.example'})
    assert code == 403
    assert not any(fc.dest.iterdir())


def test_a_subject_cannot_smuggle_in_extra_headers(tmp_path):
    f = tmp_path / 'a.xlsm'
    f.write_bytes(b'x')
    raw = email_draft.build_draft(f, 'a@b.com\r\nBcc: spy@evil.example', 'Hi\r\nBcc: spy@evil.example', 'body')
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg['Bcc'] is None
    assert '\n' not in msg['Subject'] and '\n' not in msg['To']


# ── the hub page ─────────────────────────────────────────────────────────

def test_the_hub_offers_field_capture():
    from conftest import run_streamlit
    at = run_streamlit(default_timeout=180).run()
    tool = next(r for r in at.sidebar.radio if r.label == 'Tool')
    at = tool.set_value('Field Capture').run()
    assert not at.exception
    assert any('Field Capture' in m.value for m in at.markdown)
    assert server._server is not None                    # the page started its server


# ── the web app and its packaging ────────────────────────────────────────

def test_every_file_the_page_loads_exists():
    html = (WEB / 'index.html').read_text(encoding='utf-8')
    refs = re.findall(r'(?:src|href)="([^"#?]+)"', html)
    assert refs
    for ref in refs:
        assert (WEB / ref).is_file(), ref


def test_the_label_reader_ships_complete():
    """Tesseract picks one of three cores at run time by what the browser
    supports; a missing one breaks label reading on exactly those PCs."""
    core = WEB / 'vendor' / 'tesseract' / 'core'
    for variant in ('lstm', 'simd-lstm', 'relaxedsimd-lstm'):
        assert (core / f'tesseract-core-{variant}.wasm.js').stat().st_size > 1_000_000, variant
    assert (WEB / 'vendor' / 'tesseract' / 'lang' / 'eng.traineddata.gz').stat().st_size > 1_000_000
    assert (WEB / 'vendor' / 'tesseract' / 'worker.min.js').is_file()


def test_both_builds_bundle_the_web_app_with_its_libraries():
    for spec in ('OTDRSuite.spec', 'OTDRSuite-mac.spec'):
        text = (REPO_ROOT / 'desktop' / spec).read_text(encoding='utf-8')
        m = re.search(r'_add_tree\("fieldcapture",\s*\(([^)]*)\)\)', text)
        assert m, spec
        exts = set(re.findall(r'"(\.[a-z]+)"', m.group(1)))
        assert {'.py', '.html', '.js', '.css', '.gz'} <= exts, (spec, exts)


def test_in_the_suite_the_page_uses_the_hub_server_not_a_phone_share_sheet():
    js = (WEB / 'app.js').read_text(encoding='utf-8')
    assert "get('host') === 'suite'" in js
    assert "SUITE ? 'api/blank-form'" in js           # the FQA Builder's template
    assert "fetch('api/save?name='" in js             # saved on the PC ...
    assert "postJson('api/email'" in js               # ... and handed to the mail program
    assert "if (!SUITE && 'serviceWorker' in navigator" in js   # no offline cache on the PC
