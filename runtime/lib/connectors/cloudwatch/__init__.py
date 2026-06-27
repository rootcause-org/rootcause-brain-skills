"""AWS CloudWatch Logs connector.

The shared ``lib.cloudwatch`` module owns the read-only Logs API. This connector exists so the
integration catalog has an importable module and project brains can use either:

    from lib import cloudwatch
    from lib.connectors import cloudwatch

CLI:
    python -m lib.connectors.cloudwatch --tail /app --hours 1
"""

from lib.cloudwatch import filter_log_events, insights, log_groups, search, tail
from lib.cloudwatch import _main as main

__all__ = ["filter_log_events", "insights", "log_groups", "search", "tail", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
