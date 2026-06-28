"""Module entry point so ``python -m lib.connectors.paypal ...`` runs the connector CLI."""

from . import main

raise SystemExit(main())
