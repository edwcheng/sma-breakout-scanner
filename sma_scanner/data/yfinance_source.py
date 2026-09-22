"""yfinance (Yahoo Finance) source - the no-API-key fallback.

Kept because it needs no credentials, but note Yahoo rate-limits shared
IPs aggressively (HTTP 429), which makes it unreliable for bulk scans.
Alpaca is the preferred source.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Optional

import pandas as pd

from .base import DataSource, FetchBatchResult, PriceFrame, OHLCV_COLUMNS


class YFinanceSource(DataSource):
    name = "yfinance"
    supports_batching = True

    def __init__(self, *, period: str = "1y", interval: str = "1d", request_delay: float = 0.2):
        try:
            import yfinance  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise ImportError("yfinance source requires `pip install yfinance`") from exc
        self.period = period
        self.interval = interval
        self.request_delay = request_delay

    def fetch_batch(self, symbols: Iterable[str], **kwargs: Any) -> FetchBatchResult:
        import yfinance as yf

        symbols = [s.strip().upper() for s in symbols if s and s.strip()]
        result = FetchBatchResult()
        if not symbols:
            return result

        period = kwargs.get("period", self.period)
        raw = yf.download(
            symbols,
            period=period,
            interval=kwargs.get("interval", self.interval),
            auto_adjust=True,
            progress=False,
            group_by="column",
            threads=False,
        )
        if raw is None or raw.empty:
            for s in symbols:
                result.add_error(s, "yfinance returned no data (likely rate limited)")
            return result
        time.sleep(self.request_delay)

        # With group_by="column" the columns are flat when only one symbol
        # is requested; handle both shapes.
        single = len(symbols) == 1
        for sym in symbols:
            try:
                if single:
                    df = raw.copy()
                else:
                    df = raw.xs(sym, axis=1, level=1)
            except (KeyError, TypeError):
                result.add_error(sym, "symbol missing from yfinance response")
                continue

            df = df.rename(columns={"Adj Close": "Close"})
            df = df.loc[:, [c for c in OHLCV_COLUMNS if c in df.columns]]
            if df.isna().all().all() or df.empty:
                result.add_error(sym, "all-NaN series")
                continue
            df = df.dropna(subset=["Close"])
            if df.empty or "Close" not in df.columns:
                result.add_error(sym, "no usable Close data")
                continue
            for col in OHLCV_COLUMNS:
                if col not in df.columns:
                    df[col] = pd.NA
            df = df[list(OHLCV_COLUMNS)].astype(
                {c: "float64" for c in ("Open", "High", "Low", "Close", "Volume")}
            )
            df.index = pd.to_datetime(df.index)
            df = df.sort_index()
            result.add_frame(PriceFrame(symbol=sym, df=df, source="yfinance"))

        return result
