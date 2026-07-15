# Ship And Verify

This file is kept only as a compatibility link for old references. The external-developer publish path
is now the [`brain-publish`](../brain-publish/SKILL.md) skill.

Use public surfaces only:

1. Commit the brain change locally.
2. Run best-effort local checks with Local Brain Work (`local-brain-work`).
3. For production-infra confidence, push a `dev/*` ref and run `rc ask --brain-ref dev/<branch>`.
4. Capture the exact tested SHA, push it to `origin/main`, and run `rc dev brain sync`.
5. For a shared project brain using `stable` or `edge`, run `rc dev brain promote --channel <channel>
   --sha <exact-sha>` with a project-maintainer login. Tenant brains use `main`; do not promote them.
6. Run `rc dev brain status -o json`. Do not claim success until the intended channel resolves the
   exact SHA, or a safe normal run without `--brain-ref` proves `channel:<channel> @ <sha>`.
7. If tenant publish, action wiring, authorization, or manual reconcile remains, prepare a RootCause
   support request with project, tenant, ref, commit SHA, status output, verification run ids, and
   requested product outcome.

Do not use private RootCause repos, host credentials, SSM, registry queries, or operator-only slash
commands from this kit.
