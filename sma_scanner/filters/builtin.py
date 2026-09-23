"""Built-in screening conditions.

Each one is small, independent, and registered by name. To add a new
condition later, copy any class here, change `name` and `evaluate`, and
it becomes selectable from config - no other file changes.
"""

from __future__ import annotations

import pandas as pd

from ..indicators.context import IndicatorContext
from .base import Filter, FilterResult
from .registry import register


@register
class SmaBreakoutFilter(Filter):
    """Fast SMA crossing the slow SMA within the last N bars.

    This is the screen the tool was built for: a 20-day SMA breaking out
    above the 50-day SMA (a "golden cross"), where the cross is *recent*
    rather than long-established.

    Params:
        fast: fast SMA period (default 20)
        slow: slow SMA period (default 50)
        lookback: how many bars back the cross may have happened (default 10)
        direction: "up" (golden) or "down" (death cross)
        min_spread_pct: optional minimum current gap between the SMAs
        volume_lookback: bars used for the breakout-day volume baseline
        min_volume_ratio: optional gate, e.g. 1.5 requires the breakout bar
            to trade at least 1.5x its baseline volume
    """

    name = "sma_breakout"
    description = (
        "Fast SMA crossed the slow SMA within the last N bars "
        "(20-over-50 golden cross)."
    )

    def __init__(
        self,
        fast: int = 20,
        slow: int = 50,
        lookback: int = 10,
        direction: str = "up",
        min_spread_pct: float | None = None,
        volume_lookback: int = 20,
        min_volume_ratio: float | None = None,
    ) -> None:
        super().__init__(
            fast=fast, slow=slow, lookback=lookback,
            direction=direction, min_spread_pct=min_spread_pct,
            volume_lookback=volume_lookback, min_volume_ratio=min_volume_ratio,
        )
        self.fast = int(fast)
        self.slow = int(slow)
        self.lookback = int(lookback)
        self.direction = str(direction).lower()
        # Coerce optional thresholds: a string straight out of a config file
        # would otherwise raise TypeError per-symbol and land in "skipped".
        self.min_spread_pct = (
            None if min_spread_pct is None else float(min_spread_pct)
        )
        self.volume_lookback = int(volume_lookback)
        self.min_volume_ratio = (
            None if min_volume_ratio is None else float(min_volume_ratio)
        )
        # Fail at construction, not per-symbol: a bad value here otherwise
        # shows up as every symbol being "skipped" instead of a loud error.
        if self.fast >= self.slow:
            raise ValueError(f"fast ({fast}) must be < slow ({slow})")
        if self.lookback < 1:
            raise ValueError("lookback must be >= 1")
        if self.direction not in {"up", "down"}:
            raise ValueError(f"direction must be 'up' or 'down', got {direction!r}")
        if self.volume_lookback < 1:
            raise ValueError("volume_lookback must be >= 1")

    # -- reporting ------------------------------------------------------
    def report_labels(self) -> Dict[str, str]:
        return {"sma_fast": f"SMA{self.fast}", "sma_slow": f"SMA{self.slow}"}

    def evaluate(self, ctx: IndicatorContext) -> FilterResult:
        # +1 so the SMA is warm and we can see the bar before the cross.
        ctx.require_bars(self.slow + self.lookback + 1)

        cross = ctx.recent_cross(
            self.fast, self.slow, direction=self.direction, lookback=self.lookback
        )
        if cross is None:
            return self.fail(
                f"no {self.direction}-cross of SMA{self.fast}/SMA{self.slow} "
                f"within {self.lookback} bars",
                sma_fast=float(ctx.sma(self.fast).iloc[-1]),
                sma_slow=float(ctx.sma(self.slow).iloc[-1]),
            )

        # Guard against a cross that has already been reversed: if the most
        # recent crossing (of either direction) is the opposite way, the
        # signal is stale and should not be reported as a fresh breakout.
        latest = ctx.last_cross(self.fast, self.slow, direction="both")
        if latest is not None and latest.direction != self.direction:
            return self.fail(
                f"{self.direction}-cross on {cross.date.date()} was reversed by a "
                f"{latest.direction}-cross on {latest.date.date()}",
                sma_fast=float(cross.fast_value),
                sma_slow=float(cross.slow_value),
            )

        spread = ctx.spread_pct(self.fast, self.slow)
        if self.min_spread_pct is not None and spread < self.min_spread_pct:
            return self.fail(
                f"spread {spread:.2f}% below required {self.min_spread_pct}%",
                spread_pct=spread,
            )

        verb = "above" if self.direction == "up" else "below"
        # Volume on the breakout bar vs the average of the bars leading up
        # to it - reported for every match so you can judge conviction.
        vol = ctx.volume_surge(cross.bar_index, self.volume_lookback)

        ratio = vol["volume_ratio"]
        if self.min_volume_ratio is not None:
            if pd.isna(ratio) or ratio < self.min_volume_ratio:
                # NaN would render as the string "nanx" - say "n/a" instead.
                shown = "n/a" if pd.isna(ratio) else f"{ratio:.2f}x"
                return self.fail(
                    f"breakout volume {shown} average below required "
                    f"{self.min_volume_ratio}x",
                    **vol,
                )

        return self.ok(
            f"SMA{self.fast} crossed {verb} SMA{self.slow} "
            f"{ctx.bars_since(cross)} bar(s) ago",
            cross_date=cross.date,
            bars_since_cross=ctx.bars_since(cross),
            spread_pct=spread,
            price=ctx.last_price,
            sma_fast=float(ctx.sma(self.fast).iloc[-1]),
            sma_slow=float(ctx.sma(self.slow).iloc[-1]),
            **vol,
        )


@register
class MinPriceFilter(Filter):
    """Require a minimum share price (drops penny stocks)."""

    name = "min_price"
    description = "Last close at or above a minimum price."

    def __init__(self, min_price: float = 10.0) -> None:
        super().__init__(min_price=min_price)
        self.min_price = float(min_price)

    def evaluate(self, ctx: IndicatorContext) -> FilterResult:
        price = ctx.last_price
        if price < self.min_price:
            return self.fail(f"price {price:.2f} < {self.min_price}", price=price)
        return self.ok(f"price {price:.2f}", price=price)


@register
class MinAvgVolumeFilter(Filter):
    """Require minimum average volume (liquidity screen)."""

    name = "min_avg_volume"
    description = "Average volume over N bars at or above a threshold."

    def __init__(self, min_volume: float = 500_000, period: int = 20) -> None:
        super().__init__(min_volume=min_volume, period=period)
        self.min_volume = float(min_volume)
        self.period = int(period)

    def evaluate(self, ctx: IndicatorContext) -> FilterResult:
        ctx.require_bars(self.period)
        avg = ctx.avg_volume(self.period)
        if avg < self.min_volume:
            return self.fail(
                f"avg volume {avg:,.0f} < {self.min_volume:,.0f}", avg_volume=avg
            )
        return self.ok(f"avg volume {avg:,.0f}", avg_volume=avg)


@register
class AboveSmaFilter(Filter):
    """Price above a long-term SMA (confirms the broader trend is up)."""

    name = "above_sma"
    description = "Last close above a given SMA (e.g. 200-day trend filter)."

    def __init__(self, period: int = 200) -> None:
        super().__init__(period=period)
        self.period = int(period)
        if self.period < 1:
            raise ValueError(f"period must be >= 1, got {period}")

    def report_columns(self):
        # Label follows the configured period, so --filter above_sma:period=50
        # gets an "SMA50" column instead of a blank "SMA200".
        return [(f"SMA{self.period}", f"sma_{self.period}")]

    def evaluate(self, ctx: IndicatorContext) -> FilterResult:
        ctx.require_bars(self.period)
        sma_val = float(ctx.sma(self.period).iloc[-1])
        price = ctx.last_price
        if price <= sma_val:
            return self.fail(
                f"price {price:.2f} <= SMA{self.period} {sma_val:.2f}",
                price=price, **{f"sma_{self.period}": sma_val},
            )
        return self.ok(
            f"price above SMA{self.period}", price=price, **{f"sma_{self.period}": sma_val}
        )


@register
class RsiRangeFilter(Filter):
    """RSI within a band - e.g. avoid already-overbought names."""

    name = "rsi_range"
    description = "RSI within [low, high] on the last bar."

    def __init__(self, low: float = 0.0, high: float = 100.0, period: int = 14) -> None:
        super().__init__(low=low, high=high, period=period)
        self.low = float(low)
        self.high = float(high)
        self.period = int(period)

    def evaluate(self, ctx: IndicatorContext) -> FilterResult:
        ctx.require_bars(self.period + 1)
        series = ctx.rsi(self.period)
        value = float(series.iloc[-1])
        if value < self.low or value > self.high:
            return self.fail(
                f"RSI {value:.1f} outside [{self.low}, {self.high}]", rsi=value
            )
        return self.ok(f"RSI {value:.1f}", rsi=value)
