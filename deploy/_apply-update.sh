#!/usr/bin/env bash
# Root-only half of the GitHub-update mechanism (see update-from-github.sh).
#
# Deliberately takes NO arguments and reads NO untrusted input — every path
# below is a fixed literal. That's what makes it safe to grant a
# low-privilege account NOPASSWD sudo on this *exact* script path: there's
# no parameter an attacker (or a bug) could use to make it do something
# other than what's written here.
#
# Validates the fetched source in $SRC, and only if validation passes,
# backs up each instance's current code (for rollback via
# qbank-rollback.sh) and syncs the code-only allow-list into each live
# instance, then restarts it. A failure for one instance is logged and does
# not roll back an instance that already succeeded.
#
# Instances are read from instances.conf (next to this script in the repo,
# and deployed alongside it) rather than hardcoded here — see that file's
# header comment.
#
# Install: sudo cp deploy/_apply-update.sh /usr/local/sbin/qbank-apply-update.sh
#          sudo chown root:root /usr/local/sbin/qbank-apply-update.sh
#          sudo chmod 750 /usr/local/sbin/qbank-apply-update.sh
# Then grant the deploy account NOPASSWD sudo on that exact path (see
# update-from-github.sh's header comment for the sudoers line).

set -euo pipefail

SRC="/opt/qbank-src"
VENV="/opt/qbank/venv"
LOG="/var/log/qbank-deploy.log"
BACKUP_ROOT="/opt/qbank-backups"
KEEP_BACKUPS=10
INSTANCES_CONF="$SRC/deploy/instances.conf"

log() { echo "$(date -Is) $*" >> "$LOG"; }

SHA=$(git -C "$SRC" rev-parse --short HEAD)

# Glob expanded relative to $SRC, not the script's own cwd — otherwise *.py
# would expand against wherever this script happens to be invoked from.
cd "$SRC"
CODE_PATHS=(*.py templates static deploy requirements.txt requirements-dev.txt)
cd - >/dev/null

"$VENV/bin/pip" install -q -r "$SRC/requirements.txt"
"$VENV/bin/python" -m py_compile "$SRC"/*.py
if ! "$VENV/bin/python" -m pytest "$SRC/tests" -q; then
  log "FAILED validation at $SHA — nothing deployed"
  exit 1
fi

mapfile -t INSTANCE_LINES < <(grep -v '^\s*#' "$INSTANCES_CONF" | grep -v '^\s*$')
log "Validated $SHA — deploying to ${#INSTANCE_LINES[@]} instance(s)"

for entry in "${INSTANCE_LINES[@]}"; do
  IFS=: read -r name label dir svc user port <<< "$entry"

  # Back up the current code before overwriting it, so qbank-rollback.sh
  # has something to restore. Same allow-list as the forward sync below —
  # this is a code-only backup for fast rollback, not a substitute for the
  # data backups in backup-bulk-data.sh / backup-extracted-data.sh.
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_dir="$BACKUP_ROOT/$name/$ts"
  mkdir -p "$backup_dir"
  for p in "${CODE_PATHS[@]}"; do
    [ -e "$dir/$p" ] && rsync -a "$dir/$p" "$backup_dir/" 2>/dev/null || true
  done
  log "$name: backed up current code to $backup_dir"

  # Prune backups beyond the last $KEEP_BACKUPS for this instance, oldest
  # first — same retention idea as restic's --keep-* flags, just plain
  # directories since this is a small, local, short-lived rollback aid.
  mapfile -t old_backups < <(ls -1 "$BACKUP_ROOT/$name" | sort | head -n -"$KEEP_BACKUPS")
  for old in "${old_backups[@]}"; do
    rm -rf "$BACKUP_ROOT/$name/$old"
    log "$name: pruned old backup $old"
  done

  for p in "${CODE_PATHS[@]}"; do
    [ -e "$SRC/$p" ] && rsync -a --delete "$SRC/$p" "$dir/" 2>/dev/null || true
  done
  chown -R "$user:$user" "$dir"
  systemctl restart "$svc"
  sleep 2
  if systemctl is-active --quiet "$svc"; then
    log "$svc -> $SHA OK"
  else
    log "$svc -> $SHA FAILED to start, check: journalctl -u $svc"
  fi
done
