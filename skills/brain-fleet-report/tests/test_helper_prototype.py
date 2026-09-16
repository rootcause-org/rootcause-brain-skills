import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import helper_prototype as helper
from prototype_format import reduced, prototype_markdown
from publish import payload
from report_schema import Prototype


def sample():
    return Prototype(repo='/tmp/repo', branch='review/test-helper', files=1, command='python skills/helper.py',
                     before=dict(stdout='old', stderr='failure', exit_code=2, sha='a'*40, run_id='before'),
                     after=dict(stdout='new', stderr='', exit_code=0, sha='b'*40, run_id='after'))


def test_publish_prototype_only_on_technical_row():
    f = SimpleNamespace(prototype=sample(), prompt=None, title='Test', title_nl='Test', kind='script_error',
                        severity='medium', root_cause=None, scope=SimpleNamespace(axis_value=lambda: ''),
                        text_en='Problem', text_nl='Owner task', ask_nl='Check?', ask_for=None,
                        evidence=SimpleNamespace(entries=[]), options=[])
    tech = payload(f, 'technical', [])
    assert tech['options'][0]['label'] == 'Merge branch'
    assert '## Before' in tech['body'] and '## After' in tech['body']
    assert 'repo@review/test-helper (1 files)' in tech['body']
    owner = payload(f, 'owner', [])
    assert owner['body'] == 'Owner task' and not owner['options']


def test_capture_protocol_separates_helper_failure_from_transport(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=json.dumps(
        dict(exit_code=2, stdout='old', stderr='error', run_id='run')), stderr='', returncode=4))
    assert helper.run_version({}, ['python', 'missing.py'], [], 'a'*40)['exit_code'] == 2
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='{"error":"denied"}', stderr='', returncode=1))
    with pytest.raises(ValueError, match='No complete'):
        helper.run_version({}, ['python', 'missing.py'], [], 'a'*40)


def test_staging_executes_exact_bytes_and_literal_arguments():
    import base64
    files = {'ARGV-DATA.bin': 'ARGV', 'skills/helper.py': base64.b64encode(b'import sys; print(sys.argv[1])').decode()}
    command = helper.console_command(files, ['python', 'skills/helper.py', '$(touch /tmp/not-executed)'])
    done = subprocess.run(['sh', '-c', command], capture_output=True, text=True)
    assert done.returncode == 0 and done.stdout.strip() == '$(touch /tmp/not-executed)'


def test_privacy_fences_and_failed_after():
    assert 'person@example.com' not in reduced('contact person@example.com')
    assert 'secretvalue' not in reduced('token=secretvalue')
    p = sample()
    p.before.stdout = '```\n<script>alert(1)</script>\n```'
    assert '````text' in prototype_markdown(p)
    raw = p.model_dump(); raw['after']['exit_code'] = 1
    with pytest.raises(ValueError, match='AFTER'):
        Prototype.model_validate(raw)
