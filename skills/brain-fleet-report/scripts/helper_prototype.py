#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic>=2"]
# ///
"""Prepare a review branch; compare committed helper bytes in the guarded production console."""
import argparse
import base64
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import tempfile

from prototype_format import prototype_markdown, reduced
from report_schema import Prototype


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def save(path, data):
    path.write_text(json.dumps(data, indent=2) + '\n')
    path.chmod(0o600)


def prepare(repo, slug, out):
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug):
        raise ValueError('Use a lowercase signature slug')
    repo = repo.expanduser().resolve()
    if out.exists():
        raise ValueError('State file already exists; resume it instead')
    git(repo, 'fetch', 'origin', 'main')
    base = git(repo, 'rev-parse', 'origin/main')
    branch = 'review/' + slug
    worktree = Path(tempfile.mkdtemp(prefix='helper-prototype-')) / 'worktree'
    git(repo, 'worktree', 'add', '-b', branch, str(worktree), base)
    state = dict(repo=str(repo), branch=branch, base=base, worktree=str(worktree))
    save(out, state)
    return state


def snapshot(repo, sha, paths):
    files = {}
    for path in paths:
        rel = PurePosixPath(path)
        if rel.is_absolute() or '..' in rel.parts or not rel.parts or rel.parts[0] == 'actions':
            raise ValueError('Stage relative grounding paths only, never actions')
        for line in git(repo, 'ls-tree', '-r', sha, '--', path).splitlines():
            mode, _, _, name = line.split(None, 3)
            if mode not in ('100644', '100755'):
                raise ValueError('Symlinks/submodules are not supported in staged paths')
            if any(part.startswith('.') for part in PurePosixPath(name).parts):
                raise ValueError('Do not stage hidden config/credentials')
            data = subprocess.check_output(['git', '-C', str(repo), 'show', f'{sha}:{name}'])
            files[name] = base64.b64encode(data).decode()
    if len(json.dumps(files).encode()) > 240_000:
        raise ValueError('Selected files exceed 240 KB console budget; narrow --path')
    return files


def console_command(files, argv):
    # No shell interpolation of filenames, source, or helper arguments. Scratch is removed on exit.
    source = 'import base64,json,os,pathlib,subprocess,tempfile\n'
    source += 'files = ' + repr(files) + '\nargv = ' + repr(argv) + '\n'
    source += '''with tempfile.TemporaryDirectory(prefix="review-helper-") as root:
 for name,data in files.items():
  path=pathlib.Path(root)/name
  path.parent.mkdir(parents=True,exist_ok=True)
  path.write_bytes(base64.b64decode(data))
 env=dict(os.environ)
 env["PYTHONPATH"]=root+os.pathsep+env.get("PYTHONPATH", "")
 result=subprocess.run(argv,cwd=root,env=env)
 raise SystemExit(result.returncode)
'''
    if len(source.encode()) > 250_000:
        raise ValueError('Command exceeds console stdin budget; narrow --path/args')
    return 'python - <<\'REVIEW_HELPER\'\n' + source + '\nREVIEW_HELPER'


def run_version(files, argv, scope, sha):
    cmd = ['rc', 'dev', 'console', 'bash', 'run', *scope, '--raw-output', '-o', 'json',
           '--timeout', '120', '--', '-']
    done = subprocess.run(cmd, input=console_command(files, argv), text=True,
                          capture_output=True, timeout=180)
    try:
        result = json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError('Console transport failed: ' + reduced(done.stderr)) from exc
    if 'exit_code' not in result or not result.get('run_id') or result.get('timed_out') or result.get('stdout_truncated') or result.get('stderr_truncated'):
        raise ValueError('No complete helper result: ' + reduced(json.dumps(result)))
    return dict(stdout=reduced(result.get('stdout', '')), stderr=reduced(result.get('stderr', '')),
                exit_code=result['exit_code'], sha=sha, run_id=result['run_id'], seq=result.get('seq', 0))


def capture(state, project, tenant, principal_kind, principal_id, paths, argv):
    repo, wt = Path(state['repo']), Path(state['worktree'])
    if git(wt, 'branch', '--show-current') != state['branch'] or git(wt, 'status', '--porcelain'):
        raise ValueError('Commit the helper and tests on the prepared branch first; worktree must be clean')
    after = git(wt, 'rev-parse', 'HEAD')
    git(wt, 'merge-base', '--is-ancestor', state['base'], after)
    changed = git(wt, 'diff', '--name-only', state['base'], after).splitlines()
    if not changed:
        raise ValueError('Prototype has no changes')
    if any(p.startswith('actions/') for p in changed):
        raise ValueError('Action changes are outside helper prototypes')
    if not argv or argv[0] != 'python' or len(argv) < 2 or not argv[1].endswith('.py'):
        raise ValueError('Command must be python relative/helper.py [args]; no shell or inline code')
    script = PurePosixPath(argv[1])
    if script.is_absolute() or '..' in script.parts or script.parts[0] == 'actions':
        raise ValueError('Helper must be a relative grounding Python path')
    before_files = snapshot(repo, state['base'], paths)
    after_files = snapshot(repo, after, paths)
    if str(script) not in after_files:
        raise ValueError('Include the helper in --path')
    unstaged = [p for p in changed if p.endswith('.py') and '/tests/' not in '/' + p and not p.startswith('tests/') and p not in after_files]
    if unstaged:
        raise ValueError('Changed Python omitted from --path: ' + ', '.join(unstaged))
    scope = ['--project', project, '--tenant', tenant] if tenant else ['--project', project, '--scope', 'project']
    if bool(principal_kind) != bool(principal_id):
        raise ValueError('Supply both principal flags')
    if principal_kind:
        scope += ['--principal-kind', principal_kind, '--principal-id', principal_id]
    before = run_version(before_files, argv, scope, state['base'])
    after_result = run_version(after_files, argv, scope, after)
    prototype = Prototype(repo=str(repo), branch=state['branch'], before=before, after=after_result,
                          files=len(changed), command=shlex.join(['rc', 'dev', 'console', 'bash', 'run', *scope]) + ' · staged ' + shlex.join(argv))
    # Publishing is explicit-ref only; never main or a production channel.
    git(wt, 'push', 'origin', f'{after}:refs/heads/{state["branch"]}')
    if git(wt, 'ls-remote', 'origin', f'refs/heads/{state["branch"]}').split()[0] != after:
        raise ValueError('Remote branch changed; recapture before publishing the card')
    return prototype


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='operation', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--repo', type=Path, required=True)
    prep.add_argument('--slug', required=True)
    prep.add_argument('--state', type=Path, required=True)
    cap = sub.add_parser('capture')
    cap.add_argument('--state', type=Path, required=True)
    cap.add_argument('--project', required=True)
    cap.add_argument('--tenant')
    cap.add_argument('--principal-kind')
    cap.add_argument('--principal-id')
    cap.add_argument('--path', action='append', required=True)
    cap.add_argument('--out', type=Path, required=True)
    cap.add_argument('--read-only-reviewed', action='store_true', required=True,
                     help='Confirm helper/imports were inspected: no sends, actions, or customer writes')
    cap.add_argument('command', nargs=argparse.REMAINDER)
    args = p.parse_args()
    if args.operation == 'prepare':
        print(json.dumps(prepare(args.repo, args.slug, args.state), indent=2))
    else:
        argv = args.command[1:] if args.command[:1] == ['--'] else args.command
        result = capture(json.loads(args.state.read_text()), args.project, args.tenant,
                         args.principal_kind, args.principal_id, args.path, argv)
        save(args.out, result.model_dump())
        print(prototype_markdown(result))


if __name__ == '__main__':
    main()
