"""Module entry point so ``python -m lib.connectors.zendesk ...`` runs the connector CLI."""

from . import main

raise SystemExit(main())
