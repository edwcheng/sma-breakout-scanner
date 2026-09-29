# VPS setup

Setting up the daily scan on a cloud Ubuntu VPS, with the repo at
`/home/ubuntu/projects/sma-breakout-scanner` and the report published to
GitHub Pages. Run each block in order; nothing here needs root except where
`sudo` is shown.

The box needs **no inbound ports** - the scan only makes outbound HTTPS calls
and pushes to GitHub. If `ufw` is enabled, you do not need to open anything.

---

## 1. Base packages and clock

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git python3-venv python3-pip cron util-linux

# Readable log lines and a report timestamp that matches your day.
sudo timedatectl set-timezone Asia/Shanghai
timedatectl
```

**The system timezone decides when cron fires.** With the box on
`Asia/Shanghai`, `0 9 * * *` means 09:00 local = 01:00 UTC, which is after the
US close. If you leave the box on UTC, use `0 1 * * *` instead. The `TZ` the
script exports only affects the report's own timestamp and the log lines - it
does **not** change the cron schedule.

Check Python:

```bash
python3 --version    # 3.10+ is fine; 24.04 ships 3.12
```

## 2. Clone

```bash
mkdir -p ~/projects && cd ~/projects
git clone https://github.com/edwcheng/sma-breakout-scanner.git
cd sma-breakout-scanner
```

## 3. Virtualenv and dependencies

The venv lives inside the repo, which is already gitignored.

```bash
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
```

`pandas`, `numpy` and `lxml` all ship manylinux wheels, so no compiler or
`python3-dev` is needed.

Skipping the venv: if the system Python already has everything
(`python3 -c "import pandas,numpy,lxml,requests,yaml,alpaca,yfinance"` prints
OK), step 3 can be skipped. `scripts/run_scan.sh` defaults to
`$REPO_DIR/venv/bin/python`, so every call below then needs an override:
`PY=/usr/bin/python3 bash scripts/run_scan.sh ...`, and the step 8 cron line
needs the same `PY=/usr/bin/python3` prefix.

## 4. Credentials

```bash
cp .env.example .env
chmod 600 .env
nano .env      # paste ALPACA_API_KEY and ALPACA_SECRET_KEY
```

Paper keys work: historical bars come from `data.alpaca.markets` for both paper
and live accounts.

Confirm the box can reach all three data sources before going further:

```bash
for url in \
  "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies" \
  "https://www.tradingview.com/markets/etfs/funds-most-traded/" \
  "https://data.alpaca.markets/v2/stocks/bars?symbols=AAPL"; do
  printf '%s -> ' "$url"
  curl -sS -o /dev/null -w '%{http_code}\n' --max-time 20 "$url"
done
```

`401` from Alpaca is a pass - it means reachable and unauthenticated.

## 5. First run

This also builds the two universe caches (`data/*.csv` are gitignored, so they
do not exist after a clone) and proves the whole path works.

```bash
mkdir -p results site
./venv/bin/python main.py -v \
  -o results/breakouts.csv \
  --html site/index.html \
  --summary-json results/summary.json
```

Without a venv, replace `./venv/bin/python` with `python3`.

Takes about 25 seconds. What to look for at the end:

```
health: universe=603 evaluated=603 skipped=0 matches=22 fetch_errors=0 tickers_cache=0.0d etfs_cache=0.06d
```

`universe` around 603 and `fetch_errors=0` means it worked. `universe` around
100 means the S&P 500 scrape failed and only ETFs loaded - the health gate will
refuse to publish that, which is the intended behaviour.

## 6. Let the VPS push to GitHub

An SSH deploy key scoped to this one repository is the right shape - it cannot
touch anything else in your account.

```bash
ssh-keygen -t ed25519 -C "sma-scanner@$(hostname)" -f ~/.ssh/sma_deploy -N ""
cat ~/.ssh/sma_deploy.pub
```

Add that public key at: **repo → Settings → Deploy keys → Add deploy key**, and
tick **Allow write access**.

Then point git at it through a host alias, so it does not interfere with any
other key on the box:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github-sma
  HostName github.com
  User git
  IdentityFile ~/.ssh/sma_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config

cd ~/projects/sma-breakout-scanner
git remote set-url origin git@github-sma:edwcheng/sma-breakout-scanner.git

# "Hi edwcheng/sma-breakout-scanner! You've successfully authenticated..."
ssh -T git@github-sma
```

## 7. Publish the first report

Dry run first - scans, gates, and leaves the file on disk without pushing:

```bash
cd ~/projects/sma-breakout-scanner
bash scripts/run_scan.sh --no-publish
```

Then for real. The first push creates the `gh-pages` branch as an orphan
containing only the report:

```bash
bash scripts/run_scan.sh
```

Without a venv, prefix both with `PY=/usr/bin/python3`, e.g.
`PY=/usr/bin/python3 bash scripts/run_scan.sh --no-publish`.

Now turn on Pages: **repo → Settings → Pages → Source: Deploy from a branch →
`gh-pages` / `/ (root)`**. The report appears at
`https://edwcheng.github.io/sma-breakout-scanner/` within a minute or two.

## 8. Schedule it

Create the log file first - cron's `>>` runs as `ubuntu`, and a redirect into
`/var/log` fails before the script even starts if the file does not exist and is
not writable:

```bash
sudo touch /var/log/sma-scan.log
sudo chown ubuntu:adm /var/log/sma-scan.log
```

Then:

```bash
crontab -e
```

Add (box on Asia/Shanghai):

```cron
# 09:00 Asia/Shanghai == 01:00 UTC, after the US close.
0 9 * * * /home/ubuntu/projects/sma-breakout-scanner/scripts/run_scan.sh >> /var/log/sma-scan.log 2>&1
```

If you left the box on UTC, use `0 1 * * *` instead.

Without a venv, prefix the command so the script does not look for
`venv/bin/python`:

```cron
0 9 * * * PY=/usr/bin/python3 /home/ubuntu/projects/sma-breakout-scanner/scripts/run_scan.sh >> /var/log/sma-scan.log 2>&1
```

Optional - failure notifications. Set it in the crontab, since cron does not
read your shell profile:

```cron
NOTIFY_URL=https://ntfy.sh/your-private-topic
0 9 * * * /home/ubuntu/projects/sma-breakout-scanner/scripts/run_scan.sh >> /var/log/sma-scan.log 2>&1
```

No Alpaca keys belong here - `main.py` reads `.env` from the repo root.

## 9. Log rotation

The script appends a few lines a day, so this is housekeeping rather than an
emergency:

```bash
sudo tee /etc/logrotate.d/sma-scan >/dev/null <<'EOF'
/var/log/sma-scan.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 ubuntu adm
    su ubuntu adm
}
EOF
sudo logrotate -d /etc/logrotate.d/sma-scan    # dry run
```

`su ubuntu adm` is required on current Ubuntu: `/var/log` is group-writable
(`775 root:syslog`), and logrotate 3.19+ refuses to rotate without it
(`error: skipping ... because parent directory has insecure permissions`).
If you also run the monthly housekeeping script below, list both logs on the
first line: `/var/log/sma-scan.log /var/log/sma-housekeeping.log {`.

Rotation lands next to the live log: `/var/log/sma-scan.log.1` (yesterday,
plain text due to `delaycompress`), then `.2.gz` ... `.14.gz`. So `daily` +
`rotate 14` keeps roughly 14 days of history.

## 10. Verify the schedule fired

```bash
systemctl status cron --no-pager
grep CRON /var/log/syslog | tail -5
tail -30 /var/log/sma-scan.log
```

The first real cron run is the only step that cannot be tested by hand, so
check the log the next morning.

---

## When it breaks

The script always exits 0 only if it published, and every failure names itself
in the log with an `ERROR:` line. The gate's `UNHEALTHY:` lines say exactly
which threshold failed.

| Symptom | Cause |
|---|---|
| Log file empty, or cron emails an error | The redirect failed. Check `sudo touch` / `chown` from step 8. |
| `UNHEALTHY: universe 100 < 500` | The S&P 500 scrape failed; only ETFs loaded. Usually Wikipedia markup or a blocked request. |
| `UNHEALTHY: fetch error rate ...` | Alpaca rejected or timed out on most symbols. Check the key in `.env`, and the `e.g. AAPL` symbols named in the message. |
| `UNHEALTHY: etfs cache 9.2 days old` | The TradingView refresh failed and the stale list was used. Re-run with `--refresh-tickers` to see the error. |
| `ERROR: the scan failed after 3 attempt(s)` | The scan itself raised. Run it by hand with `-v` for the traceback. |
| Report on Pages is older than yesterday | The gate refused to publish, or the push failed. `tail /var/log/sma-scan.log` says which. |
| `another run holds .../scan.lock` | Overlapping runs. Harmless unless it repeats - lower the frequency or raise `ATTEMPTS`. |

## Maintenance

- Update dependencies occasionally: `./venv/bin/pip install -r requirements.txt --upgrade`
  (without a venv, this is a system-wide upgrade - use with care).
- Monthly housekeeping: `scripts/monthly_housekeeping.sh` checks the log file,
  logrotate config, disk use, `.env` perms, cron entry, report freshness, git
  cleanliness, and security updates. Schedule it for the 1st of each month,
  after the daily scan:

  ```bash
  sudo touch /var/log/sma-housekeeping.log
  sudo chown ubuntu:adm /var/log/sma-housekeeping.log
  crontab -e
  ```

  ```cron
  0 2 1 * * /home/ubuntu/projects/sma-breakout-scanner/scripts/monthly_housekeeping.sh >> /var/log/sma-housekeeping.log 2>&1
  ```

- Disk does not grow: the CSV and HTML are overwritten each run, and logs rotate.
- If the Alpaca key is rotated, update `.env`. Nothing else changes.
- `sudo apt install unattended-upgrades` if the image does not already have it.
- Rotating the deploy key: generate a new one, add it in the repo settings,
  then remove the old key.
