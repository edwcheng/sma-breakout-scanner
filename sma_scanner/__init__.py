"""SMA breakout scanner.

A modular stock screener: pluggable data sources, memoized indicators,
and a registry of composable filters selected from config.
"""

__version__ = "1.0.0"

from .config import DEFAULT_FILTERS, ScanConfig
from .data import (
    AlpacaSource,
    DataSource,
    PriceFrame,
    SyntheticSource,
    YFinanceSource,
    build_source,
    fetch_sp500_tickers,
)
from .filters import Filter, FilterResult, available_filters, create_filter
from .indicators import IndicatorContext, detect_crossovers, sma
from .scanner import ScanResult, Scanner, SymbolResult

__all__ = [
    "__version__",
    "ScanConfig",
    "DEFAULT_FILTERS",
    "Scanner",
    "ScanResult",
    "SymbolResult",
    "DataSource",
    "PriceFrame",
    "AlpacaSource",
    "YFinanceSource",
    "SyntheticSource",
    "build_source",
    "fetch_sp500_tickers",
    "Filter",
    "FilterResult",
    "available_filters",
    "create_filter",
    "IndicatorContext",
    "sma",
    "detect_crossovers",
]
