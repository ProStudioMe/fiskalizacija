#!/usr/bin/env bash
# Sepko backup → OneDrive mount (/backup)
set -euo pipefail

BACKUP_ROOT="${BACKUP_DIR:-/backup}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
INCLUDE_UPLOADS="${BACKUP_INCLUDE_UPLOADS:-1}"
INCLUDE_CERTS="${BACKUP_INCLUDE_CERTS:-1}"

PGHOST="${PGHOST:-${SEPKO_BACKUP_PGHOST:-db}}"
PGPORT="${PGPORT:-${SEPKO_BACKUP_PGPORT:-5432}}"
PGUSER="${PGUSER:-${SEPKO_BACKUP_PGUSER:-sepko}}"
PGPASSWORD="${PGPASSWORD:-${SEPKO_BACKUP_PGPASSWORD:-sepko}}"
PGDATABASE="${PGDATABASE:-${SEPKO_BACKUP_PGDATABASE:-sepko}}"
export PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE

STAMP="$(date -u +%Y-%m-%d_%H%M%S)"
OUT="${BACKUP_ROOT}/${STAMP}"
mkdir -p "${OUT}"

echo "[sepko-backup] start ${STAMP} → ${OUT}"

echo "[sepko-backup] pg_dump ${PGDATABASE}@${PGHOST}:${PGPORT}"
pg_dump \
  --format=custom \
  --compress=9 \
  --file="${OUT}/sepko.dump" \
  --no-owner \
  --no-acl

if [[ "${INCLUDE_UPLOADS}" == "1" && -d /data/uploads ]]; then
  echo "[sepko-backup] uploads"
  tar -C /data -czf "${OUT}/uploads.tar.gz" uploads
fi

if [[ "${INCLUDE_CERTS}" == "1" && -d /certs ]]; then
  (
    cd /certs
    # shellcheck disable=SC2046
    set -- $(find . -maxdepth 2 -type f \( -iname '*.p12' -o -iname '*.pfx' \) 2>/dev/null || true)
    if (($# > 0)); then
      echo "[sepko-backup] certs ($# file(s))"
      tar -czf "${OUT}/certs.tar.gz" "$@"
    fi
  )
fi

{
  echo "stamp=${STAMP}"
  echo "utc=$(date -u -Iseconds)"
  echo "database=${PGDATABASE}"
  echo "host=${PGHOST}"
  echo "dump=sepko.dump"
  [[ -f "${OUT}/uploads.tar.gz" ]] && echo "uploads=uploads.tar.gz"
  [[ -f "${OUT}/certs.tar.gz" ]] && echo "certs=certs.tar.gz"
  echo "sha256:"
  (cd "${OUT}" && sha256sum ./* 2>/dev/null || true)
} > "${OUT}/MANIFEST.txt"

# marker da OneDrive vidi gotov folder (ne djelomičan dump)
touch "${OUT}/.complete"

echo "[sepko-backup] retention > ${RETENTION_DAYS}d"
find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type d -mtime "+${RETENTION_DAYS}" -exec rm -rf {} +

echo "[sepko-backup] done $(du -sh "${OUT}" | cut -f1)"
