"""Pluggable market-data sources.

Each source implements the `DataSource` interface so the scanner stays
decoupled from *where* prices come from. Swap sources with one config
change - the scanner never needs to know.
"""

from .base import DataSource, FetchBatchResult, PriceFrame, OHLCV_COLUMNS
from .alpaca_source import AlpacaSource, AlpacaAuthError
from .yfinance_source import YFinanceSource
from .csv_source import CsvSource
from .synthetic_source import SyntheticSource
from .sp500 import fetch_sp500_tickers

#: Registry used by config to instantiate a source by name.
SOURCE_REGISTRY = {
    "alpaca": AlpacaSource,
    "yfinance": YFinanceSource,
    "csv": CsvSource,
    "synthetic": SyntheticSource,
}


def build_source(name: str, **kwargs) -> DataSource:
    """Instantiate a registered data source by name."""
    try:
        cls = SOURCE_REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"Unknown data source {name!r}. Available: {sorted(SOURCE_REGISTRY)}"
        )
    return cls(**kwargs)


__all__ = [
    "DataSource",
    "FetchBatchResult",
    "PriceFrame",
    "OHLCV_COLUMNS",
    "AlpacaSource",
    "AlpacaAuthError",
    "YFinanceSource",
    "CsvSource",
    "SyntheticSource",
    "fetch_sp500_tickers",
    "SOURCE_REGISTRY",
    "build_source",
]
