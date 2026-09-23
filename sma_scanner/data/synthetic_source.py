"""Deterministic synthetic price source for offline testing.

Lets us prove the indicator + filter logic is correct without hitting a
live API (or burning rate limit). `breakout_symbols` get an engineered
"V" recovery so their 20-day SMA crosses above the 50-day SMA near the
end of the series; everything else is a plain random walk.
"""

from __future__ import annotations

import math
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from .base import DataSource, FetchBatchResult, PriceFrame, OHLCV_COLUMNS


class SyntheticSource(DataSource):
    name = "synthetic"
    supports_batching = True

    def __init__(
        self,
        *,
        days: int = 400,
        base_price: float = 100.0,
        vol: float = 0.012,
        seed: int = 7,
        trough_offset: int = 12,
        recovery_drift: float = 0.045,
        decline_drift: float = -0.006,
        base_volume: int = 2_000_000,
        breakout_symbols: Optional[Iterable[str]] = None,
    ) -> None:
        # Symbols that should exhibit a fresh golden cross (for tests).
        self.days = days
        self.base_price = base_price
        self.vol = vol
        self.seed = seed
        self.trough_offset = trough_offset
        self.recovery_drift = recovery_drift
        self.decline_drift = decline_drift
        self.base_volume = base_volume
        self.breakout_symbols = self._as_symbol_set(breakout_symbols)

    # ------------------------------------------------------------------
    def _random_walk(self, rng: np.random.Generator) -> np.ndarray:
        """Flat, trendless series: any crossover here is chance."""
        shocks = rng.normal(0.0, self.vol, self.days)
        return self.base_price * np.exp(np.cumsum(shocks))

    def _breakout_walk(self, rng: np.random.Generator) -> np.ndarray:
        """Decline into a trough, then a sharp recovery (golden cross).

        The decline keeps SMA20 below SMA50; the steep recovery pulls
        SMA20 back up through SMA50 a few bars before the series ends.
        """
        n = self.days
        trough_at = n - self.trough_offset
        drift = np.empty(n)
        drift[:trough_at] = self.decline_drift
        drift[trough_at:] = self.recovery_drift
        shocks = rng.normal(0.0, self.vol * 0.6, n)
        path = self.base_price * np.exp(np.cumsum(drift + shocks))
        return path

    def _to_frame(self, symbol: str, closes: np.ndarray, rng) -> PriceFrame:
        idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=len(closes), freq="D")
        intraday = np.abs(rng.normal(0, self.vol * 0.5, len(closes))) + 0.001
        df = pd.DataFrame(
            {
                "Open": closes * (1 + rng.normal(0, 0.002, len(closes))),
                "High": closes * (1 + intraday),
                "Low": closes * (1 - intraday),
                "Close": closes,
                "Volume": rng.integers(
                    self.base_volume * 0.5, self.base_volume * 1.5, len(closes)
                ).astype(float),
            },
            index=idx,
        )
        df = df[list(OHLCV_COLUMNS)]
        return PriceFrame(symbol=symbol, df=df, source="synthetic")

    @staticmethod
    def _as_symbol_set(values) -> set:
        """Accept None / iterable / comma-separated string.

        `is not None` rather than truthiness, so an explicit empty list
        really does clear the set instead of falling back to the default.
        """
        if values is None:
            return set()
        if isinstance(values, str):
            values = values.split(",")
        return {str(s).strip().upper() for s in values if str(s).strip()}

    def fetch_batch(
        self, symbols: Iterable[str], *, breakout_symbols: Optional[Iterable[str]] = None, **kwargs
    ) -> FetchBatchResult:
        symbols = [s.strip().upper() for s in symbols if s and s.strip()]
        # None means "not supplied" -> keep the constructor's set.
        breakouts = (
            self.breakout_symbols
            if breakout_symbols is None
            else self._as_symbol_set(breakout_symbols)
        )
        limit = kwargs.get("limit")
        result = FetchBatchResult()
        for i, sym in enumerate(symbols):
            rng = np.random.default_rng(self.seed + i)
            closes = self._breakout_walk(rng) if sym in breakouts else self._random_walk(rng)
            frame = self._to_frame(sym, closes, rng)
            if limit and len(frame) > int(limit):
                frame = PriceFrame(
                    symbol=sym, df=frame.df.tail(int(limit)), source=frame.source
                )
            result.add_frame(frame)
        return result
