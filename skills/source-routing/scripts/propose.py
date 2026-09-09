# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6,<7"]
# ///
"""Rank a local mirrored document tree for question topics; bounded text, provider-neutral metadata."""
import argparse
from datetime import datetime, timezone
import fnmatch
from pathlib import Path
import re
import yaml
from common import read, write, words, table


def inventory(root, mount, body_kb, includes, excludes):
    docs = []
    root = Path(root).resolve()
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            continue
        rel = path.relative_to(root).as_posix()
        if any(x.startswith('.') for x in path.relative_to(root).parts):
            continue
        if not any(fnmatch.fnmatch(rel, g) for g in includes) or any(fnmatch.fnmatch(rel, g) for g in excludes):
            continue
        meta, offset = {}, 0
        with path.open('rb') as f:
            first = f.readline(512)
            if first.strip() == b'---':
                header = bytearray()
                while len(header) < 16384:
                    line = f.readline(16384 - len(header))
                    if not line:
                        break
                    if line.strip() == b'---':
                        offset = f.tell()
                        try:
                            meta = yaml.safe_load(header.decode('utf-8', errors='replace')) or {}
                        except yaml.YAMLError:
                            meta = {'metadata_warning': 'Malformed YAML; title/path/body fallback'}
                        break
                    header.extend(line)
                if not offset:
                    meta = {'metadata_warning': 'Frontmatter incomplete or exceeds 16 KB'}
            if not isinstance(meta, dict):
                meta = {'metadata_warning': 'Frontmatter is not a mapping'}
            f.seek(offset)
            prefix = ''
            if body_kb and not (meta.get('title') and (meta.get('url') or meta.get('source_url'))):
                prefix = f.read(body_kb * 1024).decode('utf-8', errors='replace')
        title = str(meta.get('title') or (re.search(r'^# (.+)', prefix, re.M) or [None, path.stem])[1])
        url = str(meta.get('url') or meta.get('source_url') or '')
        if not url:
            url = next(iter(re.findall(r'https?://[^\s`<>\]\)]+', prefix)), '')
        docs.append({'path': mount.rstrip('/') + '/' + rel, 'relative_path': rel, 'title': title,
                     'url': url, 'size': path.stat().st_size, 'date': str(meta.get('updated_at') or meta.get('source_updated_at') or meta.get('last_edited_time') or meta.get('datum') or meta.get('date') or ''),
                     'source_version': str(meta.get('source_version') or ''),
                     'metadata': meta, '_local': str(path), '_offset': offset,
                     '_terms': words(rel + ' ' + title + ' ' + str(meta))})
    return docs


def propose(digest, usage, docs, per_topic, body_kb, year):
    if (digest['project'], digest.get('tenant')) != (usage['project'], usage.get('tenant')):
        raise ValueError('digest and trace scopes differ')
    observed = {s['path']: s['methods'] for s in usage['sources']}
    rows = []
    for topic in digest['topics']:
        terms = set(topic['terms']) | words(topic['topic'])
        candidates = []
        for doc in docs:
            methods = observed.get(doc['path'], {})
            relevant = {m: sorted(set(ids) & set(topic['run_ids'])) for m, ids in methods.items()}
            relevant = {m: ids for m, ids in relevant.items() if ids}
            matches = terms & doc['_terms']
            body_matches = set()
            if len(matches) < 2 and not relevant and body_kb:
                if '_body_terms' not in doc:
                    with open(doc['_local'], 'rb') as f:
                        f.seek(doc['_offset'])
                        doc['_body_terms'] = words(f.read(body_kb * 1024).decode('utf-8', errors='replace'))
                body_matches = terms & doc['_body_terms']
            score = len(matches) * 3 + min(4, len(body_matches))
            strong = set(relevant) & {'grounding_selected', 'read_command', 'journal_reference'}
            score += 12 if strong else 1 if relevant else 0
            if not score:
                continue
            years = [int(y) for y in re.findall(r'(?<!\d)(20\d{2})(?!\d)', doc['relative_path'])]
            archived = bool(re.search(r'(?:archive|archief|obsolete|deprecated|verouderd)', doc['relative_path'], re.I))
            outdated = archived or bool(years and max(years) < year) or doc['metadata'].get('deprecated') is True
            score += 3 if years and max(years) == year else -3 if outdated else 0
            if doc['date'].startswith(str(year)):
                score += 2
            noise = bool(re.search(r'(?:pivot|draaitabel|shortcut)', doc['relative_path'], re.I))
            why = []
            if doc['metadata'].get('metadata_warning'):
                why.append(doc['metadata']['metadata_warning'])
            if relevant:
                why.append('trace: ' + ', '.join(f'{m} ({len(ids)})' for m, ids in relevant.items()))
            if matches:
                why.append('keyword heuristic: ' + ', '.join(sorted(matches)[:5]))
            elif body_matches:
                why.append(f'body heuristic (first {body_kb} KB): ' + ', '.join(sorted(body_matches)[:4]))
            if doc['date']:
                why.append('source date ' + doc['date'])
            if doc['source_version']:
                why.append('version ' + doc['source_version'])
            if years:
                why.append('path year ' + str(max(years)))
            if archived:
                why.append('archive path')
            if outdated:
                why.append('likely outdated; review timeless advice separately')
            if noise:
                why.append('likely operational noise')
            # Modification time of the local export is deliberately not source freshness.
            candidates.append((score, {'topic': topic['topic'], 'title': doc['title'], 'url': doc['url'], 'path': doc['path'],
                              'why': '; '.join(why), 'verdict': 'noise' if noise else 'outdated' if outdated else '?'}))
        chosen = sorted(candidates, key=lambda x: (-x[0], x[1]['path']))[:per_topic]
        if chosen:
            rows.extend(row for _, row in chosen)
        else:
            rows.append({'topic': topic['topic'], 'title': 'No matching source', 'url': '', 'path': '', 'why': 'Coverage gap; identify an authoritative source', 'verdict': '?'})
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--digest', required=True)
    p.add_argument('--trace', required=True)
    p.add_argument('--root', required=True, help='local export or brain tree; read-only')
    p.add_argument('--mount', default='/kb', help='runtime prefix represented by --root, e.g. /kb/google_drive or /brain')
    p.add_argument('--include', action='append', help='relative-path globs (default *.md); repeatable')
    p.add_argument('--exclude', action='append', default=[])
    p.add_argument('--body-kb', type=int, default=4)
    p.add_argument('--per-topic', type=int, default=5)
    p.add_argument('--out', required=True, help='JSON inventory + candidate rows')
    p.add_argument('--markdown', required=True)
    a = p.parse_args()
    if a.body_kb < 0 or a.per_topic < 1 or not Path(a.root).is_dir():
        p.error('root must exist; body-kb >= 0 and per-topic >= 1')
    if (Path(a.root) / 'manifest.json').exists() and (Path(a.root) / 'articles').is_dir():
        p.error('rc export: use --root EXPORT/articles --mount /kb')
    docs = inventory(a.root, a.mount, a.body_kb, a.include or ['*.md'], a.exclude)
    digest = read(a.digest)
    rows = propose(digest, read(a.trace), docs, a.per_topic, a.body_kb, datetime.now(timezone.utc).year)
    public_docs = [{k: v for k, v in d.items() if not k.startswith('_')} for d in docs]
    # YAML dates are normalized to strings for portable JSON.
    for d in public_docs:
        d['metadata'] = {k: str(v) for k, v in d['metadata'].items()}
    write(a.out, {'schema': 'source-routing/v1', 'project': digest['project'], 'tenant': digest.get('tenant'), 'inventory': public_docs, 'rows': rows})
    Path(a.markdown).write_text('# Source routing — proposal\n\nEdit Verdict: use / outdated / noise / ?. Delete unwanted rows. No approval is implied by a keyword match. Old paths default to outdated; dated timeless advice may still be useful.\n\n' + table(rows))
    print(f'{len(docs)} documents; {len(rows)} proposed rows')


if __name__ == '__main__':
    main()
