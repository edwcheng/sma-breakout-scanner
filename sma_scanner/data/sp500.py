"""S&P 500 constituent ticker list.

Fetched from Wikipedia and cached to disk, because index membership
changes rarely and we do not want a network dependency (or a scraper
breakage) to block every scan run.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List

import pandas as pd

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
UA = "sma-scanner/1.0 (market data research; https://github.com/) python-requests"

DEFAULT_CACHE = Path(__file__).resolve().parents[2] / "data" / "sp500_tickers.csv"

#: The real index is ~500 names. Far below this means the cache was truncated
#: or half-written - re-scrape instead of quietly scanning a short universe.
MIN_PLAUSIBLE = 400


def _normalize(raw_symbols: List[str], style: str = "dot") -> List[str]:
    """Clean tickers into a single share-class convention.

    Wikipedia writes class shares as BRK.B. Alpaca wants that dot form;
    Yahoo/yfinance wants BRK-B. Getting this wrong means every class-share
    name silently fails to resolve on one provider or the other.

    style:
        "dot"  -> BRK.B  (Alpaca; also Wikipedia's own notation)
        "dash" -> BRK-B  (yfinance / Yahoo)
    """
    out: List[str] = []
    for s in raw_symbols:
        if not isinstance(s, str):
            continue
        s = s.strip()
        if not s or s.lower() in {"nan", "symbol"}:
            continue
        s = re.sub(r"\s+", "", s)
        if style == "dash":
            s = s.replace(".", "-")
        else:  # "dot"
            s = s.replace("-", ".")
        s = s.upper()
        if s:
            out.append(s)
    return sorted(dict.fromkeys(out))


def _from_wikipedia(style: str = "dot") -> List[str]:
    """Scrape the constituents table. Raises on failure - caller handles.

    Wikipedia rejects the default urllib User-Agent with HTTP 403, so we
    fetch the HTML ourselves with a descriptive UA and hand the markup
    to pandas.
    """
    import io

    import requests

    resp = requests.get(
        WIKI_URL,
        headers={"User-Agent": UA},
        timeout=30,
    )
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))
    for tbl in tables:
        cols = {str(c).strip().lower() for c in tbl.columns}
        if "symbol" in cols:
            sym_col = next(c for c in tbl.columns if str(c).strip().lower() == "symbol")
            tickers = _normalize(tbl[sym_col].astype(str).tolist(), style)
            if len(tickers) >= MIN_PLAUSIBLE:  # sanity: real list is ~500
                return tickers
    raise RuntimeError("Wikipedia page parsed, but no constituents table found")


def _read_cache(path: Path, style: str = "dot") -> List[str]:
    df = pd.read_csv(path)
    col = df.columns[0]
    return _normalize(df[col].astype(str).tolist(), style)


def fetch_sp500_tickers(
    *,
    refresh: bool = False,
    cache_path: str | os.PathLike = DEFAULT_CACHE,
    symbols_file: str | os.PathLike | None = None,
    style: str = "dot",
) -> List[str]:
    """Return S&P 500 tickers, preferring: explicit file > cache > Wikipedia.

    Args:
        refresh: force a re-scrape of Wikipedia even if a cache exists.
        cache_path: where to read/write the cached CSV.
        symbols_file: optional user-supplied file (CSV or newline-delimited)
            to scan an arbitrary universe instead of the index.

    Raises:
        RuntimeError: if no source of tickers is available.
    """
    # 1. Explicit universe file always wins (lets you scan custom lists).
    if symbols_file:
        p = Path(symbols_file).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"symbols file not found: {p}")
        if p.suffix.lower() in {".csv", ".tsv"}:
            df = pd.read_csv(p, sep="\t" if p.suffix.lower() == ".tsv" else ",")
            ticks = _normalize(df[df.columns[0]].astype(str).tolist(), style)
        else:
            ticks = _normalize(p.read_text(encoding="utf-8").splitlines(), style)
        if not ticks:
            raise ValueError(f"no tickers parsed from {p}")
        return ticks

    cache = Path(cache_path).expanduser()

    # 2. Cache hit (unless refreshing) - but only if it looks complete.
    cached: List[str] = []
    if cache.exists() and not refresh:
        try:
            cached = _read_cache(cache, style)
        except Exception:  # noqa: BLE001 - unreadable cache is just a miss
            cached = []
        if len(cached) >= MIN_PLAUSIBLE:
            return cached
        # Too short to trust: fall through and re-scrape.

    # 3. Scrape and cache. If the scrape fails we still prefer a short cache
    # over no universe at all.
    try:
        ticks = _from_wikipedia(style)
    except Exception:
        if cached:
            return cached
        raise
    cache.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"Symbol": ticks}).to_csv(cache, index=False)
    return ticks
