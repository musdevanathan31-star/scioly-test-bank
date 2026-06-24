#!/usr/bin/env bash
# Root-only rollback of one instance's code to a backup taken by
# _apply-update.sh (see that script's BACKUP_ROOT/KEEP_BACKUPS logic).
#
# Unlike qbank-apply-update.sh, this script needs arguments (which instance,
# which backup) — so the safety boundary moves into the script itself: both
# arguments are validated against a closed set (the literal instance names
# in instances.conf, and an actual existing directory under that instance's
# backup root) before anything is touched. No globbing, no path traversal,
# no shell interpolation of anything that didn't pass validation.
#
#   qbank-rollback.sh <instance-name> <backup-timestamp|latest>
#
# Install: sudo cp deploy/qbank-rollback.sh /usr/local/sbin/qbank-rollback.sh
#          sudo chown root:root /usr/local/sbin/qbank-rollback.sh
#          sudo chmod 750 /usr/local/sbin/qbank-rollback.sh
# Sudoers: qbank-admin ALL=(root) NOPASSWD: /usr/local/sbin/qbank-rollback.sh

set -euo pipefail

SRC="/opt/qbank-src"
LOG="/var/log/qbank-deploy.log"
BACKUP_ROOT="/opt/qbank-backups"
INSTANCES_CONF="$SRC/deploy/instances.conf"

log() { echo "$(date -Is) $*" >> "$LOG"; }
die() { log "ROLLBACK REJECTED: $*"; echo "$*" >&2; exit 1; }

INSTANCE_NAME="${1:-}"
REQUESTED_TS="${2:-}"
[ -n "$INSTANCE_NAME" ] && [ -n "$REQUESTED_TS" ] || die "usage: qbank-rollback.sh <instance-name> <backup-timestamp|latest>"

dir="" svc="" user=""
while IFS=: read -r name label cdir csvc cuser cport; do
  [[ "$name" == \#* ]] && continue
  [ -z "$name" ] && continue
  if [ "$name" = "$INSTANCE_NAME" ]; then
    dir="$cdir"; svc="$csvc"; user="$cuser"
  fi
done < "$INSTANCES_CONF"
[ -n "$dir" ] || die "unknown instance: $INSTANCE_NAME"

INSTANCE_BACKUP_ROOT="$BACKUP_ROOT/$INSTANCE_NAME"
[ -d "$INSTANCE_BACKUP_ROOT" ] || die "no backups exist for instance: $INSTANCE_NAME"

if [ "$REQUESTED_TS" = "latest" ]; then
  TS="$(ls -1 "$INSTANCE_BACKUP_ROOT" | sort | tail -n 1)"
  [ -n "$TS" ] || die "no backups exist for instance: $INSTANCE_NAME"
else
  # Must be exactly one of the real, existing backup directory names —
  # matched literally, not interpolated into a path until confirmed to
  # already exist on disk.
  TS=""
  for existing in $(ls -1 "$INSTANCE_BACKUP_ROOT"); do
    [ "$existing" = "$REQUESTED_TS" ] && TS="$existing"
  done
  [ -n "$TS" ] || die "no such backup '$REQUESTED_TS' for instance: $INSTANCE_NAME"
fi

BACKUP_DIR="$INSTANCE_BACKUP_ROOT/$TS"
[ -d "$BACKUP_DIR" ] || die "backup directory vanished: $BACKUP_DIR"

rsync -a --delete "$BACKUP_DIR/" "$dir/"
chown -R "$user:$user" "$dir"
systemctl restart "$svc"
sleep 2
if systemctl is-active --quiet "$svc"; then
  log "$INSTANCE_NAME: rolled back to $TS, $svc OK"
  echo "Rolled back $INSTANCE_NAME to $TS — $svc is active."
else
  log "$INSTANCE_NAME: rolled back to $TS, $svc FAILED to start, check: journalctl -u $svc"
  echo "Rolled back $INSTANCE_NAME to $TS but $svc failed to start — check journalctl -u $svc" >&2
  exit 1
fi
