"""Most-traded US ETF ticker list (TradingView).

TradingView publishes a "most traded" ETF table sorted by *dollar* volume
(Price x Volume), which is a better liquidity ranking than raw share count:
a $5 ETF trading 10M shares is not as tradeable as a $500 ETF trading 1M.

The page is server-rendered - the table ships in the initial HTML - so we
can scrape it without a browser. Results are cached to disk because the
list changes slowly, but membership does drift, so the cache carries a
timestamp and goes stale after `refresh_days` (7 by default). A failed
refresh degrades to the stale cache rather than aborting the scan.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

TRADINGVIEW_URL = "https://www.tradingview.com/markets/etfs/funds-most-traded/"
UA = "sma-scanner/1.0 (market data research) python-requests"

DEFAULT_CACHE = Path(__file__).resolve().parents[2] / "data" / "most_traded_etfs.csv"

#: Cache older than this is re-scraped on the next run.
DEFAULT_REFRESH_DAYS = 7

#: Rows carry data-rowkey="EXCHANGE:TICKER"; the tbody marker scopes the
#: search to the screener table and not the rest of the page.
_TBODY_MARKER = 'data-testid="selectable-rows-table-body"'
_ROWKEY_RE = re.compile(r'data-rowkey="([A-Za-z0-9]+):([A-Za-z0-9.\-^=]+)"')

#: Exchanges we treat as US-listed. Guards against the page ever mixing
#: in a foreign listing (it is the /markets/etfs/ US view today).
US_EXCHANGES = {"AMEX", "NASDAQ", "NYSE", "CBOE", "BATS", "NYSE ARCA", "AMEX ARCA"}


def _fetch_html(timeout: float = 30.0) -> str:
    import requests

    resp = requests.get(
        TRADINGVIEW_URL, headers={"User-Agent": UA}, timeout=timeout
    )
    resp.raise_for_status()
    return resp.text


def parse_most_traded(html: str, *, limit: int = 100, us_only: bool = True) -> List[str]:
    """Extract tickers from the screener HTML, in the page's own rank order.

    The page returns rows already sorted by Price x Volume descending, so
    truncating to `limit` preserves "most traded" semantics. Returns an
    empty list if the markup changed and nothing matched - callers treat
    that as a scrape failure rather than an empty universe.
    """
    start = html.find(_TBODY_MARKER)
    if start < 0:
        return []
    end = html.find("</tbody>", start)
    segment = html[start: end if end > 0 else len(html)]

    out: List[str] = []
    for exchange, ticker in _ROWKEY_RE.findall(segment):
        if us_only and exchange.upper() not in US_EXCHANGES:
            continue
        sym = ticker.strip().upper()
        if sym and sym not in out:
            out.append(sym)
        if len(out) >= limit:
            break
    return out[:limit]


def _read_cache(path: Path) -> Tuple[List[str], Optional[datetime]]:
    """Return (tickers, cache_timestamp). Timestamp is None if unrecorded."""
    import pandas as pd

    df = pd.read_csv(path)
    if df.empty:
        return [], None
    syms = [str(s).strip().upper() for s in df[df.columns[0]].tolist()]
    syms = [s for s in syms if s and s.lower() != "nan"]

    stamp = None
    if len(df.columns) > 1:
        raw = str(df[df.columns[1]].iloc[0])
        try:
            stamp = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
        except ValueError:
            stamp = None
    return syms, stamp


def _write_cache(path: Path, symbols: List[str]) -> None:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(
        {
            "Symbol": symbols,
            "FetchedAt": [datetime.now(timezone.utc).isoformat()] * len(symbols),
        }
    )
    df.to_csv(path, index=False)


def _cache_age_days(stamp: Optional[datetime], now: datetime) -> float:
    if stamp is None:
        return float("inf")  # undated cache -> treat as stale
    return (now - stamp).total_seconds() / 86400.0


def fetch_most_traded_etfs(
    *,
    refresh: bool = False,
    cache_path: str | os.PathLike = DEFAULT_CACHE,
    limit: int = 100,
    refresh_days: int = DEFAULT_REFRESH_DAYS,
    on_warning=None,
) -> List[str]:
    """Return the most-traded US ETF tickers: cache -> scrape -> stale cache.

    Args:
        refresh: ignore cache age and re-scrape now.
        cache_path: CSV cache location (also stores the fetch timestamp).
        limit: how many ETFs to keep (page rank order = most traded first).
        refresh_days: re-scrape once the cache is older than this.
        on_warning: optional callable(str) for non-fatal problems.

    Raises:
        RuntimeError: if the scrape fails and no usable cache exists.
    """
    warn = on_warning or (lambda msg: None)
    cache = Path(cache_path).expanduser()
    now = datetime.now(timezone.utc)

    cached: List[str] = []
    if cache.exists():
        try:
            cached, stamp = _read_cache(cache)
        except Exception as exc:  # noqa: BLE001 - corrupt cache is not fatal
            warn(f"ETF cache unreadable ({exc}); will re-scrape")
            cached, stamp = [], None
        age = _cache_age_days(stamp, now)
        fresh = bool(cached) and not refresh and age <= refresh_days
        if fresh and len(cached) >= limit:
            return cached[:limit]
        if fresh:
            # Fresh but too short to satisfy the request - e.g. it was written
            # by an earlier run with a smaller `--etf-limit`. Returning it would
            # silently scan a smaller universe than the caller asked for.
            warn(
                f"ETF cache holds {len(cached)} tickers but {limit} were "
                "requested; re-scraping"
            )

    try:
        scraped = parse_most_traded(_fetch_html(), limit=limit)
    except Exception as exc:  # noqa: BLE001 - network/markup problems
        if cached:
            warn(f"ETF refresh failed ({exc}); using cache from {cache}")
            return cached[:limit]
        raise RuntimeError(f"Could not fetch ETF list: {exc}") from exc

    if not scraped:
        if cached:
            warn("ETF page returned no tickers (markup changed?); using cache")
            return cached[:limit]
        raise RuntimeError("ETF page returned no tickers - markup may have changed")

    try:
        _write_cache(cache, scraped)
    except Exception as exc:  # noqa: BLE001 - caching is best-effort
        warn(f"Could not write ETF cache ({exc})")
    return scraped[:limit]
