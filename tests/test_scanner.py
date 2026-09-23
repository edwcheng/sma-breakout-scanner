"""Unit tests for indicator math, crossover detection, and filter behaviour.

Run:  python3.11 -m unittest discover -s tests -v
  or: pytest tests/
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sma_scanner import scanner as scanner_mod  # noqa: E402
from sma_scanner.config import DEFAULT_FILTERS, ScanConfig  # noqa: E402
from sma_scanner.data import PriceFrame, SyntheticSource  # noqa: E402
from sma_scanner.data import etf_list, sp500  # noqa: E402
from sma_scanner.data.csv_source import CsvSource  # noqa: E402
from sma_scanner.data.etf_list import (  # noqa: E402
    fetch_most_traded_etfs,
    parse_most_traded,
)
from sma_scanner.data.yfinance_source import _coerce_ohlcv, _resolve_close  # noqa: E402
from sma_scanner.filters import available_filters  # noqa: E402
from sma_scanner.filters.builtin import (  # noqa: E402
    AboveSmaFilter,
    MinPriceFilter,
    SmaBreakoutFilter,
)
from sma_scanner.indicators import (  # noqa: E402
    IndicatorContext,
    detect_crossovers,
    rsi,
    sma,
)
from sma_scanner.reporter import print_summary  # noqa: E402
from sma_scanner.scanner import ScanResult, Scanner, SymbolResult  # noqa: E402
from sma_scanner.summary import (  # noqa: E402
    build_summary,
    cache_health,
    check,
    format_health_line,
    format_markdown,
)
from sma_scanner.summary import main as summary_main  # noqa: E402


def make_frame(symbol: str, closes, volume=1_000_000.0) -> PriceFrame:
    """`volume` may be a scalar or a per-bar sequence."""
    vols = list(volume) if isinstance(volume, (list, tuple)) else [volume] * len(closes)
    # freq="D" rather than bdate_range: with `end` + `periods`, bdate_range
    # can return a shorter index than requested.
    idx = pd.date_range(end=pd.Timestamp("2024-06-01"), periods=len(closes), freq="D")
    df = pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": vols,
        },
        index=idx,
    )
    return PriceFrame(symbol=symbol, df=df, source="test")


class TestSma(unittest.TestCase):
    def test_matches_manual_calculation(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        out = sma(s, 3)
        self.assertTrue(out.iloc[:2].isna().all())
        self.assertAlmostEqual(out.iloc[2], 2.0)  # (1+2+3)/3
        self.assertAlmostEqual(out.iloc[3], 3.0)
        self.assertAlmostEqual(out.iloc[4], 4.0)

    def test_rejects_non_positive_period(self):
        with self.assertRaises(ValueError):
            sma(pd.Series([1.0, 2.0]), 0)


class TestCrossoverDetection(unittest.TestCase):
    def test_step_up_produces_golden_cross(self):
        # Flat, then a step up: SMA20 reacts faster, so it crosses above SMA50.
        closes = [100.0] * 60 + [110.0] * 30
        s = pd.Series(closes)
        fast, slow = sma(s, 20), sma(s, 50)
        events = detect_crossovers(fast, slow, direction="up")

        self.assertTrue(events, "expected a golden cross")
        e = events[0]
        # The defining property of a cross: above now, not above on the prior bar.
        self.assertGreater(fast.iloc[e.bar_index], slow.iloc[e.bar_index])
        self.assertLessEqual(fast.iloc[e.bar_index - 1], slow.iloc[e.bar_index - 1])
        self.assertEqual(e.direction, "up")

    def test_flat_series_never_crosses(self):
        # Perfectly flat => the SMAs are equal, and a cross needs strict >.
        s = pd.Series([100.0] * 120)
        events = detect_crossovers(sma(s, 20), sma(s, 50), direction="up")
        self.assertEqual(events, [])

    def test_lookback_excludes_stale_cross(self):
        # Cross exists at bar ~60 but the series has since flattened out,
        # so nothing crosses inside a recent window.
        closes = [100.0] * 60 + [110.0] * 60
        s = pd.Series(closes)
        recent = detect_crossovers(sma(s, 20), sma(s, 50), direction="up", lookback=10)
        alltime = detect_crossovers(sma(s, 20), sma(s, 50), direction="up")
        self.assertEqual(recent, [])
        self.assertTrue(alltime, "cross should exist when no lookback is applied")

    def test_step_down_produces_death_cross(self):
        closes = [100.0] * 60 + [90.0] * 30
        s = pd.Series(closes)
        events = detect_crossovers(sma(s, 20), sma(s, 50), direction="down")
        self.assertTrue(events, "expected a death cross")
        self.assertEqual(events[0].direction, "down")


class TestSmaBreakoutFilter(unittest.TestCase):
    def test_fresh_breakout_passes(self):
        # Cross happens 4 bars from the end - inside the default window.
        frame = make_frame("FRESH", [100.0] * 60 + [110.0] * 5)
        ctx = IndicatorContext(frame)
        res = SmaBreakoutFilter(fast=20, slow=50, lookback=10).evaluate(ctx)
        self.assertTrue(res.passed, res.reason)
        self.assertLessEqual(res.metrics["bars_since_cross"], 10)

    def test_stale_breakout_is_rejected(self):
        closed = [100.0] * 60 + [110.0] * 60
        frame = make_frame("STALE", closed)
        ctx = IndicatorContext(frame)
        res = SmaBreakoutFilter(fast=20, slow=50, lookback=10).evaluate(ctx)
        self.assertFalse(res.passed)
        self.assertIn("no up-cross", res.reason)

    def test_reversed_signal_is_rejected(self):
        # Golden cross followed by a death cross is not a live breakout.
        closes = [100.0] * 60 + [110.0] * 40 + [90.0] * 40
        ctx = IndicatorContext(make_frame("REVERSED", closes))
        res = SmaBreakoutFilter(fast=20, slow=50, lookback=80).evaluate(ctx)
        self.assertFalse(res.passed)
        self.assertIn("reversed", res.reason)

    def test_min_spread_gate(self):
        closes = [100.0] * 60 + [110.0] * 5
        ctx = IndicatorContext(make_frame("SPRD", closes))
        # An impossibly wide required spread must reject an otherwise valid cross.
        res = SmaBreakoutFilter(fast=20, slow=50, lookback=10, min_spread_pct=99.0).evaluate(ctx)
        self.assertFalse(res.passed)
        self.assertIn("spread", res.reason)

    def test_insufficient_history_is_raised(self):
        ctx = IndicatorContext(make_frame("SHORT", [100.0] * 30))
        with self.assertRaises(Exception):
            SmaBreakoutFilter(fast=20, slow=50, lookback=10).evaluate(ctx)


class TestRegistryAndConfig(unittest.TestCase):
    def test_builtin_filters_registered(self):
        names = set(available_filters())
        for expected in {"sma_breakout", "min_price", "min_avg_volume", "above_sma", "rsi_range"}:
            self.assertIn(expected, names)

    def test_config_roundtrip(self):
        cfg = ScanConfig(history_bars=250, max_symbols=10)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cfg.json")
            cfg.save(path)
            loaded = ScanConfig.load(path)
        self.assertEqual(loaded.history_bars, 250)
        self.assertEqual(loaded.max_symbols, 10)
        self.assertEqual(loaded.data_source, cfg.data_source)


class TestVolumeMetrics(unittest.TestCase):
    def test_ratio_measured_against_pre_breakout_baseline(self):
        """Breakout-day volume is compared to the 20 bars BEFORE it, and the
        spike bar must not inflate its own baseline."""
        closes = [100.0] * 60 + [110.0] * 5   # golden cross lands on bar 60
        vols = [1_000_000.0] * 65
        vols[60] = 3_000_000.0                # breakout bar trades 3x
        ctx = IndicatorContext(make_frame("VOL", closes, vols))
        res = SmaBreakoutFilter(fast=20, slow=50, lookback=10).evaluate(ctx)

        self.assertTrue(res.passed, res.reason)
        self.assertAlmostEqual(res.metrics["cross_volume"], 3_000_000.0)
        self.assertAlmostEqual(res.metrics["pre_cross_avg_volume"], 1_000_000.0)
        self.assertAlmostEqual(res.metrics["volume_ratio"], 3.0, places=2)

    def test_min_volume_ratio_gate(self):
        closes = [100.0] * 60 + [110.0] * 5
        vols = [1_000_000.0] * 65
        vols[60] = 1_200_000.0  # only 1.2x - weak conviction
        ctx = IndicatorContext(make_frame("WEAK", closes, vols))
        res = SmaBreakoutFilter(
            fast=20, slow=50, lookback=10, min_volume_ratio=2.0
        ).evaluate(ctx)
        self.assertFalse(res.passed)
        self.assertIn("volume", res.reason)


class TestAboveSmaFilter(unittest.TestCase):
    def test_price_below_long_sma_rejected(self):
        closes = [200.0 - 0.2 * i for i in range(250)]  # steady decline
        ctx = IndicatorContext(make_frame("DOWN", closes))
        res = AboveSmaFilter(period=200).evaluate(ctx)
        self.assertFalse(res.passed)
        self.assertIn("SMA200", res.reason)

    def test_price_above_long_sma_passes(self):
        closes = [100.0 + 0.2 * i for i in range(250)]  # steady uptrend
        ctx = IndicatorContext(make_frame("UP", closes))
        res = AboveSmaFilter(period=200).evaluate(ctx)
        self.assertTrue(res.passed, res.reason)


class TestScannerIntegration(unittest.TestCase):
    def test_detects_engineered_breakouts(self):
        """End-to-end: only the symbols engineered to break out should match.

        Pins the chain to sma_breakout alone so this isolates crossover
        detection from any other gate.
        """
        source = SyntheticSource(days=400, seed=11, breakout_symbols=["AAA", "CCC"])
        cfg = ScanConfig(filters=[{"name": "sma_breakout", "params": {}}])
        scanner = Scanner(cfg, source=source)
        result = scanner.run(["AAA", "BBB", "CCC", "DDD"])

        self.assertEqual(result.evaluated, 4)
        self.assertEqual(sorted(m.symbol for m in result.matches), ["AAA", "CCC"])
        # Every match must carry the reporting metrics.
        for m in result.matches:
            self.assertIn("cross_date", m.metrics)
            self.assertIn("spread_pct", m.metrics)

    def test_engineered_breakouts_survive_default_trend_gate(self):
        """Regression: the engineered "V" must clear the default 200-day gate.

        When the dip spanned the whole series it dragged the 200-day average
        down to meet the recovering price, so price finished below its own
        long-term trend and the default filter set matched nothing - the
        offline demo looked broken unless the gate was removed by hand.
        """
        source = SyntheticSource(days=400, seed=11, breakout_symbols=["AAA", "CCC"])
        cfg = ScanConfig()  # default: sma_breakout + above_sma(200)
        result = Scanner(cfg, source=source).run(["AAA", "BBB", "CCC", "DDD"])

        self.assertEqual(sorted(m.symbol for m in result.matches), ["AAA", "CCC"])

    def test_trend_gate_narrows_results(self):
        """Adding the 200-day gate must not widen the result set."""
        source = SyntheticSource(days=400, seed=11, breakout_symbols=["AAA", "CCC"])
        symbols = ["AAA", "BBB", "CCC", "DDD"]

        plain = Scanner(ScanConfig(filters=[{"name": "sma_breakout", "params": {}}]), source=source)
        gated = Scanner(ScanConfig(), source=source)  # default = breakout + above_sma(200)

        n_plain = plain.run(symbols).n_matches
        n_gated = gated.run(symbols).n_matches
        self.assertLessEqual(n_gated, n_plain)


class TestResultSorting(unittest.TestCase):
    """Every output form reads ScanResult.matches, so ranking lives there."""

    def _result(self, specs, **kwargs):
        res = ScanResult(**kwargs)
        for sym, ratio in specs:
            sr = SymbolResult(symbol=sym)
            if ratio is not None:
                sr.metrics["volume_ratio"] = ratio
            res.results.append(sr)
        return res

    def test_ranked_by_volume_ratio_descending(self):
        res = self._result([("LOW", 0.5), ("HIGH", 3.0), ("MID", 1.2)])
        self.assertEqual([m.symbol for m in res.matches], ["HIGH", "MID", "LOW"])

    def test_rows_follow_the_same_order(self):
        res = self._result([("LOW", 0.5), ("HIGH", 3.0), ("MID", 1.2)])
        self.assertEqual([r["symbol"] for r in res.rows()], ["HIGH", "MID", "LOW"])

    def test_missing_metric_sorts_last_not_dropped(self):
        res = self._result([("NONE", None), ("HIGH", 2.0), ("LOW", 0.4)])
        self.assertEqual([m.symbol for m in res.matches], ["HIGH", "LOW", "NONE"])

    def test_nan_is_treated_as_missing(self):
        res = self._result([("NAN", float("nan")), ("HIGH", 2.0)])
        self.assertEqual([m.symbol for m in res.matches], ["HIGH", "NAN"])

    def test_ties_break_on_symbol(self):
        res = self._result([("ZZZ", 1.5), ("AAA", 1.5)])
        self.assertEqual([m.symbol for m in res.matches], ["AAA", "ZZZ"])

    def test_ascending_direction(self):
        res = self._result([("LOW", 0.5), ("HIGH", 3.0)], sort_desc=False)
        self.assertEqual([m.symbol for m in res.matches], ["LOW", "HIGH"])

    def test_disabled_sort_returns_alphabetical(self):
        res = self._result([("ZZZ", 9.0), ("AAA", 0.1)], sort_by=None)
        self.assertEqual([m.symbol for m in res.matches], ["AAA", "ZZZ"])

    def test_alternate_metric(self):
        res = ScanResult(sort_by="spread_pct", sort_desc=True)
        for sym, spread in [("A", 1.0), ("B", 5.0)]:
            sr = SymbolResult(symbol=sym)
            sr.metrics["spread_pct"] = spread
            res.results.append(sr)
        self.assertEqual([m.symbol for m in res.matches], ["B", "A"])

    def _date_result(self, desc):
        """Symbols whose alphabetical order differs from their date order."""
        res = ScanResult(sort_by="cross_date", sort_desc=desc)
        for sym, day in [("AAA", "2026-02-01"), ("ZZZ", "2026-09-01"), ("MMM", "2026-05-01")]:
            sr = SymbolResult(symbol=sym)
            sr.metrics["cross_date"] = pd.Timestamp(day)
            res.results.append(sr)
        return res

    def test_date_metric_ranks_chronologically_descending(self):
        """Regression: cross_date used to rank as "missing" -> alphabetical."""
        res = self._date_result(desc=True)
        self.assertEqual([m.symbol for m in res.matches], ["ZZZ", "MMM", "AAA"])

    def test_date_metric_ranks_chronologically_ascending(self):
        res = self._date_result(desc=False)
        self.assertEqual([m.symbol for m in res.matches], ["AAA", "MMM", "ZZZ"])

    def test_date_metric_is_not_alphabetical_in_both_directions(self):
        # The tell-tale of the old bug: ascending and descending agreed.
        desc = [m.symbol for m in self._date_result(True).matches]
        asc = [m.symbol for m in self._date_result(False).matches]
        self.assertEqual(desc, list(reversed(asc)))

    def test_plain_date_object_ranks_too(self):
        res = ScanResult(sort_by="cross_date", sort_desc=True)
        for sym, d in [("EARLY", date(2026, 1, 1)), ("LATE", date(2026, 12, 1))]:
            sr = SymbolResult(symbol=sym)
            sr.metrics["cross_date"] = d
            res.results.append(sr)
        self.assertEqual([m.symbol for m in res.matches], ["LATE", "EARLY"])


class TestEtfList(unittest.TestCase):
    """TradingView screener parsing + cache freshness rules."""

    @staticmethod
    def _html(rows):
        body = "".join(
            f'<tr data-rowkey="{ex}:{sym}"><td>x</td></tr>' for ex, sym in rows
        )
        return f'<tbody data-testid="selectable-rows-table-body">{body}</tbody>'

    @staticmethod
    def _backdate(cache: Path, symbols, when="2020-01-01T00:00:00+00:00"):
        pd.DataFrame({"Symbol": symbols, "FetchedAt": [when] * len(symbols)}).to_csv(
            cache, index=False
        )

    def test_parses_in_page_rank_order(self):
        html = self._html([("NASDAQ", "QQQ"), ("AMEX", "SPY"), ("AMEX", "VOO")])
        self.assertEqual(parse_most_traded(html), ["QQQ", "SPY", "VOO"])

    def test_limit_truncates_most_traded(self):
        html = self._html([("AMEX", f"E{i}") for i in range(20)])
        self.assertEqual(parse_most_traded(html, limit=5), ["E0", "E1", "E2", "E3", "E4"])

    def test_drops_duplicates(self):
        html = self._html([("AMEX", "SPY"), ("AMEX", "SPY"), ("AMEX", "QQQ")])
        self.assertEqual(parse_most_traded(html), ["SPY", "QQQ"])

    def test_skips_non_us_exchanges(self):
        html = self._html([("LSE", "VUSA"), ("AMEX", "SPY")])
        self.assertEqual(parse_most_traded(html), ["SPY"])

    def test_returns_empty_when_markup_missing(self):
        self.assertEqual(parse_most_traded("<html><body>nothing</body></html>"), [])

    def test_scrapes_and_caches_on_first_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "etfs.csv"
            with mock.patch.object(
                etf_list, "_fetch_html", return_value=self._html([("AMEX", "SPY")])
            ) as fh:
                out = fetch_most_traded_etfs(cache_path=cache)
            fh.assert_called_once()
            self.assertEqual(out, ["SPY"])
            self.assertTrue(cache.exists())

    def test_empty_parse_raises_when_no_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(etf_list, "_fetch_html", return_value="<html/>"):
                with self.assertRaises(RuntimeError):
                    fetch_most_traded_etfs(cache_path=Path(tmp) / "etfs.csv")

    def test_fresh_cache_avoids_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "etfs.csv"
            etf_list._write_cache(cache, ["SPY", "QQQ"])
            # limit matches what the cache holds, so it can satisfy the request
            with mock.patch.object(etf_list, "_fetch_html") as fh:
                out = fetch_most_traded_etfs(cache_path=cache, limit=2, refresh_days=7)
            fh.assert_not_called()
            self.assertEqual(out, ["SPY", "QQQ"])

    def test_stale_cache_triggers_rescrape(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "etfs.csv"
            self._backdate(cache, ["OLD"])
            with mock.patch.object(
                etf_list, "_fetch_html", return_value=self._html([("AMEX", "SPY")])
            ):
                out = fetch_most_traded_etfs(cache_path=cache, refresh_days=7)
            self.assertEqual(out, ["SPY"])
            # and the cache is rewritten with a fresh timestamp
            self.assertEqual(etf_list._read_cache(cache)[0], ["SPY"])
            self.assertLess(etf_list._cache_age_days(etf_list._read_cache(cache)[1],
                                                     datetime.now(timezone.utc)), 1)

    def test_refresh_flag_ignores_fresh_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "etfs.csv"
            etf_list._write_cache(cache, ["OLD"])
            with mock.patch.object(
                etf_list, "_fetch_html", return_value=self._html([("AMEX", "SPY")])
            ):
                out = fetch_most_traded_etfs(cache_path=cache, refresh=True)
            self.assertEqual(out, ["SPY"])

    def test_cache_shorter_than_limit_triggers_rescrape(self):
        """Regression: a 50-row cache silently satisfied `--etf-limit 100`."""
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "etfs.csv"
            etf_list._write_cache(cache, [f"OLD{i}" for i in range(50)])
            warnings = []
            html = self._html([("AMEX", f"NEW{i}") for i in range(120)])
            with mock.patch.object(etf_list, "_fetch_html", return_value=html):
                out = fetch_most_traded_etfs(
                    cache_path=cache, limit=100, refresh_days=7,
                    on_warning=warnings.append,
                )
            self.assertEqual(len(out), 100)
            self.assertEqual(out[0], "NEW0")
            self.assertTrue(any("re-scraping" in w for w in warnings))

    def test_cache_at_or_above_limit_is_still_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "etfs.csv"
            etf_list._write_cache(cache, [f"E{i}" for i in range(100)])
            with mock.patch.object(etf_list, "_fetch_html") as fh:
                out = fetch_most_traded_etfs(
                    cache_path=cache, limit=50, refresh_days=7
                )
            fh.assert_not_called()
            self.assertEqual(len(out), 50)

    def test_failed_refresh_falls_back_to_stale_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "etfs.csv"
            self._backdate(cache, ["SPY"])
            warnings = []
            with mock.patch.object(
                etf_list, "_fetch_html", side_effect=OSError("offline")
            ):
                out = fetch_most_traded_etfs(
                    cache_path=cache, refresh_days=7, on_warning=warnings.append
                )
            self.assertEqual(out, ["SPY"])
            self.assertTrue(any("offline" in w for w in warnings))


class TestUniverseComposition(unittest.TestCase):
    def _scanner(self, universe):
        return Scanner(ScanConfig(universe=universe))

    def test_both_combines_sp500_and_etfs(self):
        with mock.patch.object(scanner_mod, "fetch_sp500_tickers",
                               return_value=["AAPL"]), \
             mock.patch.object(scanner_mod, "fetch_most_traded_etfs",
                               return_value=["SPY"]):
            self.assertEqual(self._scanner("both").resolve_universe(), ["AAPL", "SPY"])

    def test_sp500_only_skips_etf_fetch(self):
        with mock.patch.object(scanner_mod, "fetch_sp500_tickers",
                               return_value=["AAPL"]), \
             mock.patch.object(scanner_mod, "fetch_most_traded_etfs") as etf:
            self.assertEqual(self._scanner("sp500").resolve_universe(), ["AAPL"])
        etf.assert_not_called()

    def test_etf_only_skips_sp500_fetch(self):
        with mock.patch.object(scanner_mod, "fetch_sp500_tickers") as sp, \
             mock.patch.object(scanner_mod, "fetch_most_traded_etfs",
                               return_value=["SPY"]):
            self.assertEqual(self._scanner("etf").resolve_universe(), ["SPY"])
        sp.assert_not_called()

    def test_overlap_is_deduped(self):
        with mock.patch.object(scanner_mod, "fetch_sp500_tickers",
                               return_value=["AAPL", "SPY"]), \
             mock.patch.object(scanner_mod, "fetch_most_traded_etfs",
                               return_value=["SPY", "QQQ"]):
            self.assertEqual(
                self._scanner("both").resolve_universe(), ["AAPL", "SPY", "QQQ"]
            )

    def test_default_universe_includes_etfs(self):
        self.assertEqual(ScanConfig().universe, "both")

    def test_unknown_universe_raises(self):
        with self.assertRaises(ValueError):
            self._scanner("nasdaq").resolve_universe()


class TestConfigIsolation(unittest.TestCase):
    """Mutating one config must not leak into defaults or other instances."""

    def setUp(self):
        # restore pristine defaults in case another test touched them
        DEFAULT_FILTERS[0]["params"]["fast"] = 20

    def tearDown(self):
        DEFAULT_FILTERS[0]["params"]["fast"] = 20

    def test_mutation_does_not_reach_new_instances(self):
        c1 = ScanConfig()
        c1.filters[0]["params"]["fast"] = 999
        self.assertEqual(ScanConfig().filters[0]["params"]["fast"], 20)

    def test_mutation_does_not_reach_default_filter_constant(self):
        c1 = ScanConfig()
        c1.filters[0]["params"]["fast"] = 999
        self.assertEqual(DEFAULT_FILTERS[0]["params"]["fast"], 20)

    def test_with_overrides_does_not_share_the_list(self):
        c1 = ScanConfig()
        c2 = c1.with_overrides(max_symbols=5)
        self.assertIsNot(c1.filters, c2.filters)
        c2.filters[0]["params"]["fast"] = 777
        self.assertEqual(c1.filters[0]["params"]["fast"], 20)

    def test_patch_breakout_only_affects_one_run(self):
        """The CLI's --fast path mutates config; that must stay local."""
        cfg = ScanConfig()
        cfg.filters[0]["params"]["fast"] = 50
        self.assertEqual(ScanConfig().filters[0]["params"]["fast"], 20)


class TestRsiWarmup(unittest.TestCase):
    def test_warmup_bars_are_nan_not_100(self):
        out = rsi(pd.Series([100.0] * 30), 14)
        self.assertTrue(out.iloc[:14].isna().all())
        self.assertFalse(out.iloc[14:].isna().any())

    def test_flat_series_is_neutral_not_overbought(self):
        """Regression: a flat series scored 100 (maximally overbought).

        With no losses the RSI formula returns 100, which marked a price
        that never moved as the most overbought reading possible - so
        `rsi_range(high=70)` rejected every flat name. A flat series is
        neutral (50), not extreme.
        """
        out = rsi(pd.Series([100.0] * 30), 14)
        self.assertAlmostEqual(out.iloc[-1], 50.0, places=6)

    def test_one_up_move_then_flat_is_overbought_not_neutral(self):
        """Only a series with *no* gain and *no* loss is neutral.

        A single rise followed by flat prices has positive average gain and
        zero average loss, so 100 is the correct RSI - this guards against
        over-correcting the flat-series fix into "any zero loss is 50".
        """
        out = rsi(pd.Series([100.0] * 15 + [101.0] + [101.0] * 15), 14)
        self.assertAlmostEqual(out.iloc[-1], 100.0, places=6)

    def test_rsi_range_accepts_flat_price(self):
        """The user-visible consequence of the flat-series bug."""
        from sma_scanner.filters.builtin import RsiRangeFilter

        closes = [100.0] * 60
        idx = pd.date_range(end=pd.Timestamp("2024-06-01"), periods=60, freq="D")
        df = pd.DataFrame(
            {"Open": closes, "High": closes, "Low": closes,
             "Close": closes, "Volume": [1_000_000.0] * 60},
            index=idx,
        )
        ctx = IndicatorContext(PriceFrame(symbol="FLAT", df=df, source="test"))
        self.assertTrue(RsiRangeFilter(low=0, high=70).evaluate(ctx).passed)

    def test_rising_series_is_100_after_warmup(self):
        out = rsi(pd.Series([100.0 + i for i in range(30)]), 14)
        self.assertEqual(out.iloc[-1], 100.0)

    def test_falling_series_is_near_zero_after_warmup(self):
        out = rsi(pd.Series([100.0 - i for i in range(30)]), 14)
        self.assertAlmostEqual(out.iloc[-1], 0.0, places=6)


class TestLookbackBoundary(unittest.TestCase):
    """A cross exactly `lookback` bars ago must be found."""

    def _series(self, cross_at, n=20):
        slow = pd.Series([10.0] * n)
        fast = pd.Series([9.0] * cross_at + [11.0] * (n - cross_at))
        return fast, slow, n

    def test_cross_on_the_boundary_is_found(self):
        fast, slow, n = self._series(cross_at=16)  # 3 bars ago
        for lookback in (3, 4, 5):
            with self.subTest(lookback=lookback):
                ev = detect_crossovers(fast, slow, direction="up", lookback=lookback)
                self.assertEqual([(n - 1) - e.bar_index for e in ev], [3])

    def test_cross_outside_window_is_still_excluded(self):
        fast, slow, n = self._series(cross_at=16)  # 3 bars ago
        self.assertEqual(detect_crossovers(fast, slow, direction="up", lookback=2), [])

    def test_bar_index_stays_absolute(self):
        """Truncating the window must not renumber the bars."""
        fast, slow, n = self._series(cross_at=16)
        ev = detect_crossovers(fast, slow, direction="up", lookback=4)
        self.assertEqual(ev[0].bar_index, 16)


class TestCsvSourceColumns(unittest.TestCase):
    def _write(self, directory: Path, name: str, header: str):
        rows = "\n".join(
            f"2024-01-{i + 1:02d},{100 + i},{101 + i},{99 + i},{100 + i},{1000000}"
            for i in range(30)
        )
        (directory / f"{name}.csv").write_text(f"{header}\n{rows}")

    def test_adj_close_is_recognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._write(d, "AAA", "Date,Open,High,Low,Adj Close,Volume")
            res = CsvSource(d).fetch_batch(["AAA"])
            self.assertIn("AAA", res.frames)
            self.assertEqual(res.frames["AAA"].close.iloc[-1], 129.0)

    def test_underscore_header_is_recognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._write(d, "BBB", "Date,Open,High,Low,adj_close,Volume")
            self.assertIn("BBB", CsvSource(d).fetch_batch(["BBB"]).frames)

    def test_real_close_wins_over_adj_close(self):
        """Both present -> no duplicate Close column, unadjusted value used."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._write(d, "DDD", "Date,Open,High,Low,Close,Adj Close,Volume")
            res = CsvSource(d).fetch_batch(["DDD"])
            self.assertEqual(list(res.frames["DDD"].df.columns),
                             ["Open", "High", "Low", "Close", "Volume"])

    def test_missing_file_and_unparsable_file_are_distinct_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._write(d, "CCC", "Date,Open,High,Low,Nonsense,Volume")
            res = CsvSource(d).fetch_batch(["ZZZ", "CCC"])
            self.assertEqual(res.errors["ZZZ"], "no CSV file for symbol")
            self.assertIn("no usable Close data", res.errors["CCC"])


class TestYFinanceNumericCoercion(unittest.TestCase):
    def test_missing_columns_padded_with_na_do_not_raise(self):
        df = pd.DataFrame({"Close": [1.0, 2.0], "Volume": [10.0, 20.0]})
        out = _coerce_ohlcv(df)
        self.assertEqual(set(out.columns), {"Open", "High", "Low", "Close", "Volume"})
        self.assertTrue((out.dtypes == "float64").all())

    def test_unparseable_values_become_nan(self):
        df = pd.DataFrame({"Close": ["a", "2.0"], "Volume": [1, 2]})
        out = _coerce_ohlcv(df)
        self.assertTrue(pd.isna(out["Close"].iloc[0]))
        self.assertEqual(out["Close"].iloc[1], 2.0)


class TestYFinanceCloseColumn(unittest.TestCase):
    """Regression: a duplicate Close column silently killed the whole scan."""

    @staticmethod
    def _frame(columns):
        idx = pd.date_range("2024-01-01", periods=5, freq="D")
        return pd.DataFrame(
            {c: [100.0 + i for i in range(5)] for c in columns}, index=idx
        )

    def test_real_close_wins_and_adj_close_is_dropped(self):
        df = _resolve_close(self._frame(
            ["Open", "High", "Low", "Close", "Adj Close", "Volume"]))
        self.assertNotIn("Adj Close", df.columns)
        self.assertEqual(list(df.columns).count("Close"), 1)

    def test_adj_close_is_adopted_when_no_real_close(self):
        df = _resolve_close(self._frame(["Open", "High", "Low", "Adj Close", "Volume"]))
        self.assertIn("Close", df.columns)

    def test_duplicate_close_would_have_broken_indicators(self):
        """Documents the failure mode: frame.close must stay a Series."""
        df = _resolve_close(self._frame(
            ["Open", "High", "Low", "Close", "Adj Close", "Volume"]))
        pf = PriceFrame(symbol="X", df=_coerce_ohlcv(df), source="yfinance")
        self.assertIsInstance(pf.close, pd.Series)

    def test_price_frame_rejects_duplicate_columns(self):
        # Built from a list of rows: a dict comprehension would de-dupe names.
        idx = pd.date_range("2024-01-01", periods=5, freq="D")
        vals = [100.0 + i for i in range(5)]
        df = pd.DataFrame(
            [vals] * 6, index=["Open", "High", "Low", "Close", "Close", "Volume"],
            columns=idx,
        ).T
        self.assertEqual(list(df.columns).count("Close"), 2)
        with self.assertRaises(ValueError):
            PriceFrame(symbol="X", df=df, source="probe")


class TestFilterValidation(unittest.TestCase):
    def test_bad_direction_raises_at_construction(self):
        with self.assertRaises(ValueError):
            SmaBreakoutFilter(direction="sideways")

    def test_string_thresholds_are_coerced(self):
        f = SmaBreakoutFilter(min_spread_pct="1.5", min_volume_ratio="2")
        self.assertEqual(f.min_spread_pct, 1.5)
        self.assertEqual(f.min_volume_ratio, 2.0)

    def test_zero_volume_lookback_raises(self):
        with self.assertRaises(ValueError):
            SmaBreakoutFilter(volume_lookback=0)

    def test_above_sma_rejects_zero_period(self):
        with self.assertRaises(ValueError):
            AboveSmaFilter(period=0)


class TestNaNGates(unittest.TestCase):
    """A NaN metric must FAIL a numeric gate, never silently clear it.

    `nan < threshold` is False, so every bare `if value < threshold` gate
    passed whenever the underlying number was unavailable - a liquidity or
    price screen quietly admitting exactly the symbols it meant to exclude.
    """

    def _ctx(self, closes, volumes=None):
        n = len(closes)
        vols = volumes if volumes is not None else [1_000_000.0] * n
        idx = pd.date_range(end=pd.Timestamp("2024-06-01"), periods=n, freq="D")
        df = pd.DataFrame(
            {"Open": closes, "High": closes, "Low": closes,
             "Close": closes, "Volume": vols},
            index=idx,
        )
        return IndicatorContext(PriceFrame(symbol="NAN", df=df, source="test"))

    def test_min_price_fails_on_nan_price(self):
        ctx = self._ctx([100.0, float("nan")])
        self.assertFalse(MinPriceFilter(min_price=10.0).evaluate(ctx).passed)

    def test_min_avg_volume_fails_on_all_nan_volume(self):
        from sma_scanner.filters.builtin import MinAvgVolumeFilter

        ctx = self._ctx([100.0] * 60, [float("nan")] * 60)
        res = MinAvgVolumeFilter(min_volume=1_000_000, period=20).evaluate(ctx)
        self.assertFalse(res.passed)
        self.assertIn("unavailable", res.reason)

    def test_min_spread_fails_when_spread_unavailable(self):
        # A crossover exists, but with a one-bar series the spread cannot be
        # computed - the required minimum must not be satisfied by NaN.
        ctx = self._ctx([100.0] * 60 + [110.0] * 5)
        f = SmaBreakoutFilter(fast=20, slow=50, lookback=10, min_spread_pct=1.0)
        # sanity: a real spread still passes
        self.assertTrue(f.evaluate(ctx).passed)
        with mock.patch.object(
            IndicatorContext, "spread_pct", return_value=float("nan")
        ):
            self.assertFalse(f.evaluate(ctx).passed)


class TestVolumeWindow(unittest.TestCase):
    """avg_volume must average the last N BARS, not the last N non-NaN values."""

    def _ctx(self, volumes):
        n = len(volumes)
        closes = [100.0] * n
        idx = pd.date_range(end=pd.Timestamp("2024-06-01"), periods=n, freq="D")
        df = pd.DataFrame(
            {"Open": closes, "High": closes, "Low": closes,
             "Close": closes, "Volume": volumes},
            index=idx,
        )
        return IndicatorContext(PriceFrame(symbol="GAP", df=df, source="test"))

    def test_gap_does_not_pull_in_out_of_window_bars(self):
        # Last 20 bars hold 18 NaN plus two 100s; the 30 leading 10s are
        # OUTSIDE the window and must not enter the average.
        vols = [10.0] * 30 + [float("nan")] * 18 + [100.0, 100.0]
        self.assertAlmostEqual(self._ctx(vols).avg_volume(20), 100.0)

    def test_fully_nan_window_is_nan(self):
        vols = [10.0] * 30 + [float("nan")] * 20
        self.assertTrue(pd.isna(self._ctx(vols).avg_volume(20)))


class TestSyntheticOverride(unittest.TestCase):
    def _broke_out(self, frame) -> bool:
        return bool(frame.close.iloc[-1] > frame.close.iloc[-20])

    def test_empty_list_override_clears_the_set(self):
        src = SyntheticSource(days=60, seed=1, breakout_symbols=["AAA"])
        res = src.fetch_batch(["AAA"], breakout_symbols=[])
        self.assertFalse(self._broke_out(res.frames["AAA"]))

    def test_string_override_is_not_iterated_characterwise(self):
        src = SyntheticSource(days=60, seed=1)
        res = src.fetch_batch(["AAA", "BBB"], breakout_symbols="AAA,BBB")
        self.assertTrue(self._broke_out(res.frames["AAA"]))
        self.assertTrue(self._broke_out(res.frames["BBB"]))

    def test_absent_override_keeps_constructor_default(self):
        src = SyntheticSource(days=60, seed=1, breakout_symbols=["AAA"])
        res = src.fetch_batch(["AAA"])
        self.assertTrue(self._broke_out(res.frames["AAA"]))

    def test_limit_is_honoured(self):
        src = SyntheticSource(days=120, seed=1)
        res = src.fetch_batch(["AAA"], limit=30)
        self.assertEqual(len(res.frames["AAA"]), 30)


class TestUniverseStyleAndCache(unittest.TestCase):
    def test_dot_style_for_alpaca(self):
        self.assertEqual(Scanner(ScanConfig())._ticker_style(), "dot")

    def test_dash_style_for_yfinance(self):
        self.assertEqual(
            Scanner(ScanConfig(data_source="yfinance"))._ticker_style(), "dash"
        )

    def test_style_reaches_the_fetch(self):
        with mock.patch.object(
            scanner_mod, "fetch_sp500_tickers", return_value=["BRK-B"]
        ) as fn:
            Scanner(ScanConfig(data_source="yfinance")).resolve_universe()
        self.assertEqual(fn.call_args.kwargs["style"], "dash")

    def test_truncated_cache_triggers_rescrape(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "sp500.csv"
            pd.DataFrame({"Symbol": [f"S{i}" for i in range(10)]}).to_csv(cache, index=False)
            with mock.patch.object(
                sp500, "_from_wikipedia", return_value=[f"W{i}" for i in range(500)]
            ) as scrape:
                out = sp500.fetch_sp500_tickers(cache_path=cache)
            scrape.assert_called_once()
            self.assertEqual(len(out), 500)

    def test_short_cache_is_used_when_scrape_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "sp500.csv"
            pd.DataFrame({"Symbol": ["AAA", "BBB"]}).to_csv(cache, index=False)
            with mock.patch.object(sp500, "_from_wikipedia", side_effect=OSError("offline")):
                out = sp500.fetch_sp500_tickers(cache_path=cache)
            self.assertEqual(out, ["AAA", "BBB"])

    def test_ticker_cache_default_is_absolute(self):
        self.assertTrue(Path(ScanConfig().ticker_cache).is_absolute())


class TestReportLabels(unittest.TestCase):
    def _run(self, filters):
        src = SyntheticSource(days=400, seed=11, breakout_symbols=["AAA"])
        return Scanner(ScanConfig(filters=filters), source=src).run(["AAA", "BBB"])

    def test_labels_follow_the_configured_periods(self):
        res = self._run(
            [{"name": "sma_breakout", "params": {"fast": 50, "slow": 200, "lookback": 10}}]
        )
        self.assertEqual(res.report_labels, {"sma_fast": "SMA50", "sma_slow": "SMA200"})

    def test_trend_column_follows_above_sma_period(self):
        res = self._run([{"name": "above_sma", "params": {"period": 50}}])
        self.assertEqual(res.extra_columns, [("SMA50", "sma_50")])

    def test_console_header_uses_dynamic_labels(self):
        res = ScanResult(
            report_labels={"sma_fast": "SMA50", "sma_slow": "SMA200"}, sort_by=None
        )
        sr = SymbolResult(symbol="AAA")
        sr.metrics.update(
            price=1.0, sma_fast=1.0, sma_slow=1.0, spread_pct=1.0, volume_ratio=2.0
        )
        res.results.append(sr)
        buf = io.StringIO()
        print_summary(res, stream=buf)
        text = buf.getvalue()
        header = next(line for line in text.splitlines() if line.startswith("SYMBOL"))
        self.assertEqual(
            header.split(),
            ["SYMBOL", "PRICE", "SMA50", "SMA200", "SPREAD%", "CROSSED", "AGO", "VOLxAVG"],
        )


class TestRunHealthSummary(unittest.TestCase):
    """The gate that stops an automated run publishing a useless report."""

    def _healthy(self) -> dict:
        return {
            "universe_size": 603,
            "evaluated": 600,
            "skipped": 3,
            "matches": 5,
            "fetch_errors": 3,
            "fetch_error_rate": 0.005,
            "fetch_error_symbols": ["ZZZ"],
            "caches": {},
        }

    def test_healthy_run_reports_nothing(self):
        self.assertEqual(check(self._healthy()), [])

    def test_total_data_outage_is_refused(self):
        """Every symbol failing to load is not 'zero signals today'."""
        summary = {
            "universe_size": 603,
            "evaluated": 0,
            "skipped": 0,
            "matches": 0,
            "fetch_errors": 603,
            "fetch_error_rate": 1.0,
            "fetch_error_symbols": ["AAPL"],
            "caches": {},
        }
        problems = check(summary)
        self.assertTrue(problems)
        self.assertTrue(any("fetch error rate" in p for p in problems))
        self.assertTrue(any("evaluated" in p for p in problems))

    def test_truncated_universe_is_refused(self):
        """A failed S&P scrape leaves an ETF-only universe - too small to trust."""
        problems = check({**self._healthy(), "universe_size": 100, "evaluated": 100})
        self.assertTrue(any("universe 100" in p for p in problems))

    def test_partial_symbol_failures_are_tolerated(self):
        """A handful of bad symbols is normal and must not block publishing."""
        self.assertEqual(check({**self._healthy(), "fetch_errors": 10,
                                "fetch_error_rate": 0.016}), [])

    def test_stale_cache_is_refused(self):
        summary = {
            **self._healthy(),
            "caches": {"etfs": {"stale": True, "age_days": 9.2, "exists": True}},
        }
        problems = check(summary)
        self.assertTrue(any("etfs cache" in p and "9.2" in p for p in problems))

    def test_missing_cache_is_refused(self):
        summary = {
            **self._healthy(),
            "caches": {"tickers": {"stale": True, "age_days": None, "exists": False}},
        }
        self.assertTrue(any("missing" in p for p in check(summary)))

    # -- cache_health ---------------------------------------------------
    def test_cache_health_missing_file_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = cache_health(os.path.join(tmp, "nope.csv"),
                                now=datetime.now(timezone.utc), max_age_days=7)
        self.assertFalse(info["exists"])
        self.assertTrue(info["stale"])
        self.assertIsNone(info["age_days"])

    def test_cache_health_age_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "etfs.csv")
            Path(path).write_text("Symbol\nSPY\n", encoding="utf-8")
            now = datetime.now(timezone.utc)

            fresh = cache_health(path, now=now, max_age_days=7)
            self.assertFalse(fresh["stale"])

            # Backdate the file past the refresh window: this is exactly what a
            # failed TradingView scrape looks like - the old list, still in use.
            old = (now - timedelta(days=9)).timestamp()
            os.utime(path, (old, old))
            self.assertTrue(cache_health(path, now=now, max_age_days=7)["stale"])

    def test_cache_health_without_threshold_does_not_judge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sp500.csv")
            Path(path).write_text("Symbol\nAAPL\n", encoding="utf-8")
            info = cache_health(path, now=datetime.now(timezone.utc))
        self.assertIsNone(info["stale"])
        self.assertIsNotNone(info["age_days"])

    # -- build_summary --------------------------------------------------
    def _result(self, **kwargs) -> ScanResult:
        res = ScanResult(data_source="alpaca", universe_size=2)
        sr = SymbolResult(symbol="AAA")
        sr.metrics["volume_ratio"] = 1.4
        res.results.append(sr)
        res.fetch_errors = kwargs.get("fetch_errors", {})
        return res

    def test_summary_reports_counts_and_errors(self):
        res = self._result(fetch_errors={"BBB": "timeout"})
        summary = build_summary(res, ScanConfig(universe="both"),
                                now=datetime(2024, 6, 1, tzinfo=timezone.utc))
        self.assertEqual(summary["universe_size"], 2)
        self.assertEqual(summary["evaluated"], 1)
        self.assertEqual(summary["matches"], 1)
        self.assertEqual(summary["fetch_errors"], 1)
        self.assertEqual(summary["fetch_error_rate"], 0.5)
        self.assertEqual(summary["fetch_error_symbols"], ["BBB"])
        self.assertEqual(summary["universe_source"], "both")
        self.assertEqual(set(summary["caches"]), {"tickers", "etfs"})

    def test_explicit_symbols_skips_cache_judgement(self):
        """--symbols reads no ticker list, so a missing cache means nothing."""
        summary = build_summary(self._result(), ScanConfig(universe="both"),
                                explicit_symbols=True)
        self.assertEqual(summary["caches"], {})
        self.assertEqual(summary["universe_source"], "explicit")

    def test_sp500_only_run_ignores_the_etf_cache(self):
        summary = build_summary(self._result(), ScanConfig(universe="sp500"))
        self.assertEqual(set(summary["caches"]), {"tickers"})

    def test_etf_staleness_uses_the_configured_refresh_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ScanConfig(universe="etf",
                             etf_cache=os.path.join(tmp, "etfs.csv"),
                             etf_refresh_days=3)
            Path(cfg.etf_cache).write_text("Symbol\nSPY\n", encoding="utf-8")
            old = (datetime.now(timezone.utc) - timedelta(days=5)).timestamp()
            os.utime(cfg.etf_cache, (old, old))
            summary = build_summary(self._result(), cfg)
        self.assertEqual(summary["caches"]["etfs"]["max_age_days"], 3)
        self.assertTrue(summary["caches"]["etfs"]["stale"])

    def test_health_line_is_one_line(self):
        line = format_health_line(build_summary(self._result(), ScanConfig()))
        self.assertTrue(line.startswith("health: "))
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn("fetch_errors=0", line)

    def test_markdown_table_lists_the_counters(self):
        md = format_markdown(build_summary(self._result(), ScanConfig()))
        self.assertIn("| Universe | 2 |", md)
        self.assertIn("| Matches | 1 |", md)

    # -- the CLI the schedulers call ------------------------------------
    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good.json")
            Path(good).write_text(json.dumps(self._healthy()), encoding="utf-8")
            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(summary_main([good, "--min-universe", "500"]), 0)

            bad = os.path.join(tmp, "bad.json")
            Path(bad).write_text(json.dumps({**self._healthy(),
                                             "fetch_error_rate": 0.9,
                                             "fetch_errors": 543}),
                                 encoding="utf-8")
            with mock.patch("sys.stdout", io.StringIO()), \
                    mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(summary_main([bad]), 1)

    def test_cli_missing_summary_is_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "absent.json")
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(summary_main([missing]), 1)

    def test_cli_writes_the_markdown_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            Path(path).write_text(json.dumps(self._healthy()), encoding="utf-8")
            md = os.path.join(tmp, "step-summary.md")
            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(summary_main([path, "--markdown", md]), 0)
            self.assertIn("Run health", Path(md).read_text(encoding="utf-8"))

    def test_cli_survives_an_unwritable_markdown_path(self):
        """A missing run-summary file must not turn a healthy run into a failure."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            Path(path).write_text(json.dumps(self._healthy()), encoding="utf-8")
            with mock.patch("sys.stdout", io.StringIO()), \
                    mock.patch("sys.stderr", io.StringIO()):
                rc = summary_main([path, "--markdown", os.path.join(tmp, "nodir", "x.md")])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
