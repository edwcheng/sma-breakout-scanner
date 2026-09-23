"""Scan orchestration.

Ties the pieces together: resolve the universe -> fetch prices -> build
an indicator context per symbol -> run every configured filter -> collect
results. The scanner knows nothing about *which* filters run; that is
config data.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .config import ScanConfig
from .data import (
    PriceFrame,
    build_source,
    fetch_most_traded_etfs,
    fetch_sp500_tickers,
)
from .filters import Filter, FilterResult
from .indicators import IndicatorContext, InsufficientHistory


@dataclass
class SymbolResult:
    """Everything we learned about one symbol."""

    symbol: str
    results: List[FilterResult] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    skipped: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.skipped is None and all(r.passed for r in self.results)

    @property
    def failed_filter(self) -> Optional[str]:
        for r in self.results:
            if not r.passed:
                return r.filter_name
        return None

    @property
    def failure_reason(self) -> str:
        for r in self.results:
            if not r.passed:
                return r.reason
        return self.skipped or ""


def _rank_value(sr: "SymbolResult", key: Optional[str]) -> Optional[float]:
    """Numeric rank for one symbol, or None when the metric is missing.

    Dates are ranked by instant rather than rejected. `cross_date` is a real,
    displayed metric, so letting it fall through to `float()` made
    `--sort-by cross_date` rank every symbol as "missing" - i.e. sort
    alphabetically, identically in both directions.
    """
    if not key:
        return None
    value = sr.metrics.get(key)
    if value is None:
        return None
    # pd.Timestamp subclasses datetime, so check the narrower type first.
    if isinstance(value, datetime):
        if value != value:  # NaT
            return None
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return float(dt.timestamp())
    if isinstance(value, date):
        midnight = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
        return float(midnight.timestamp())
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN -> missing


@dataclass
class ScanResult:
    """Aggregate outcome of a scan."""

    results: List[SymbolResult] = field(default_factory=list)
    fetch_errors: Dict[str, str] = field(default_factory=dict)
    universe_size: int = 0
    data_source: str = ""
    filters_used: List[str] = field(default_factory=list)

    #: Ranking applied to `matches` (and therefore to every output form).
    sort_by: Optional[str] = "volume_ratio"
    sort_desc: bool = True

    #: {metric_key: header label} contributed by the active filters, so report
    #: columns follow the configured periods rather than hardcoded
    #: "SMA20"/"SMA50"/"SMA200".
    report_labels: Dict[str, str] = field(default_factory=dict)
    #: Extra (label, metric_key) report columns contributed by filters.
    extra_columns: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def matches(self) -> List[SymbolResult]:
        """Passing symbols, ranked by `sort_by` (highest conviction first).

        Symbols missing the metric sort last instead of being dropped, so a
        filter set that never emits `volume_ratio` still returns everything.
        Ties break on symbol for reproducible output.
        """
        found = [r for r in self.results if r.passed]
        if not self.sort_by:
            return sorted(found, key=lambda r: r.symbol)
        missing = float("inf")

        def key(sr: SymbolResult):
            v = _rank_value(sr, self.sort_by)
            if v is None:
                return (missing, sr.symbol)
            return ((-v if self.sort_desc else v), sr.symbol)

        return sorted(found, key=key)

    @property
    def evaluated(self) -> int:
        return len(self.results)

    @property
    def n_matches(self) -> int:
        return len(self.matches)

    def rows(self) -> List[Dict[str, Any]]:
        """Flat dicts for CSV/table output."""
        rows = []
        for r in self.matches:
            row = {"symbol": r.symbol}
            row.update({k: v for k, v in r.metrics.items()})
            rows.append(row)
        return rows

    def failed_rows(self) -> List[Dict[str, Any]]:
        rows = []
        for r in self.results:
            if r.passed:
                continue
            rows.append(
                {
                    "symbol": r.symbol,
                    "failed_filter": r.failed_filter or "data",
                    "reason": r.failure_reason,
                }
            )
        return rows


class Scanner:
    """Runs a configured screen over a universe of symbols."""

    def __init__(
        self,
        config: ScanConfig,
        *,
        source=None,
        progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.config = config
        self.filters: List[Filter] = config.build_filters()
        self.source = source
        self.progress = progress or (lambda msg: None)
        self._source_name = config.data_source

    # -- universe -------------------------------------------------------
    def _ticker_style(self) -> str:
        """Share-class notation the configured source expects.

        Alpaca wants BRK.B; Yahoo/yfinance wants BRK-B. Getting this wrong
        makes every class-share ticker silently fail to resolve.
        """
        return "dash" if self.config.data_source == "yfinance" else "dot"

    def resolve_universe(self) -> List[str]:
        """Build the symbol list: S&P 500, most-traded ETFs, or both.

        An explicit `symbols_file` always wins, so a custom list can be
        scanned without touching any of the scrapers.
        """
        cfg = self.config
        if cfg.universe == "file" or cfg.symbols_file:
            if not cfg.symbols_file:
                raise ValueError("universe='file' requires symbols_file")
            symbols = fetch_sp500_tickers(
                symbols_file=cfg.symbols_file, style=self._ticker_style()
            )
        elif cfg.universe in {"sp500", "etf", "both"}:
            symbols = []
            if cfg.universe in {"sp500", "both"}:
                symbols += fetch_sp500_tickers(
                    refresh=cfg.refresh_tickers,
                    cache_path=cfg.ticker_cache,
                    style=self._ticker_style(),
                )
            if cfg.universe in {"etf", "both"}:
                symbols += fetch_most_traded_etfs(
                    refresh=cfg.refresh_tickers,
                    cache_path=cfg.etf_cache,
                    limit=cfg.etf_limit,
                    refresh_days=cfg.etf_refresh_days,
                    on_warning=self.progress,
                )
            # De-dupe while keeping order (an ETF is never an S&P 500 member,
            # but a stale cache or an edited file could overlap).
            symbols = list(dict.fromkeys(symbols))
        else:
            raise ValueError(
                f"Unknown universe {cfg.universe!r} "
                "(expected: sp500, etf, both, file)"
            )
        if cfg.max_symbols:
            symbols = symbols[: cfg.max_symbols]
        return symbols

    # -- data -----------------------------------------------------------
    def _get_source(self):
        if self.source is not None:
            return self.source
        return build_source(self.config.data_source, **self.config.source_kwargs)

    # -- core -----------------------------------------------------------
    def run(self, symbols: Optional[Sequence[str]] = None) -> ScanResult:
        if symbols is None:
            symbols = self.resolve_universe()
        symbols = list(symbols)
        labels: Dict[str, str] = {}
        extras: List[Tuple[str, str]] = []
        for f in self.filters:
            labels.update(f.report_labels())
            extras.extend(f.report_columns())

        result = ScanResult(
            universe_size=len(symbols),
            filters_used=[type(f).name for f in self.filters],
            sort_by=self.config.sort_by,
            sort_desc=self.config.sort_desc,
            report_labels=labels,
            extra_columns=extras,
        )
        if not symbols:
            return result

        source = self._get_source()
        result.data_source = getattr(source, "name", self.config.data_source)

        self.progress(f"Fetching {len(symbols)} symbols from {result.data_source}...")
        batch = source.fetch_batch(symbols, limit=self.config.history_bars)
        result.fetch_errors = dict(batch.errors)

        frames: Dict[str, PriceFrame] = batch.frames
        self.progress(f"Got prices for {len(frames)} symbols; screening...")

        for i, sym in enumerate(symbols, 1):
            if self.config.verbose and i % 50 == 0:
                self.progress(f"  screened {i}/{len(symbols)}")
            frame = frames.get(sym)
            if frame is None:
                continue  # already recorded in fetch_errors
            result.results.append(self._evaluate(sym, frame))

        return result

    def _evaluate(self, symbol: str, frame: PriceFrame) -> SymbolResult:
        ctx = IndicatorContext(frame)
        sr = SymbolResult(symbol=symbol)
        try:
            for f in self.filters:
                fr = f.evaluate(ctx)
                sr.results.append(fr)
                if fr.passed:
                    sr.metrics.update(fr.metrics)
                if not fr.passed:
                    # Short-circuit: remaining filters need not run.
                    break
        except InsufficientHistory as exc:
            sr.skipped = f"insufficient history: {exc}"
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not kill the scan
            sr.skipped = f"{type(exc).__name__}: {exc}"
        return sr
