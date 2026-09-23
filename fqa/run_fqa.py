"""
FQA Builder engine
==================

Builds a Lumen FQA Site Survey package from a ZeroDB production sheet.

Contract, matching the rest of the suite: exactly ONE JSON manifest line
on stdout, everything else on stderr, so a caller can run this in a
subprocess and parse the last line.

    python -m fqa.run_fqa \\
        --production "Span 4 ... Production Sheet final.xlsx" \\
        --job job.json \\
        --closures closures.json \\
        --out "Span 4 ... FQA SITE SURVEY.xlsm"

`--closures` is the measured distance from Site A, in metres, of each
splice location in span order -- what the traces say.  Without it the
Event Log falls back to the production sheet's own footage marks and the
manifest says so, loudly: those marks are hand-copied in the field and
Span 4's are wrong at four of eleven segments.

Robert, 2026-09-23.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from dataclasses import asdict
from datetime import date

from .event_chain import DEFAULT_ENTRY_OFFSET_M, DEFAULT_TOLERANCE_M, build_chain
from .fat import build_fat
from .job_facts import JobFacts, derive
from .production_sheet import read_production_sheet
from .writer import Exception_, FqaBuild, write_fqa

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE = os.path.join(HERE, 'templates', 'FQA_Site_Survey_v1_1.xlsm')


def _load_json(path: str | None):
    if not path:
        return None
    with open(path, 'r', encoding='utf-8') as fh:
        return json.load(fh)


def _closure_list(data) -> list[float] | None:
    """Accept either a bare list of metres or the richer shape a splice
    report would hand over ({'closures': [{'km': 5.01}, ...]})."""
    if data is None:
        return None
    if isinstance(data, dict):
        data = data.get('closures') or data.get('distances_m') or []
    out: list[float] = []
    for item in data:
        if isinstance(item, dict):
            if 'm' in item:
                out.append(float(item['m']))
            elif 'km' in item:
                out.append(float(item['km']) * 1000.0)
            else:
                raise ValueError(f'closure entry has no m or km: {item!r}')
        else:
            out.append(float(item))
    return out


def build(production: str,
          out: str,
          *,
          template: str = DEFAULT_TEMPLATE,
          job_data: dict | None = None,
          closures: list[float] | None = None,
          span_length_m: float | None = None,
          exceptions: list[dict] | None = None,
          entry_offset_m: int = DEFAULT_ENTRY_OFFSET_M,
          tolerance_m: int = DEFAULT_TOLERANCE_M) -> dict:
    """Build one FQA package.  Returns the manifest dict."""
    prod = read_production_sheet(production)
    job = derive(prod, JobFacts.from_dict(job_data or {}))

    chain = build_chain(
        prod,
        trace_distances_m=closures,
        span_length_m=span_length_m,
        entry_offset_m=entry_offset_m,
        site_a_text=job.site_a.site_text or None,
        site_z_text=job.site_z.site_text or None,
        tolerance_m=tolerance_m,
    )

    exc = [Exception_(fiber=e.get('fiber'), event=e.get('event'),
                      description=e.get('description', ''))
           for e in (exceptions or [])]

    package = FqaBuild(
        job=job,
        chain=chain,
        fat_rows=build_fat(job.fiber_count, prod.site_a, prod.site_z),
        exceptions=exc,
    )
    sheets = write_fqa(template, out, package)

    return {
        'ok': True,
        'out': os.path.abspath(out),
        'template': os.path.abspath(template),
        'sheets': sheets,
        'locations': len(prod.locations),
        'events': chain.event_count,
        'span_length_m': chain.span_length_m,
        'fiber_count': job.fiber_count,
        'fat_rows': len(package.fat_rows),
        'exceptions': len(exc),
        'distance_source': chain.distance_source,
        'missing_facts': job.missing(),
        'warnings': list(prod.warnings) + list(chain.warnings),
        'job': json.loads(job.to_json()),
        'event_log': [
            {'event': str(e.number), 'vault': e.vault_id,
             'location': e.location_text, 'sheet': e.location.sheet,
             'from_a_m': e.dist_from_a_m, 'from_z_m': e.dist_from_z_m,
             'to_next_m': e.dist_to_next_m,
             'footage_from_a_m': e.footage_from_a_m, 'delta_m': e.delta_m}
            for e in chain.events
        ],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Build a Lumen FQA package.')
    ap.add_argument('--production', required=True,
                    help='the span’s ZeroDB production sheet (.xlsx)')
    ap.add_argument('--out', required=True, help='where to write the .xlsm')
    ap.add_argument('--template', default=DEFAULT_TEMPLATE)
    ap.add_argument('--job', help='JSON of the job facts (see JobFacts)')
    ap.add_argument('--closures',
                    help='JSON list of measured metres from Site A, one per '
                         'splice location in span order')
    ap.add_argument('--exceptions', help='JSON list of exception rows')
    ap.add_argument('--span-length', type=float,
                    help='measured span length in metres')
    ap.add_argument('--entry-offset', type=int, default=DEFAULT_ENTRY_OFFSET_M)
    ap.add_argument('--tolerance', type=int, default=DEFAULT_TOLERANCE_M)
    args = ap.parse_args(argv)

    # Engine chatter must not land on stdout: the manifest is the only
    # thing there.  Same discipline as run_splicereport.
    real_stdout = sys.stdout
    sys.stdout = io.StringIO()

    def emit(payload):
        sys.stdout = real_stdout
        real_stdout.write(json.dumps(payload, default=_json_default) + '\n')
        real_stdout.flush()

    try:
        manifest = build(
            args.production, args.out,
            template=args.template,
            job_data=_load_json(args.job),
            closures=_closure_list(_load_json(args.closures)),
            span_length_m=args.span_length,
            exceptions=_load_json(args.exceptions),
            entry_offset_m=args.entry_offset,
            tolerance_m=args.tolerance,
        )
    except Exception as exc:                     # noqa: BLE001 - reported
        emit({'ok': False, 'error': f'{type(exc).__name__}: {exc}'})
        return 1
    emit(manifest)
    return 0


def _json_default(o):
    if isinstance(o, date):
        return o.isoformat()
    raise TypeError(type(o))


if __name__ == '__main__':
    raise SystemExit(main())
