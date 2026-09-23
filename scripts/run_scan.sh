#!/usr/bin/env bash
#
# Daily SMA scan for a persistent machine (cron or a systemd timer).
#
#   /opt/sma-breakout-scanner/scripts/run_scan.sh
#
# What it does, in order:
#   1. refuses to start if another run is still going        (flock)
#   2. runs the scan, retrying transient failures            (ATTEMPTS)
#   3. checks the run's own health counters                  (the gate)
#   4. publishes only if the gate passed                     (git push)
#
# Step 3 is the point of the whole thing. The scanner deliberately does not
# fail on a data outage - it skips symbols and falls back to stale caches - so
# "it exited 0" is not the same as "the report is worth publishing". On a
# failure nothing is pushed, so the previously published report stays live
# rather than being replaced by a plausible-looking empty one.
#
# Install:
#   chmod +x scripts/run_scan.sh
#   crontab -e
#   0 1 * * * /opt/sma-breakout-scanner/scripts/run_scan.sh >> /var/log/sma-scan.log 2>&1
#
# Add a logrotate entry for /var/log/sma-scan.log - this appends every day.
#
# Publishing needs a credential for the git remote: an SSH deploy key scoped to
# this repository (recommended) or a credential helper. The scanner's Alpaca
# keys stay in .env next to the code and are never touched by this script.
#
# Every setting below can be overridden from the environment.

set -euo pipefail

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV="${VENV:-$REPO_DIR/venv}"
PY="${PY:-$VENV/bin/python}"
SITE_DIR="${SITE_DIR:-$REPO_DIR/site}"
RESULTS_DIR="${RESULTS_DIR:-$REPO_DIR/results}"

#: Working checkout of the Pages branch. Created on first run.
PUBLISH_DIR="${PUBLISH_DIR:-$REPO_DIR/publish}"
PUBLISH_BRANCH="${PUBLISH_BRANCH:-gh-pages}"
PUBLISH="${PUBLISH:-1}"        # 0 = write the report, do not push
REMOTE="${REMOTE:-origin}"
GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-sma-scanner}"
GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-sma-scanner@localhost}"

#: Retries for the scan itself (transient network/API errors).
ATTEMPTS="${ATTEMPTS:-3}"
RETRY_DELAY="${RETRY_DELAY:-30}"

#: Gate thresholds. Defaults mirror sma_scanner.summary.
MIN_UNIVERSE="${MIN_UNIVERSE:-500}"
MIN_EVALUATED_RATIO="${MIN_EVALUATED_RATIO:-0.95}"
MAX_FETCH_ERROR_RATE="${MAX_FETCH_ERROR_RATE:-0.02}"

#: Optional plain-text webhook (ntfy.sh, Slack/Discord) for failure notices.
NOTIFY_URL="${NOTIFY_URL:-}"

#: Extra flags appended to the scan command, for tuning the screen per host
#: without editing this file - e.g. EXTRA_ARGS="--universe sp500 --lookback 5".
#: Split on whitespace on purpose; quote-free expansion is intended.
EXTRA_ARGS="${EXTRA_ARGS:-}"

LOCK_FILE="${LOCK_FILE:-$REPO_DIR/.scan.lock}"

# The report stamps its own generation time with local time.
export TZ="${TZ:-Asia/Shanghai}"
# cron hands over a nearly empty environment.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

notify() {
  [ -n "$NOTIFY_URL" ] || return 0
  curl -fsS -m 15 -X POST -H 'Title: SMA scan failed' --data "$1" \
    "$NOTIFY_URL" >/dev/null 2>&1 || log "warning: could not reach NOTIFY_URL"
}

die() {
  log "ERROR: $*"
  notify "SMA scan on $(hostname) failed: $*"
  exit 1
}

usage() {
  cat <<'EOF'
Usage: run_scan.sh [--no-publish] [--help]

  --no-publish   run the scan and the health gate, but leave the report on
                 disk instead of pushing it (useful for a first test run)
  --help         this message

All configuration is via environment variables; see the header of this file.
EOF
}

# ----------------------------------------------------------------------
# 1. Run the scan
# ----------------------------------------------------------------------
run_scan() {
  local attempt
  for attempt in $(seq 1 "$ATTEMPTS"); do
    log "scan attempt ${attempt}/${ATTEMPTS}"
    if "$PY" main.py -v \
        -o "$RESULTS_DIR/breakouts.csv" \
        --html "$SITE_DIR/index.html" \
        --summary-json "$RESULTS_DIR/summary.json" \
        ${EXTRA_ARGS}; then
      return 0
    fi
    if [ "$attempt" -lt "$ATTEMPTS" ]; then
      log "attempt ${attempt} failed; retrying in ${RETRY_DELAY}s"
      sleep "$RETRY_DELAY"
    fi
  done
  return 1
}

# ----------------------------------------------------------------------
# 2. Health gate
# ----------------------------------------------------------------------
check_health() {
  local summary="$RESULTS_DIR/summary.json"
  [ -f "$summary" ] || return 1
  "$PY" -m sma_scanner.summary "$summary" \
    --min-universe "$MIN_UNIVERSE" \
    --min-evaluated-ratio "$MIN_EVALUATED_RATIO" \
    --max-fetch-error-rate "$MAX_FETCH_ERROR_RATE"
}

# ----------------------------------------------------------------------
# 3. Publish
# ----------------------------------------------------------------------
publish() {
  local src="$SITE_DIR/index.html" url

  [ -f "$src" ] || { log "no report at $src"; return 1; }

  if [ "$PUBLISH" != "1" ]; then
    log "PUBLISH=0 - report left at $src, nothing pushed"
    return 0
  fi

  url="$(git -C "$REPO_DIR" remote get-url "$REMOTE")" || return 1

  if [ ! -d "$PUBLISH_DIR/.git" ]; then
    log "preparing the publish checkout at $PUBLISH_DIR"
    rm -rf "$PUBLISH_DIR"
    if git clone --quiet --branch "$PUBLISH_BRANCH" "$url" "$PUBLISH_DIR" 2>/dev/null; then
      log "using the existing $PUBLISH_BRANCH branch"
    else
      # First run: the branch does not exist yet, so start an orphan one that
      # holds the report and nothing else.
      log "creating an orphan $PUBLISH_BRANCH branch"
      git clone --quiet "$url" "$PUBLISH_DIR" || return 1
      git -C "$PUBLISH_DIR" checkout --quiet --orphan "$PUBLISH_BRANCH" || return 1
      git -C "$PUBLISH_DIR" rm -rf --quiet . || true
    fi
  else
    # Catch up with the remote in case it moved (or was rewritten).
    git -C "$PUBLISH_DIR" fetch --quiet origin "$PUBLISH_BRANCH" &&
      git -C "$PUBLISH_DIR" reset --quiet --hard "origin/$PUBLISH_BRANCH" ||
      log "warning: could not refresh $PUBLISH_BRANCH; publishing on top of local"
  fi

  cp "$src" "$PUBLISH_DIR/index.html"
  # Cheap insurance against Jekyll ever being switched on for this branch.
  touch "$PUBLISH_DIR/.nojekyll"

  git -C "$PUBLISH_DIR" add -A
  if git -C "$PUBLISH_DIR" diff --cached --quiet; then
    log "report is unchanged since the last publish; nothing to push"
    return 0
  fi

  git -C "$PUBLISH_DIR" \
      -c user.name="$GIT_AUTHOR_NAME" -c user.email="$GIT_AUTHOR_EMAIL" \
      commit --quiet -m "Daily scan $(date '+%Y-%m-%d')" || return 1

  if ! git -C "$PUBLISH_DIR" push --quiet origin "$PUBLISH_BRANCH"; then
    log "push rejected; rebasing on the remote and retrying once"
    git -C "$PUBLISH_DIR" pull --quiet --rebase origin "$PUBLISH_BRANCH" || return 1
    git -C "$PUBLISH_DIR" push --quiet origin "$PUBLISH_BRANCH" || return 1
  fi

  log "published $(basename "$src") to $PUBLISH_BRANCH"
}

# ----------------------------------------------------------------------
main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --no-publish) PUBLISH=0 ;;
      -h|--help)    usage; exit 0 ;;
      *)            usage >&2; exit 2 ;;
    esac
    shift
  done

  [ -x "$PY" ] || die "no python at $PY - create the venv or set PY="
  [ -f "$REPO_DIR/.env" ] || log "warning: no $REPO_DIR/.env (Alpaca keys must come from the environment)"

  # One run at a time. A second cron tick while a run is still going exits
  # quietly rather than fighting over the output files. If flock is missing we
  # warn and carry on - failing closed here would silently skip every run.
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
      log "another run holds $LOCK_FILE; exiting"
      exit 0
    fi
  else
    log "warning: flock not available; running without the single-run guard"
  fi

  mkdir -p "$SITE_DIR" "$RESULTS_DIR"
  cd "$REPO_DIR"

  log "=== scan starting (TZ=$TZ) ==="
  run_scan || die "the scan failed after ${ATTEMPTS} attempt(s); nothing was published"

  log "checking run health"
  check_health || die "the scan ran but its output is not trustworthy; nothing was published"

  publish || die "the report could not be published"

  log "=== done ==="
}

main "$@"
