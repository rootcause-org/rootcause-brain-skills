"""Module entry point so ``python -m lib.connectors.posthog ...`` runs the connector CLI."""

from . import main

raise SystemExit(main())
