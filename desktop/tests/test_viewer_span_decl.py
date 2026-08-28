"""The span is DECLARED, because there is nothing in the file to read.

FastReporter never infers a launch reel.  Its Spans by Distance dialog holds a
Launch / Span / Receive length per measurement and a person sets it, usually by
nominating the events that bound the cable.  That is the whole reason FR does
not make our mistake: it cannot invent a reel, because it never guesses.

We went looking for the same field and it is not there.  Swept over 19 folders,
the EXFO proprietary block's `SpansLength` equals the trace's end event every
time -- worst difference 2.8 m, which is last-sample versus end-event -- and
`StartPosition` reads 0.0 even on spans carrying a real 1 km reel:

    DefuniakFR A     SpansLength  2041.4    end  2041.5     StartPosition 0.0
    TUCROM           SpansLength 97177.6    end 97180.1     StartPosition 0.0
    SEANOR           SpansLength 109969.8   end 109972.6    StartPosition 0.0
    ... 19 folders, no exceptions

So FR reads Launch 0.0000 on our files because the files say nothing, and its
rule reduces to "mirror B about its own end".  That is the pre-PR#65 rule we
deliberately replaced, and it fails on the Banning <-> Indio shape (A shot
through a reel, B trimmed) where it put 0 of 4,734 splices on a partner.

Hence this: the declaration is ours to make.  Three sources for the frame now,
in order of how much each actually knows --

    1. the span the tech declared        (this file)
    2. measured from the shared events   (test_viewer_fr_grid / mirror_frame)
    3. inferred from one trace's reels   (the original rule)

A declared span outranks the other two outright.  A measured frame is an
inference from the events; a reel frame is an inference from one trace; a
declared span is something somebody knows.

WHY IT ONLY MOVES B.  A is drawn in its raw frame because report grids hand the
viewer cell distances already shifted by `launch_a_km` (app.py's `_vkm`), so
moving A would land every deep link in the wrong place.  Only `a.start_km` and
`b.end_km` reach the picture.  The other two edges are stored anyway so the
declaration is complete for the engine when it comes to read the same file.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'viewer'))

import trace_server as TS      # noqa: E402

VIEWER = os.path.join(ROOT, 'viewer', 'viewer.html')
SRC = open(VIEWER, encoding='utf-8').read()

A_DIR, B_DIR = '/spans/A', '/spans/B'


def _store(tmp_path, monkeypatch):
    """Point the store at a temp file — never the tech's real one."""
    p = tmp_path / 'spans.json'
    monkeypatch.setattr(TS, 'SPAN_STORE', str(p))
    monkeypatch.setitem(TS.CONFIG, 'dir_a', A_DIR)
    monkeypatch.setitem(TS.CONFIG, 'dir_b', B_DIR)
    return p


# ── the store ────────────────────────────────────────────────────────────

def test_nothing_declared_reads_as_nothing(tmp_path, monkeypatch):
    """The common case, and it must not look like a declaration of zero."""
    _store(tmp_path, monkeypatch)
    assert TS.span_decl() == {'a': None, 'b': None}


def test_an_edge_round_trips_through_disk(tmp_path, monkeypatch):
    p = _store(tmp_path, monkeypatch)
    TS.span_decl_set('a', 'start', 1.0049)
    TS.span_decl_set('b', 'end', 96.1246)
    assert TS.span_decl() == {'a': {'start_km': 1.0049}, 'b': {'end_km': 96.1246}}
    # ...and it is really on disk, which is the point: the browser forgets, the
    # engine has to be able to read it later, and a tech should set it once.
    on_disk = json.loads(p.read_text(encoding='utf-8'))
    entry = on_disk[TS._span_key(A_DIR, B_DIR)]
    assert entry['a']['start_km'] == 1.0049
    assert entry['dir_a'] == A_DIR and entry['dir_b'] == B_DIR


def test_clearing_one_direction_leaves_the_other(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    TS.span_decl_set('a', 'start', 1.0)
    TS.span_decl_set('b', 'end', 96.0)
    TS.span_decl_set('a', 'clear', 0)
    assert TS.span_decl() == {'a': None, 'b': {'end_km': 96.0}}


def test_clearing_everything_drops_the_row(tmp_path, monkeypatch):
    """An emptied span must not leave a husk behind that reads as declared."""
    p = _store(tmp_path, monkeypatch)
    TS.span_decl_set('a', 'start', 1.0)
    TS.span_decl_set('a', 'clear', 0)
    assert TS.span_decl() == {'a': None, 'b': None}
    assert json.loads(p.read_text(encoding='utf-8')) == {}


def test_both_edges_of_one_direction_coexist(tmp_path, monkeypatch):
    """start and end are separate edges, not a toggle."""
    _store(tmp_path, monkeypatch)
    TS.span_decl_set('a', 'start', 1.0049)
    TS.span_decl_set('a', 'end', 96.1196)
    assert TS.span_decl()['a'] == {'start_km': 1.0049, 'end_km': 96.1196}


def test_the_key_survives_a_trailing_separator_and_case(tmp_path, monkeypatch):
    """Same folder pair typed two ways must not open two entries — on Windows
    a pasted path differs from a browsed one by exactly this."""
    _store(tmp_path, monkeypatch)
    TS.span_decl_set('a', 'start', 1.0)
    assert TS.span_decl(A_DIR + '/', B_DIR)['a'] == {'start_km': 1.0}


def test_a_corrupt_store_falls_back_rather_than_throwing(tmp_path, monkeypatch):
    """A half-written file must degrade to 'no declaration', which is exactly
    what the viewer did before any of this existed — not a 500 on /api/list."""
    p = _store(tmp_path, monkeypatch)
    p.write_text('{ this is not json', encoding='utf-8')
    assert TS.span_decl() == {'a': None, 'b': None}


def test_the_write_is_atomic(tmp_path, monkeypatch):
    """os.replace, not a truncating open: a killed write must not eat spans
    the tech set on every other folder pair on this machine."""
    _store(tmp_path, monkeypatch)
    src = open(os.path.join(ROOT, 'viewer', 'trace_server.py'), encoding='utf-8').read()
    fn = src[src.index('def span_decl_set'):]
    fn = fn[:fn.index('\ndef ')]
    assert 'os.replace(' in fn and "'.tmp'" in fn


def test_a_bad_direction_or_edge_is_refused(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    for bad in (('c', 'start'), ('a', 'middle'), ('', '')):
        try:
            TS.span_decl_set(bad[0], bad[1], 1.0)
        except ValueError:
            continue
        raise AssertionError('accepted %r' % (bad,))


def test_the_listing_ships_the_declaration(tmp_path, monkeypatch):
    """The viewer needs it at boot, in the same round trip as everything else
    about the frame — a second fetch would race the first draw."""
    src = open(os.path.join(ROOT, 'viewer', 'trace_server.py'), encoding='utf-8').read()
    assert "'span_decl': span_decl()," in src


# ── the viewer's priority order ──────────────────────────────────────────

def test_declared_outranks_measured_outranks_guessed():
    fn = SRC[SRC.index('function mirrorOriginKm(t) {'):][:400]
    assert 'declaredOriginKm(t)' in fn
    assert 'reelOriginKm(t) + gMirrorDelta' in fn
    assert re.search(r'return d != null \? d :', fn), (
        'a declared span has to win outright — applying the measured '
        'correction on top would re-correct what the tech already fixed')


def test_the_measurement_stands_down_when_a_span_is_declared():
    fn = SRC[SRC.index('function refreshMirrorFrame()'):][:600]
    assert 'if (spanIsDeclared())' in fn


def test_only_the_two_edges_that_move_b_reach_the_picture():
    """A stays raw so report deep links keep landing; see the module docstring."""
    fn = SRC[SRC.index('function declaredOriginKm(t)'):][:700]
    assert 'A.start_km' in fn and 'B.end_km' in fn
    assert 'a.end_km' not in fn and 'b.start_km' not in fn


def test_the_declaration_is_stored_in_the_directions_own_frame():
    """Raw km, not display km and not an event number: the mirror gets
    re-derived and events get renumbered by a re-analysis, but the metre a
    connector sits at does not move."""
    fn = SRC[SRC.index("tb.addEventListener('contextmenu'"):][:600]
    assert 'data-km' in fn
    grid = SRC[SRC.index('cells.push(lossCell(evLoss(e)'):][:400]
    assert 'data-km="${e.dist_km}"' in grid, 'must tag the RAW km, not dispKm'


def test_the_tech_can_set_and_clear_it_from_the_grid():
    assert 'function showSpanMenu(' in SRC
    assert 'function setSpanEdge(' in SRC
    for edge in ("'start'", "'end'", "'clear'"):
        assert edge in SRC[SRC.index('function showSpanMenu('):][:1200]


def test_a_failed_save_is_reported_not_swallowed():
    """The tech has to know the span did not stick, or they will trust a frame
    that reverted the moment they reloaded."""
    fn = SRC[SRC.index('async function setSpanEdge('):][:900]
    assert 'could not save the span' in fn
