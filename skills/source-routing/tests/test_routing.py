import sys
from pathlib import Path
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from digest import reduce_trace, cluster, paths, collect
from argparse import Namespace
from unittest.mock import patch
from datetime import datetime, timezone
import json
from common import table
from ingest import parse, merge, HUMAN_START, HUMAN_END
from propose import inventory, propose


class RoutingTests(unittest.TestCase):
    def test_tenant_rows_need_no_tenant_column_and_retries_collapse(self):
        now = datetime.now(timezone.utc).isoformat()
        rows = {'runs': [{'run_id': rid, 'kind': 'email', 'created_at': now, 'local_thread_id': 'thread', 'turn_key': key}
                         for rid, key in [('r1', now), ('r2', 'retry:uuid')]]}
        def fake_rc(argv):
            self.assertIn('tenant-a', argv)
            if argv[0] == 'fleet':
                return json.dumps(rows)
            return json.dumps({'type': 'run', 'run_id': argv[2], 'project': 'p', 'tenant': 'tenant-a', 'question': 'Where is my parcel?'})
        with tempfile.TemporaryDirectory() as tmp, patch('digest.rc', side_effect=fake_rc):
            args = Namespace(project='p', tenant='tenant-a', days=60, kind=['email'], runs=None, cache=tmp, refresh=False)
            result = collect(args)
        self.assertEqual(result['coverage']['runs_in_window'], 2)
        self.assertEqual(result['coverage']['trace_errors'], 0)
        self.assertEqual(result['coverage']['question_count'], 1)
        self.assertEqual(set(result['questions'][0]['run_ids']), {'r1', 'r2'})

    def test_shell_path_forms(self):
        for command, expected in [("cat '/kb/Price list.md'", '/kb/Price list.md'),
                                  ('cat /kb/a.md|head', '/kb/a.md'),
                                  ('cat /kb/a.md>/tmp/out', '/kb/a.md')]:
            self.assertEqual(paths(command), [expected])

    def test_trace_does_not_mine_system_prompt_or_failed_reads(self):
        result = reduce_trace([
            {'type': 'run', 'run_id': 'r', 'question': 'Help?', 'system_prompt': '/kb/unused.md'},
            {'type': 'event', 'tool': 'bash', 'command': 'cat /kb/a.md', 'exit_code': 1},
            {'type': 'event', 'tool': 'grounding', 'args': {'selected': [{'path': '/kb/a b.md'}]}},
            {'type': 'event', 'tool': 'reply', 'args': {'journal': {'sources': ['/kb/c.md']}}},
        ])
        self.assertEqual({(e['path'], e['method']) for e in result['evidence']}, {
            ('/kb/a.md', 'failed_read'), ('/kb/a b.md', 'grounding_selected'), ('/kb/c.md', 'journal_reference')})

    def test_review_roundtrip_preserves_human_override_and_is_idempotent(self):
        row = {'topic': 'A | B', 'title': 'A [guide]', 'url': 'https://example.org/a(b)', 'path': '/kb/a.md', 'why': 'matched', 'verdict': 'outdated'}
        parsed = parse(table([row]))
        self.assertEqual(parsed[0]['topic'], row['topic'])
        human = {**row, 'verdict': 'use'}
        old = '# Human introduction\n' + HUMAN_START + '\n' + table([human]) + HUMAN_END
        result = merge(old, parsed)
        self.assertTrue(result.startswith(old))
        self.assertIn('prefer: /kb/a.md; avoid: —', result)
        self.assertEqual(merge(result, parsed), result)

    def test_invalid_review_refused(self):
        with self.assertRaises(ValueError):
            parse('| Topic | Document | Path | Why | Verdict |\n| a | b | /kb/a.md | why | yes |')
        with self.assertRaises(ValueError):
            merge('<!-- source-routing:generated:start -->', [])

    def test_bounded_provider_neutral_inventory_and_old_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / '2023').mkdir()
            (p / '2023/price.md').write_text('---\ntitle: Price list\nurl: https://example.org/prices\nsource_version: 12\n---\nPrice')
            (p / 'current.md').write_text('---\ntitle: New price list\nupdated_at: 2026-09-01\n---\nPrice')
            (p / 'escaped.md').symlink_to('/etc/hosts')
            docs = inventory(p, '/kb/future-provider', 0, ['*.md'], [])
            self.assertEqual(len(docs), 2)
            digest = {'project': 'p', 'topics': [{'topic': 'Price', 'terms': ['price'], 'run_ids': ['r']}]}
            trace = {'project': 'p', 'sources': []}
            rows = propose(digest, trace, docs, 5, 0, 2026)
            self.assertEqual({r['path']: r['verdict'] for r in rows}, {'/kb/future-provider/2023/price.md': 'outdated', '/kb/future-provider/current.md': '?'})

    def test_unmatched_topics_and_counts_survive(self):
        d = {'questions': [{'subject': 'Delivery', 'text': 'Parcel delivery', 'run_ids': ['r']}, {'subject': 'Unrelated', 'text': 'Something else', 'run_ids': ['s']}]}
        cluster(d, {'Shipping': ['delivery']})
        self.assertEqual(sum(t['count'] for t in d['topics']), 2)
        self.assertEqual(len(d['topics']), 2)


if __name__ == '__main__':
    unittest.main()
