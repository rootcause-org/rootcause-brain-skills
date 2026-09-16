#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic>=2", "psycopg[binary]>=3.2"]
# ///
"""Fill one open review item's evidence entry using collected originals from OUT."""
import argparse
import json
from pathlib import Path
from uuid import UUID

from psycopg.types.json import Jsonb
from queue_db import connect
from report_schema import EvidenceEntry, evidence_entry_errors


def update_evidence(conn, item_id, run_id, raw, evidence_path, *, force=False):
    item_id, run_id = str(UUID(str(item_id))), str(UUID(str(run_id)))
    entry = EvidenceEntry.model_validate(dict(raw, run_id=raw.get('run_id', run_id)))
    if entry.run_id.lower() != run_id:
        raise ValueError('--run and entry.run_id differ')
    evidence_path = Path(evidence_path)
    errors = evidence_entry_errors(entry, json.loads(evidence_path.read_text()), evidence_path.parent)
    if errors:
        raise ValueError('; '.join(errors))
    patch = entry.model_dump(exclude_none=True, exclude={'run_id', 'label'})
    if not patch:
        raise ValueError('Supply at least one excerpt or manual feedback')
    row = conn.execute("SELECT evidence,status FROM review_items WHERE id=%s FOR UPDATE", (item_id,)).fetchone()
    if not row or row['status'] != 'open':
        raise ValueError('Item is missing or no longer open')
    entries = row['evidence']
    matches = [e for e in entries if e.get('run_id', '').lower() == run_id]
    if len(matches) != 1:
        raise ValueError('Expected exactly one matching evidence entry')
    target = matches[0]
    for key, value in patch.items():
        if not target.get(key) or force:
            target[key] = value
    conn.execute('UPDATE review_items SET evidence=%s WHERE id=%s', (Jsonb(entries), item_id))
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--item', required=True, type=UUID)
    parser.add_argument('--run', required=True, type=UUID)
    parser.add_argument('--json', required=True, dest='entry', type=json.loads)
    parser.add_argument('--evidence', type=Path, default=Path('evidence.json'))
    parser.add_argument('--dsn')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    with connect(args.dsn, write=True) as conn:
        result = update_evidence(conn, args.item, args.run, args.entry, args.evidence, force=args.force)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
