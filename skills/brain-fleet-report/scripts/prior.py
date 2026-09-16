#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]>=3.2"]
# ///
"""Read queue history from Postgres (including decisions and retest triggers)."""
import argparse
import json
from pathlib import Path
import re

from queue_db import connect, project_ids

ARCHIVE = re.compile(r'#archived:[0-9a-f-]{36}$')


def read_prior(conn, projects):
    ids = project_ids(conn, projects)
    rows = conn.execute('''SELECT i.*,p.name AS project,s.audience,s.tenant_id,t.slug AS tenant FROM review_items i
      JOIN review_sessions s ON s.id=i.session_id JOIN projects p ON p.id=s.project_id
      LEFT JOIN tenants t ON t.id=s.tenant_id
      WHERE s.project_id=ANY(%s::uuid[]) AND s.cadence='queue'
        AND i.signature IS NOT NULL
      ORDER BY i.last_seen NULLS FIRST,i.position''', (list(ids.values()),)).fetchall()
    result = {}
    for row in rows:
        signature = ARCHIVE.sub('', row['signature'])
        entry = result.setdefault(signature, {'finding': {}, 'rows': []})
        entry['rows'].append(row)
        # Archived generations remain in the table, but cannot override the active payload.
        if ARCHIVE.search(row['signature']):
            continue
        entry['date'] = str(row['last_seen'])
        entry['prompt_date'] = str(row['first_seen'])
        f = entry['finding']
        f.update(signature=signature, severity=row['severity'],
                 status=row['status'], root_cause={'plane': row['plane'] or 'unknown', 'confidence': 'low'},
                 impact={'runs': len(row['evidence']), 'threads': 0})
        if row['audience'] == 'technical' or not f.get('title'):
            f['title'] = row['subject']
        if row['audience'] == 'owner':
            f.update(title_nl=row['subject'], text_nl=row['body'], ask_nl=row['ask'], ask_for=row['ask_for'], options=row['options'])
        else:
            f['text_en'] = row['body']
        prompt = {k: v for k, v in row['prompt'].items() if k != 'text'}
        if prompt:
            f['prompt'] = prompt
        audiences = {r['audience'] for r in entry['rows'] if not ARCHIVE.search(r['signature'])}
        f['audience'] = 'both' if len(audiences) == 2 else row['audience']
    return result


def prior_findings(report_json_path=None, *, projects=None, dsn=None, **_):
    if projects is None:
        path = Path(report_json_path)
        path = path if path.suffix == '.json' else path / 'manifest.json'
        data = json.loads(path.read_text())
        projects = data.get('projects') or data['coverage']['projects']
    with connect(dsn) as conn:
        return read_prior(conn, projects)


def prior_table(findings):
    lines = ['## Prior findings (Postgres; reuse signatures)', '',
             'signature | project | audience | status | decision_label | instruction / retest trigger | first_seen | last_seen | applied receipt | title',
             '--- | --- | --- | --- | --- | --- | --- | --- | --- | ---']
    for signature, entry in findings.items():
        for row in entry['rows']:
            values = [signature, row['project'], row['audience'], row['status'], row['decision_label'],
                      row['decision_instruction'], row['first_seen'], row['last_seen'],
                      json.dumps(row['applied'], ensure_ascii=False, default=str), row['subject']]
            lines.append(' | '.join(str(v or '').replace('|', '\\|').replace('\n', ' ') for v in values))
    if not findings:
        lines.append('No prior queue items.')
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('out_dir', type=Path, nargs='?')
    parser.add_argument('--project', action='append')
    parser.add_argument('--dsn')
    args = parser.parse_args()
    if not args.out_dir and not args.project:
        parser.error('provide OUT or --project')
    print(prior_table(prior_findings(args.out_dir, projects=args.project, dsn=args.dsn)))
