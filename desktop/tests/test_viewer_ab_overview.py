"""A+B was silently excluded from whole-cable overview, and the field noticed.

Asking for a cable in both directions gave you 48 traces and a message about
narrowing the range, because the gate read `dirs.length === 1`.  One direction
went to 1152; two went to 48.

That exclusion was never about the transfer.  It was there because the event
grid's column clustering was cubic, so 2,304 traces would not render at all --
and that was fixed when the grid was rebuilt to hold a cable.  Measured in the
browser afterwards, on 864 fibers BOTH ways (1,728 traces, 22,767 events):

    clustering       348 ms     the part that used to be impossible
    fetch         36,127 ms     62 MB through a single-threaded server

So the remaining cost is transfer, it is linear in traces, and it belongs to
the tech who asked for a whole cable twice over.

Verified end to end at that size before this was written: 1,728 traces loaded
(864 A + 864 B), overview mode on 1,726 of them, the FastReporter grid rendered
with 58 event columns, Minimum / Maximum / Average pinned and populated
(0.187, 0.185, 0.193 ...), and 13 body rows in the DOM -- the virtualiser still
doing its job at cable scale.

These are source assertions.  There is no Node here, so the JS cannot be
executed in CI; what is pinned is the shape of the decisions, which is what
regressed last time.
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
VIEWER_HTML = os.path.join(ROOT, 'viewer', 'viewer.html')


def _src():
    return open(VIEWER_HTML, encoding='utf-8').read()


def _fn(src, name):
    """The body of one top-level function, for scoped assertions."""
    i = src.index(f'function {name}(')
    j = src.index('\nfunction ', i + 1)
    return src[i:j]


def _code(body):
    """The same body with comments stripped.

    Every "must NOT appear" assertion below has to run on this: the comments
    explaining why `dirs.length === 1` was wrong necessarily quote it, and a
    naive substring check then fails on the explanation rather than the code.
    """
    out = []
    for line in body.split('\n'):
        stripped = line.lstrip()
        if stripped.startswith('//'):
            continue
        if '//' in line:
            line = line[:line.index('//')]
        out.append(line)
    return '\n'.join(out)


# ── the gate ─────────────────────────────────────────────────────────────

def test_overview_no_longer_requires_a_single_direction():
    """The whole bug, in one clause."""
    body = _code(_fn(_src(), 'addFibers'))
    assert "dirs.length === 1" not in body, \
        'A+B is being excluded from overview again'


def test_overview_is_chosen_on_size_alone():
    body = _fn(_src(), 'addFibers')
    assert re.search(r'const overview = tasks\.length > MAX_DETAIL_TRACES', body)


def test_the_cap_scales_with_how_many_directions_were_asked_for():
    """1152 fibers is the cable; in A+B that is 2304 traces, not 1152.  Capping
    on traces would quietly halve the cable the moment a tech picked A+B."""
    body = _fn(_src(), 'addFibers')
    assert 'MAX_OVERVIEW_FIBERS' in body, 'the cap should be per direction'
    assert re.search(r'MAX_OVERVIEW_FIBERS \* dirs\.length', body)


def test_the_detail_regime_is_untouched():
    """Small loads must still take the full-resolution path -- that is where a
    tech reads an actual splice loss."""
    body = _fn(_src(), 'addFibers')
    assert 'const MAX_DETAIL_TRACES = 48;' in body
    assert 'MAX_DETAIL_TRACES' in body.split('const overview')[1][:200]


# ── the loader ───────────────────────────────────────────────────────────

def test_the_bulk_loader_groups_by_direction():
    """/api/traces serves ONE direction per request, and in A+B the task list
    interleaves them.  Without grouping, every request would ask one direction
    for the other's fibers."""
    body = _fn(_src(), 'loadOverview')
    assert 'byDir' in body
    assert re.search(r'for \(const \[dir, dirTasks\] of byDir\)', body)


def test_the_loader_no_longer_takes_a_direction_argument():
    """It reads the direction off each task now; a caller passing dirs[0] would
    silently load one direction twice."""
    src = _src()
    assert re.search(r'async function loadOverview\(tasks\)', src)
    assert 'loadOverview(tasks, dirs[0])' not in src
    assert 'await loadOverview(tasks)' in src


def test_each_request_asks_for_the_direction_it_is_grouped_under():
    body = _fn(_src(), 'loadOverview')
    assert re.search(r'/api/traces\?dir=\$\{dir\}&fibers=\$\{spec\}', body)


def test_progress_still_repaints_per_chunk():
    """A 36-second load that paints nothing reads as a hang.  The chunk loop
    re-fits, re-chips and redraws as traces land, and that has to survive the
    extra nesting the direction grouping added."""
    body = _code(_fn(_src(), 'loadOverview'))
    for call in ('renderChips();', 'draw();', 'setReadout('):
        assert call in body, f'{call} fell out of the chunk loop'
    assert 'renderEventTable' not in body, \
        'rebuilding ~10k rows per chunk costs more than the redraw buys'
