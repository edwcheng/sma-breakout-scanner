"""Alpaca Markets historical market-data source.

Docs: https://docs.alpaca.markets/docs/historical-data-candles

Why Alpaca: a real REST API (no scraping), free tier, and - crucially -
it accepts *many symbols per request*, so scanning the whole S&P 500
costs a handful of HTTP calls instead of 500.

Paper vs live:
    Trading endpoints differ by account type:
        paper -> https://paper-api.alpaca.markets/v2
        live  -> https://api.alpaca.markets/v2
    Market data does NOT differ - historical bars are served from
    https://data.alpaca.markets for BOTH paper and live keys, which is
    why that is the default here. We only read data, never place orders.

Two transports, same output contract:
    * alpaca-py SDK  - official, handles paging/retries. Used when available.
    * direct REST    - dependency-free fallback (requests only).

Auth is read from env vars by default so keys never land in source:
    ALPACA_API_KEY      (key id)
    ALPACA_SECRET_KEY   (secret)
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd
import requests

from .base import DataSource, FetchBatchResult, PriceFrame, OHLCV_COLUMNS

# Alpaca returns exchange-local timestamps; daily bars are keyed to ET.
_MARKET_TZ = "America/New_York"


class AlpacaAuthError(RuntimeError):
    """Raised for 401/403 - retrying would never help."""


def _sdk_available() -> bool:
    try:
        import alpaca.data.historical  # noqa: F401

        return True
    except ImportError:
        return False


class AlpacaSource(DataSource):
    """Fetch daily OHLCV bars from Alpaca's Market Data v2 API."""

    name = "alpaca"
    supports_batching = True

    BASE_URL = "https://data.alpaca.markets"
    BARS_PATH = "/v2/stocks/bars"
    #: Alpaca caps `limit` at 10000 per request.
    MAX_LIMIT = 10_000

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        feed: str = "iex",
        adjustment: str = "split",
        timeframe: str = "1Day",
        batch_size: int = 50,
        limit: int = 1000,
        max_retries: int = 5,
        backoff_base: float = 1.5,
        timeout: float = 30.0,
        request_delay: float = 0.25,
        use_sdk: bool = True,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.api_key = api_key or os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID")
        self.secret_key = (
            secret_key or os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY")
        )
        if not self.api_key or not self.secret_key:
            raise AlpacaAuthError(
                "Missing Alpaca credentials. Set ALPACA_API_KEY and ALPACA_SECRET_KEY "
                "(or pass them explicitly)."
            )
        # Market data host by default; settable for proxies or experimentation.
        self.base_url = (base_url or os.getenv("ALPACA_BASE_URL") or self.BASE_URL).rstrip("/")
        self.feed = feed
        self.adjustment = adjustment
        self.timeframe = timeframe
        self.batch_size = batch_size
        self.limit = limit
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.timeout = timeout
        self.request_delay = request_delay
        self.session = session or requests.Session()
        self.use_sdk = bool(use_sdk) and _sdk_available()
        self._sdk_client: Any = None

    # ------------------------------------------------------------------
    # shared normalization
    # ------------------------------------------------------------------
    @staticmethod
    def _rows_to_frame(symbol: str, rows: Sequence[Dict[str, Any]]) -> Optional[PriceFrame]:
        """rows: [{'t','o','h','l','c','v'}, ...] -> PriceFrame."""
        if not rows:
            return None
        recs = [
            {
                "Open": float(r["o"]),
                "High": float(r["h"]),
                "Low": float(r["l"]),
                "Close": float(r["c"]),
                "Volume": float(r.get("v") or 0.0),
            }
            for r in rows
        ]
        idx = pd.to_datetime([r["t"] for r in rows], utc=True, format="ISO8601")
        idx = idx.tz_convert(_MARKET_TZ).tz_localize(None).normalize()

        df = pd.DataFrame(recs, index=idx, columns=list(OHLCV_COLUMNS))
        df = df[~df.index.duplicated(keep="last")].sort_index()
        if df.empty:
            return None
        return PriceFrame(symbol=symbol, df=df, source="alpaca")

    @staticmethod
    def _norm(symbol: str) -> str:
        """Alpaca class-share form: BRK-B -> BRK.B."""
        return symbol.strip().upper().replace("-", ".")

    def _fetch_chunk(
        self,
        chunk: List[str],
        start: Any,
        end: Any,
        window_bars: int,
        errors: Dict[str, str],
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch one chunk, bisecting on failure.

        Alpaca rejects an entire request if ANY symbol in it is invalid, so a
        single unresolvable ticker would otherwise cost us a whole chunk.
        Bisecting narrows the blame down to the one bad symbol.
        """
        req_limit = min(self.MAX_LIMIT, len(chunk) * window_bars)
        try:
            return (
                self._fetch_via_sdk(chunk, start, end, req_limit)
                if self.use_sdk
                else self._fetch_via_rest(chunk, start, end, req_limit)
            )
        except AlpacaAuthError:
            raise  # credentials are a real problem - fail loudly
        except Exception as exc:  # noqa: BLE001
            if len(chunk) == 1:
                errors[chunk[0]] = f"{type(exc).__name__}: {exc}"
                return {}
            mid = len(chunk) // 2
            left = self._fetch_chunk(chunk[:mid], start, end, window_bars, errors)
            right = self._fetch_chunk(chunk[mid:], start, end, window_bars, errors)
            left.update(right)
            return left

    @staticmethod
    def _fmt_dt(value: Any) -> str:
        if value is None:
            raise ValueError("start/end must not be None")
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.isoformat().replace("+00:00", "Z")
        if isinstance(value, date):
            return value.isoformat()
        return str(value)

    @staticmethod
    def _to_datetime(value: Any) -> Optional[datetime]:
        """Coerce to a tz-aware UTC datetime for the SDK."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
        return pd.to_datetime(value).to_pydatetime()

    def _finalize(self, collected: Dict[str, List[Dict[str, Any]]], symbols: List[str]):
        result = FetchBatchResult()
        for sym in symbols:
            rows = collected.get(sym) or []
            if not rows:
                result.add_error(sym, "no bars returned by Alpaca")
                continue
            try:
                frame = self._rows_to_frame(sym, rows)
            except (KeyError, ValueError, TypeError) as exc:
                result.add_error(sym, f"malformed bars: {exc}")
                continue
            if frame is None:
                result.add_error(sym, "empty history after normalization")
            else:
                result.add_frame(frame)
        return result

    # ------------------------------------------------------------------
    # transport 1: official SDK
    # ------------------------------------------------------------------
    def _get_sdk_client(self) -> Any:
        if self._sdk_client is None:
            from alpaca.data.historical import StockHistoricalDataClient

            try:
                self._sdk_client = StockHistoricalDataClient(
                    self.api_key, self.secret_key, url_override=self.base_url
                )
            except TypeError:
                # Older versions have no url_override kwarg.
                self._sdk_client = StockHistoricalDataClient(self.api_key, self.secret_key)
        return self._sdk_client

    def _fetch_via_sdk(
        self, symbols: List[str], start: Any, end: Any, limit: int
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Returns {symbol: [bar dicts]}. Raises AlpacaAuthError on bad creds."""
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        client = self._get_sdk_client()
        tf = TimeFrame.Day if str(self.timeframe).lower() in {"1day", "1d"} else self.timeframe
        collected: Dict[str, List[Dict[str, Any]]] = {s: [] for s in symbols}

        req_kwargs: Dict[str, Any] = {
            "symbol_or_symbols": symbols,
            "timeframe": tf,
            "start": self._to_datetime(start),
            "limit": limit,
            "adjustment": Adjustment(self.adjustment.lower()),
        }
        end_dt = self._to_datetime(end)
        if end_dt is not None:
            req_kwargs["end"] = end_dt
        # Free-tier keys are IEX-only; only send feed if it is not the default
        # to avoid tripping entitlement errors on restricted keys.
        try:
            req_kwargs["feed"] = DataFeed(self.feed.lower())
        except ValueError:
            pass

        request = StockBarsRequest(**req_kwargs)
        try:
            barset = client.get_stock_bars(request)
        except Exception as exc:  # noqa: BLE001 - surface as auth/data error
            msg = f"{type(exc).__name__}: {exc}"
            if "401" in msg or "403" in msg or "unauthorized" in msg.lower():
                raise AlpacaAuthError(
                    f"Alpaca rejected the credentials ({msg}). Historical bars come from "
                    "https://data.alpaca.markets (works with paper keys); the paper-api "
                    "host is for orders, not bars."
                ) from exc
            raise RuntimeError(f"Alpaca SDK bars request failed: {msg}") from exc

        data = getattr(barset, "data", None) or {}
        for sym, bars in data.items():
            if sym not in collected:
                continue
            for b in bars or []:
                collected[sym].append(
                    {
                        "t": b.timestamp.isoformat() if hasattr(b.timestamp, "isoformat")
                        else str(b.timestamp),
                        "o": b.open,
                        "h": b.high,
                        "l": b.low,
                        "c": b.close,
                        "v": b.volume,
                    }
                )
        return collected

    # ------------------------------------------------------------------
    # transport 2: direct REST
    # ------------------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Accept": "application/json",
        }

    def _get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        url = self.base_url + self.BARS_PATH
        last_err: Optional[str] = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(
                    url, headers=self._headers(), params=params, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_err = f"network error: {exc}"
                time.sleep(self.backoff_base ** attempt)
                continue

            code = resp.status_code
            if code in (401, 403):
                raise AlpacaAuthError(
                    f"Alpaca rejected the credentials (HTTP {code}) at {url}. "
                    "Check ALPACA_API_KEY / ALPACA_SECRET_KEY and that this key has "
                    "market-data entitlement. Note: historical bars come from "
                    "https://data.alpaca.markets (works with paper keys); the "
                    "paper-api host is for orders, not bars."
                )
            if code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else self.backoff_base ** (attempt + 1)
                time.sleep(min(wait, 60.0))
                last_err = "rate limited (429)"
                continue
            if 500 <= code < 600:
                time.sleep(self.backoff_base ** attempt)
                last_err = f"server error {code}"
                continue
            if code != 200:
                raise RuntimeError(f"Alpaca bars request failed: HTTP {code} - {resp.text[:200]}")
            try:
                return resp.json()
            except ValueError as exc:
                raise RuntimeError(f"Alpaca returned non-JSON payload: {exc}") from exc

        raise RuntimeError(f"Alpaca request failed after {self.max_retries} retries ({last_err})")

    def _fetch_via_rest(
        self, symbols: List[str], start: Any, end: Any, limit: int
    ) -> Dict[str, List[Dict[str, Any]]]:
        collected: Dict[str, List[Dict[str, Any]]] = {s: [] for s in symbols}
        start_s = self._fmt_dt(start)
        end_s = self._fmt_dt(end) if end is not None else None

        for i in range(0, len(symbols), self.batch_size):
            chunk = symbols[i : i + self.batch_size]
            page_token: Optional[str] = None
            while True:
                params: Dict[str, Any] = {
                    "symbols": ",".join(chunk),
                    "timeframe": self.timeframe,
                    "start": start_s,
                    "limit": limit,
                    "adjustment": self.adjustment,
                    "feed": self.feed,
                }
                if end_s:
                    params["end"] = end_s
                if page_token:
                    params["page_token"] = page_token

                payload = self._get(params)
                for sym, bars in (payload.get("bars") or {}).items():
                    if sym in collected:
                        collected[sym].extend(bars or [])
                page_token = payload.get("next_page_token")
                if not page_token:
                    break
            if self.request_delay:
                time.sleep(self.request_delay)
        return collected

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    @property
    def transport(self) -> str:
        return "alpaca-py-sdk" if self.use_sdk else "rest"

    def fetch_batch(
        self,
        symbols: Iterable[str],
        *,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        limit: Optional[int] = None,
        **kwargs,
    ) -> FetchBatchResult:
        """Fetch daily bars for many symbols, chunked and paginated.

        Symbols Alpaca has no data for land in `result.errors` rather than
        raising, so one bad ticker cannot abort a 500-name scan.
        """
        symbols = [s.strip().upper() for s in symbols if s and s.strip()]
        if not symbols:
            return FetchBatchResult()

        # Alpaca resolves class shares as BRK.B. Dash-style input (BRK-B, as
        # Yahoo uses) is accepted and normalized, then mapped back at the end
        # so the caller sees the symbols it asked for.
        alias: Dict[str, str] = {}
        requested: List[str] = []
        for s in symbols:
            norm = self._norm(s)
            if norm not in alias:
                alias[norm] = s
                requested.append(norm)

        bars = int(limit or self.limit)
        now = datetime.now(timezone.utc)

        # Bars are returned ASCENDING from `start` and truncated at `limit`.
        # Without an explicit `end` we would receive the OLDEST bars in the
        # window instead of the most recent ones - stale data, silently.
        if end is None:
            end = now
        # ~252 trading days per 365 calendar days, plus slack for holidays.
        window_days = int(bars * 1.55) + 15
        if start is None:
            start = now - timedelta(days=window_days)
        # How many bars one symbol can actually have inside that window.
        window_bars = int(window_days * 252 / 365) + 20

        # `limit` is a GLOBAL budget shared by every symbol in the request:
        # ask for 100 bars across 2 symbols and the first symbol can consume
        # all 100, leaving the rest empty. Size the chunk so every symbol fits.
        fits = max(1, self.MAX_LIMIT // window_bars)
        chunk_size = max(1, min(self.batch_size, fits))

        collected: Dict[str, List[Dict[str, Any]]] = {s: [] for s in requested}
        errors: Dict[str, str] = {}
        chunks = [requested[i : i + chunk_size] for i in range(0, len(requested), chunk_size)]

        for chunk in chunks:
            got = self._fetch_chunk(chunk, start, end, window_bars, errors)
            for s in chunk:
                collected[s] = collected.get(s, []) + (got.get(s) or [])
            if self.request_delay:
                time.sleep(self.request_delay)

        result = self._finalize(collected, requested)
        # Real transport errors are more informative than the generic
        # "no bars returned" message finalize() would produce.
        for sym, msg in errors.items():
            if sym not in result.frames:
                result.errors[sym] = msg

        # Defensive: if the global budget still ran short, Alpaca quietly
        # serves OLDER bars to later symbols rather than erroring. Drop any
        # symbol whose last bar trails the batch's newest by over a week,
        # so a truncated series can never produce a fake signal.
        if result.frames:
            newest = max(f.last_date() for f in result.frames.values())
            for sym, frame in list(result.frames.items()):
                if (newest - frame.last_date()).days > 10:
                    del result.frames[sym]
                    result.errors[sym] = (
                        f"stale/truncated series: last bar {frame.last_date().date()} "
                        f"vs newest {newest.date()}"
                    )

        # Report under the symbols the caller asked for, not the normalized ones.
        result.frames = {
            alias.get(k, k): PriceFrame(symbol=alias.get(k, k), df=f.df, source=f.source)
            for k, f in result.frames.items()
        }
        result.errors = {alias.get(k, k): v for k, v in result.errors.items()}
        return result
