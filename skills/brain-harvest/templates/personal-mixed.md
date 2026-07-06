# Archetype: personal / mixed mailbox

A personal or mixed inbox (the bollen-klara shape): many unrelated topics, no single product, heavy
triage need, and privacy-sensitive threads. The brain leans on a triage skill + per-case files +
distilled patterns + an explicit escalation/privacy policy. Edit this skeleton; delete unused sections.

```text
AGENTS.md                     # router: which case file / pattern answers which kind of message
terminology.md                # names, recurring correspondents' roles, shorthand (distilled, not raw)
skills/
  triage/SKILL.md             # how to decide draft vs skip vs escalate for this mixed inbox
notes/
  onboarding-inbox.md         # survey facts: message mix, languages, sign-off norms — distilled
  mailbox-patterns.md         # recurring situation -> canonical response pattern (no raw quotes)
  escalation-privacy.md       # what NEVER gets auto-drafted; what must go to a human; sensitive-topic list
cases/
  <case-slug>.md              # one distilled case runbook per recurring situation (no verbatim mail)
```

## Triage skill (skeleton)

State the draft/skip/escalate decision in `skills/triage/SKILL.md`, but push **deterministic** rules
into `rc triage rules` and **broad guidance** into `rc triage policy` — the brain skill explains the
*why*, the triage surface enforces the *what*.

## Escalation + privacy (skeleton)

- Sensitive topics that must never auto-draft (health, legal, finances, minors): `<list>`.
- Always-escalate-to-human senders/subjects: push to `rc triage rules effect=...`.
- Redaction rule: distilled patterns only — see the skill's privacy gate. `brain_lint.py` enforces it.

## What goes where (do not misfile)

- **Persona settings**: voice, warmth, signature, language.
- **Triage** (`rc triage ...`): draft/skip/escalate decisions, deterministic sender/subject rules.
- **Brain files**: case runbooks, terminology, escalation criteria, distilled patterns.

Privacy is acute here: personal mail carries credentials, addresses, health data. No raw thread bodies
reach a brain file, ever. If legacy onboarding committed raw mail, the history-rewrite decision is the
operator's — do not silently `git rm`.
