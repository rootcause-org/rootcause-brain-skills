"""Module entry point so ``python -m lib.connectors.salesforce ...`` runs the connector CLI."""

from . import main

raise SystemExit(main())
