#!/bin/bash
# Nightly DB backup: pg_dump from the 'db' container -> gzip -> S3.
# Installed by infra/ec2-bootstrap.sh as /usr/local/bin/backup-db.sh
# Triggered by /etc/cron.d/job-search-db-backup at 03:00 UTC.

set -euo pipefail

BUCKET="fernflores-job-search-resumes"
PREFIX="db-backups"
TS=$(date -u +%Y%m%dT%H%M%SZ)
KEY="${PREFIX}/jobsearch-${TS}.sql.gz"

CONTAINER=$(docker ps -qf name=db)
if [ -z "${CONTAINER}" ]; then
  echo "[$(date -u +%FT%TZ)] ERROR: db container not running" >&2
  exit 1
fi

echo "[$(date -u +%FT%TZ)] Starting backup -> s3://${BUCKET}/${KEY}"

docker exec "${CONTAINER}" pg_dump -U jobsearch jobsearch \
  | gzip \
  | aws s3 cp - "s3://${BUCKET}/${KEY}"

echo "[$(date -u +%FT%TZ)] Backup complete"
