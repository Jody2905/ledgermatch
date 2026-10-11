"""Times are stored in UTC (so they sort and compare correctly whatever the
server's location) and shown in the computer's own time zone."""

from datetime import datetime, timezone


def local_time(stamp):
    """'2026-10-11T04:59:12+00:00' -> '2026-10-11 00:59' on a computer set to UTC-4."""
    if not stamp:
        return ""
    when = datetime.fromisoformat(stamp)
    if when.tzinfo is None:          # treat anything without a zone as UTC
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone().strftime("%Y-%m-%d %H:%M")
