"""A KeyEvent's time of travel is SIGNED, and the Viewer was reading it unsigned.

Where this came from.  Robert asked whether the Viewer could honour a span that
FastReporter had written into a .sor.  Driving FR to set one (Spans by Distance
> Launch fiber length) and diffing the saved file showed FR re-bases every event
so the span start is 0 — which puts everything ahead of it, the OTDR port and
any launch connector, at a NEGATIVE position.

Read unsigned, -1528 comes back as 4,294,965,768 and the event lands 87,594 km
out.  That number is all through our own comments as "the time-of-travel
artifact the viewer's reader still emits".  It is not an artifact, the
instrument does not emit it, and the engine never saw it:

    viewer/sor_reader324802a.py        tot = unpack('<I', ...)   UNSIGNED
    splicereport/sor_reader324802a.py  tot = unpack('<i', ...)   signed
    secretsauce/sor_reader324802a.py   tot = unpack('<I', ...)   UNSIGNED

Measured on the same two files, before the fix:

                          splicereport          viewer
    span-declared     1.0054, one at -0.0312    87,593.9277 km
    panel FTH01       1.0956, one at -0.0153    87,593.9436 km

So splice reports have always been right about these files and the Viewer has
always been wrong.  Nothing in the report moves; this is a Viewer correction
that brings it into line with the engine.

IT IS NOT HYPOTHETICAL.  Both tie-panel folders on disk are span-declared files
— 6 of 6 sampled fibers in each — and they are the shape PR#118's panel work
cares about:

    panel FTH01     events -0.0153 .. 1.0956   curve falls off at 2.1390 km
    panel PTL1PTL6  events -0.0204 .. 1.0971   curve falls off at 2.1242 km

which reads as a 1 km launch reel, the ~62 m panel jumper as the declared span,
and a 1 km receive reel — with the events span-relative and the curve still in
the raw frame about a kilometre away.  Realigning those two is NOT done here;
see the module note at the end.

Secret Sauce shares the unsigned read.  It works on trace shape rather than
event distance so the impact is probably nil, but that is unverified and
deliberately left alone rather than changed on a guess.
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def _reader_src(which):
    return open(os.path.join(ROOT, which, 'sor_reader324802a.py'),
                encoding='utf-8').read()


def _tot_unpack(src):
    """The struct format the reader uses for a KeyEvent's time of travel."""
    m = re.search(r"tot\s*=\s*struct\.unpack_from\('<(\w)'", src)
    assert m, 'the reader no longer unpacks `tot` from KeyEvents'
    return m.group(1)


def test_the_viewer_reads_the_time_of_travel_signed():
    """'<i', not '<I'.  One letter, and it is the whole bug."""
    assert _tot_unpack(_reader_src('viewer')) == 'i'


def test_the_viewer_and_the_engine_agree_on_the_sign():
    """These two readers are deliberately isolated copies, which is exactly how
    they drifted apart.  A file cannot mean two different things depending on
    which one opened it."""
    assert _tot_unpack(_reader_src('viewer')) == _tot_unpack(_reader_src('splicereport'))


def test_the_87594_number_is_gone_from_the_viewers_reasoning():
    """It only ever existed because of the unsigned read, so nothing may still
    be keyed on seeing it."""
    src = _reader_src('viewer')
    assert '87593' not in src and '87594' not in src


def test_a_negative_event_is_what_marks_a_declared_span():
    """The signal a reader should key on, now that it is readable.

    Not asserted as behaviour yet — nothing consumes it — but pinned so the
    meaning is on the record with the fix that made it visible.
    """
    tot = 4294965768                       # what the file really contains
    signed = tot - 2**32
    assert signed == -1528
    # scale from a known-good event on the same span: 49273 units = 1004.9 m
    m_per_unit = 1004.9 / 49273
    assert abs(signed * m_per_unit - (-31.16)) < 0.1     # FR prints -0.0311 km


# ── What is deliberately NOT fixed here ──────────────────────────────────
#
# A span-declared file leaves its EVENTS in the span frame and its SAMPLES in
# the raw frame, and stores nothing that records the gap.  Diffed every block:
# DataPts, FxdParams and SupParams are byte-identical to the un-spanned twin;
# only KeyEvents, the proprietary block and the checksum move.  Searched every
# numeric field for the 1036.03 m shift and the 2041.40 m absolute length —
# in the span-declared file neither value survives.
#
# FR still aligns such a file opened cold, so it must MEASURE the offset:
# (end of fiber found in the samples) - (span length) reproduces FR's shift to
# about 100 mm.  That is implementable, and it is the obvious next step, but it
# is inference — and it would be tuned on the handful of span-declared files we
# happen to own.  The reel detector this Viewer already carries is what a
# plausible inference tuned on too few spans looks like in the field, so the
# offset half waits for ground truth rather than shipping on a guess.
