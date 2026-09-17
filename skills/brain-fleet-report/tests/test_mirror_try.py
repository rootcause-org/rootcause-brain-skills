import base64
from pathlib import Path
import subprocess
import sys

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'mirror-try/scripts'))
import mirror_try as mirror


def commit(repo):
    mirror.git(repo, 'add', '.')
    mirror.git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.org', 'commit', '-qm', 'fixture')
    return mirror.git(repo, 'rev-parse', 'HEAD')


def test_revision_import_closure_and_working_diff(tmp_path):
    mirror.git(tmp_path, 'init', '-q')
    folder = tmp_path / 'helpers'
    folder.mkdir()
    (folder / 'main.py').write_text('from sibling import result\nprint(result)\n')
    (folder / 'sibling.py').write_text('from package import result\n')
    package = tmp_path / 'package'
    package.mkdir()
    (package / '__init__.py').write_text('from .value import result\n')
    (package / 'value.py').write_text('result = "before"\n')
    before = commit(tmp_path)
    (package / 'value.py').write_text('result = "after"\n')
    for working, expected in [(False, 'before'), (True, 'after')]:
        files = mirror.stage(tmp_path, before, ['helpers/main.py'], working=working)
        assert set(files) == {'helpers/main.py', 'helpers/sibling.py', 'package/__init__.py', 'package/value.py'}
        done = subprocess.run(['sh', '-c', mirror.console_command(files, ['python', 'helpers/main.py'])],
                              capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == expected
    (folder / 'sibling.py').unlink()
    (folder / 'sibling.py').symlink_to(package / 'value.py')
    with pytest.raises(ValueError, match='Symlinks'):
        mirror.stage(tmp_path, before, ['helpers/main.py'], working=True)


def test_compare_stages_changed_dependency_both_sides_before_call(tmp_path, monkeypatch):
    mirror.git(tmp_path, 'init', '-q')
    (tmp_path / 'helper.py').write_text('import dependency\nprint(dependency.value)\n')
    (tmp_path / 'dependency.py').write_text('value=1\n')
    before = commit(tmp_path)
    (tmp_path / 'dependency.py').write_text('value=2\n')
    after = commit(tmp_path)
    seen = []
    def run(files, argv, scope, sha, name):
        seen.append((base64.b64decode(files['dependency.py']), scope, sha))
        return {'exit_code': 0}
    monkeypatch.setattr(mirror, 'run_version', run)
    mirror.compare(tmp_path, after, False, before, ['python', 'helper.py'], 'project', 'tenant', 'parent', '42')
    assert [s[0] for s in seen] == [b'value=1\n', b'value=2\n']
    assert seen[0][1] == seen[1][1] == ['--project', 'project', '--tenant', 'tenant', '--principal-kind', 'parent', '--principal-id', '42']
    (tmp_path / 'actions').mkdir()
    (tmp_path / 'actions/write.py').write_text('print(1)')
    bad = commit(tmp_path)
    seen.clear()
    with pytest.raises(ValueError, match='actions'):
        mirror.compare(tmp_path, bad, False, before, ['python', 'helper.py'], 'project', paths=['actions/write.py'])
    assert not seen


def test_large_staging_is_compressed_and_paths_are_guarded():
    source = b'# padding\n' * 15000 + b'print("large payload ran")\n'
    files = {'helper.py': base64.b64encode(source).decode()}
    command = mirror.console_command(files, ['python', 'helper.py'])
    assert len(command.encode()) < 120_000
    result = subprocess.run(['sh', '-c', command], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == 'large payload ran'
    for path in ('../outside.py', '/absolute.py', '.env', 'nested/actions/write.py'):
        with pytest.raises(ValueError, match='grounding'):
            mirror.console_command({path: ''}, ['python', 'helper.py'])


def test_hidden_and_action_changes_are_omitted_and_broken_base_runs(tmp_path, monkeypatch):
    mirror.git(tmp_path, 'init', '-q')
    (tmp_path / 'helper.py').write_text('def (:')
    before = commit(tmp_path)
    (tmp_path / 'helper.py').write_text('print("fixed")\n')
    (tmp_path / '.gitignore').write_text('cache/\n')
    (tmp_path / 'actions').mkdir()
    (tmp_path / 'actions' / 'write.py').write_text('raise Exception("must never run")')
    after = commit(tmp_path)
    def run(files, argv, scope, sha, name):
        assert set(files) == {'helper.py'}
        done = subprocess.run(['sh', '-c', mirror.console_command(files, argv)], capture_output=True, text=True)
        return {'exit_code': done.returncode, 'stdout': done.stdout, 'stderr': done.stderr}
    monkeypatch.setattr(mirror, 'run_version', run)
    result = mirror.compare(tmp_path, after, False, before, ['python', 'helper.py'], 'project')
    assert result['before']['exit_code'] != 0 and 'SyntaxError' in result['before']['stderr']
    assert result['after']['exit_code'] == 0 and result['after']['stdout'] == 'fixed\n'
