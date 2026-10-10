"""
Entry indicators on 30m closes (pure functions, no I/O).

  EMA line:  EMA(15) of close, smoothed with SMA(5)   (TradingView: EMA 15, smoothing SMA 5)
  BB basis:  EMA(24) of close                          (Bollinger Bands 24, basis type EMA)

A rejection gives the direction; the entry is the first 30m close beyond
BOTH lines in that direction.
"""
import os
from typing import List, Optional

EMA_LENGTH = int(os.getenv("ENTRY_EMA_LENGTH", 15))
EMA_SMOOTHING = int(os.getenv("ENTRY_EMA_SMOOTHING", 5))   # SMA length; 0 or 1 = no smoothing
BB_LENGTH = int(os.getenv("ENTRY_BB_LENGTH", 24))


def ema(values: List[float], length: int) -> List[Optional[float]]:
    """EMA seeded with the SMA of the first `length` values (TradingView style)."""
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < length:
        return out
    k = 2 / (length + 1)
    prev = sum(values[:length]) / length
    out[length - 1] = prev
    for i in range(length, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def sma(values: List[Optional[float]], length: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    for i in range(length - 1, len(values)):
        window = values[i - length + 1:i + 1]
        if None not in window:
            out[i] = sum(window) / length
    return out


def ema_line(closes: List[float]) -> List[Optional[float]]:
    line = ema(closes, EMA_LENGTH)
    return sma(line, EMA_SMOOTHING) if EMA_SMOOTHING > 1 else line


def bb_basis(closes: List[float]) -> List[Optional[float]]:
    return ema(closes, BB_LENGTH)


def crossed_for_entry(side: str, close: float, ema_value: Optional[float], basis_value: Optional[float]) -> bool:
    """True when the close is beyond both lines in the trade's direction."""
    if ema_value is None or basis_value is None:
        return False
    if side == "long":
        return close > ema_value and close > basis_value
    return close < ema_value and close < basis_value


ATR_LENGTH = int(os.getenv("ENTRY_ATR_LENGTH", 14))


def atr(bars: List[dict], length: int = ATR_LENGTH) -> List[Optional[float]]:
    """Average true range with Wilder smoothing (TradingView's ta.atr)."""
    out: List[Optional[float]] = [None] * len(bars)
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b["h"] - b["l"])
        else:
            pc = bars[i - 1]["c"]
            trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    if len(bars) < length:
        return out
    prev = sum(trs[:length]) / length
    out[length - 1] = prev
    for i in range(length, len(bars)):
        prev = (prev * (length - 1) + trs[i]) / length
        out[i] = prev
    return out
