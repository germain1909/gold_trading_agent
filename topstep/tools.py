# topstep/tools.py

from typing import Optional, Dict, Any
from .client import TopstepClient
from .TopStepContractFinder import TopstepContractFinder
from datetime import date
from typing import List, Dict, Any
from .utils import save_minute_bars_to_csv
import os





# Reuse a single shared client instance
_client = TopstepClient()
# Instantiate a shared contract finder
_finder = TopstepContractFinder()

def get_yesterdays_daily_bar(symbol: str = "MGC") -> Optional[Dict[str, Any]]:
    """
    Tool: Return yesterday's closed daily bar for the active contract
    of the given symbol root (e.g. 'MGC', 'GC', 'CL', 'NQ','MNQ','M6E','6E').

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


def guess_futures_contract(
    symbol: str,
    year: int,
    month: int,
    day: int
) -> Dict[str, Any]:
    """
    Tool: Guess the correct futures contract (e.g. MGCJ25, CLM24, NQU26, etc.)
    for a given symbol and calendar date.

    Example:
        guess_futures_contract("MGC", 2025, 5, 19)
        → {"symbol": "MGC", "date": "2025-05-19", "contract": "MGCM25"}

    This uses TopstepContractFinder heuristics based on typical CME rollover
    schedules for MGC/GC, CL, NQ/MNQ, 6E/M6E.
    """
    dt = date(year, month, day)
    contract = _finder.guess_contract(symbol, dt)

    return {
        "symbol": symbol.upper(),
        "date": dt.isoformat(),
        "contract": contract,
    }

def get_daily_bar_for_contract_on_date(
    contract_id: str,
    year: int,
    month: int,
    day: int,
    live: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Tool: Fetch a single CLOSED daily bar for a specific contractId
    on a given calendar date (UTC).

    Args:
        contract_id: e.g. "CON.F.US.ENQ.Z25" or "CON.F.US.MGC.Z25"
        year, month, day: date you care about
        live: whether to query the live environment (default False)

    Returns:
        A dict like:
        {
            "t": "2025-05-19T00:00:00+00:00",
            "o": 24455.75,
            "h": 24993.5,
            "l": 24361.75,
            "c": 24962.75,
            "v": 593852
        }
        or None if no bar is found.
    """
    target_date = date(year, month, day)
    return _client.get_daily_bar_for_contract_on_date(
        contract_id=contract_id,
        target_date=target_date,
        live=live,
    )

def get_minute_bars_for_cme_session(
    contract_id: str,
    year: int,
    month: int,
    day: int,
    live: bool = False,
    unit_number: int = 1,
) -> List[Dict[str, Any]]:
    """
    Tool: Fetch CLOSED minute bars for a CME metals session
    for a specific contractId.

    Session is:
        23:00 UTC on (year-month-day)
        → 22:00 UTC next day

    Args:
        contract_id: e.g. "CON.F.US.MGC.Z25"
        year, month, day: the CME trade date you care about
        live: live vs sim environment
        unit_number: minutes per bar (1 = 1m, 10 = 10m)

    Returns:
        List of bar dicts with keys: t, o, h, l, c, v
    """
    session_date = date(year, month, day)
    return _client.get_minute_bars_for_cme_session(
        contract_id=contract_id,
        session_date=session_date,
        live=live,
        unit_number=unit_number,
    )


def get_and_save_minute_bars_for_session(
    contract_id: str,
    year: int,
    month: int,
    day: int,
    live: bool = False,
    unit_number: int = 1,
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Tool: Fetch CLOSED minute bars for a CME metals session, save to CSV,
    and return a JSON summary.

    Session window:
        23:00 UTC on (year-month-day)
        → 22:00 UTC next day
    """

    # Folder where all historical data goes
    data_folder = "historical_data"

    # Ensure folder exists
    os.makedirs(data_folder, exist_ok=True)

    session_date = date(year, month, day)

    # Auto-generate a filename if needed
    if filename is None:
        safe_contract = contract_id.replace(".", "_")
        filename = f"{safe_contract}_{unit_number}m_{session_date.isoformat()}.csv"

     # Build final filepath INSIDE the historical_data folder
    filepath = os.path.join(data_folder, filename)

    # Fetch bars from client
    bars = _client.get_minute_bars_for_cme_session(
        contract_id=contract_id,
        session_date=session_date,
        live=live,
        unit_number=unit_number,
    )

    # Save to CSV using utils
    save_minute_bars_to_csv(bars, filepath)

    return {
        "contractId": contract_id,
        "sessionDate": session_date.isoformat(),
        "live": live,
        "unitNumber": unit_number,
        "filename": filename,
        "barCount": len(bars),
    }