"""
FQA Builder — the shared user interface
=======================================

Takes a span's ZeroDB production sheet and builds the Lumen FQA Site
Survey package from it: the cover page, the Fiber Assignment Table, the
Event Log and the Exception Reporting tab.

It is its own app rather than a page of the hub because it does not need
traces to be useful — a production sheet alone gets you a package to
check — and because the people who fill these in are not always the
people running Splice Report.  It still takes closure distances from the
suite when they are there: paste them, or point it at a Splice Report
manifest.

Dev run:  streamlit run fqa/app.py --server.port 8514

Robert, 2026-09-23.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fqa.event_chain import DEFAULT_ENTRY_OFFSET_M, DEFAULT_TOLERANCE_M   # noqa: E402
from fqa.completeness import BLOCKING, audit, summary
from fqa.event_chain import build_chain
from fqa.fat import build_fat
from fqa.job_facts import JobFacts, derive                                # noqa: E402
from fqa.production_sheet import read_production_sheet                    # noqa: E402
from fqa.run_fqa import DEFAULT_TEMPLATE, build
from fqa.writer import form_version                           # noqa: E402


def _staged_upload(upload):
    """Put an uploaded production sheet on disk and return its path.

    The reader wants a file, not a buffer -- it opens the workbook twice
    and these are 70 MB workbooks.  Streamlit reruns the whole script on
    every keystroke in the job form, so the bytes are written ONCE and
    keyed on the file's name and size; without that guard, typing a CLLI
    would rewrite 70 MB per character.
    """
    if upload is None:
        return None
    key = (upload.name, upload.size)
    if st.session_state.get('fqa_upload_key') != key:
        staged = os.path.join(tempfile.gettempdir(), 'fqa_uploads')
        os.makedirs(staged, exist_ok=True)
        path = os.path.join(staged, upload.name)
        with open(path, 'wb') as fh:
            fh.write(upload.getbuffer())
        st.session_state['fqa_upload_key'] = key
        st.session_state['fqa_upload_path'] = path
    return st.session_state.get('fqa_upload_path')


def _downloads() -> str:
    d = os.path.join(os.path.expanduser('~'), 'Downloads')
    return d if os.path.isdir(d) else os.path.expanduser('~')


def _parse_closures(text: str) -> list[float]:
    """Accept whatever a tech pastes: JSON, commas, spaces or one per line.

    The unit is decided for the LIST, never per value.  Deciding per value
    would read the entry splice -- 60 m from the frame on every span we
    have -- as 60 km, and it would do it silently in the middle of an
    otherwise correct chain.  A whole list whose largest entry is under
    500 has to be kilometres: no span ends 500 m from where it started.
    """
    if not text.strip():
        return []
    cleaned = text.replace('[', ' ').replace(']', ' ').replace(',', ' ')
    out: list[float] = []
    for tok in cleaned.split():
        try:
            out.append(float(tok))
        except ValueError:
            raise ValueError(f'{tok!r} is not a number')
    if out and max(out) < 500:
        out = [v * 1000.0 for v in out]
    return out


def _parse_exceptions(text: str) -> list[dict]:
    """One per line: `fiber, event, description` (event optional)."""
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(',', 2)]
        fiber = parts[0]
        event = parts[1] if len(parts) > 1 and parts[1] else None
        desc = parts[2] if len(parts) > 2 else ''
        rows.append({'fiber': fiber, 'event': event, 'description': desc})
    return rows


def _not_complete_section(gaps):
    """The 'Not complete' list.

    Drawn from the live form, not from the last build, so it shrinks as
    the boxes above are filled -- the point is to work THROUGH it, not to
    be told afterwards what was wrong.
    """
    counts = summary(gaps)
    if not gaps:
        st.success('Nothing outstanding.')
        return

    blocking = [g for g in gaps if g.level == BLOCKING]
    checks = [g for g in gaps if g.level != BLOCKING]

    if blocking:
        st.markdown(f'**{len(blocking)} will send the package back**')
        st.dataframe(
            [{'Where': g.where, 'Missing': g.what, 'What it is': g.fix}
             for g in blocking],
            use_container_width=True, hide_index=True)
    if checks:
        with st.expander(f'{len(checks)} worth a look', expanded=not blocking):
            st.dataframe(
                [{'Where': g.where, 'Note': g.what, 'Why': g.fix}
                 for g in checks],
                use_container_width=True, hide_index=True)


def render(default_out_dir: str | None = None,
           dest_row=None) -> None:
    """Draw the FQA Builder.

    Called two ways and it has to behave the same in both: as the whole of
    the standalone app, and as one page of the OTDR Suite hub.  The hub
    passes its own `dest_row` -- the 'Save reports to' control every other
    report page shows -- so the tech meets the same folder picker here as
    on Splice Report rather than a bare text box.

    Nothing in here calls st.stop(): in the hub that would take the rest
    of the page down with it.
    """
    st.markdown('#### FQA Builder')
    st.caption('Lumen Site Survey / Fiber Quality Assurance package, built from '
               'the span’s production sheet.')

    upload = st.file_uploader(
        'Production sheet',
        type=['xlsx', 'xlsm'],
        help='The span’s ZeroDB production sheet — one tab per location, in '
             'order from the A end to the Z end. Drag it in, or Browse.')

    with st.expander('…or give me a path instead', expanded=False):
        # key= without value=: a widget that owns its own session-state slot
        # must not also be handed a value, or Streamlit ignores one of the two
        # and the box stops accepting what is typed into it.
        st.text_input(
            'Production sheet path',
            placeholder='/path/to/Span 4 … Production Sheet final.xlsx',
            key='fqa_prod',
            help='Useful for a sheet that is already on a share, or one too '
                 'big to push through the browser.')
        st.caption('A path avoids the upload entirely, which matters: the '
                   'production sheets run to 250 MB.')

    prod_path = _staged_upload(upload) or (st.session_state.get('fqa_prod') or '')

    if not prod_path:
        return
    if not os.path.exists(prod_path):
        st.error('No file at that path.')
        return

    try:
        prod = read_production_sheet(prod_path)
    except Exception as exc:                                  # noqa: BLE001
        st.error(f'Could not read that production sheet — {exc}')
        return

    for warning in prod.warnings:
        st.warning(warning)

    st.success(f'{len(prod.locations)} locations: {len(prod.splices)} splices '
               f'between {len(prod.terminations)} terminations.')
    with st.expander('Route as read', expanded=False):
        st.dataframe(
            [{'#': i, 'tab': l.sheet, 'type': l.kind, 'vault': l.vault_id,
              'name': l.name, 'address': l.address, 'tech': l.technicians,
              'date': l.work_date} for i, l in enumerate(prod.locations)],
            use_container_width=True, hide_index=True)

    # The derivation runs once per production sheet; the form below edits the
    # result rather than re-deriving over the user's typing.
    if st.session_state.get('fqa_derived_for') != prod_path:
        st.session_state['fqa_job'] = json.loads(derive(prod).to_json())
        st.session_state['fqa_derived_for'] = prod_path
    job = JobFacts.from_dict(st.session_state['fqa_job'])

    st.markdown('##### The job')
    st.caption('Pre-filled from the production sheet where it could answer. '
               'Everything else is yours.')

    cols = st.columns(2)
    for col, tag, site in ((cols[0], 'A', job.site_a), (cols[1], 'Z', job.site_z)):
        with col:
            st.markdown(f'**Site {tag}**')
            site.address = st.text_input(f'{tag} street address', site.address or '',
                                         key=f'{tag}_addr') or None
            c1, c2 = st.columns(2)
            site.alias = c1.text_input(f'{tag} alias', site.alias or '',
                                       key=f'{tag}_alias') or None
            site.clli = c2.text_input(f'{tag} CLLI', site.clli or '',
                                      key=f'{tag}_clli') or None
            c1, c2, c3, c4 = st.columns(4)
            site.floor = c1.text_input('Floor', site.floor or '001',
                                       key=f'{tag}_floor') or None
            site.room = c2.text_input('Room', site.room or '0001',
                                      key=f'{tag}_room') or None
            site.aisle = c3.text_input('Aisle', site.aisle or '',
                                       key=f'{tag}_aisle') or None
            site.bay = c4.text_input('Bay', site.bay or '',
                                     key=f'{tag}_bay') or None
            c1, c2 = st.columns(2)
            site.rmu = c1.text_input('RMU (shelf)', site.rmu or '',
                                     key=f'{tag}_rmu') or None
            site.vendor_part = c2.text_input('Panel vendor part #',
                                             site.vendor_part or '',
                                             key=f'{tag}_vpart') or None
            st.caption(f'Test from device: `{site.test_from_device or "—"}`')

    c1, c2, c3 = st.columns(3)
    job.market = c1.text_input('Market / city', job.market or '') or None
    job.project = c2.text_input('NetBuild / project', job.project or '') or None
    job.customer = c3.text_input('Customer or FEC #', job.customer or '') or None

    c1, c2, c3, c4 = st.columns(4)
    job.contractor = c1.text_input('Contractor', job.contractor or '') or None
    job.tester_1 = c2.text_input('Tester #1', job.tester_1 or '') or None
    job.tester_2 = c3.text_input('Tester #2', job.tester_2 or '') or None
    job.revision = c4.text_input('Revision', job.revision or '1')

    c1, c2, c3, c4 = st.columns(4)
    # value=None, not today.  Defaulting it to today answers a question
    # nobody asked -- the box stops reading as empty, the completeness
    # list stops naming it, and a package ships claiming the OTDR was
    # calibrated the morning it was tested.
    _cal = c1.date_input('Test-equipment calibration',
                         value=job.calibration_date)
    job.calibration_date = _cal if isinstance(_cal, date) else None
    job.fiber_count = c2.number_input('Fibers tested', min_value=0, step=24,
                                      value=int(job.fiber_count or 0)) or None
    job.cable_manufacturer = c3.text_input('Cable manufacturer',
                                           job.cable_manufacturer or '') or None
    job.cable_type = c4.text_input('Cable type', job.cable_type or '') or None

    c1, c2, c3 = st.columns(3)
    job.cable_comp = c1.selectbox('Cable composition', ['RIBBON', 'Loose Tube'],
                                  index=0 if job.cable_comp == 'RIBBON' else 1)
    job.fiber_type = c2.selectbox(
        'Fiber type', ['SMF 28', 'AllWave', 'Ultra', 'Ultra LL', 'LEAF',
                       'TrueWave', 'LS', 'MetroCor', 'Other'],
        index=0 if job.fiber_type == 'SMF 28' else 8)
    job.direction = c3.selectbox(
        'Direction', ['NA - Single Span', 'Clockwise (Working)',
                      'Clockwise (Protect)', 'Counter-Clockwise (Working)',
                      'Counter-Clockwise (Protect)'])

    st.session_state['fqa_job'] = json.loads(job.to_json())

    st.markdown('##### Measured distances')
    st.caption('One distance from Site A per splice location, in span order — '
               f'{len(prod.splices)} of them. Metres, or km if you paste km. '
               'Leave it empty to fall back on the production sheet’s own '
               'footage marks.')
    c1, c2 = st.columns([3, 1])
    closure_text = c1.text_area(
        'Closure distances', height=90, label_visibility='collapsed',
        placeholder='60, 5010, 7650, 13540, …')
    span_len = c2.number_input('Span length (m)', min_value=0, step=10, value=0)

    st.markdown('##### Exception reporting')
    st.caption('One per line: fiber, event #, description. The event number is '
               'optional.')
    exc_text = st.text_area(
        'Exceptions', height=90, label_visibility='collapsed',
        placeholder='195, 6, .162 REBURNED 3 TIMES\n239, , 23.65KM -70 ref')

    # The template box lives below this section, so read its revision
    # from session state; on the first run the box does not exist yet and
    # the shipped template is what will be used.
    _tpl = st.session_state.get('fqa_template') or DEFAULT_TEMPLATE
    try:
        _template_revision = form_version(_tpl) if os.path.exists(_tpl) else None
    except Exception:                                    # noqa: BLE001
        _template_revision = None

    st.markdown('##### Not complete')
    st.caption('Everything this package is still missing, from the job form, '
               'the production sheet and the measurements. It updates as you '
               'fill the boxes above.')
    try:
        _closures_now = _parse_closures(closure_text)
    except ValueError:
        _closures_now = []
    _chain_now = build_chain(
        prod,
        trace_distances_m=(_closures_now
                           if len(_closures_now) == len(prod.splices) else None),
        span_length_m=float(span_len) or None,
        site_a_text=job.site_a.site_text or None,
        site_z_text=job.site_z.site_text or None)
    _fat_now = build_fat(job.fiber_count, prod.site_a, prod.site_z)
    _gaps = audit(prod, job, _chain_now, _fat_now,
                  _parse_exceptions(exc_text), form_revision=_template_revision)
    _not_complete_section(_gaps)

    with st.expander('Template and tolerances', expanded=False):
        st.session_state.setdefault('fqa_template', DEFAULT_TEMPLATE)
        # key= without value=: a widget owning a session-state slot must
        # not also be handed a value, or Streamlit ignores one of them.
        template = st.text_input('FQA form template', key='fqa_template',
                                 help='A blank Lumen Site Survey form. Replace '
                                      'this when Lumen publishes a new revision.')
        c1, c2 = st.columns(2)
        entry_offset = c1.number_input(
            'Frame to entry splice (m)', min_value=0, step=10,
            value=DEFAULT_ENTRY_OFFSET_M,
            help='The short run from the ILA frame out to the entry splice, at '
                 'each end. The production sheet does not measure it.')
        tolerance = c2.number_input(
            'Footage-mark tolerance (m)', min_value=0, step=5,
            value=DEFAULT_TOLERANCE_M,
            help='How far the footage marks may sit from the measured distance '
                 'before the build calls it out.')

    # The hub hands us its own 'Save reports to' row so this page offers
    # the same native folder picker as Splice Report; standalone there is
    # no picker to borrow, so a path box stands in.
    fallback = default_out_dir or _downloads()
    if dest_row is not None:
        out_dir = dest_row('fqa_dest', fallback)
    else:
        out_dir = st.text_input('Save to', fallback) or fallback
    default_name = os.path.splitext(os.path.basename(prod_path))[0]
    default_name = default_name.replace('Production Sheet', '').strip(' -_')
    out_name = st.text_input('File name',
                             f'{default_name} - FQA SITE SURVEY.xlsm')

    if st.button('Build the FQA package', type='primary'):
        try:
            closures = _parse_closures(closure_text)
        except ValueError as exc:
            st.error(f'Closure distances: {exc}')
            return
        if closures and len(closures) != len(prod.splices):
            st.error(f'{len(closures)} distances for {len(prod.splices)} splice '
                     f'locations. They have to line up, or the wrong vault gets '
                     f'the wrong distance.')
            return

        out_path = os.path.join(out_dir, out_name)
        try:
            manifest = build(
                prod_path, out_path,
                template=template,
                job_data=st.session_state['fqa_job'],
                closures=closures or None,
                span_length_m=float(span_len) or None,
                exceptions=_parse_exceptions(exc_text),
                entry_offset_m=int(entry_offset),
                tolerance_m=int(tolerance),
            )
        except Exception as exc:                              # noqa: BLE001
            st.error(f'{type(exc).__name__}: {exc}')
            return

        st.success(f'Wrote {out_path}')
        c1, c2, c3, c4 = st.columns(4)
        c1.metric('Events', manifest['events'])
        c2.metric('Span length', f'{manifest["span_length_m"] or 0:,} m')
        c3.metric('FAT rows', manifest['fat_rows'])
        c4.metric('Distances from', manifest['distance_source'])

        if manifest['distance_source'] == 'footage':
            st.warning(
                'The Event Log distances came from the production sheet’s footage '
                'marks, not from a trace. Those are copied off a cable by hand — '
                'check them before this goes to the customer.')

        if manifest['missing_facts']:
            st.warning('Still blank on the form: '
                       + ', '.join(manifest['missing_facts']))

        for w in manifest['warnings']:
            st.warning(w)

        st.markdown('###### Event Log as written')
        st.dataframe(manifest['event_log'], use_container_width=True,
                     hide_index=True)

        with open(out_path, 'rb') as fh:
            st.download_button('Download the package', fh.read(), file_name=out_name,
                               mime='application/vnd.ms-excel.sheet.macroEnabled.12')
