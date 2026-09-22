"""Output: console summary and CSV export."""

from __future__ import annotations

import csv
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence

from .scanner import ScanResult


def _fmt(value: Any, spec: str = ".2f") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        if value != value:  # NaN
            return "-"
        return format(value, spec)
    if isinstance(value, (datetime, date)):
        return str(value)[:10]
    return str(value)


def _csv_value(value: Any) -> Any:
    """Flatten pandas Timestamps / NaN for CSV writing."""
    if value is None:
        return ""
    if hasattr(value, "isoformat") and not isinstance(value, (str, bytes)):
        return str(value)[:10]
    if isinstance(value, float) and value != value:
        return ""
    return value


def write_csv(rows: Sequence[Dict[str, Any]], path: str) -> None:
    out = Path(str(path)).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        # Still write a header so downstream tooling sees an empty result set.
        with open(out, "w", newline="", encoding="utf-8") as fh:
            fh.write("symbol\n")
        return
    # Stable, predictable column order: symbol first, then everything else.
    cols: List[str] = ["symbol"]
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({c: _csv_value(r.get(c)) for c in cols})


def print_summary(res: ScanResult, *, show_failed: bool = False, stream=sys.stdout) -> None:
    """Human-readable scan report."""
    w = stream.write
    w("\n" + "=" * 78 + "\n")
    w("  SMA BREAKOUT SCAN\n")
    w("=" * 78 + "\n")
    w(f"  Universe        : {res.universe_size} symbols\n")
    w(f"  Data source     : {res.data_source}\n")
    w(f"  Filters         : {', '.join(res.filters_used) or '(none)'}\n")
    w(f"  Evaluated       : {res.evaluated}\n")
    if res.fetch_errors:
        w(f"  No data         : {len(res.fetch_errors)} (see --verbose)\n")
    w(f"  MATCHES         : {res.n_matches}\n")
    w("=" * 78 + "\n\n")

    matches = res.matches
    if not matches:
        w("  No symbols matched the configured conditions.\n\n")
    else:
        header = (
            f"{'SYMBOL':<8} {'PRICE':>9} {'SMA20':>9} {'SMA50':>9} "
            f"{'SPREAD%':>8} {'CROSSED':>12} {'AGO':>4} {'VOLxAVG':>8}"
        )
        w(header + "\n")
        w("-" * len(header) + "\n")
        for m in matches:
            mt = m.metrics
            ratio = mt.get("volume_ratio")
            ratio_s = "-" if ratio is None or ratio != ratio else f"{ratio:.2f}x"
            w(
                f"{m.symbol:<8} "
                f"{_fmt(mt.get('price')):>9} "
                f"{_fmt(mt.get('sma_fast')):>9} "
                f"{_fmt(mt.get('sma_slow')):>9} "
                f"{_fmt(mt.get('spread_pct')):>8} "
                f"{_fmt(mt.get('cross_date'), ''):>12} "
                f"{_fmt(mt.get('bars_since_cross'), '.0f'):>4} "
                f"{ratio_s:>8}\n"
            )
        w("\n")
        w("  VOLxAVG = breakout-day volume / average volume of the 20 bars before it\n\n")

    if show_failed:
        failed = res.failed_rows()
        if failed:
            w(f"--- Rejected ({len(failed)}) ---\n")
            for row in failed[:50]:
                w(f"  {row['symbol']:<8} {row['failed_filter']:<16} {row['reason']}\n")
            if len(failed) > 50:
                w(f"  ... {len(failed) - 50} more\n")
            w("\n")


def print_errors(res: ScanResult, *, stream=sys.stdout) -> None:
    if not res.fetch_errors:
        return
    stream.write(f"--- Fetch errors ({len(res.fetch_errors)}) ---\n")
    for sym, msg in list(res.fetch_errors.items())[:30]:
        stream.write(f"  {sym}: {msg}\n")
    if len(res.fetch_errors) > 30:
        stream.write(f"  ... {len(res.fetch_errors) - 30} more\n")
    stream.write("\n")
