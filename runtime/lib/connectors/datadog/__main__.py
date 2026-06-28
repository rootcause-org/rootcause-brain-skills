"""Module entry point so ``python -m lib.connectors.datadog ...`` runs the connector CLI."""

from . import main

raise SystemExit(main())
