# SMA Breakout Scanner

Scans the S&P 500 for stocks whose **20-day SMA has broken out above the 50-day SMA**
(a "golden cross"), and is built so the screening conditions can be changed without
touching the scanning engine.

Data comes from **Alpaca Markets** (official `alpaca-py` SDK), which accepts many
symbols per request — the full index costs a handful of HTTP calls.

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
| `universe` | `"sp500"` \| `"file"` | where symbols come from |
| `symbols_file` | string \| null | symbol file, when `universe: "file"` |
| `ticker_cache` | string | local cache for the scraped S&P 500 list |
| `refresh_tickers` | bool | re-scrape the constituent list, ignore cache |
| `max_symbols` | int \| null | cap the universe (fast runs) |
| `data_source` | string | `alpaca`, `yfinance`, `csv`, `synthetic` |
| `source_kwargs` | object | extra constructor args for the source |
| `history_bars` | int | daily bars fetched per symbol |
| `filters` | array | the screen — see below |
| `output_csv` | string \| null | CSV path for matches |
| `html_output` | string \| null | standalone HTML report path |
| `show_failed` | bool | also list rejected symbols and why |
| `verbose` | bool | progress log + fetch errors |

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
  config.py              ScanConfig - universe, source, and the filter list
  data/                  pluggable sources behind one interface
    base.py              DataSource ABC + PriceFrame contract
    alpaca_source.py     Alpaca (SDK primary, REST fallback)
    yfinance_source.py   no-API-key fallback
    csv_source.py        local cached CSVs
    synthetic_source.py  deterministic data for offline tests
    sp500.py             index membership (Wikipedia, cached)
  indicators/
    indicators.py        SMA/EMA/RSI/volume/crossover primitives
    context.py           per-symbol memoized indicator view
  filters/
    base.py              Filter ABC + FilterResult
    registry.py          name -> class lookup
    builtin.py           the concrete conditions
  scanner.py             orchestration: fetch -> evaluate -> collect
  reporter.py            console table + CSV
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
python3.11 main.py -o results/breakouts.csv --html web/index.html
```

**09:00 Asia/Shanghai (UTC+8)** is a good slot: that is 01:00 UTC, after the US
close, so each run reflects the previous *completed* US session rather than a
mid-session partial bar. A full run takes about 25 seconds.

### Use cron on a persistent machine

The hosted task scheduler was tested and **cannot** deliver results here. Its
tasks fire on time but execute in an isolated sandbox with a separate
filesystem - a probe task fired exactly on schedule and its marker file never
appeared in `/workspace` - and no delivery channel was bound, so the output went
nowhere. Daily automation therefore needs an environment where cron owns the
filesystem:

```cron
# 09:00 Asia/Shanghai == 01:00 UTC
0 1 * * * cd /path/to/sma_scanner && python3.11 main.py -o results/breakouts.csv --html web/index.html >> /var/log/sma-scan.log 2>&1
```

If the machine's local time is already UTC+8, use `0 9 * * *` instead.

Keep credentials in `.env` on that machine. Never inline the Alpaca key into a
scheduled-task prompt - task definitions are stored server-side in plaintext.

---

## Tests

```bash
python3.11 -m unittest discover -s tests -v
```

Covers SMA math, crossover detection (including stale-cross and reversal cases),
filter gating, config round-trip, and an end-to-end scan against generated data.

---

## Caveats

- Signals are computed on the bars Alpaca returns; on a free (IEX) feed the current
  day's bar may still be forming. Run after the close for settled values.
- `adjustment=split` is used by default so splits do not create fake crossovers.
- This is a screening tool, not investment advice.
