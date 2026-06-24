#!/usr/bin/env bash
# Moves an instance's growing data (event directories, auth_users.json,
# events_custom.json, textbooks/) off its app directory and onto a
# separate DATA_ROOT -- formalizes the manual rsync runbook in README's
# "Migrating an instance's data to a new DATA_ROOT" into a reusable,
# idempotent tool. Mirrors backup-bulk-data.sh's own event-directory
# discovery (every top-level directory except known code directories) so
# the two stay consistent.
#
# Copy mode (default) -- safe to re-run, never touches or deletes the
# source:
#   deploy/migrate-data-root.sh <instance-app-dir> <data-root> <user>:<group>
#
# Cleanup mode -- removes the now-redundant source copies, but only after
# confirming each one's content is byte-identical to what's already under
# <data-root>. Run this only after adding DATA_ROOT to the instance's
# .env, restarting it, and confirming the landing page shows the same
# question counts as before (see README):
#   deploy/migrate-data-root.sh <instance-app-dir> <data-root> --cleanup
#
# Example:
#   deploy/migrate-data-root.sh /opt/qbank/app /data/qbank/ncms qbank:qbank
#   # ... add DATA_ROOT=/data/qbank/ncms to /opt/qbank/.env, restart, verify ...
#   deploy/migrate-data-root.sh /opt/qbank/app /data/qbank/ncms --cleanup

set -euo pipefail

USAGE="usage: migrate-data-root.sh <instance-app-dir> <data-root> <user>:<group>|--cleanup"
APP_DIR="${1:?$USAGE}"
DATA_ROOT="${2:?$USAGE}"
MODE_ARG="${3:?$USAGE}"
EXCLUDE_DIRS=(deploy static templates tests __pycache__ .git backup)

# Same discovery as backup-bulk-data.sh: every top-level directory under
# the app dir except known code directories is data (this naturally
# includes textbooks/ -- it's just another non-excluded top-level dir).
cd "$APP_DIR"
PATHS=()
for d in */; do
  d="${d%/}"
  skip=false
  for ex in "${EXCLUDE_DIRS[@]}"; do
    [ "$d" = "$ex" ] && skip=true && break
  done
  $skip || PATHS+=("$d")
done
cd - >/dev/null

# Plus the known top-level data files this app keeps next to the code by
# default (see README's "Separating app code from data").
for f in auth_users.json events_custom.json; do
  [ -e "$APP_DIR/$f" ] && PATHS+=("$f")
done

if [ "${#PATHS[@]}" -eq 0 ]; then
  echo "Nothing under $APP_DIR to migrate."
  exit 0
fi

if [ "$MODE_ARG" = "--cleanup" ]; then
  echo "Cleanup mode: removing source copies already verified present under $DATA_ROOT."
  for p in "${PATHS[@]}"; do
    if [ ! -e "$DATA_ROOT/$p" ]; then
      echo "  SKIP $p -- not found under $DATA_ROOT, refusing to delete the only copy."
      continue
    fi
    if ! diff -rq "$APP_DIR/$p" "$DATA_ROOT/$p" >/dev/null 2>&1; then
      echo "  SKIP $p -- differs from $DATA_ROOT/$p (did the instance write to it after migrating? re-run in copy mode first)."
      continue
    fi
    rm -rf "$APP_DIR/$p"
    echo "  removed $APP_DIR/$p"
  done
  exit 0
fi

USER_GROUP="$MODE_ARG"
mkdir -p "$DATA_ROOT"
echo "Copying into $DATA_ROOT: ${PATHS[*]}"
for p in "${PATHS[@]}"; do
  rsync -a "$APP_DIR/$p" "$DATA_ROOT/"
done
chown -R "$USER_GROUP" "$DATA_ROOT"

cat <<EOF

Copied -- the source under $APP_DIR is untouched. Next steps:
  1. Add DATA_ROOT=$DATA_ROOT to this instance's .env
  2. Restart the instance and confirm the landing page shows the same
     question counts as before
  3. Once verified: $0 $APP_DIR $DATA_ROOT --cleanup
EOF
