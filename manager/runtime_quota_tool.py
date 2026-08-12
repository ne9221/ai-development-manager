"""Platform-neutral callable for the read-only runtime quota MCP tool."""

from typing import Any

from pydantic import StrictInt

from manager.runtime_bridge import read_runtime_status


def runtime_quota_status(max_age_minutes: StrictInt = 60) -> dict[str, Any]:
    """Return the bounded public runtime quota contract from the fixed Drive SSOT."""
    if isinstance(max_age_minutes, bool) or not isinstance(max_age_minutes, int) or not 1 <= max_age_minutes <= 1440:
        raise ValueError("max_age_minutes must be an integer from 1 to 1440")
    return read_runtime_status(max_age_minutes=max_age_minutes)
