# Case selection — pick representative, deliberately non-trivial cases

You are choosing which historical cases to replay through the production brain. Input:
`candidates.md` (one line per case: id, channel, date, agent, word counts, hint flags, inbound
preview, reply preview) and, when present, the brain's `notes/mailbox-patterns.md` type table.

Pick `target` cases (default 10) so that:

- **Coverage** — the ticket-type distribution of the mailbox is represented roughly in proportion,
  but every type with a real answer pattern appears at least once. Use the patterns note's type
  names as `type_tag` when it exists; otherwise coin short kebab-case tags.
- **Deliberately hard** — at least half are non-trivial: ambiguous, multi-step, needs data we do
  not have (DB / account state), escalation or handover, misdirected sender (end customer writing
  to the software vendor), feature request, billing/invoicing, "please do it for me" requests.
  A case where the only correct move is *ask one precise question* or *hand over* is a valid,
  gradable outcome — include a few.
- **Skip the trivial** — no FAQ-only one-liners that any KB answers; skip internal test chatter,
  empty or agent-first threads, duplicates of an already-picked case.
- **Skip the unreproducible** — a case whose reply depends on a live chat back-and-forth of more
  than two customer turns, or on an attachment we cannot pass.

For each pick, record *why* it is representative or hard in one sentence. Then write the selection
with the script (one `--pick` per case):

```bash
uv run "$SKILL/scripts/simulate.py" select --scratch "$SCRATCH" \
  --pick 'S…|how-to|easy|the canonical menu-path question, checks path precision' \
  --pick 'S…|mail-delivery|hard|needs the appointment icons; correct move is one question + handover'
```

Do not paste customer text anywhere outside the scratch dir.
