# SMA Breakout Scanner

Scans the S&P 500 **plus the 100 most-traded US ETFs** for symbols whose
**20-day SMA has broken out above the 50-day SMA** (a "golden cross"), and is built
so the screening conditions can be changed without touching the scanning engine.

Results are ranked by **breakout-day volume vs its 20-day baseline** (highest
conviction first) in every output form — console, CSV, and the HTML report.

Data comes from **Alpaca Markets** (official `alpaca-py` SDK), which accepts many
symbols per request — the full universe costs a handful of HTTP calls.

---

## Quick start

```bash
cd /workspace/sma_scanner
python3.11 -m pip install -r requirements.txt

cp .env.example .env      # then paste your Alpaca keys into .env
python3.11 main.py
```

### Alpaca credentials

Market data is served from `https://data.alpaca.markets` for **both paper and live
keys**, so a paper key works fine here. (`paper-api.alpaca.markets` is for *orders*
only — it does not serve bars.)

```
ALPACA_API_KEY=your_key_id
ALPACA_SECRET_KEY=your_secret
```

Keys are read from `.env` or the environment; they are never written into source,
and `.env` is gitignored.

---

## What counts as a breakout

The screen is deliberately stricter than "20 SMA is currently above 50 SMA":

1. The 20-day SMA crossed **above** the 50-day SMA, and
2. that crossing happened within the last **10 trading days** (fresh signal), and
3. the cross has **not since been reversed** by a death cross, and
4. price is trading **above the 200-day SMA** (trend gate), and
5. optionally the current gap between the SMAs meets a minimum spread.

Condition 1 alone would match stocks that crossed months ago; condition 2 makes it
an actionable signal; condition 4 keeps breakouts occurring inside a downtrend out
of the result set.

### Breakout-day volume

Every match reports how the breakout bar's volume compared with its own baseline:

```
VOLxAVG = breakout-day volume / average volume of the 20 bars before it
```

The baseline **excludes the breakout bar itself**, otherwise a spike would inflate
the very average it is measured against. `>1.0x` means the cross came on
above-normal participation. The CSV also carries the raw numbers
(`cross_volume`, `pre_cross_avg_volume`, `volume_ratio`).

To require conviction rather than just report it:

```bash
python3.11 main.py --filter sma_breakout:min_volume_ratio=1.5 --filter above_sma:period=200
```

---

## Output ordering

Matches are ranked by `volume_ratio` — **highest conviction first** — in the console
table, the CSV, and the HTML report alike. The HTML report also highlights the
ranked column, and its column headers stay clickable for ad-hoc re-sorting.

Symbols with no volume metric (NaN or absent) sort **last** rather than being
dropped, so a filter set that never emits `volume_ratio` still reports everything.

```bash
python3.11 main.py --sort-by spread_pct    # rank by SMA gap instead
python3.11 main.py --sort-asc              # weakest conviction first
python3.11 main.py --sort-by ""            # plain alphabetical
```

Column headers follow the periods you configured — `--fast 50 --slow 200` prints
`SMA50`/`SMA200`, and the trend gate adds an `SMA200` column (or `SMA50` with
`--filter above_sma:period=50`). Labels always match the numbers beneath them.

---

## Universe

The default universe is **S&P 500 + the 100 most-traded US ETFs** (~603 symbols).

| `universe` | Contents |
|---|---|
| `both` | S&P 500 + most-traded ETFs (default) |
| `sp500` | S&P 500 constituents only |
| `etf` | Most-traded US ETFs only |
| `file` | Your own symbol file (`symbols_file`) |

```bash
python3.11 main.py                      # both (default)
python3.11 main.py --universe sp500     # stocks only
python3.11 main.py --universe etf       # ETFs only
```

### The ETF list

ETFs come from TradingView's
[most-traded US ETFs](https://www.tradingview.com/markets/etfs/funds-most-traded/)
table, which ranks by *dollar* volume (Price × Volume) — a better liquidity measure
than raw share count, since a $5 fund trading 10M shares is less tradeable than a
$500 fund trading 1M. The page is server-rendered, so no browser is needed.

The list is cached to `data/most_traded_etfs.csv` **with a fetch timestamp**, and is
re-scraped automatically once the cache is older than `etf_refresh_days` (7 days) —
that satisfies "refresh every week" without a separate cron entry. Membership drifts
slowly, so a failed refresh falls back to the stale cache with a warning instead of
aborting the scan.

```bash
python3.11 main.py --etf-limit 50          # fewer ETFs
python3.11 main.py --etf-refresh-days 1    # refresh daily instead
python3.11 main.py --refresh-tickers       # force refresh now (both lists)
```

---

## Changing the filtering conditions

Filters are selected by **name** from config, so conditions are data, not code.

```bash
# Tighten the window to 5 bars
python3.11 main.py --lookback 5

# Look for breakdowns instead of breakouts
python3.11 main.py --direction down

# Different averages (e.g. 50 over 200)
python3.11 main.py --fast 50 --slow 200

# Compose several conditions (all must pass)
python3.11 main.py \
  --filter sma_breakout:fast=20,slow=50,lookback=10 \
  --filter min_price:min_price=15 \
  --filter min_avg_volume:min_volume=1000000

# See every available filter
python3.11 main.py --list-filters
```

### Configuration file

Every setting can live in a JSON file (YAML too, if `pyyaml` is installed)
instead of flags. Start from [`examples/scan_config.json`](examples/scan_config.json),
or generate one from the current flags:

```bash
python3.11 main.py --save-config scan.json   # writes config, then exits
python3.11 main.py --config scan.json        # run it
```

**Precedence:** defaults → config file → CLI flags. Any flag you pass on the
command line wins over the file, and a flag left unset leaves the file's value
alone. So `--config` gives you a saved baseline you can still tweak per run:

```bash
python3.11 main.py --config scan.json --max-symbols 40   # quick smoke test
```

| Key | Type | Meaning |
|---|---|---|
| `universe` | `"sp500"` \| `"etf"` \| `"both"` \| `"file"` | where symbols come from |
| `symbols_file` | string \| null | symbol file, when `universe: "file"` |
| `ticker_cache` | string | local cache for the scraped S&P 500 list |
| `etf_cache` | string | local cache for the ETF list |
| `etf_limit` | int | how many most-traded ETFs to include |
| `etf_refresh_days` | int | re-scrape the ETF list once cache is this old |
| `refresh_tickers` | bool | force re-scrape every list, ignore cache |
| `max_symbols` | int \| null | cap the universe (fast runs) |
| `data_source` | string | `alpaca`, `yfinance`, `csv`, `synthetic` |
| `source_kwargs` | object | extra constructor args for the source |
| `history_bars` | int | daily bars per symbol (fetched by Alpaca, trimmed by others) |
| `filters` | array | the screen — see below |
| `output_csv` | string \| null | CSV path for matches |
| `html_output` | string \| null | standalone HTML report path |
| `summary_json` | string \| null | machine-readable run health (see below) |
| `show_failed` | bool | also list rejected symbols and why |
| `verbose` | bool | progress log + fetch errors |
| `sort_by` | string \| null | metric used to rank output (default `volume_ratio`) |
| `sort_desc` | bool | `true` = highest first |

`filters` is an ordered list; **all** must pass for a symbol to match, and
evaluation short-circuits on the first failure. Each entry is
`{"name": ..., "params": {...}}`, where `params` keys are the filter's own
arguments (same names as the table below). A `null` param means "no gate" —
e.g. `"min_volume_ratio": null` reports the volume ratio without requiring one.

Unknown top-level keys are rejected with an error rather than silently ignored,
so typos fail loudly.

> Note: `--symbols` (an explicit comma-separated list) is CLI-only — it has no
> config-file equivalent.

### Built-in filters

| Name | Purpose | Key params |
|---|---|---|
| `sma_breakout` | fast/slow SMA crossover within N bars | `fast`, `slow`, `lookback`, `direction`, `min_spread_pct`, `volume_lookback`, `min_volume_ratio` |
| `min_price` | minimum share price | `min_price` |
| `min_avg_volume` | liquidity screen | `min_volume`, `period` |
| `above_sma` | price above a long-term SMA (trend) | `period` |
| `rsi_range` | RSI inside a band | `low`, `high`, `period` |

### Adding a new condition

Write one class and register it — no other file changes:

```python
# sma_scanner/filters/builtin.py
from .base import Filter, FilterResult
from .registry import register

@register
class GapUpFilter(Filter):
    name = "gap_up"
    description = "Yesterday's close was at least X% above the prior close."

    def __init__(self, min_pct: float = 3.0):
        super().__init__(min_pct=min_pct)
        self.min_pct = float(min_pct)

    def evaluate(self, ctx) -> FilterResult:
        change = ctx.change_pct(1)
        if change < self.min_pct:
            return self.fail(f"move {change:.2f}% < {self.min_pct}%", change_pct=change)
        return self.ok(f"moved {change:.2f}%", change_pct=change)
```

It is immediately usable: `--filter gap_up:min_pct=4`.

Note that `--filter` **replaces** the default set, so including the trend gate
means naming it: `--filter sma_breakout --filter above_sma:period=200`.

---

## Architecture

```
main.py                  CLI: flags -> config -> scan -> report
sma_scanner/
  config.py              ScanConfig - universe, source, filters, ranking
  data/                  pluggable sources behind one interface
    base.py              DataSource ABC + PriceFrame contract
    alpaca_source.py     Alpaca (SDK primary, REST fallback)
    yfinance_source.py   no-API-key fallback
    csv_source.py        local cached CSVs
    synthetic_source.py  deterministic data for offline tests
    sp500.py             index membership (Wikipedia, cached)
    etf_list.py          most-traded ETFs (TradingView, cached + timestamped)
  indicators/
    indicators.py        SMA/EMA/RSI/volume/crossover primitives
    context.py           per-symbol memoized indicator view
  filters/
    base.py              Filter ABC + FilterResult
    registry.py          name -> class lookup
    builtin.py           the concrete conditions
  scanner.py             orchestration: fetch -> evaluate -> collect
  reporter.py            console table + CSV
  summary.py             run health for schedulers (counts, caches, gate)
```

Each layer only knows the interface below it. `scanner.py` never references a
specific filter or data source — that is why new conditions need no engine changes.

---

## Data sources

```bash
--source alpaca      # default; reads ALPACA_* credentials
--source yfinance    # no key, but Yahoo rate-limits shared IPs aggressively
--source csv         # --source-kw directory=path/to/csvs
--source synthetic   # generated data, for offline runs
```

Useful flags: `--symbols AAPL,MSFT` (explicit list), `--max-symbols 25` (quick run),
`--history-bars 400`, `-o results.csv`, `--html web/index.html`, `--show-failed`, `-v`.

**On `history_bars`:** Alpaca fetches exactly that many bars per symbol. The other
sources can only *trim* to it — yfinance is queried by calendar `period` (default
`1y`), CSV returns whatever the file holds, and synthetic always generates its
configured `days`. So raising `history_bars` only reaches further back on Alpaca.

---

## Web report

```bash
python3.11 main.py -o results/breakouts.csv --html web/index.html
```

Writes a single self-contained HTML file - no CDN, no build step, opens straight
from disk or drops onto any static host. Columns are click-to-sort, and the
volume column is colour-coded: green at or above 1.5x baseline (strong
conviction), amber below 0.8x (weak).

## Running it daily

The command to run each day:

```bash
python3.11 main.py -o results/breakouts.csv --html site/index.html \
  --summary-json results/summary.json
```

**09:00 Asia/Shanghai (UTC+8)** is a good slot: that is 01:00 UTC, after the US
close, so each run reflects the previous *completed* US session rather than a
mid-session partial bar. A full run takes about 25 seconds.

### Don't publish a run you can't trust

The scanner is deliberately forgiving: a symbol that fails to fetch is skipped,
and a failed universe refresh falls back to the stale cache with a warning.
Neither raises. That is right for an interactive run, and wrong for a scheduled
one, where the failure mode is a page that looks plausible and is empty.

`--summary-json` writes down what the run knows about itself:

```json
{
  "universe_size": 603, "evaluated": 603, "skipped": 0, "matches": 22,
  "fetch_errors": 0, "fetch_error_rate": 0.0, "fetch_error_symbols": [],
  "caches": {"tickers": {"age_days": 0.0, "stale": false},
             "etfs": {"age_days": 0.06, "stale": false}},
  "universe_source": "both"
}
```

Gate a run on it before publishing. The exit code is 0 only if it clears every
threshold:

```bash
python3.11 -m sma_scanner.summary results/summary.json
python3.11 -m sma_scanner.summary results/summary.json \
    --min-universe 500 --min-evaluated-ratio 0.95 --max-fetch-error-rate 0.02 \
    --markdown "$GITHUB_STEP_SUMMARY"
```

Four checks: the universe is not suspiciously small (a missing or truncated
symbol list), at least 95% of it was evaluated, fewer than 2% of symbols failed
to fetch, and no cache the run actually used is stale. A handful of bad symbols
is normal and passes; a data outage does not. Only caches the run read are
judged - a `--symbols` run consults no ticker list, so it is not judged on one.

Both schedulers below call this same one-liner, so a run behaves identically
whichever one you pick.

### Use cron on a persistent machine

The hosted task scheduler was tested and **cannot** deliver results here. Its
tasks fire on time but execute in an isolated sandbox with a separate
filesystem - a probe task fired exactly on schedule and its marker file never
appeared in `/workspace` - and no delivery channel was bound, so the output went
nowhere. Daily automation therefore needs an environment where cron owns the
filesystem. [`scripts/run_scan.sh`](scripts/run_scan.sh) wraps the whole thing -
lock, retry, gate, publish:

```bash
chmod +x scripts/run_scan.sh
crontab -e
# 01:00 UTC == 09:00 Asia/Shanghai
0 1 * * * /opt/sma-breakout-scanner/scripts/run_scan.sh >> /var/log/sma-scan.log 2>&1
```

If the machine's local time is already UTC+8, use `0 9 * * *` instead. Note that
the system timezone decides when cron fires; the `TZ` the script exports only
affects the report's own timestamp. Add a logrotate entry for that log - it is
appended to every day.

A step-by-step runbook for a fresh box - packages, venv, deploy key, cron,
logrotate, and a troubleshooting table - is in
[`docs/vps-setup.md`](docs/vps-setup.md).

The script runs the scan (retrying transient failures), gates it, and only then
pushes `site/index.html` to the `gh-pages` branch that GitHub Pages serves. A
failed gate means nothing is pushed, so the last good report stays live. It
refuses to run twice at once, and can be exercised without publishing:

```bash
./scripts/run_scan.sh --no-publish              # scan + gate, leave the file
EXTRA_ARGS="--universe sp500" ./scripts/run_scan.sh
```

Publishing needs a credential for the remote; an SSH deploy key scoped to this
repository is the right shape. Keep credentials in `.env` on that machine.
Never inline the Alpaca key into a scheduled-task prompt - task definitions are
stored server-side in plaintext.

### Or: let GitHub Actions run it

[`.github/workflows/daily-scan.yml`](.github/workflows/daily-scan.yml) does the
same thing with no server: it installs the dependencies, runs the scan, uploads
the HTML as a Pages artifact and deploys it. Setup is three steps:

1. Add `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` as repository secrets
   (Settings -> Secrets and variables -> Actions).
2. Set Pages to build from Actions (Settings -> Pages -> Source -> **GitHub
   Actions**). One-time only.
3. Done - it runs at 01:17 UTC (09:17 Asia/Shanghai) and on demand via
   *Run workflow*.

Notes that matter:

- **Schedule is best-effort.** Scheduled runs are queued like any other job and
  can be delayed under load, or occasionally dropped. The workflow deliberately
  avoids the top of the hour, where that is most likely. A late run is harmless
  here, but do not treat the timestamp as precise.
- **Public repos go to sleep.** GitHub disables scheduled workflows in a public
  repository after 60 days with no repository activity, and an artifact deploy
  pushes no commits. If the schedule stops, re-enable it on the Actions tab, or
  commit the daily CSV back to the repo to keep it active.
- **Nothing persists between runs.** The universe caches (`data/*.csv`) are
  rebuilt from Wikipedia and TradingView every run, which costs a few seconds
  and keeps the universe fresh.
- **Failed scans do not publish.** The workflow retries three times, then runs
  the same health gate described above against `results/summary.json`. A run
  that fetched nothing fails the job instead of replacing a good page with an
  empty one, so the previously published report stays live. The counters are
  also written to the run's summary page, so every run documents its own health,
  and the CSV plus the summary JSON are kept as artifacts even when the job
  fails.

---

## Tests

```bash
python3.11 -m unittest discover -s tests -v
```

Covers SMA math, crossover detection (including stale-cross and reversal cases),
filter gating, volume metrics, result ranking, ETF parsing (rank order, de-duping,
non-US filtering), ETF cache freshness and stale fallback, universe composition and
de-duplication, config round-trip, and an end-to-end scan against generated data.

115 tests, no network access required. Also covers the regression cases that were
reported as latent bugs: config mutation leaking into defaults, RSI warm-up bars,
the crossover `lookback` boundary, CSV `Adj Close` handling, filter validation,
report labels following the configured periods, engineered breakouts clearing the
default 200-day trend gate, ranking by a date metric, duplicate `Close` columns
from Yahoo, and an ETF cache too short for the requested `--etf-limit`. A later
review added: NaN metrics failing (rather than silently clearing) the numeric
gates, the volume-lookback window staying inside its own bounds, and a flat price
series reading as neutral RSI rather than overbought.

Run health has its own coverage: cache age thresholds and staleness, the data
outage that must be refused, the handful of bad symbols that must not be, and
the gate CLI's exit codes - including that an unwritable run-summary file
cannot turn a healthy run into a failure.

---

## Caveats

- Signals are computed on the bars Alpaca returns; on a free (IEX) feed the current
  day's bar may still be forming. Run after the close for settled values.
- `adjustment=split` is used by default so splits do not create fake crossovers.
- The ETF list depends on TradingView's markup. If it changes, the scraper finds no
  tickers and falls back to the cached list with a warning rather than silently
  scanning a short universe.
- Dollar-volume ranking means the ETF list can include leveraged/inverse funds
  (e.g. TQQQ, SOXL). They are liquid, but they are not buy-and-hold instruments —
  filter them out with `--filter min_price:...` style gates if you don't want them.
- This is a screening tool, not investment advice.
