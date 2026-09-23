"""Read price history from local CSV files.

Useful for replaying a cached scan offline (no API calls, no rate limit)
or for scanning broker exports. Expects one file per symbol:
`<directory>/<SYMBOL>.csv` with a date column and OHLCV columns.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from .base import DataSource, FetchBatchResult, PriceFrame, OHLCV_COLUMNS

#: Canonical header -> our column name. Keyed on a normalised form so that
#: "Adj Close", "adj_close", "ADJ  CLOSE" and "Close" all resolve.
_ALIASES = {
    "open": "Open",
    "high": "High",
    "low": "Low",
    "close": "Close",
    "adj close": "Close",  # broker exports often ship only the adjusted close
    "volume": "Volume",
}


def _canonical(name: object) -> str:
    """Lowercase, collapse separators: 'Adj_Close' -> 'adj close'."""
    return re.sub(r"[\s_\-]+", " ", str(name).strip().lower())


class CsvSource(DataSource):
    name = "csv"
    supports_batching = False

    def __init__(self, directory: str | os.PathLike, *, date_column: str = "Date"):
        self.directory = Path(directory).expanduser()
        self.date_column = date_column
        if not self.directory.is_dir():
            raise FileNotFoundError(f"CSV data directory not found: {self.directory}")

    def _read_one(self, symbol: str) -> Optional[PriceFrame]:
        path = self.directory / f"{symbol}.csv"
        if not path.exists():
            return None
        df = pd.read_csv(path)
        # Be forgiving about the index column name.
        candidates = [self.date_column, "Date", "date", "timestamp", "Timestamp", "Time"]
        date_col = next((c for c in candidates if c in df.columns), None)
        if date_col is None:
            date_col = df.columns[0]
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        df = df.dropna(subset=[date_col]).set_index(date_col)

        # Map headers case-insensitively. If a real "Close" exists it wins
        # and "Adj Close" is ignored, so we never create duplicate columns.
        canon = {c: _canonical(c) for c in df.columns}
        has_close = any(v == "close" for v in canon.values())
        rename = {}
        for col, key in canon.items():
            if key == "adj close":
                if not has_close:
                    rename[col] = "Close"
            elif key in _ALIASES:
                rename[col] = _ALIASES[key]
        df = df.rename(columns=rename)
        df = df.loc[:, ~df.columns.duplicated()]

        for col in OHLCV_COLUMNS:
            if col not in df.columns:
                df[col] = pd.NA
        df = df[list(OHLCV_COLUMNS)].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Close"]).sort_index()
        if df.empty:
            return None
        return PriceFrame(symbol=symbol, df=df, source="csv")

    def fetch_batch(self, symbols: Iterable[str], **kwargs) -> FetchBatchResult:
        limit = kwargs.get("limit")
        result = FetchBatchResult()
        for sym in symbols:
            sym = sym.strip().upper()
            path = self.directory / f"{sym}.csv"
            if not path.exists():
                result.add_error(sym, "no CSV file for symbol")
                continue
            try:
                frame = self._read_one(sym)
            except (pd.errors.ParserError, ValueError, OSError) as exc:
                result.add_error(sym, f"csv parse failed: {exc}")
                continue
            if frame is None:
                # The file exists but yielded nothing usable - say so, rather
                # than claiming the file is missing.
                result.add_error(sym, "no usable Close data (check column names)")
                continue
            if limit and len(frame) > int(limit):
                frame = PriceFrame(
                    symbol=sym, df=frame.df.tail(int(limit)), source=frame.source
                )
            result.add_frame(frame)
        return result
