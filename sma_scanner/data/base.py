"""Core abstractions for the data layer.

`PriceFrame` is a thin typed wrapper around a pandas DataFrame with a
clear contract (a DatetimeIndex and OHLCV columns). Keeping it explicit
means every downstream component - indicators, filters, the scanner -
can rely on the same shape regardless of which source produced it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

import pandas as pd

# Canonical column contract every source must produce.
OHLCV_COLUMNS: tuple = ("Open", "High", "Low", "Close", "Volume")


@dataclass
class PriceFrame:
    """Normalized OHLCV history for a single symbol."""

    symbol: str
    df: pd.DataFrame  # DatetimeIndex ascending, columns = OHLCV_COLUMNS
    source: str = "unknown"
    # Populated by sources that know how fresh their data is (e.g. Alpaca
    # free-tier feeds are deliberately delayed). Filters can use this to
    # avoid reporting a signal off a stale bar.
    data_delay_minutes: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.df, pd.DataFrame):
            raise TypeError(f"df must be a pandas DataFrame, got {type(self.df)}")
        missing = [c for c in OHLCV_COLUMNS if c not in self.df.columns]
        if missing:
            raise ValueError(f"{self.symbol}: PriceFrame missing columns {missing}")
        # Duplicate labels make `df["Close"]` return a DataFrame rather than a
        # Series, which breaks every indicator with a confusing float() error
        # far from the real cause. Reject it here, next to the source.
        dupes = sorted(set(self.df.columns[self.df.columns.duplicated()]))
        if dupes:
            raise ValueError(
                f"{self.symbol}: PriceFrame has duplicate columns {dupes}"
            )
        if not isinstance(self.df.index, pd.DatetimeIndex):
            raise TypeError(f"{self.symbol}: df index must be a DatetimeIndex")
        if len(self.df) == 0:
            raise ValueError(f"{self.symbol}: empty price history")

    @property
    def close(self) -> pd.Series:
        return self.df["Close"]

    @property
    def volume(self) -> pd.Series:
        return self.df["Volume"]

    def __len__(self) -> int:
        return len(self.df)

    def last_date(self) -> pd.Timestamp:
        return self.df.index[-1]


@dataclass
class FetchBatchResult:
    """Result of a batched multi-symbol fetch.

    Batch APIs (Alpaca) return some symbols and silently omit others
    (delisted, no data for window, typo'd ticker). Rather than raise,
    we surface both the successes and the reason for each miss.
    """

    frames: Dict[str, PriceFrame] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)

    def add_frame(self, frame: PriceFrame) -> None:
        self.frames[frame.symbol] = frame

    def add_error(self, symbol: str, reason: str) -> None:
        self.errors[symbol] = reason

    def merge(self, other: "FetchBatchResult") -> "FetchBatchResult":
        self.frames.update(other.frames)
        self.errors.update(other.errors)
        return self


class DataSource(ABC):
    """Interface for anything that can supply price history.

    To add a new provider, subclass this, implement `fetch_batch`, and
    register it in `sma_scanner.data.registry`. Nothing else changes.
    """

    #: Human-readable source name, stored on each PriceFrame for provenance.
    name: str = "abstract"

    #: Whether this source can fetch many symbols per request. The scanner
    #: uses this to decide between one big call and a per-symbol loop.
    supports_batching: bool = False

    @abstractmethod
    def fetch_batch(self, symbols: Iterable[str], **kwargs) -> FetchBatchResult:
        """Fetch history for many symbols; return whatever succeeded."""
        raise NotImplementedError

    def fetch_one(self, symbol: str, **kwargs) -> Optional[PriceFrame]:
        """Convenience single-symbol fetch."""
        return self.fetch_batch([symbol], **kwargs).frames.get(symbol)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} name={self.name!r}>"
