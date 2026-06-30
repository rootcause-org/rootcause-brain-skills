"""Module entry point so ``python -m lib.connectors.appsignal ...`` runs the connector CLI."""

from . import main

raise SystemExit(main())
