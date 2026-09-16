"""Original quote sources; run from OUT or pass evidence/out_dir explicitly. No network calls."""
import json
from pathlib import Path
import re


def excerpt_matches(excerpt, original):
    """Same normalization/ellipsis contract as weekly-review build.py."""
    def normalized(value):
        value = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', value)
        return ' '.join(value.replace('*', '').replace('’', "'").split())
    original = normalized(original)
    parts = [normalized(part) for part in re.split(r'…|\.\.\.', excerpt) if normalized(part)]
    return bool(parts or not original) and all(part in original for part in parts)


def evidence_sources(run_id, evidence, out_dir):
    sources = {key: [] for key in ('question', 'proposed', 'sent', 'feedback')}

    def add(key, value):
        if isinstance(value, str) and value.strip() and value not in sources[key]:
            sources[key].append(value)

    for run in evidence.get('runs', []):
        if str(run.get('run_id')).lower() == run_id.lower():
            add('question', run.get('question'))
            add('proposed', run.get('draft_markdown'))
    # Drill stores originals alongside its reduced human-readable report.
    path = Path(out_dir) / 'details' / f'evidence-{run_id}.json'
    if path.exists():
        data = json.loads(path.read_text())
        for key in ('question', 'proposed'):
            add(key, data.get(key))
    for delta in evidence.get('deltas', []):
        if str(delta.get('related_run_id')).lower() == run_id.lower():
            add('question', delta.get('question_excerpt'))
            add('proposed', delta.get('proposed_body'))
            add('sent', delta.get('sent_body_clean') or delta.get('sent_body'))
    # This dedicated feed contains human feedback; run review/evaluation scores do not.
    for row in evidence.get('feedback', []):
        if str(row.get('run_id')).lower() == run_id.lower():
            add('question', row.get('question'))
            manual = {key: row[key] for key in ('score', 'comment') if row.get(key) is not None and row.get(key) != ''}
            if manual:
                sources['feedback'].append(manual)
    return sources


def evidence_excerpts(run_id, *, evidence=None, out_dir=None) -> dict:
    """Return source text for the judge to shorten verbatim; missing fields stay absent.

    With one argument, cwd is the collected OUT directory. UUIDs only: ambiguous prefixes
    must first be resolved by drill. Full bodies are source material, not publish-ready excerpts.
    """
    from uuid import UUID
    run_id = str(UUID(run_id))
    out_dir = Path(out_dir or Path.cwd())
    if evidence is None:
        evidence = json.loads((out_dir / 'evidence.json').read_text())
    sources = evidence_sources(run_id, evidence, out_dir)
    return dict(run_id=run_id, label=run_id[:8]) | {key: values[0] for key, values in sources.items() if values}
