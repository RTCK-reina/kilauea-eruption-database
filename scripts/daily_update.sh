#!/usr/bin/env bash
# Daily refresh of the Kīlauea database, plus the brief's data context.
#
# Runs on the machine that holds the database (the collectors need network
# access, which the Cowork device VM does not have). Refreshes the sources that
# change day to day, rebuilds the analysis views, writes the brief context as
# JSON, and runs the integrity checks.
#
#   scripts/daily_update.sh            # incremental
#   scripts/daily_update.sh --full     # every source, including ScienceBase
#
# Install with cron on the machine holding the database. Run it before the
# Cowork brief task fires, so the context it reads is already fresh:
#   30 5 * * *  cd ~/Developer/Kilauea && ./scripts/daily_update.sh        >> /tmp/kilauea.log 2>&1
#   0  4 * * 0  cd ~/Developer/Kilauea && ./scripts/daily_update.sh --full >> /tmp/kilauea.log 2>&1
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"

mkdir -p logs briefs
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DATE_HST="$(TZ=Pacific/Honolulu date +%Y%m%d)"
LOG="logs/update_${STAMP}.log"

{
    echo "=== kilauea update ${STAMP} ==="
    "$PY" -m kilauea update "$@"

    # The brief's numbers all come from here. --record inserts a brief_run row
    # so the brief's own forecast can be scored against the actual onset later.
    "$PY" -m kilauea brief-context --record -o "briefs/context_${DATE_HST}.json" > /dev/null
    cp "briefs/context_${DATE_HST}.json" briefs/context_latest.json
    echo "brief context written: briefs/context_${DATE_HST}.json"

    "$PY" -m kilauea validate
} 2>&1 | tee "$LOG"

# Keep a month of run logs and contexts; the database is the durable artefact.
find logs   -name 'update_*.log'   -mtime +30 -delete
find briefs -name 'context_2*.json' -mtime +60 -delete

# The archive committed to the repository is a snapshot, so it is behind the
# database by design and there is nothing to say most days. Past a month the
# gap is worth closing, or people are cloning a stale database.
CORE_GZ=data/kilauea_core.db.gz
if [ -f "$CORE_GZ" ] && [ -n "$(find "$CORE_GZ" -mtime +30 2>/dev/null)" ]; then
    printf 'note: %s is over 30 days old. Refresh it with:\n' "$CORE_GZ"
    printf '  %s -m kilauea core-db --db data/kilauea.db -o data/kilauea_core.db --force\n' "$PY"
    printf '  gzip -9 -c data/kilauea_core.db > %s\n' "$CORE_GZ"
fi
