# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Read-only review evidence; run from a brain checkout. No inference or brain writes."""
import argparse
from datetime import date, datetime, timedelta, timezone
import math
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'brain-fleet-report/scripts'))
from fr_common import Rc, find_brain_root, load_overlay, project_name, parallel, day_bounds, tzinfo
from drill import fetch_run


def collect(rc, base, days, only_feedback, now, start=None, end=None):
    since = start or now - timedelta(days=days)
    until = end or now
    lookback = max(days, math.ceil((now-since).total_seconds()/86400))
    evidence = rc.json(*base, 'dev', 'learning', 'evidence', '--days', str(lookback),
                       '--limit', '100', '--include-bodies')
    if evidence is None:
        raise RuntimeError('Learning evidence unavailable; cannot claim an empty review')
    warnings = []
    def in_window(row, fields):
        dates = [datetime.fromisoformat(row[k].replace('Z', '+00:00')) for k in fields if row.get(k)]
        return bool(dates) and since <= max(dates) < until
    feedback = {r['run_id']: r for r in evidence.get('feedback', [])
                if in_window(r, ('score_set_at', 'comment_set_at'))}
    eligible = set(feedback)
    # The lesson feed omits positive scores and held-out evaluations. Read reviewed runs too,
    # but never turn evidence excluded by the learning surface into training material.
    cursor = None
    seen = set()
    while True:
        page = rc.json(*base, 'run', 'list', '--reviewed', '--limit', '100',
                       *(['--before', cursor] if cursor else []))
        if page is None:
            warnings.append('Reviewed-run feed unavailable; positive scores may be missing.')
            break
        rows = page.get('runs', [])
        for run in rows:
            review = run.get('review') or run.get('feedback') or {}
            created = datetime.fromisoformat(run['created_at'].replace('Z', '+00:00'))
            rid = run['run_id']
            if rid not in feedback and since <= created < until and (review.get('score') is not None or review.get('comment')):
                feedback[rid] = {**review, 'run_id': rid, 'topic': run.get('topic', ''),
                                 'date_basis': 'run_created_at', 'created_at': run['created_at']}
        cursor = page.get('next_before')
        if not cursor or cursor in seen or (rows and all(datetime.fromisoformat(r['created_at'].replace('Z', '+00:00')) < since for r in rows)):
            break
        seen.add(cursor)
    deltas = [] if only_feedback else [d for d in evidence.get('deltas', []) if in_window(d, ('sent_at', 'received_at'))]
    for plane in ('feedback', 'deltas'):
        if len(evidence.get(plane, [])) >= 100:
            warnings.append(f'{plane}: server limit reached (100); coverage is partial.')
    if any(r.get('date_basis') for r in feedback.values()):
        warnings.append('Additional scored examples use the conversation date; their feedback date is not exposed.')
    by_run = {}
    for row in deltas:
        if row.get('related_run_id'):
            by_run.setdefault(row['related_run_id'], []).append(row)
    ids = set(feedback) | (set() if only_feedback else set(by_run))
    def rank(rid):
        return (rid not in feedback, min((d.get('similarity', 1) for d in by_run.get(rid, [])), default=1), rid)
    def fetch(rid):
        return {'run_id': rid, 'url': f'https://app.replypen.com/runs/{rid}',
                'feedback': feedback.get(rid), 'deltas': by_run.get(rid, []),
                'learning_allowed': rid in eligible or (rid not in feedback and bool(by_run.get(rid))),
                'detail': fetch_run(rc, base, rid)}
    items = parallel(sorted(ids, key=rank), fetch)
    warnings += [f"Read incomplete: {r.get('feed', 'run')}" for r in rc.errors]
    scores = [f['score'] for f in feedback.values() if f.get('score') is not None]
    return {'items': items, 'warnings': warnings, 'metrics': {'feedback': len(feedback),
            'scored': len(scores), 'score_sum': sum(scores), 'score_distribution': {str(n): scores.count(n) for n in range(1,6)},
            'deltas': 0 if only_feedback else len(deltas)}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--days', type=int, default=7)
    p.add_argument('--date', help='last included calendar day, project timezone; default rolling window')
    p.add_argument('--project')
    p.add_argument('--tenant')
    p.add_argument('--only-feedback', action='store_true')
    p.add_argument('--out-dir')
    p.add_argument('--refresh', action='store_true')
    a = p.parse_args()
    if a.days < 1:
        p.error('--days must be positive')
    root = find_brain_root()
    overlay = load_overlay(root)
    project = a.project or project_name(root)
    now = datetime.now(timezone.utc)
    tz = tzinfo(overlay.get('timezone', 'Europe/Brussels'))
    last = date.fromisoformat(a.date) if a.date else None
    start = day_bounds(last-timedelta(days=a.days-1), tz)[0] if last else now-timedelta(days=a.days)
    end = day_bounds(last, tz)[1] if last else now
    out = Path(a.out_dir) if a.out_dir else root / '.rootcause/feedback-review' / f'{last or now.date()}-{a.days}d'
    out.mkdir(parents=True, exist_ok=True)
    rc = Rc(raw_dir=out / 'raw', cwd=root, refresh=a.refresh)
    rc.raw_dir.mkdir(exist_ok=True)
    base = ['--project', project] + (['--tenant', a.tenant] if a.tenant else ['--scope', 'project'])
    data = collect(rc, base, a.days, a.only_feedback, now, start, end)
    data.update(project=project, tenant=a.tenant or '', days=a.days, only_feedback=a.only_feedback,
                generated_at=now.isoformat(), since=start.isoformat(), until=end.isoformat(), window_basis='calendar' if last else 'rolling',
                lang=overlay.get('owner', {}).get('lang', 'nl'), owner=overlay.get('owner', {}).get('name', ''))
    (out / 'evidence.json').write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
    print(f"{len(data['items'])} examples; {len(data['warnings'])} coverage notes\n{out.resolve()}")

if __name__ == '__main__':
    main()
