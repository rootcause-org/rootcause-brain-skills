# Ship And Verify

This file is kept only as a compatibility link for old references. The external-developer publish path
is now the [`brain-publish`](../brain-publish/SKILL.md) skill.

Use public surfaces only:

1. Commit the brain change locally.
2. Run best-effort local checks with Local Brain Work (`brain-dev`).
3. For production-infra confidence, push a `dev/*` ref and run `rc ask --brain-ref dev/<branch>`.
4. Check whether the installed `rc` exposes a public publish/promote command.
5. If no public publish/promote surface exists, prepare a RootCause support request with project,
   tenant, ref, commit SHA, verification run ids, and requested product outcome.

Do not use private RootCause repos, host credentials, SSM, registry queries, or operator-only slash
commands from this kit.
