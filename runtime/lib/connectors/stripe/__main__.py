"""Module entry point so ``python -m lib.connectors.stripe ...`` runs the connector CLI."""

from . import main

raise SystemExit(main())
