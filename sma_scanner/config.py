"""Configuration for a scan run.

The scan's behaviour - universe, data source, and especially the list of
filters - is expressed as data here, so changing screening conditions
means editing a config file (or passing CLI flags), not editing code.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from .filters import Filter, create_filter

#: Default screen: 20-day SMA breaking out above the 50-day SMA,
#: where the cross happened within the last 10 trading days,
#: and only while trading above the 200-day SMA (trend gate).
DEFAULT_FILTERS: List[Dict[str, Any]] = [
    {"name": "sma_breakout", "params": {"fast": 20, "slow": 50, "lookback": 10}},
    {"name": "above_sma", "params": {"period": 200}},
]


@dataclass
class ScanConfig:
    # -- universe -------------------------------------------------------
    #: "sp500" | "etf" | "both" | "file"
    universe: str = "both"
    symbols_file: Optional[str] = None  # used when universe == "file"
    ticker_cache: str = "data/sp500_tickers.csv"
    etf_cache: str = "data/most_traded_etfs.csv"
    etf_limit: int = 100  # how many most-traded ETFs to include
    etf_refresh_days: int = 7  # re-scrape the ETF list once cache is older
    refresh_tickers: bool = False  # force-refresh every list
    max_symbols: Optional[int] = None  # cap for quick test runs

    # -- data -----------------------------------------------------------
    data_source: str = "alpaca"
    source_kwargs: Dict[str, Any] = field(default_factory=dict)
    history_bars: int = 400  # daily bars to fetch per symbol

    # -- screening ------------------------------------------------------
    filters: List[Dict[str, Any]] = field(
        default_factory=lambda: [dict(f) for f in DEFAULT_FILTERS]
    )

    # -- output ---------------------------------------------------------
    output_csv: Optional[str] = None
    html_output: Optional[str] = None  # standalone HTML report
    show_failed: bool = False
    verbose: bool = False

    #: Metric the outputs are ranked by. "volume_ratio" = breakout-day
    #: volume vs its 20-day baseline, highest conviction first.
    sort_by: Optional[str] = "volume_ratio"
    sort_desc: bool = True

    # ------------------------------------------------------------------
    def build_filters(self) -> List[Filter]:
        """Instantiate configured filters in order."""
        out: List[Filter] = []
        for spec in self.filters:
            spec = dict(spec)
            name = spec.pop("name")
            params = spec.pop("params", {}) or {}
            out.append(create_filter(name, **params))
        return out

    def with_overrides(self, **kwargs: Any) -> "ScanConfig":
        """Return a copy with selected fields replaced (ignores None)."""
        clean = {k: v for k, v in kwargs.items() if v is not None}
        return replace(self, **clean)

    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "universe": self.universe,
            "symbols_file": self.symbols_file,
            "ticker_cache": self.ticker_cache,
            "etf_cache": self.etf_cache,
            "etf_limit": self.etf_limit,
            "etf_refresh_days": self.etf_refresh_days,
            "refresh_tickers": self.refresh_tickers,
            "max_symbols": self.max_symbols,
            "data_source": self.data_source,
            "source_kwargs": self.source_kwargs,
            "history_bars": self.history_bars,
            "filters": self.filters,
            "output_csv": self.output_csv,
            "html_output": self.html_output,
            "show_failed": self.show_failed,
            "verbose": self.verbose,
            "sort_by": self.sort_by,
            "sort_desc": self.sort_desc,
        }

    def save(self, path: str | os.PathLike) -> None:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScanConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown config keys: {sorted(unknown)}")
        return cls(**data)

    @classmethod
    def load(cls, path: Optional[str | os.PathLike]) -> "ScanConfig":
        """Load JSON (or YAML if PyYAML is installed). None -> defaults."""
        if not path:
            return cls()
        p = Path(str(path)).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"config file not found: {p}")
        text = p.read_text(encoding="utf-8")
        if p.suffix.lower() in {".yaml", ".yml"}:
            try:
                import yaml  # type: ignore
            except ImportError as exc:
                raise ImportError("YAML config requires `pip install pyyaml`") from exc
            data = yaml.safe_load(text) or {}
        else:
            data = json.loads(text or "{}")
        if not isinstance(data, dict):
            raise ValueError("config root must be an object")
        return cls.from_dict(data)
