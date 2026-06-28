"""Module entry point so ``python -m lib.connectors.recurly ...`` runs the connector CLI."""

from . import main

raise SystemExit(main())
