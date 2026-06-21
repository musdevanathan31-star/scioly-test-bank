#!/usr/bin/env bash
# Backs up the bulk binary data (event directories: PDFs, images/, texts/,
# textbooks/, "large test packets") to S3 via restic — encrypted client-side,
# deduplicated, versioned. Run as the instance's own system user (e.g.
# qbank), which already owns the data being backed up.
#
#   deploy/backup-bulk-data.sh <instance-app-dir> <backup-env-file>
#
# Example: deploy/backup-bulk-data.sh /opt/qbank/app /opt/qbank/backup/.env
#
# Deliberately backs up "every top-level directory except known code
# directories" rather than a hardcoded event list — new events get created
# as plain directories under the app root (see events.py), so this picks
# them up automatically without ever needing to edit this script.

set -euo pipefail

APP_DIR="${1:?usage: backup-bulk-data.sh <instance-app-dir> <backup-env-file>}"
ENV_FILE="${2:?usage: backup-bulk-data.sh <instance-app-dir> <backup-env-file>}"
EXCLUDE_DIRS=(deploy static templates tests __pycache__ .git backup)

set -a
source "$ENV_FILE"
set +a

cd "$APP_DIR"
TARGETS=()
for d in */; do
  d="${d%/}"
  skip=false
  for ex in "${EXCLUDE_DIRS[@]}"; do
    [ "$d" = "$ex" ] && skip=true && break
  done
  $skip || TARGETS+=("$d")
done

if [ "${#TARGETS[@]}" -eq 0 ]; then
  echo "Nothing to back up yet (no data directories under $APP_DIR)."
  exit 0
fi

echo "Backing up: ${TARGETS[*]}"
restic backup "${TARGETS[@]}"

# Retention: keep enough recent snapshots to be useful without growing
# storage unbounded as the libraries grow.
restic forget --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune
