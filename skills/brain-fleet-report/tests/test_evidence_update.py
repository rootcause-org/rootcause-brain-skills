import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from evidence_update import update_evidence

RUN = '11111111-1111-1111-1111-111111111111'
ITEM = '22222222-2222-2222-2222-222222222222'


class Connection:
    def __init__(self):
        self.row = {'status': 'open', 'evidence': [{'run_id': RUN, 'label': 'Keep', 'sent': 'Existing'}]}
        self.writes = []

    def execute(self, sql, args):
        if sql.startswith('UPDATE'):
            self.writes.append(args)
        return self

    def fetchone(self):
        return self.row


def test_fill_preserves_identity_and_existing_excerpt(tmp_path):
    path = tmp_path / 'evidence.json'
    path.write_text(json.dumps({'runs': [{'run_id': RUN, 'question': 'Beste, wat kost dit? Groeten.'}],
                                'deltas': [{'related_run_id': RUN, 'sent_body': 'Nieuw antwoord.'}]}))
    conn = Connection()
    entry = update_evidence(conn, ITEM, RUN, {'label': 'Replace', 'question': 'wat kost dit?', 'sent': 'Nieuw antwoord.'}, path)
    assert entry == {'run_id': RUN, 'label': 'Keep', 'question': 'wat kost dit?', 'sent': 'Existing'}
    assert len(conn.writes) == 1
    assert update_evidence(conn, ITEM, RUN, {'sent': 'Nieuw antwoord.'}, path, force=True)['sent'] == 'Nieuw antwoord.'


def test_non_substring_rejected_before_write(tmp_path):
    path = tmp_path / 'evidence.json'
    path.write_text(json.dumps({'runs': [{'run_id': RUN, 'question': 'Wat kost dit?'}]}))
    conn = Connection()
    with pytest.raises(ValueError, match='does not match'):
        update_evidence(conn, ITEM, RUN, {'question': 'Wanneer is dit?'}, path)
    assert conn.writes == []
