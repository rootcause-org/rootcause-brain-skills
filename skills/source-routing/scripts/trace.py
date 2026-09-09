# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Aggregate compact trace evidence from digest JSON; never infer usage from answer similarity."""
import argparse
from collections import defaultdict
from pathlib import Path
from common import read, write, cell


def aggregate(data):
    sources = defaultdict(lambda: defaultdict(set))
    for run in data['runs']:
        for e in run.get('evidence', []):
            sources[e['path']][e['method']].add(run['run_id'])
    return {'schema': 'source-routing/v1', 'project': data['project'], 'tenant': data.get('tenant'),
            'coverage': data['coverage'], 'runs_without_evidence': sum(not r.get('evidence') and not r.get('error') for r in data['runs']),
            'sources': [{'path': path, 'methods': {k: sorted(v) for k, v in methods.items()}}
                        for path, methods in sorted(sources.items())]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--digest', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--markdown', required=True)
    a = p.parse_args()
    data = aggregate(read(a.digest))
    write(a.out, data)
    lines = ['# Source usage evidence', '', 'Selection/read commands show grounding access, not that a claim was supported. Missing evidence is unknown, not unused.', '', '| Path | Evidence | Runs |', '|---|---|---|']
    for s in data['sources']:
        lines.append('| ' + cell(s['path']) + ' | ' + ', '.join(s['methods']) + ' | ' + str(len(set().union(*map(set, s['methods'].values())))) + ' |')
    Path(a.markdown).write_text('\n'.join(lines) + '\n')


if __name__ == '__main__':
    main()
