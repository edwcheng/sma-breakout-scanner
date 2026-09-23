"""Technical indicator primitives.

Pure functions over pandas Series - no I/O, no state - so they are
trivial to unit-test and reuse across filters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import pandas as pd


# ----------------------------------------------------------------------
# moving averages
# ----------------------------------------------------------------------
def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average. Leading `period-1` values are NaN."""
    if period <= 0:
        raise ValueError(f"SMA period must be positive, got {period}")
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int, *, adjust: bool = False) -> pd.Series:
    """Exponential moving average."""
    if period <= 0:
        raise ValueError(f"EMA period must be positive, got {period}")
    return series.ewm(span=period, adjust=adjust, min_periods=period).mean()


def avg_volume(volume: pd.Series, period: int) -> float:
    """Mean share volume over the last `period` bars (scalar).

    The window is the last `period` *bars*, not the last `period` non-NaN
    values. Dropping NaN before slicing silently reached back past the
    window - a symbol with a gap in its volume series (halt, missing data)
    would be measured against months-old bars. If the window holds no
    usable value the result is NaN, and callers treat that as "unknown"
    rather than substituting an out-of-window number.
    """
    if len(volume) == 0:
        return float("nan")
    window = volume.tail(period).dropna()
    if window.empty:
        return float("nan")
    return float(window.mean())


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's Relative Strength Index (0-100)."""
    if period <= 0:
        raise ValueError(f"RSI period must be positive, got {period}")
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    # Wilder smoothing == EWM with alpha = 1/period
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    # Warm-up bars (first period-1) have no average yet and must stay NaN -
    # reporting them as 100 would make a flat opening look overbought.
    warming_up = avg_loss.isna()
    # No gain AND no loss means the price never moved: conventionally a
    # neutral 50, not 100. Checking both is essential - a pure decline also
    # has zero average gain, and only zero *loss* means "no weakness yet".
    flat = avg_gain.eq(0.0) & avg_loss.eq(0.0)
    # Zero loss => RSI 100. Mask (rather than replace with pd.NA) to keep a
    # clean float dtype across pandas versions.
    avg_loss = avg_loss.mask(avg_loss == 0.0)
    rs = avg_gain / avg_loss
    out = 100.0 - (100.0 / (1.0 + rs))
    out = out.where(avg_loss.notna(), 100.0)
    out = out.where(~flat, 50.0)
    out = out.where(~warming_up, float("nan"))
    return out.astype(float)


def pct_change(series: pd.Series, periods: int = 1) -> float:
    """Percent change over `periods` bars, as a plain float (e.g. 3.2 = +3.2%)."""
    if len(series) <= periods:
        return float("nan")
    prev = series.iloc[-(periods + 1)]
    last = series.iloc[-1]
    if not prev or pd.isna(prev):
        return float("nan")
    return float((last - prev) / prev * 100.0)


# ----------------------------------------------------------------------
# crossovers
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Crossover:
    """A single crossing event between a fast and a slow series."""

    date: pd.Timestamp
    bar_index: int  # positional index within the frame
    direction: str  # "up" = fast crossed above slow (golden), "down" = death
    fast_value: float
    slow_value: float
    spread_pct: float  # (fast - slow) / slow * 100 at the cross

    @property
    def is_golden(self) -> bool:
        return self.direction == "up"

    def describe(self, fast_label: str = "fast", slow_label: str = "slow") -> str:
        verb = "crossed above" if self.is_golden else "crossed below"
        return (
            f"{fast_label} {verb} {slow_label} on {self.date.date()} "
            f"({self.fast_value:.2f} vs {self.slow_value:.2f})"
        )


def detect_crossovers(
    fast: pd.Series,
    slow: pd.Series,
    *,
    direction: str = "up",
    lookback: Optional[int] = None,
) -> List[Crossover]:
    """Find every bar where `fast` crosses `slow` in the given direction.

    Args:
        fast: the faster (more reactive) series, e.g. 20-day SMA.
        slow: the slower series, e.g. 50-day SMA.
        direction: "up" for golden crosses, "down" for death crosses,
            "both" for every crossing.
        lookback: if set, only report crossings within the last `lookback`
            bars. A cross exactly `lookback` bars ago IS included (the
            window keeps one extra bar of context for the comparison).

    Returns:
        Chronological list of Crossover events. Empty if none.
    """
    if direction not in {"up", "down", "both"}:
        raise ValueError(f"direction must be up/down/both, got {direction!r}")

    aligned = pd.concat([fast.rename("f"), slow.rename("s")], axis=1)
    if lookback is not None:
        if lookback < 1:
            raise ValueError("lookback must be >= 1")
        # +2, not +1: a cross needs the bar itself AND the bar before it.
        # A cross exactly `lookback` bars ago sits on the left edge, so the
        # prior bar must survive the truncation too.
        aligned = aligned.tail(lookback + 2)

    f, s = aligned["f"], aligned["s"]
    pf, ps = f.shift(1), s.shift(1)

    # NaN comparisons yield False, so warm-up NaNs are naturally skipped.
    up = (pf <= ps) & (f > s)
    down = (pf >= ps) & (f < s)
    if direction == "up":
        mask = up
    elif direction == "down":
        mask = down
    else:
        mask = up | down

    events: List[Crossover] = []
    for pos, (ts, hit) in enumerate(zip(aligned.index, mask)):
        if not bool(hit):
            continue
        fv, sv = float(f.iloc[pos]), float(s.iloc[pos])
        spread = (fv - sv) / sv * 100.0 if sv else float("nan")
        # Positional index within the *full* frame is what callers expect for
        # "how many bars ago", so recompute from the tail offset.
        bar_index = len(fast) - len(aligned) + pos
        events.append(
            Crossover(
                date=ts,
                bar_index=bar_index,
                direction="up" if bool(up.iloc[pos]) else "down",
                fast_value=fv,
                slow_value=sv,
                spread_pct=spread,
            )
        )
    return events


def last_crossover(
    fast: pd.Series, slow: pd.Series, *, direction: str = "up"
) -> Optional[Crossover]:
    """Most recent crossing event, or None."""
    events = detect_crossovers(fast, slow, direction=direction)
    return events[-1] if events else None


def current_spread_pct(fast: pd.Series, slow: pd.Series) -> float:
    """Latest (fast - slow) / slow * 100, or NaN if not computable."""
    if fast.empty or slow.empty:
        return float("nan")
    fv, sv = fast.iloc[-1], slow.iloc[-1]
    if pd.isna(fv) or pd.isna(sv) or not sv:
        return float("nan")
    return float((fv - sv) / sv * 100.0)
