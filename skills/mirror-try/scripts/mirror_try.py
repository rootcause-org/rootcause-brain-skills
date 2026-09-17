#!/usr/bin/env python3
"""Compare a read-only mirror helper at two revisions in the scoped production console."""
import argparse
import ast
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import zlib

# All kit skills ship together; retain the fleet's established privacy reducer.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'brain-fleet-report/scripts'))
from prototype_format import reduced


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def snapshot(repo, sha, paths):
    entries = tree(repo, sha)
    selected = set()
    for path in paths:
        safe_path(path)
        selected.update(name for name in entries if name == path or name.startswith(path.rstrip('/') + '/'))
    return stage(Path(repo), sha, selected)


def console_command(files, argv, repo_name="helper"):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", repo_name):
        raise ValueError("Invalid scratch repository name")
    # No shell interpolation of filenames, source, or helper arguments. Scratch is removed on exit.
    source = 'import base64,json,os,pathlib,subprocess,tempfile,zlib\n'
    for name in files:
        safe_path(name)
    payload = base64.b64encode(zlib.compress(json.dumps(files).encode())).decode()
    source += 'files = json.loads(zlib.decompress(base64.b64decode(' + repr(payload) + ')))\n'
    source += 'argv = ' + repr(argv) + '\n'
    source += 'scratch = ' + repr('/tmp/try/' + repo_name) + '\n'
    source += '''pathlib.Path(scratch).mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix="revision-", dir=scratch) as root:
 for name,data in files.items():
  path=pathlib.Path(root)/name
  path.parent.mkdir(parents=True,exist_ok=True)
  path.write_bytes(base64.b64decode(data))
  path.chmod(0o700)
 env=dict(os.environ)
 env["PYTHONPATH"]=root+os.pathsep+env.get("PYTHONPATH", "")
 result=subprocess.run(argv,cwd=root,env=env)
 raise SystemExit(result.returncode)
'''
    if len(source.encode()) > 120_000:
        raise ValueError('Command exceeds 120 KB shell argument budget; narrow --path/args')
    return 'python - <<\'REVIEW_HELPER\'\n' + source + '\nREVIEW_HELPER'


def run_version(files, argv, scope, sha, repo_name="helper"):
    cmd = ['rc', 'dev', 'console', 'bash', 'run', *scope, '--raw-output', '-o', 'json',
           '--timeout', '120', '--', '-']
    done = subprocess.run(cmd, input=console_command(files, argv, repo_name), text=True,
                          capture_output=True, timeout=180)
    try:
        result = json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError('Console transport failed: ' + reduced(done.stderr)) from exc
    if 'exit_code' not in result or not result.get('run_id') or result.get('timed_out') or result.get('stdout_truncated') or result.get('stderr_truncated'):
        raise ValueError('No complete helper result: ' + reduced(json.dumps(result)))
    return dict(stdout=reduced(result.get('stdout', '')), stderr=reduced(result.get('stderr', '')),
                exit_code=result['exit_code'], sha=sha, run_id=result['run_id'], seq=result.get('seq', 0))


def safe_path(name):
    path = PurePosixPath(name)
    if (path.is_absolute() or not path.parts or '..' in path.parts
            or any(p.startswith('.') for p in path.parts) or 'actions' in path.parts):
        raise ValueError('Stage relative grounding paths only; no hidden files or actions: ' + name)
    return path


def tree(repo, ref):
    entries = {}
    for entry in subprocess.check_output(['git', '-C', str(repo), 'ls-tree', '-rz', ref]).split(b'\0'):
        if entry:
            meta, name = entry.split(b'\t', 1)
            entries[name.decode()] = meta.split()[0].decode()
    return entries


def stage(repo, ref, seeds, *, working=False):
    """Bounded closure of static sibling/root Python imports, plus explicit data paths."""
    entries = tree(repo, ref)
    if working:
        for name in git(repo, 'ls-files').splitlines():
            entries.setdefault(name, '100644')
    files, pending = {}, list(seeds)
    while pending:
        name = pending.pop()
        path = safe_path(name)
        if name in files or name not in entries:
            continue  # deletions/new helper absent in the baseline are meaningful evidence
        if entries[name] not in ('100644', '100755'):
            raise ValueError('Symlinks/submodules are not supported: ' + name)
        if working:
            local = repo / name
            if local.is_symlink() or any(p.is_symlink() for p in local.parents if p != repo.parent):
                raise ValueError('Symlinks are not supported: ' + name)
            if not local.exists():
                continue
            data = local.read_bytes()
        else:
            data = subprocess.check_output(['git', '-C', str(repo), 'show', f'{ref}:{name}'])
        files[name] = base64.b64encode(data).decode()
        if len(json.dumps(files).encode()) > 240_000:
            raise ValueError('Selected files exceed 240 KB console budget; narrow the change')
        if path.suffix != '.py':
            continue
        for parent in path.parents:
            init = str(parent / '__init__.py')
            if init in entries and init != name:
                pending.append(init)
        try:
            parsed = ast.parse(data, filename=name)
        except SyntaxError:
            continue  # Let the actual interpreter report this revision's syntax failure.
        for node in ast.walk(parsed):
            if isinstance(node, ast.Import):
                modules = [(alias.name, 0) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [(node.module or '', node.level)]
                modules += [('.'.join(filter(None, [node.module, alias.name])), node.level)
                            for alias in node.names if alias.name != '*']
            else:
                continue
            for module, level in modules:
                parent = path.parent
                for _ in range(max(0, level - 1)):
                    parent = parent.parent
                roots = [parent] if level else [path.parent, PurePosixPath('.')]
                for base in roots:
                    target = base.joinpath(*module.split('.')) if module else base
                    candidates = [str(target) + '.py', str(target / '__init__.py')]
                    found = next((c for c in candidates if c in entries), None)
                    if found:
                        pending.append(found)
                        break
    return files


def compare(repo, ref, diff, base, argv, project, tenant=None, principal_kind=None,
            principal_id=None, paths=()):
    repo = repo.expanduser().resolve()
    if not argv or len(argv) < 2 or argv[0] not in ('python', 'python3') or not argv[1].endswith('.py'):
        raise ValueError('Command must be python relative/helper.py [args]; no shell or inline code')
    safe_path(argv[1])
    if bool(principal_kind) != bool(principal_id):
        raise ValueError('Supply both principal flags')
    base_sha = git(repo, 'rev-parse', '--verify', (base or ('HEAD' if diff else 'origin/main')) + '^{commit}')
    after_sha = git(repo, 'rev-parse', '--verify', (ref or 'HEAD') + '^{commit}')
    changed = git(repo, 'diff', '--name-only', '--no-renames', base_sha, *([] if diff else [after_sha])).splitlines()
    if not changed:
        raise ValueError('No changed files to compare')
    grounding_changes = [p for p in changed if not any(
        part.startswith('.') or part == 'actions' for part in PurePosixPath(p).parts)]
    seeds = {argv[1], *paths, *(p for p in grounding_changes if '/tests/' not in '/' + p and not p.startswith('tests/'))}
    before_files = stage(repo, base_sha, seeds)
    after_files = stage(repo, after_sha, seeds, working=diff)
    if argv[1] not in after_files:
        raise ValueError('Helper missing from staged tree')
    scope = ['--project', project, '--tenant', tenant] if tenant else ['--project', project, '--scope', 'project']
    if principal_kind:
        scope += ['--principal-kind', principal_kind, '--principal-id', principal_id]
    if diff:
        after_sha = 'worktree:' + hashlib.sha256(json.dumps(after_files, sort_keys=True).encode()).hexdigest()
    # Resolve and validate both payloads before either production call.
    name = re.sub('[^A-Za-z0-9_-]', '-', repo.name)
    console_command(before_files, argv, name)
    console_command(after_files, argv, name)
    return dict(repo=str(repo), base=base_sha, ref=after_sha, files=len(changed),
                staged_before=sorted(before_files), staged_after=sorted(after_files),
                command=reduced(shlex.join(argv)),
                before=run_version(before_files, argv, scope, base_sha, name),
                after=run_version(after_files, argv, scope, after_sha, name))


def side_by_side(result):
    columns = []
    for label in ('before', 'after'):
        value = result[label]
        columns.append([label.upper(), 'exit: ' + str(value['exit_code']), 'stdout:',
                        *value['stdout'].splitlines(), 'stderr:', *value['stderr'].splitlines()])
    width = max(len(line) for line in columns[0])
    return '\n'.join((columns[0][i] if i < len(columns[0]) else '').ljust(width) + ' | ' +
                     (columns[1][i] if i < len(columns[1]) else '')
                     for i in range(max(map(len, columns))))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', type=Path, required=True)
    choice = p.add_mutually_exclusive_group(required=True)
    choice.add_argument('--ref')
    choice.add_argument('--diff', action='store_true', help='Tracked working-tree changes against HEAD')
    p.add_argument('--base', help='Override base commit (default origin/main; HEAD with --diff)')
    p.add_argument('--project', required=True)
    p.add_argument('--tenant')
    p.add_argument('--principal-kind')
    p.add_argument('--principal-id', '--principal', dest='principal_id')
    p.add_argument('--path', action='append', default=[], help='Additional data/import file')
    p.add_argument('--cmd', required=True, help='Quoted python relative/helper.py [args]')
    p.add_argument('--json', action='store_true')
    p.add_argument('--read-only-reviewed', action='store_true', required=True,
                   help='Helper/imports inspected: no sends, actions, or customer writes')
    args = p.parse_args()
    result = compare(args.repo, args.ref, args.diff, args.base, shlex.split(args.cmd),
                     args.project, args.tenant, args.principal_kind, args.principal_id, args.path)
    print(json.dumps(result, indent=2) if args.json else side_by_side(result))
    return 0 if result['after']['exit_code'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
