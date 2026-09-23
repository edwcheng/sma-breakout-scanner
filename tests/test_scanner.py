"""Unit tests for indicator math, crossover detection, and filter behaviour.

Run:  python3.11 -m unittest discover -s tests -v
  or: pytest tests/
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sma_scanner import scanner as scanner_mod  # noqa: E402
from sma_scanner.config import ScanConfig  # noqa: E402
from sma_scanner.data import PriceFrame, SyntheticSource  # noqa: E402
from sma_scanner.data import etf_list  # noqa: E402
from sma_scanner.data.etf_list import (  # noqa: E402
    fetch_most_traded_etfs,
    parse_most_traded,
)
from sma_scanner.filters import available_filters  # noqa: E402
from sma_scanner.filters.builtin import AboveSmaFilter, SmaBreakoutFilter  # noqa: E402
from sma_scanner.indicators import (  # noqa: E402
    IndicatorContext,
    detect_crossovers,
    sma,
)
from sma_scanner.scanner import ScanResult, Scanner, SymbolResult  # noqa: E402


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

        Pins the chain to sma_breakout alone - the default config also applies
        the 200-day trend gate, which the synthetic downtrend fails.
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
            with mock.patch.object(etf_list, "_fetch_html") as fh:
                out = fetch_most_traded_etfs(cache_path=cache, refresh_days=7)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
