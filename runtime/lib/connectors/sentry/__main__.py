"""Module entry point so ``python -m lib.connectors.sentry ...`` runs the connector CLI."""

from . import main

raise SystemExit(main())
