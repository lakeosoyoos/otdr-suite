"""Report grid -> Viewer pop-out: the two bugs found on 2026-09-28.

1. SRC WAS NEVER FILLED IN.  ``_render_clickable_grid`` (app.py) embeds
   ``var SRC = "__SRC__";`` but only ever replaced ``__TABLE__`` and
   ``__ORIGIN__``.  Every pop-out click sent the literal ``src=__SRC__``, in
   the first-open URL and in the ``otdr-jump`` message.  The Viewer maps any
   src other than 'uni' to 'sr', so a Unidirectional cell opened a Viewer
   that judged its verdicts by the Splice Report's gate and offered "Back to
   Splice Report".

2. A PLAIN CLICK COULD KEEP THE PREVIOUS FIBER.  Open the pop-out from a cell
   (``?fiber=20``), then plain-click another cell (fiber 3): F3 and F20 were
   both plotted.  The grid does ``vw.focus(); vw.postMessage({..replace:
   true})``.  The jump runs applyTarget -> clearAll() -> F3 loading.  The
   focus listener fetches /api/list first, comes back while F3 is still in
   flight, sees an empty gTraces and re-runs the boot load, which re-applies
   the page URL's deep link: fiber 20.  gBootInFlight only covers a boot pass
   already running, not that gap.

   Fix: the URL's deep link is spent (``gDeepLinkDone``) once it has put a
   trace on the canvas or a jump message has arrived; bootLoad declines after
   that.  A page that has never shown anything still gets the focus retry.

pytest has no JS engine (and CI runs it on Windows), so bug 2 follows
test_viewer_boot_race.py: pin the structure in viewer.html, then drive an
asyncio mirror whose rules are PARSED OUT OF viewer.html, with a teeth check
that the same mirror without the fix reproduces the F3 + F20 result.  The
mirror's answers were checked once against the real viewer.html run headless
in JavaScriptCore (macOS jsc) with a stub DOM: same results in every case.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
APP_PY = os.path.join(ROOT, 'app.py')
VIEWER_HTML = os.path.join(ROOT, 'viewer', 'viewer.html')

CELL = ("<span class='vc' data-fiber='3' data-km='1.25' data-dir='a' "
        "title=''>F3 0.412</span>")


def _app_src():
    return open(APP_PY, encoding='utf-8').read()


def _viewer_src():
    return open(VIEWER_HTML, encoding='utf-8').read()


# ═══ 1. the grid document carries the report's src ═══════════════════════

def _render_grid(src, table=CELL, port=8771):
    """Run app.py's _render_clickable_grid on its own and return the HTML
    document it hands to st_components_html."""
    tree = ast.parse(_app_src())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == '_render_clickable_grid')
    seen = {}

    def fake_components_html(doc, **kw):
        seen['doc'] = doc

    mod = types.ModuleType('grid')
    import app_theme
    mod.__dict__.update(json=json, st_components_html=fake_components_html,
                        app_theme=app_theme)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), 'app.py', 'exec'),
         mod.__dict__)
    mod._render_clickable_grid(table, port, src=src)
    return seen['doc']


def _grid_call_src(port_name):
    """The src= keyword app.py passes at the _render_clickable_grid call whose
    port argument is `port_name` (the Unidirectional page uses _uni_port)."""
    for node in ast.walk(ast.parse(_app_src())):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == '_render_clickable_grid'
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Name)
                and node.args[1].id == port_name):
            kw = {k.arg: k.value for k in node.keywords}
            return kw['src']
    raise AssertionError(f'no _render_clickable_grid call with port {port_name}')


def _src_literal(doc):
    m = re.search(r'var SRC = "((?:[^"\\]|\\.)*)";', doc)
    assert m, 'the grid script no longer declares var SRC = "...";'
    return m.group(1)


def test_uni_grid_document_carries_uni_not_the_placeholder():
    uni = ast.literal_eval(_grid_call_src('_uni_port'))
    assert uni == 'uni'
    doc = _render_grid(uni)
    assert '__SRC__' not in doc, 'the pop-out still sends the literal __SRC__'
    assert re.findall(r'__[A-Z]+__', doc) == [], 'a placeholder was left in'
    assert _src_literal(doc) == 'uni'
    # Both doors read SRC: the first-open URL and the live jump message.
    assert '"&src=" + SRC' in doc
    assert 'src: SRC' in doc


def test_splice_report_grid_carries_sr():
    call = _grid_call_src('port')
    assert isinstance(call, ast.Name) and call.id == '_p'
    page = _app_src().split('\ndef page_splice_report(', 1)[1].split('\ndef ', 1)[0]
    assert "_p = 'sr'" in page
    doc = _render_grid('sr')
    assert '__SRC__' not in doc
    assert _src_literal(doc) == 'sr'


def test_no_src_adds_no_src_to_the_link():
    """src='' (the default) must stay falsy in JS, so the first-open URL gets
    no &src= and the Viewer keeps its standalone gate."""
    doc = _render_grid('')
    assert '__SRC__' not in doc
    assert _src_literal(doc) == ''


def test_src_is_a_safe_js_string():
    """Only 'sr' and 'uni' reach it today; any other text must stay one JS
    string and must not end the <script> early."""
    nasty = 'x"y\\z</script><b>'
    doc = _render_grid(nasty)
    assert json.loads('"' + _src_literal(doc) + '"') == nasty
    assert doc.count('</script>') == 1


def test_table_text_is_never_taken_for_a_placeholder():
    table = CELL + '<td>__SRC__ __ORIGIN__</td>'
    doc = _render_grid('uni', table=table)
    assert '<td>__SRC__ __ORIGIN__</td>' in doc


def test_plain_click_focuses_then_posts_a_replace_jump():
    """The premise of the mirror below: an open window gets vw.focus() and
    THEN the jump, and a plain click (no shift) sends replace: true."""
    doc = _render_grid('uni')
    i_focus = doc.index('vw.focus();')
    i_post = doc.index('vw.postMessage(')
    assert i_focus < i_post
    assert 'replace: !stack' in doc
    assert 'jump(el, ev.shiftKey)' in doc


# ═══ 2. the URL's deep link is applied once ══════════════════════════════

def _strip_comments(src):
    """Drop // line comments (same crude rule as test_viewer_boot_race.py;
    the regions read here have no // inside strings)."""
    return '\n'.join(re.sub(r'//.*$', '', ln) for ln in src.split('\n'))


def _fn_body(src, head):
    i = src.index(head)
    return src[i:src.index('\n}', i)]


def _listener_body(src, event):
    i = src.index(f"window.addEventListener('{event}'")
    return src[i:src.index('\n});', i)]


RULE_NAMES = ('bootload_declines', 'boot_spends', 'jump_spends',
              'jump_reads_info')
MAIN_RULES = dict.fromkeys(RULE_NAMES, False)     # viewer.html before the fix


def rules_from_source(src=None):
    """What viewer.html does about the URL's deep link, read out of it.

    bootload_declines  bootLoad returns early once the link is spent
    boot_spends        a boot pass that put traces up spends the link
    jump_spends        an otdr-jump spends it BEFORE anything awaits
    jump_reads_info    a jump that beats the first /api/list reads it itself
    """
    src = _strip_comments(src if src is not None else _viewer_src())
    boot = _fn_body(src, 'async function bootLoad(')
    msg = _listener_body(src, 'message')
    i_spend = msg.find('gDeepLinkDone = true;')
    i_type = msg.find("d.type !== 'otdr-jump'")
    i_chain = msg.find('Promise.resolve(')
    return {
        'bootload_declines': re.search(
            r'\{\s*if\s*\(\s*!gInfo\s*\|\|\s*gDeepLinkDone\s*\)\s*return\s*;',
            boot) is not None,
        'boot_spends': re.search(
            r'await\s+applyTarget\([^;]*\);\s*'
            r'if\s*\(\s*gTraces\.length\s*\)\s*gDeepLinkDone\s*=\s*true\s*;\s*$',
            boot) is not None,
        'jump_spends': 0 <= i_type < i_spend < i_chain,
        'jump_reads_info': re.search(
            r'\.then\(\s*\(\)\s*=>\s*gInfo\s*\|\|\s*loadInfo\(\)\s*\)\s*'
            r'\.then\(\s*\(\)\s*=>\s*applyTarget\(d\)\s*\)', msg) is not None,
    }


def test_every_rule_is_present_in_viewer_html():
    rules = rules_from_source()
    missing = [k for k, v in rules.items() if not v]
    assert not missing, (
        'viewer.html lost %r.  These keep the URL deep link from being '
        're-applied after a cell jump; if the code changed shape, teach '
        'rules_from_source the new form rather than deleting the check'
        % missing)


def test_the_flag_starts_false_and_nothing_resets_it():
    src = _strip_comments(_viewer_src())
    assert len(re.findall(r'\blet\s+gDeepLinkDone\s*=\s*false\s*;', src)) == 1
    assert len(re.findall(r'\bgDeepLinkDone\s*=\s*false\b', src)) == 1, (
        'something resets gDeepLinkDone: a reset re-opens the old fiber '
        'coming back on the next focus')


def test_only_bootload_and_the_jump_spend_the_link():
    """Spending it anywhere else (applyTarget, the boot IIFE, loadInfo) would
    kill the retry for a page that has never shown anything."""
    src = _strip_comments(_viewer_src())
    spends = [m.start() for m in re.finditer(r'\bgDeepLinkDone\s*=\s*true', src)]
    b0 = src.index('async function bootLoad(')
    b1 = src.index('\n}', b0)
    m0 = src.index("window.addEventListener('message'")
    m1 = src.index('\n});', m0)
    assert len(spends) == 2
    assert all(b0 < s < b1 or m0 < s < m1 for s in spends), spends


def test_focus_retry_still_boots_an_empty_page():
    """The retry the focus listener exists for must survive: it still boots
    through bootLoadOnce when the canvas is empty."""
    body = _listener_body(_strip_comments(_viewer_src()), 'focus')
    assert re.search(r'if\s*\(\s*ok\s*&&\s*!gTraces\.length\s*\)', body)
    assert 'bootLoadOnce()' in body


# ─── the mirror ──────────────────────────────────────────────────────────
#
# addFibers plans against gTraces synchronously and commits after its
# fetches; loadInfo is one fetch of /api/list; the focus listener, the jump
# listener, bootLoad and bootLoadOnce have the shapes in viewer.html, with the
# four rules above switched on or off.  The trace server is single-threaded
# and serves /api/list before the /api/trace calls queued after it, which is
# why the focus retry comes back first (LIST_S < TRACE_S).

LIST_S = 0.01
TRACE_S = 0.03


class _Server:
    def __init__(self, up=True, fibers=(3, 20)):
        self.up = up
        self.fibers = set(fibers)


class _Viewer:
    URL = {'fiber': 20, 'dir': 'both'}     # the cell that opened the window

    def __init__(self, rules, server, list_s=LIST_S, trace_s=TRACE_S):
        self.rules = rules
        self.server = server
        self.list_s = list_s
        self.trace_s = trace_s
        self.info = None                   # gInfo
        self.traces = []                   # gTraces keys
        self.done = False                  # gDeepLinkDone
        self.in_flight = None              # gBootInFlight

    async def load_info(self):             # fetch('/api/list')
        await asyncio.sleep(self.list_s)
        if not self.server.up:
            return False
        self.info = {'fibers': set(self.server.fibers)}
        return True

    async def _load_one(self, key, fiber):  # fetch('/api/trace') + push
        await asyncio.sleep(self.trace_s)
        if fiber in self.server.fibers:
            self.traces.append(key)

    async def add_fibers(self, fiber, dirs):
        plan = [(f'{d}-{fiber}', fiber) for d in dirs
                if f'{d}-{fiber}' not in self.traces]
        await asyncio.gather(*(self._load_one(k, f) for k, f in plan))

    def clear_all(self):
        self.traces = []

    async def apply_target(self, t):
        if not self.info or not t:
            return
        if t.get('replace'):
            self.clear_all()
        d = t.get('dir', 'both')
        await self.add_fibers(t['fiber'], ['a', 'b'] if d == 'both' else [d])

    async def boot_load(self):
        if not self.info or (self.rules['bootload_declines'] and self.done):
            return
        await self.apply_target(dict(self.URL))
        if self.rules['boot_spends'] and self.traces:
            self.done = True

    def boot_load_once(self):
        if self.in_flight is not None:
            return self.in_flight          # join the pass in flight

        async def run():
            try:
                await self.boot_load()
            finally:
                self.in_flight = None
        self.in_flight = asyncio.ensure_future(run())
        return self.in_flight

    async def boot(self, tries=2):         # the boot IIFE
        ok = await self.load_info()
        for _ in range(tries):
            if ok is not False:
                break
            await asyncio.sleep(0.005)
            ok = await self.load_info()
        await self.boot_load_once()

    async def on_focus(self):              # window 'focus'
        ok = await self.load_info()
        if ok and not self.traces:
            await self.boot_load_once()

    def on_message(self, d):               # window 'message', otdr-jump
        if self.rules['jump_spends']:
            self.done = True
        pending = self.in_flight           # Promise.resolve(gBootInFlight)

        async def chain():
            if pending is not None:
                try:
                    await pending
                except Exception:
                    pass
            if self.rules['jump_reads_info'] and not self.info:
                await self.load_info()
            await self.apply_target(d)
        return asyncio.ensure_future(chain())


async def _click(v, fiber, replace=True, focus_first=True):
    """The grid's click on an open window: vw.focus(); vw.postMessage(...)."""
    d = {'fiber': fiber, 'dir': 'both', 'replace': replace}
    if focus_first:
        f = asyncio.ensure_future(v.on_focus())
        await asyncio.sleep(0)             # focus listener is at its fetch
        m = v.on_message(d)
    else:
        m = v.on_message(d)
        f = asyncio.ensure_future(v.on_focus())
    await asyncio.gather(f, m)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _opened_then_clicked(rules, list_s=LIST_S, trace_s=TRACE_S,
                         focus_first=True, replace=True):
    async def go():
        v = _Viewer(rules, _Server(), list_s, trace_s)
        await v.boot()
        assert sorted(v.traces) == ['a-20', 'b-20']
        await _click(v, 3, replace=replace, focus_first=focus_first)
        await asyncio.sleep(3 * max(list_s, trace_s))   # let stragglers land
        return v
    return _run(go())


@pytest.mark.parametrize('focus_first', [True, False])
@pytest.mark.parametrize('list_s,trace_s', [(0.01, 0.03), (0.005, 0.05),
                                            (0.03, 0.01)])
def test_plain_click_replaces_the_fiber_the_window_opened_on(
        list_s, trace_s, focus_first):
    """THE REPORTED BUG.  Opened on F20, plain click on F3: F3 only."""
    v = _opened_then_clicked(rules_from_source(), list_s, trace_s, focus_first)
    assert sorted(v.traces) == ['a-3', 'b-3'], (
        'plain click left %r: the URL fiber came back' % (v.traces,))


def test_the_mirror_reproduces_the_bug_without_the_fix():
    """Teeth.  Same mirror, pre-fix rules: F3 AND F20, as seen in the VM."""
    v = _opened_then_clicked(MAIN_RULES)
    assert sorted(v.traces) == ['a-20', 'a-3', 'b-20', 'b-3'], v.traces


def test_shift_click_still_stacks():
    v = _opened_then_clicked(rules_from_source(), replace=False)
    assert sorted(v.traces) == ['a-20', 'a-3', 'b-20', 'b-3']


def test_clear_then_focus_does_not_bring_the_link_back():
    async def go(rules):
        v = _Viewer(rules, _Server())
        await v.boot()
        v.clear_all()                      # the tech clears the canvas
        await v.on_focus()
        return v
    assert _run(go(rules_from_source())).traces == []
    assert sorted(_run(go(MAIN_RULES)).traces) == ['a-20', 'b-20']   # teeth


def test_focus_retry_still_loads_a_page_that_never_reached_the_server():
    """Cold start: the server was not up during boot, so the link never ran.
    The next focus must still load it."""
    async def go():
        s = _Server(up=False)
        v = _Viewer(rules_from_source(), s)
        await v.boot()
        assert v.info is None and v.traces == []
        s.up = True
        await v.on_focus()
        return v
    assert sorted(_run(go()).traces) == ['a-20', 'b-20']


def test_focus_retry_still_loads_folders_seeded_after_boot():
    async def go():
        s = _Server(fibers=())
        v = _Viewer(rules_from_source(), s)
        await v.boot()                     # link applied, nothing to load
        assert v.traces == [] and not v.done
        s.fibers = {3, 20}
        await v.on_focus()
        return v
    assert sorted(_run(go()).traces) == ['a-20', 'b-20']


def test_click_on_a_page_that_never_loaded_shows_only_the_click():
    async def go(rules):
        s = _Server(up=False)
        v = _Viewer(rules, s)
        await v.boot()
        s.up = True
        await _click(v, 3)
        await asyncio.sleep(0.2)
        return v
    assert sorted(_run(go(rules_from_source())).traces) == ['a-3', 'b-3']
    assert sorted(_run(go(MAIN_RULES)).traces) != ['a-3', 'b-3']     # teeth


def test_a_jump_that_beats_the_first_list_is_not_dropped():
    """The jump spends the link, so bootLoad no longer loads F20 in its place;
    the jump must then land on its own rather than be dropped for want of
    gInfo, or the window stays blank."""
    async def go(rules):
        v = _Viewer(rules, _Server(), list_s=0.03)
        b = asyncio.ensure_future(v.boot())
        await asyncio.sleep(0)             # boot is at its first fetch
        await _click(v, 3)
        await b
        await asyncio.sleep(0.2)
        return v
    assert sorted(_run(go(rules_from_source())).traces) == ['a-3', 'b-3']
    assert sorted(_run(go(MAIN_RULES)).traces) == ['a-20', 'b-20']   # teeth
