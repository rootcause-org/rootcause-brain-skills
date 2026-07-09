"""AWS CloudWatch Logs connector.

The shared ``lib.cloudwatch`` module owns the read-only Logs API. This connector exists so the
integration catalog has an importable module and project brains can use either:

    from lib import cloudwatch
    from lib.connectors import cloudwatch

CLI:
    python -m lib.connectors.cloudwatch --tail /app --hours 1
"""

from lib.cloudwatch import (
    filter_events,
    filter_log_events,
    grep,
    groups,
    insights,
    list_log_groups,
    log_groups,
    logs,
    query,
    recent,
    search,
    tail,
)
from lib.cloudwatch import _main as main

__all__ = [
    "filter_events",
    "filter_log_events",
    "grep",
    "groups",
    "insights",
    "list_log_groups",
    "log_groups",
    "logs",
    "query",
    "recent",
    "search",
    "tail",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
