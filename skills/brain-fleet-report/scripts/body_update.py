#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic>=2", "psycopg[binary]>=3.2"]
# ///
"""Attach a verified prototype to one open technical card; preview by default."""
import argparse
import json
from pathlib import Path
from uuid import UUID
from psycopg.types.json import Jsonb
from prototype_format import prototype_markdown, merge_option, MARKER
from queue_db import connect
from report_schema import Prototype


def update_body(conn, item, project, prototype, *, write=False):
    row = conn.execute('''SELECT i.body,i.options,i.status,s.audience,p.name
        FROM review_items i JOIN review_sessions s ON s.id=i.session_id
        JOIN projects p ON p.id=s.project_id WHERE i.id=%s FOR UPDATE OF i''', (str(item),)).fetchone()
    if not row or row['status'] != 'open' or row['audience'] != 'technical' or row['name'] != project:
        raise ValueError('Expected an open technical card in the named project')
    body = row['body'].split(MARKER)[0] + MARKER + prototype_markdown(prototype)
    options = [merge_option(prototype)] + [o for o in row['options'] if o['label'] != 'Merge branch']
    if write:
        conn.execute('UPDATE review_items SET body=%s,options=%s WHERE id=%s', (body, Jsonb(options), str(item)))
    return {'body': body, 'options': options}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--item', type=UUID, required=True)
    p.add_argument('--project', required=True)
    p.add_argument('--prototype', type=Path, required=True)
    p.add_argument('--write', action='store_true')
    p.add_argument('--dsn')
    args = p.parse_args()
    prototype = Prototype.model_validate_json(args.prototype.read_text())
    # SELECT FOR UPDATE needs a writable transaction; preview always rolls back.
    with connect(args.dsn, write=True) as conn:
        print(json.dumps(update_body(conn, args.item, args.project, prototype, write=args.write), indent=2))
        if not args.write:
            conn.rollback()


if __name__ == '__main__':
    main()
