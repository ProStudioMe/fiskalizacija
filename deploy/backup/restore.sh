#!/usr/bin/env bash
# Restore Sepko backup from OneDrive mount.
# Usage: restore.sh /backup/2026-09-07_094500
set -euo pipefail

SRC="${1:-}"
if [[ -z "${SRC}" || ! -d "${SRC}" ]]; then
  echo "Usage: restore.sh /backup/<stamp>" >&2
  echo "Available:" >&2
  ls -1 "${BACKUP_DIR:-/backup}" 2>/dev/null || true
  exit 1
fi

DUMP="${SRC}/sepko.dump"
if [[ ! -f "${DUMP}" ]]; then
  echo "Missing ${DUMP}" >&2
  exit 1
fi

PGHOST="${PGHOST:-${SEPKO_BACKUP_PGHOST:-db}}"
PGPORT="${PGPORT:-${SEPKO_BACKUP_PGPORT:-5432}}"
PGUSER="${PGUSER:-${SEPKO_BACKUP_PGUSER:-sepko}}"
PGPASSWORD="${PGPASSWORD:-${SEPKO_BACKUP_PGPASSWORD:-sepko}}"
PGDATABASE="${PGDATABASE:-${SEPKO_BACKUP_PGDATABASE:-sepko}}"
export PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE

echo "[sepko-restore] ${DUMP} → ${PGDATABASE}@${PGHOST}"
echo "[sepko-restore] WARNING: overwrites existing data in ${PGDATABASE}"
pg_restore \
  --clean \
  --if-exists \
  --no-owner \
  --no-acl \
  --dbname="${PGDATABASE}" \
  "${DUMP}"

if [[ -f "${SRC}/uploads.tar.gz" && -d /data ]]; then
  echo "[sepko-restore] uploads → /data"
  tar -C /data -xzf "${SRC}/uploads.tar.gz"
fi

if [[ -f "${SRC}/certs.tar.gz" && -d /certs ]]; then
  echo "[sepko-restore] certs → /certs"
  tar -C /certs -xzf "${SRC}/certs.tar.gz"
fi

echo "[sepko-restore] done"
