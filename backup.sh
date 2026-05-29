#!/usr/bin/env bash
# OpenWebUI Monitor — PostgreSQL backup
#
# Usage:
#   ./backup.sh                  # writes ./backups/owmonitor-YYYY-MM-DD.sql.gz
#   ./backup.sh /custom/path     # writes to a custom directory
#
# Cron (daily at 03:30):
#   30 3 * * * cd /opt/openwebui-monitor && ./backup.sh >> /var/log/owmonitor-backup.log 2>&1
#
# Old files (>30 days) are auto-removed at the end. Restore is documented in
# MIGRATION_GUIDE.md, section "Восстановление из бэкапа".

set -euo pipefail

BACKUP_DIR="${1:-./backups}"
RETENTION_DAYS=30
TIMESTAMP=$(date +%F)
FILENAME="owmonitor-${TIMESTAMP}.sql.gz"
TARGET="${BACKUP_DIR}/${FILENAME}"

mkdir -p "${BACKUP_DIR}"

echo "[$(date '+%F %T')] starting backup → ${TARGET}"

# pg_dump via docker compose exec — uses the password from compose env,
# no need to expose creds here.
docker compose exec -T postgres pg_dump \
  -U owmonitor \
  -d owmonitor \
  --no-owner \
  --no-privileges \
  | gzip -9 > "${TARGET}"

# Sanity check
SIZE=$(stat --printf="%s" "${TARGET}" 2>/dev/null || stat -f%z "${TARGET}")
if [[ "${SIZE}" -lt 1024 ]]; then
  echo "[$(date '+%F %T')] FAIL: backup is suspiciously small (${SIZE} bytes), removing"
  rm -f "${TARGET}"
  exit 1
fi

echo "[$(date '+%F %T')] OK: ${TARGET} (${SIZE} bytes)"

# Rotation: remove files older than RETENTION_DAYS days
find "${BACKUP_DIR}" -name 'owmonitor-*.sql.gz' -type f -mtime "+${RETENTION_DAYS}" -delete \
  && echo "[$(date '+%F %T')] rotation OK (removed files older than ${RETENTION_DAYS}d)"
