import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from evidence_excerpts import evidence_excerpts
from report_schema import Evidence, Report, evidence_errors, load_report, soft_warnings
from publish import payload

RUN = '11111111-1111-1111-1111-111111111111'


@pytest.fixture
def corpus(tmp_path):
    data = {'runs': [{'run_id': RUN, 'question': 'Beste, wat kost **een consult**? Groeten.',
                     'draft_markdown': 'Zie [prijzen](https://example.com): €12.',
                     'review': {'score': 5}}],
            'deltas': [{'related_run_id': RUN, 'proposed_body': 'De prijs is €12.',
                        'sent_body_clean': 'De prijs is €15.'}],
            'feedback': [{'run_id': RUN, 'score': 2, 'comment': 'Verkeerde prijs'}]}
    (tmp_path / 'evidence.json').write_text(json.dumps(data))
    return data


def report(entry):
    data = json.loads((ROOT / 'fixtures/sample_report.json').read_text())
    data['findings'] = [data['findings'][0]]
    data['findings'][0]['evidence'] = {'entries': [entry]}
    return Report.model_validate(data)


def test_sources_validate_and_publish_contract(corpus, tmp_path):
    entry = evidence_excerpts(RUN, out_dir=tmp_path)
    assert entry['feedback'] == {'score': 2, 'comment': 'Verkeerde prijs'}
    entry.update(question='wat kost een consult?', proposed='Zie prijzen…€12.', sent='prijs is €15.')
    r = report(entry)
    assert evidence_errors(r, tmp_path / 'evidence.json') == []
    for audience in ('owner', 'technical'):
        assert payload(r.findings[0], audience, [RUN])['evidence'] == [entry]
    path = tmp_path / 'report.json'
    path.write_text(r.model_dump_json(exclude_unset=True))
    assert load_report(path, prior={}).findings[0].evidence.entries[0].question == entry['question']


@pytest.mark.parametrize('field,value', [('question', 'Unasked question'), ('proposed', '€99'),
                                        ('sent', '€12'), ('feedback', {'score': 5}),
                                        ('feedback', {'comment': 'Invented'})])
def test_invented_evidence_rejected(corpus, tmp_path, field, value):
    r = report({'run_id': RUN, field: value})
    assert any(field in e for e in evidence_errors(r, tmp_path / 'evidence.json'))
    path = tmp_path / 'report.json'
    path.write_text(r.model_dump_json(exclude_unset=True))
    with pytest.raises(ValueError, match=field):
        load_report(path, prior={})


def test_missing_sources_and_automatic_scores_stay_absent(corpus, tmp_path):
    corpus['deltas'] = []
    corpus['feedback'] = []
    entry = evidence_excerpts(RUN, evidence=corpus, out_dir=tmp_path)
    assert 'sent' not in entry and 'feedback' not in entry
    (tmp_path / 'evidence.json').write_text(json.dumps(corpus))
    assert evidence_errors(report({'run_id': RUN, 'feedback': {'score': 5}}), tmp_path / 'evidence.json')


def test_drill_originals_support_offline_validation(corpus, tmp_path):
    corpus['runs'][0].pop('question')
    (tmp_path / 'details').mkdir()
    (tmp_path / 'details' / f'evidence-{RUN}.json').write_text(json.dumps({'question': 'Een andere vraag?'}))
    assert evidence_excerpts(RUN, evidence=corpus, out_dir=tmp_path)['question'] == 'Een andere vraag?'


@pytest.mark.parametrize('label', ['Done in dashboard', 'done IN dashboard', 'Ja', 'Nee', 'Ok', 'Ja / Nee', 'Ok ok'])
def test_owner_labels_reject_generic_choices(label):
    r = report({'run_id': RUN}).model_dump()
    r['findings'][0]['options'] = [{'label': label, 'instruction': '…'}]
    with pytest.raises(ValueError, match='owner'):
        Report.model_validate(r)


def test_title_required_and_ask_warns():
    r = report({'run_id': RUN})
    data = r.model_dump()
    data['findings'][0].pop('title_nl')
    with pytest.raises(ValueError, match='title_nl'):
        Report.model_validate(data)
    r.findings[0].ask_nl = 'De prijzen ontbreken.'
    assert any('ask_nl' in w for w in soft_warnings(r))
    for ask in ('Wat kost een consult?', 'Vul de prijzen in: onderzoek, RX, poetsbeurt.'):
        r.findings[0].ask_nl = ask
        assert not any('ask_nl' in w for w in soft_warnings(r))
