#!/bin/bash
# Disaster-recovery restore: download a backup from S3 and apply it to the
# running 'db' container. This DESTRUCTIVELY replaces existing data in the
# jobsearch database — the backup is restored on top of the current schema,
# overwriting matching rows and recreating tables as needed.
#
# Usage:
#   restore-db.sh <s3-key>
#   restore-db.sh latest                  # restores the most recent backup
#
# Examples:
#   restore-db.sh db-backups/jobsearch-20260515T030000Z.sql.gz
#   restore-db.sh latest

set -euo pipefail

BUCKET="fernflores-job-search-resumes"
PREFIX="db-backups"

if [ $# -ne 1 ]; then
  echo "Usage: $0 <s3-key|latest>" >&2
  echo "  $0 latest                                              # most recent" >&2
  echo "  $0 db-backups/jobsearch-20260515T030000Z.sql.gz        # specific" >&2
  exit 1
fi

ARG="$1"

if [ "${ARG}" = "latest" ]; then
  KEY=$(aws s3 ls "s3://${BUCKET}/${PREFIX}/" \
    | awk '{print $4}' | grep -E '^jobsearch-.*\.sql\.gz$' | sort | tail -n1)
  if [ -z "${KEY}" ]; then
    echo "ERROR: no backups found in s3://${BUCKET}/${PREFIX}/" >&2
    exit 1
  fi
  KEY="${PREFIX}/${KEY}"
else
  KEY="${ARG}"
fi

CONTAINER=$(docker ps -qf name=db)
if [ -z "${CONTAINER}" ]; then
  echo "ERROR: db container not running" >&2
  exit 1
fi

echo "About to restore s3://${BUCKET}/${KEY} into the live 'db' container."
echo "This will OVERWRITE the current jobsearch database."
read -r -p "Type 'yes' to continue: " CONFIRM
if [ "${CONFIRM}" != "yes" ]; then
  echo "Aborted."
  exit 1
fi

echo "Downloading and restoring..."
aws s3 cp "s3://${BUCKET}/${KEY}" - \
  | gunzip \
  | docker exec -i "${CONTAINER}" psql -U jobsearch jobsearch

echo "Restore complete."
