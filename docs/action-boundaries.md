# Actions: what runs where

The agent can read action code and choose an action plus arguments. It cannot send arbitrary
Python or shell commands to the write executor. The host selects approved code and controls
whether it runs automatically or waits for a human.

| Context | Runs here | Access |
|---|---|---|
| Agent workspace | Grounding, optional `preflight.py` | Read-only data; brain mounted read-only; no action write credentials |
| Isolated policy check | `policy.py`, when effective autonomy is `policy` | Fresh read-only environment, outside the agent's control |
| Hosted action executor | Approved `script.py`, once per execution | Separate short-lived container with the configured action write credentials; no model loop |

These are execution contexts, not a promise of different Docker images. The same image can serve
read and write containers with different credentials. A working grounding database connection does
**not** grant action writes: configure the separate [action credentials](secrets.md#tenant-and-action-planes)
and applicable [write connections](actions.md#write-connections).

The action body is trusted code, not a second sandbox per business operation. It must validate inputs,
scope writes to the trusted tenant/principal, and enforce business limits itself. Seeing actions A, B
and C in the mounted brain does not let the model execute them in the write container: each request
passes through the host. But approval must also review any extra effects the chosen body performs.

## Preflight predicts; policy authorizes automation; the body enforces invariants

| Mechanism | Verdict | Current production effect |
|---|---|---|
| Manifest parameter schema | Valid/invalid arguments | Invalid inputs are rejected before dispatch. |
| Optional `preflight.py` | `ok`, `summary`, optional `reason`, `class`, `observed`, `resource_url` | A completed `ok:false` blocks a human-review proposal (or disables a chat card). It does **not** gate automatic execution. |
| `policy.py` for effective `policy` autonomy | `allow`, `reason`, optional `observed` | `allow:false` or a technical failure prevents automatic execution and falls back to human review. It is not an absolute prohibition. |
| `script.py` | Execution result | Recheck hard business limits and current state before writing, including after a human confirms. |

There is no preflight `autonomously_runnable` field. `lib.action.preflight` only loads params and
builds a verdict dictionary; it neither schedules an action nor grants write access. The production
agent requests execution through the host's `action` tool.

The host runs the installed preflight during an action-tool request, so a prior manual preflight
is not accepted as a reusable permission slip. However, **the verdict is enforced only on the
human-review path**. Effective `auto`, or `policy` with `allow:true`, can execute despite `ok:false`.
A preflight crash/timeout/unparseable result becomes **preview unavailable** and remains human-confirmable.
No preflight means schema validation alone, not proof that the business operation will succeed.

Human confirmation uses the stored preview; it does **not** rerun `preflight.py`. Data can change
between preview and execution. Check invariants again in the body, using a transaction or conditional
write where needed. The executor resolves the current approved action; an approved version changing
since proposal is surfaced/audited, not an unconditional refusal of execution.

For example, “discounts above €50 need review” belongs in `policy.py`. “Discounts above €50 are never
allowed” must be enforced in the body too; mirror it in preflight for early feedback. A policy denial
alone still permits human approval.

## Hosted Python: the request and feedback loop

```mermaid
sequenceDiagram
    participant A as Agent in read-only workspace
    participant H as Host action gate
    participant P as Isolated read-only policy check
    participant R as Human reviewer
    participant W as Separate Python write container
    opt Agent explores before requesting an action
        A->>A: Run read-only preflight with candidate params
        Note over A: Read feedback and correct arguments
    end
    A->>H: action(action_id, params, intent)
    H->>A: Run installed preflight in live workspace, if present
    A-->>H: ok, summary, observed (or unavailable)
    opt Effective autonomy is policy
        H->>P: Check these arguments and trusted scope
        P-->>H: allow or require human review
    end
    alt Effective auto or policy allows
        H->>W: Run approved script with params and write credentials
        W-->>H: Execution result
        H-->>A: Actual result for the answer
    else Human review required
        alt Available preflight says no
            H-->>A: Refusal and feedback; no executable proposal
        else Passed or preview unavailable
            H-->>A: Pending proposal (no write yet)
            H->>R: Confirmation and stored preview
            R->>H: Confirm
            Note over H: No fresh preflight here
            H->>W: Run approved script; body rechecks invariants
            W-->>H: Execution result
        end
    end
```

Make preflight feedback useful: identify the disqualifying state, suggest a grounded next step,
and show calculated effects in `observed` (for example €150 − 10% = €135). Keep `summary` short
for the confirmation UI. A prediction is never evidence that the write happened.

In Embassy mode, execution happens inside the customer's application instead, letting the action
reuse application methods, callbacks and jobs. The Python preflight still runs in the read-only
workspace; an Embassy validation dry-run is a separate check. See [action authoring](actions.md).
