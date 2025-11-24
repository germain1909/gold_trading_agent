# topstep/tools.py

from typing import Optional, Dict, Any
from .client import TopstepClient

# Reuse a single shared client instance
_client = TopstepClient()


def get_yesterdays_daily_bar(symbol: str = "MGC") -> Optional[Dict[str, Any]]:
    """
    Tool: Return yesterday's closed daily bar for the active contract
    of the given symbol root (e.g. 'MGC', 'GC', 'CL', 'NQ').

    Returns a dict like:
    {
        "t": "2025-11-21T00:00:00+00:00",
        "o": 4075.0,
        "h": 4101.1,
        "l": 4019.0,
        "c": 4062.8,
        "v": 455342
    }
    or None if not available.
    """
    bar = _client.get_yesterdays_daily_bar_for_symbol(symbol, live=False)
    return bar