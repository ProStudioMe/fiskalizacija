#!/usr/bin/env bash
# Poseban backup servis: jednokratno ili periodično → OneDrive volume.
set -euo pipefail

MODE="${1:-loop}"
INTERVAL_HOURS="${BACKUP_INTERVAL_HOURS:-24}"

run_once() {
  /usr/local/bin/backup.sh
}

if [[ "${MODE}" == "once" ]]; then
  run_once
  exit 0
fi

echo "[sepko-backup] loop every ${INTERVAL_HOURS}h → ${BACKUP_DIR:-/backup}"
# prvi dump odmah, pa sleep
while true; do
  if ! run_once; then
    echo "[sepko-backup] FAILED (exit $?) — retry after interval" >&2
  fi
  # bash arithmetic; default 24h
  sleep "$(( INTERVAL_HOURS * 3600 ))"
done
