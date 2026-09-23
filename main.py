#!/usr/bin/env python3
"""SMA breakout scanner - CLI entry point.

Examples
--------
Scan the S&P 500 for fresh 20-over-50 golden crosses:
    python main.py

Tighten the screen (cross within 5 bars, liquid names only):
    python main.py --lookback 5 --filter min_avg_volume:min_volume=1000000

Look for breakdowns instead (20 crossing below 50):
    python main.py --direction down

Offline smoke test with generated data:
    python main.py --source synthetic --symbols AAA,BBB,CCC,DDD \\
        --source-kw breakout_symbols=AAA,BBB
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Allow running as a script from the project root.
if __package__ in (None, ""):  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from sma_scanner.config import ScanConfig  # noqa: E402
from sma_scanner.filters import available_filters  # noqa: E402
from sma_scanner.html_report import write_html  # noqa: E402
from sma_scanner.reporter import (  # noqa: E402
    print_errors,
    print_summary,
    write_csv,
)
from sma_scanner.scanner import Scanner  # noqa: E402
from sma_scanner.summary import (  # noqa: E402
    build_summary,
    format_health_line,
    write_summary,
)

PROJECT_ROOT = Path(__file__).resolve().parent


# ----------------------------------------------------------------------
# minimal .env support (no extra dependency)
# ----------------------------------------------------------------------
def load_dotenv(path: Path = PROJECT_ROOT / ".env") -> None:
    """Populate os.environ from a .env file without overwriting real env vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _coerce(raw: str) -> Any:
    """Best-effort scalar typing for CLI-supplied filter params."""
    low = raw.lower()
    if low in {"true", "yes"}:
        return True
    if low in {"false", "no"}:
        return False
    if low in {"none", "null"}:
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    # comma lists for params like breakout_symbols
    if "," in raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return raw


def parse_filter_spec(spec: str) -> Dict[str, Any]:
    """`name:k=v,k=v` -> {'name': name, 'params': {...}}."""
    spec = spec.strip()
    if not spec:
        raise ValueError("empty filter spec")
    name, _, tail = spec.partition(":")
    params: Dict[str, Any] = {}
    if tail:
        for part in tail.split(","):
            if not part.strip():
                continue
            if "=" not in part:
                raise ValueError(f"filter param {part!r} must be key=value")
            k, _, v = part.partition("=")
            params[k.strip()] = _coerce(v.strip())
    return {"name": name.strip(), "params": params}


def parse_kv_pairs(items: Optional[List[str]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--source-kw expects key=value, got {item!r}")
        k, _, v = item.partition("=")
        out[k.strip()] = _coerce(v.strip())
    return out


# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sma-scanner",
        description="Scan a universe of stocks for SMA breakout signals "
        "(default: 20-day SMA crossing above the 50-day SMA).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", help="JSON/YAML config file (see --save-config)")
    p.add_argument("--save-config", help="write the effective config to PATH and exit")

    # universe
    p.add_argument(
        "--universe",
        choices=["sp500", "etf", "both", "file"],
        help="symbol universe (default: both = S&P 500 + most-traded ETFs)",
    )
    p.add_argument("--symbols-file", help="file of symbols (CSV or newline separated)")
    p.add_argument("--symbols", help="explicit comma-separated symbols (overrides universe)")
    p.add_argument("--max-symbols", type=int, help="cap the universe (quick runs)")
    p.add_argument("--etf-limit", type=int, help="how many most-traded ETFs (default 100)")
    p.add_argument("--etf-refresh-days", type=int,
                   help="re-scrape the ETF list once the cache is this old (default 7)")
    p.add_argument("--refresh-tickers", action="store_true",
                   help="force re-scrape of the S&P 500 and ETF lists")

    # data source
    p.add_argument(
        "--source",
        choices=["alpaca", "yfinance", "csv", "synthetic"],
        help="price data source",
    )
    p.add_argument("--source-kw", action="append", metavar="KEY=VALUE",
                   help="extra keyword arg for the data source (repeatable)")
    p.add_argument("--history-bars", type=int, help="daily bars to fetch per symbol")

    # the headline screen
    p.add_argument("--fast", type=int, help="fast SMA period (default 20)")
    p.add_argument("--slow", type=int, help="slow SMA period (default 50)")
    p.add_argument("--lookback", type=int, help="cross must occur within N bars (default 10)")
    p.add_argument("--direction", choices=["up", "down"], help="'up'=golden, 'down'=death cross")

    # generic filter composition
    p.add_argument("--filter", action="append", metavar="NAME:k=v,...",
                   help="filter to apply (repeatable). Replaces the default filter set.")

    # output
    p.add_argument("--sort-by", metavar="METRIC",
                   help="rank output by this metric (default: volume_ratio)")
    p.add_argument("--sort-asc", action="store_true", help="sort ascending instead")
    p.add_argument("-o", "--output", help="write matches to this CSV path")
    p.add_argument("--html", help="also render a standalone HTML report at this path")
    p.add_argument("--summary-json", metavar="PATH",
                   help="write machine-readable run health (counts, fetch errors, "
                        "cache ages) to this path - for gating automated runs")
    p.add_argument("--show-failed", action="store_true", help="also list rejected symbols")
    p.add_argument("-v", "--verbose", action="store_true", help="progress + fetch errors")

    # meta
    p.add_argument("--list-filters", action="store_true", help="show available filters and exit")
    return p


def patch_breakout(cfg: ScanConfig, args) -> None:
    """Apply --fast/--slow/--lookback/--direction to the sma_breakout filter."""
    overrides = {
        "fast": args.fast,
        "slow": args.slow,
        "lookback": args.lookback,
        "direction": args.direction,
    }
    if all(v is None for v in overrides.values()):
        return
    params = {k: v for k, v in overrides.items() if v is not None}

    found = False
    for spec in cfg.filters:
        if spec.get("name") == "sma_breakout":
            spec.setdefault("params", {}).update(params)
            found = True
            break
    if not found:
        cfg.filters.append({"name": "sma_breakout", "params": params})


def main(argv: Optional[List[str]] = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_filters:
        print("Available filters:\n")
        for name, desc in available_filters().items():
            print(f"  {name:<16} {desc}")
        print("\nUse with: --filter NAME:key=value,key=value")
        return 0

    try:
        cfg = ScanConfig.load(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    # CLI overrides (None means "leave the config value alone")
    cfg = cfg.with_overrides(
        universe=args.universe,
        symbols_file=args.symbols_file,
        refresh_tickers=True if args.refresh_tickers else None,
        max_symbols=args.max_symbols,
        etf_limit=args.etf_limit,
        etf_refresh_days=args.etf_refresh_days,
        data_source=args.source,
        history_bars=args.history_bars,
        output_csv=args.output,
        html_output=args.html,
        summary_json=args.summary_json,
        show_failed=True if args.show_failed else None,
        verbose=True if args.verbose else None,
        sort_by=args.sort_by,
        sort_desc=False if args.sort_asc else None,
    )
    if args.source_kw:
        cfg.source_kwargs.update(parse_kv_pairs(args.source_kw))

    if args.filter:
        # --filter replaces the whole set, so the SMA shortcuts cannot be
        # applied. Say so instead of silently ignoring them.
        ignored = [
            name
            for name, value in (
                ("--fast", args.fast), ("--slow", args.slow),
                ("--lookback", args.lookback), ("--direction", args.direction),
            )
            if value is not None
        ]
        if ignored:
            print(
                f"Warning: {', '.join(ignored)} ignored because --filter "
                "replaces the filter set. Put the values in the --filter "
                "spec instead (e.g. --filter sma_breakout:fast=20,slow=50).",
                file=sys.stderr,
            )
        try:
            cfg.filters = [parse_filter_spec(s) for s in args.filter]
        except ValueError as exc:
            print(f"Bad --filter spec: {exc}", file=sys.stderr)
            return 2
    else:
        patch_breakout(cfg, args)

    if args.save_config:
        cfg.save(args.save_config)
        print(f"Config written to {args.save_config}")
        return 0

    # progress goes to stdout so it interleaves nicely with the report
    def progress(msg: str) -> None:
        if cfg.verbose:
            print(msg, flush=True)

    try:
        symbols = (
            [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            if args.symbols
            else None
        )
        scanner = Scanner(cfg, progress=progress)
        result = scanner.run(symbols)
    except Exception as exc:  # noqa: BLE001 - surface a clean CLI error
        print(f"\nScan failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        if cfg.verbose:
            import traceback

            traceback.print_exc()
        return 1

    print_summary(result, show_failed=cfg.show_failed)
    if cfg.verbose:
        print_errors(result)
    # A sort metric no match carries silently falls back to input order, while
    # the summary still claims "Sorted by <metric>". Say so instead.
    if cfg.sort_by and result.n_matches:
        if not any(cfg.sort_by in m.metrics for m in result.matches):
            print(
                f"Warning: no match reports a {cfg.sort_by!r} metric, so the "
                "output is in input order, not ranked by it.",
                file=sys.stderr,
            )

    # Printed unconditionally, not just under -v: this is the line that says
    # whether everything above it is trustworthy.
    summary = build_summary(result, cfg, explicit_symbols=bool(args.symbols))
    print(format_health_line(summary))
    if cfg.summary_json:
        write_summary(summary, cfg.summary_json)
        print(f"Wrote run summary to {cfg.summary_json}")

    if cfg.output_csv:
        path = cfg.output_csv
        write_csv(result.rows(), path)
        print(f"Wrote {result.n_matches} match(es) to {path}\n")

    if cfg.html_output:
        path = write_html(result, cfg.html_output)
        print(f"Wrote HTML report to {path}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
