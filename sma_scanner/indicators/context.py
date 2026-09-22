"""Per-symbol indicator context with memoization.

Filters ask the context for indicators by parameter (e.g. `ctx.sma(50)`)
rather than recomputing them, so a symbol's history is processed once no
matter how many filters run against it. Adding an indicator is a single
method here - no filter or scanner changes required.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import pandas as pd

from ..data.base import PriceFrame
from . import indicators as ind
from .indicators import Crossover


class InsufficientHistory(Exception):
    """Raised when a symbol lacks enough bars for the requested indicator."""


class IndicatorContext:
    """Lazily-computed indicator view over one symbol's price history."""

    def __init__(self, frame: PriceFrame) -> None:
        self.frame = frame
        self._cache: Dict[Tuple, object] = {}

    # -- basics ---------------------------------------------------------
    @property
    def symbol(self) -> str:
        return self.frame.symbol

    @property
    def df(self) -> pd.DataFrame:
        return self.frame.df

    @property
    def close(self) -> pd.Series:
        return self.frame.close

    @property
    def volume(self) -> pd.Series:
        return self.frame.volume

    @property
    def last_price(self) -> float:
        return float(self.close.iloc[-1])

    @property
    def last_date(self) -> pd.Timestamp:
        return self.frame.last_date()

    def __len__(self) -> int:
        return len(self.frame)

    def require_bars(self, n: int) -> None:
        """Raise if we do not have at least `n` bars."""
        if len(self) < n:
            raise InsufficientHistory(f"{self.symbol}: {len(self)} bars, need {n}")

    def _memo(self, key: Tuple, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    # -- moving averages ------------------------------------------------
    def sma(self, period: int) -> pd.Series:
        return self._memo(("sma", period), lambda: ind.sma(self.close, period))

    def ema(self, period: int) -> pd.Series:
        return self._memo(("ema", period), lambda: ind.ema(self.close, period))

    # -- momentum / volume ----------------------------------------------
    def rsi(self, period: int = 14) -> pd.Series:
        return self._memo(("rsi", period), lambda: ind.rsi(self.close, period))

    def avg_volume(self, period: int = 20) -> float:
        return self._memo(("avgvol", period), lambda: ind.avg_volume(self.volume, period))

    def change_pct(self, periods: int = 1) -> float:
        return self._memo(("chg", periods), lambda: ind.pct_change(self.close, periods))

    # -- volume analysis ------------------------------------------------
    def volume_at(self, bar_index: int) -> float:
        """Volume on one specific bar."""
        try:
            return float(self.volume.iloc[bar_index])
        except (IndexError, TypeError, ValueError):
            return float("nan")

    def avg_volume_before(self, bar_index: int, lookback: int = 20) -> float:
        """Mean volume over the `lookback` bars immediately BEFORE `bar_index`.

        Deliberately excludes the bar itself: including it would let a volume
        spike inflate the very baseline it is being measured against.
        """
        start = max(0, bar_index - int(lookback))
        window = self.volume.iloc[start:bar_index].dropna()
        if window.empty:
            return float("nan")
        return float(window.mean())

    def volume_surge(self, bar_index: int, lookback: int = 20) -> Dict[str, float]:
        """Compare one bar's volume against its own recent baseline.

        Keys are namespaced (`pre_cross_avg_volume`) so they cannot collide
        with the trailing average emitted by the min_avg_volume filter.
        """
        vol = self.volume_at(bar_index)
        avg = self.avg_volume_before(bar_index, lookback)
        if avg and not pd.isna(avg) and avg > 0 and not pd.isna(vol):
            ratio = float(vol / avg)
        else:
            ratio = float("nan")
        return {
            "cross_volume": vol,
            "pre_cross_avg_volume": avg,
            "volume_ratio": ratio,
        }

    # -- crossovers -----------------------------------------------------
    def crossovers(
        self,
        fast_period: int,
        slow_period: int,
        *,
        direction: str = "up",
        lookback: Optional[int] = None,
    ) -> List[Crossover]:
        """Crossings between two SMAs, memoized per parameter set."""
        key = ("xover", fast_period, slow_period, direction, lookback)
        return self._memo(
            key,
            lambda: ind.detect_crossovers(
                self.sma(fast_period),
                self.sma(slow_period),
                direction=direction,
                lookback=lookback,
            ),
        )

    def last_cross(
        self, fast_period: int, slow_period: int, *, direction: str = "up"
    ) -> Optional[Crossover]:
        """Most recent crossing of any age."""
        events = self.crossovers(fast_period, slow_period, direction=direction)
        return events[-1] if events else None

    def recent_cross(
        self,
        fast_period: int,
        slow_period: int,
        *,
        direction: str = "up",
        lookback: int = 10,
    ) -> Optional[Crossover]:
        """Most recent crossing that occurred within the last `lookback` bars.

        This is the heart of a "breakout": not merely fast > slow today,
        but the cross having happened *recently* (fresh signal).
        """
        events = self.crossovers(
            fast_period, slow_period, direction=direction, lookback=lookback
        )
        return events[-1] if events else None

    def bars_since(self, cross: Crossover) -> int:
        """How many bars ago a crossover happened (0 = the latest bar)."""
        return (len(self) - 1) - cross.bar_index

    def spread_pct(self, fast_period: int, slow_period: int) -> float:
        """Current (fast - slow) / slow * 100."""
        return ind.current_spread_pct(self.sma(fast_period), self.sma(slow_period))

    # -- convenience summary --------------------------------------------
    def snapshot(self, fast_period: int, slow_period: int) -> Dict[str, object]:
        """Flat dict of the values a report typically wants to show."""
        fast = self.sma(fast_period)
        slow = self.sma(slow_period)
        return {
            "symbol": self.symbol,
            "last_date": self.last_date,
            "price": self.last_price,
            f"sma_{fast_period}": float(fast.iloc[-1]) if fast.notna().any() else float("nan"),
            f"sma_{slow_period}": float(slow.iloc[-1]) if slow.notna().any() else float("nan"),
            "spread_pct": self.spread_pct(fast_period, slow_period),
            "bars": len(self),
        }
