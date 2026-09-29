#!/usr/bin/env bash
#
# Monthly housekeeping for the sma-breakout-scanner VPS.
#
# Read-only checks plus only the safe fixes a `ubuntu` cron job can do
# without sudo. Anything needing root (logrotate file, chown) is reported
# with the exact command to run, not forced.
#
# Run by hand:
#   bash scripts/monthly_housekeeping.sh
#
# Scheduled (1st of each month, after the daily 01:00 UTC scan):
#   0 2 1 * * /home/ubuntu/projects/sma-breakout-scanner/scripts/monthly_housekeeping.sh >> /var/log/sma-housekeeping.log 2>&1

set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SITE_DIR="${SITE_DIR:-$REPO_DIR/site}"
RESULTS_DIR="${RESULTS_DIR:-$REPO_DIR/results}"
LOG_FILE="${LOG_FILE:-/var/log/sma-scan.log}"
LOCK_FILE="${LOCK_FILE:-$REPO_DIR/.scan.lock}"
SCAN_CRON_MARK="${SCAN_CRON_MARK:-run_scan.sh}"

#: Warn thresholds.
DISK_WARN_PCT="${DISK_WARN_PCT:-80}"
STALE_REPORT_DAYS="${STALE_REPORT_DAYS:-3}"

ISSUES=0

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
ok() { log "OK: $*"; }
warn() { ISSUES=$((ISSUES + 1)); log "WARN: $*"; }

usage() {
  cat <<'EOF'
Usage: monthly_housekeeping.sh [--help]

Checks disk, log rotation, cron entry, file freshness, repo health.
Exits 0 if all clean, 1 if anything needs attention (see WARN lines).
All paths/thresholds overridable via environment; see header.
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

log "=== monthly housekeeping (repo=$REPO_DIR) ==="

# 1. Scan log exists and is writable (cron appends as ubuntu).
if [ -f "$LOG_FILE" ]; then
  if [ -w "$LOG_FILE" ]; then
    ok "scan log $LOG_FILE exists and is writable ($(stat -c '%s bytes, %a %U:%G' "$LOG_FILE"))"
  else
    warn "scan log $LOG_FILE is not writable by $(whoami); fix: sudo chown ubuntu:adm $LOG_FILE"
  fi
  if [ ! -s "$LOG_FILE" ]; then
    warn "scan log $LOG_FILE is empty; the daily cron may not have fired yet (check: grep CRON /var/log/syslog | tail -5)"
  fi
else
  warn "scan log $LOG_FILE missing; create it: sudo touch $LOG_FILE && sudo chown ubuntu:adm $LOG_FILE"
fi

# 2. Log rotation needs root to install, so only check here.
if [ -f /etc/logrotate.d/sma-scan ]; then
  ok "logrotate config /etc/logrotate.d/sma-scan present"
else
  warn "no logrotate config; install with docs step 9: sudo tee /etc/logrotate.d/sma-scan (see docs/vps-setup.md)"
fi

# 3. Disk pressure.
USE_PCT=$(df -P / | awk 'NR==2 {gsub(/%/, "", $5); print $5}')
if [ "$USE_PCT" -ge "$DISK_WARN_PCT" ]; then
  warn "disk ${USE_PCT}% used (>= ${DISK_WARN_PCT}%); output is overwritten each run so investigate /var/log and ~/projects"
else
  ok "disk ${USE_PCT}% used"
fi

# 4. Secrets file.
if [ -f "$REPO_DIR/.env" ]; then
  PERMS=$(stat -c '%a' "$REPO_DIR/.env")
  if [ "$PERMS" = "600" ]; then
    ok ".env exists with 600 perms"
  else
    warn ".env perms are $PERMS, expected 600; fix: chmod 600 $REPO_DIR/.env"
  fi
else
  warn "no $REPO_DIR/.env; the scan falls back to environment variables only"
fi

# 5. Daily cron entry still present.
if crontab -l 2>/dev/null | grep -q "$SCAN_CRON_MARK"; then
  ok "daily cron entry ($SCAN_CRON_MARK) present: $(crontab -l 2>/dev/null | grep "$SCAN_CRON_MARK" | head -1 | xargs)"
else
  warn "no crontab line containing $SCAN_CRON_MARK; re-add per docs step 8"
fi

# 6. Report freshness - catches a silently failing gate/push.
for f in "$SITE_DIR/index.html" "$RESULTS_DIR/summary.json"; do
  if [ -f "$f" ]; then
    if [ "$(find "$f" -mtime +"$STALE_REPORT_DAYS" -print 2>/dev/null)" ]; then
      warn "$f is older than $STALE_REPORT_DAYS days (stale); tail $LOG_FILE for UNHEALTHY:/ERROR: lines"
    else
      ok "$f fresh ($(stat -c '%y' "$f" | cut -d. -f1))"
    fi
  else
    warn "$f missing; run: PY=/usr/bin/python3 bash scripts/run_scan.sh --no-publish"
  fi
done

# 7. Overlapping-run lock should be free at 02:00 on the 1st (daily run is 01:00).
if command -v flock >/dev/null 2>&1 && [ -f "$LOCK_FILE" ]; then
  if flock -n "$LOCK_FILE" -c true 2>/dev/null; then
    ok "scan lock free"
  else
    warn "scan lock $LOCK_FILE is held; a run may be overlapping (harmless unless it repeats)"
  fi
fi

# 8. Repo hygiene: accidental edits and object bloat.
if [ -d "$REPO_DIR/.git" ]; then
  if [ -z "$(git -C "$REPO_DIR" status --porcelain 2>/dev/null)" ]; then
    ok "git working tree clean"
  else
    warn "git working tree dirty: $(git -C "$REPO_DIR" status --porcelain 2>/dev/null | head -3 | tr '\n' ';')"
  fi
  git -C "$REPO_DIR" gc --auto --quiet 2>/dev/null && ok "git gc --auto done" || warn "git gc --auto failed"
else
  warn "$REPO_DIR is not a git checkout"
fi

# 9. Security updates.
if dpkg -l unattended-upgrades 2>/dev/null | grep -q '^ii'; then
  ok "unattended-upgrades installed"
else
  warn "unattended-upgrades not installed; consider: sudo apt install unattended-upgrades"
fi

log "=== housekeeping done: $ISSUES issue(s) ==="
[ "$ISSUES" -eq 0 ]
