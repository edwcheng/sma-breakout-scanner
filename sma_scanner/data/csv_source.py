"""Read price history from local CSV files.

Useful for replaying a cached scan offline (no API calls, no rate limit)
or for scanning broker exports. Expects one file per symbol:
`<directory>/<SYMBOL>.csv` with a date column and OHLCV columns.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from .base import DataSource, FetchBatchResult, PriceFrame, OHLCV_COLUMNS


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

        rename = {
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "adj close": "Close",
            "adj_close": "Close",
            "volume": "Volume",
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        df = df.rename(columns={c: c.capitalize() for c in df.columns})

        for col in OHLCV_COLUMNS:
            if col not in df.columns:
                df[col] = pd.NA
        df = df[list(OHLCV_COLUMNS)].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Close"]).sort_index()
        if df.empty:
            return None
        return PriceFrame(symbol=symbol, df=df, source="csv")

    def fetch_batch(self, symbols: Iterable[str], **kwargs) -> FetchBatchResult:
        result = FetchBatchResult()
        for sym in symbols:
            sym = sym.strip().upper()
            try:
                frame = self._read_one(sym)
            except (pd.errors.ParserError, ValueError, OSError) as exc:
                result.add_error(sym, f"csv parse failed: {exc}")
                continue
            if frame is None:
                result.add_error(sym, "no CSV file for symbol")
            else:
                result.add_frame(frame)
        return result
