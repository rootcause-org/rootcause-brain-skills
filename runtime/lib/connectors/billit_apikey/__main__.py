from __future__ import annotations

import sys

from lib.connectors.billit import main


def _argv() -> list[str]:
    args = sys.argv[1:]
    if "--help" in args or "-h" in args or "--connection" in args:
        return args
    return ["--connection", "billit_apikey", *args]


raise SystemExit(main(_argv()))
