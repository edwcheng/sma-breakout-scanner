"""Machine-readable run health.

A scan can exit 0 and still produce a worthless report:

- a data outage makes every symbol fail to load, so everything is skipped and
  zero matches is technically correct but says nothing about the market;
- a failed universe refresh falls back to a stale cache with a warning, which
  quietly shrinks or ages the universe.

Neither raises, so neither fails the process. For a human reading the console
that is fine. For a scheduled run that publishes whatever it produced, it is
not: the failure mode is a page that looks plausible and is wrong.

This module turns the scanner's own counters into one JSON blob that an
automated runner can gate on, plus a one-line rendering for logs. It reports
facts only - what counts as unhealthy is policy, and belongs to whoever is
doing the scheduling.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:  # keeps this module stdlib-only, so the gate CLI stays light
    from .scanner import ScanResult

#: How many failing symbols to name in the summary. Enough to spot a pattern
#: (one exchange, one letter of the alphabet) without bloating the file.
MAX_NAMED_ERRORS = 25


def cache_health(
    path: str,
    *,
    now: datetime,
    max_age_days: Optional[float] = None,
) -> Dict[str, Any]:
    """Describe one ticker cache: does it exist, how old is it, is it stale.

    `stale` is None when no threshold applies - we report the age without
    pretending to judge it.
    """
    p = Path(str(path)).expanduser()
    info: Dict[str, Any] = {
        "path": str(p),
        "exists": p.exists(),
        "age_days": None,
        "max_age_days": max_age_days,
        "stale": None,
    }
    if not p.exists():
        # The lists are written on a successful refresh, so a missing cache
        # means the last refresh failed and there was nothing to fall back to.
        info["stale"] = True
        return info
    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    age = (now - mtime).total_seconds() / 86400.0
    info["age_days"] = round(age, 2)
    info["stale"] = None if max_age_days is None else age > max_age_days
    return info


def build_summary(
    res: ScanResult,
    cfg: Any = None,
    *,
    now: Optional[datetime] = None,
    explicit_symbols: bool = False,
) -> Dict[str, Any]:
    """Collect everything an automated run needs to judge its own output.

    `explicit_symbols` marks a run driven by an explicit `--symbols` list. Such
    a run reads no ticker cache at all, so judging cache freshness would be
    reporting on a file nobody opened.
    """
    now = now or datetime.now(timezone.utc)
    universe = res.universe_size
    evaluated = res.evaluated
    skipped = sum(1 for r in res.results if r.skipped)
    errors = len(res.fetch_errors)

    summary: Dict[str, Any] = {
        "generated_utc": now.isoformat(timespec="seconds"),
        "data_source": res.data_source,
        "universe_size": universe,
        "evaluated": evaluated,
        "skipped": skipped,
        "matches": res.n_matches,
        "fetch_errors": errors,
        "fetch_error_rate": round(errors / universe, 4) if universe else None,
        "fetch_error_symbols": sorted(res.fetch_errors)[:MAX_NAMED_ERRORS],
        "filters": list(res.filters_used),
        "sort_by": res.sort_by,
        "caches": {},
    }

    if cfg is not None:
        kind = "explicit" if explicit_symbols else getattr(cfg, "universe", "both")
        summary["universe_source"] = kind
        # Only judge caches the run actually used - an ETF cache nobody read
        # is not evidence of anything.
        if kind in {"both", "sp500"}:
            summary["caches"]["tickers"] = cache_health(cfg.ticker_cache, now=now)
        if kind in {"both", "etf"}:
            summary["caches"]["etfs"] = cache_health(
                cfg.etf_cache, now=now, max_age_days=cfg.etf_refresh_days
            )
    return summary


def format_health_line(summary: Dict[str, Any]) -> str:
    """One line for the console / CI log, so the numbers are never far away."""
    parts = [
        f"universe={summary['universe_size']}",
        f"evaluated={summary['evaluated']}",
        f"skipped={summary['skipped']}",
        f"matches={summary['matches']}",
        f"fetch_errors={summary['fetch_errors']}",
    ]
    for name, info in summary.get("caches", {}).items():
        age = info.get("age_days")
        parts.append(f"{name}_cache={'missing' if age is None else f'{age}d'}")
    return "health: " + " ".join(parts)


def write_summary(summary: Dict[str, Any], path: str) -> str:
    """Write the summary as JSON. Returns the path written."""
    out = Path(str(path)).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return str(out)


# ----------------------------------------------------------------------
# Gating
# ----------------------------------------------------------------------
#: Thresholds for a run worth publishing. Loose enough that a normal day never
#: trips them, tight enough that a data outage always does.
DEFAULT_MIN_UNIVERSE = 500
DEFAULT_MIN_EVALUATED_RATIO = 0.95
DEFAULT_MAX_FETCH_ERROR_RATE = 0.02


def check(
    summary: Dict[str, Any],
    *,
    min_universe: int = DEFAULT_MIN_UNIVERSE,
    min_evaluated_ratio: float = DEFAULT_MIN_EVALUATED_RATIO,
    max_fetch_error_rate: float = DEFAULT_MAX_FETCH_ERROR_RATE,
) -> List[str]:
    """Return the reasons this run should not be published. Empty = healthy."""
    problems: List[str] = []
    universe = summary.get("universe_size") or 0
    evaluated = summary.get("evaluated") or 0

    if universe < min_universe:
        problems.append(
            f"universe {universe} < {min_universe} - a symbol list is missing, "
            "truncated or stale"
        )
    if universe and evaluated < min_evaluated_ratio * universe:
        problems.append(
            f"only {evaluated}/{universe} symbols were evaluated "
            f"({evaluated / universe:.0%} < {min_evaluated_ratio:.0%})"
        )

    rate = summary.get("fetch_error_rate")
    if rate is not None and rate > max_fetch_error_rate:
        named = ", ".join((summary.get("fetch_error_symbols") or [])[:5])
        problems.append(
            f"fetch error rate {rate:.1%} > {max_fetch_error_rate:.1%} "
            f"({summary.get('fetch_errors')} symbols; e.g. {named})"
        )

    for name, info in (summary.get("caches") or {}).items():
        if info.get("stale"):
            age = info.get("age_days")
            detail = "missing" if age is None else f"{age} days old"
            problems.append(f"{name} cache {detail} - the refresh failed")
    return problems


def format_markdown(summary: Dict[str, Any]) -> str:
    """A small table for a CI run summary - every run documents its own health."""
    rows = [
        ("Generated (UTC)", summary.get("generated_utc")),
        ("Data source", summary.get("data_source")),
        ("Universe source", summary.get("universe_source")),
        ("Universe", summary.get("universe_size")),
        ("Evaluated", summary.get("evaluated")),
        ("Skipped", summary.get("skipped")),
        ("Matches", summary.get("matches")),
        ("Fetch errors", summary.get("fetch_errors")),
    ]
    for name, info in (summary.get("caches") or {}).items():
        age = info.get("age_days")
        rows.append(
            (f"{name} cache",
             f"{'missing' if age is None else f'{age} d'}"
             f"{' (stale)' if info.get('stale') else ''}")
        )
    lines = ["| Run health | |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """Gate a run: exit 0 only if the summary clears every threshold.

        python -m sma_scanner.summary results/summary.json \\
            --markdown "$GITHUB_STEP_SUMMARY"
    """
    ap = argparse.ArgumentParser(
        prog="python -m sma_scanner.summary",
        description="Check a scan's health summary; non-zero exit if unhealthy.",
    )
    ap.add_argument("path", help="summary JSON written by main.py --summary-json")
    ap.add_argument("--min-universe", type=int, default=DEFAULT_MIN_UNIVERSE)
    ap.add_argument("--min-evaluated-ratio", type=float,
                    default=DEFAULT_MIN_EVALUATED_RATIO)
    ap.add_argument("--max-fetch-error-rate", type=float,
                    default=DEFAULT_MAX_FETCH_ERROR_RATE)
    ap.add_argument("--markdown", metavar="PATH",
                    help="append a markdown table here (e.g. $GITHUB_STEP_SUMMARY)")
    args = ap.parse_args(argv)

    try:
        summary = json.loads(Path(args.path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read summary: {exc}", file=sys.stderr)
        return 1

    print(format_health_line(summary))
    if args.markdown:
        try:
            with Path(args.markdown).open("a", encoding="utf-8") as fh:
                fh.write(format_markdown(summary) + "\n")
        except OSError as exc:  # a missing run-summary file must not fail the gate
            print(f"warning: could not write markdown: {exc}", file=sys.stderr)

    problems = check(
        summary,
        min_universe=args.min_universe,
        min_evaluated_ratio=args.min_evaluated_ratio,
        max_fetch_error_rate=args.max_fetch_error_rate,
    )
    for problem in problems:
        print(f"UNHEALTHY: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
