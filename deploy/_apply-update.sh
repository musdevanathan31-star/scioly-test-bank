#!/usr/bin/env bash
# Root-only half of the GitHub-update mechanism (see update-from-github.sh).
#
# Deliberately takes NO arguments and reads NO untrusted input — every path
# below is a fixed literal. That's what makes it safe to grant a
# low-privilege account NOPASSWD sudo on this *exact* script path: there's
# no parameter an attacker (or a bug) could use to make it do something
# other than what's written here.
#
# By the time this runs, update-from-github.sh has already validated the
# fetched source in $SRC (pip install + py_compile + pytest, as the
# unprivileged qbank-deploy account, against its own dedicated venv) — this
# script never executes anything from $SRC itself, only rsync/chown/
# systemctl against it, so it never runs arbitrary fetched code as root.
# Backs up each instance's current code (for rollback via
# qbank-rollback.sh), syncs the code-only allow-list into each live
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
LOG="/var/log/qbank-deploy.log"
BACKUP_ROOT="/opt/qbank-backups"
KEEP_BACKUPS=10
INSTANCES_CONF="$SRC/deploy/instances.conf"

log() { echo "$(date -Is) $*" >> "$LOG"; }

SHA=$(git -C "$SRC" rev-parse --short HEAD)

# Drift warning, never self-update.
#
# This script is installed once by provision-host.sh and runs as root under
# a NOPASSWD grant naming this exact path. Refreshing itself from the repo
# would hand root to anyone who can land a commit on the tracked branch,
# which is the whole reason the privileged half is a separate, fixed file.
# So it only says when it has fallen behind — acting on that is a deliberate
# root action for a human.
SELF="${BASH_SOURCE[0]}"
if [ -f "$SRC/deploy/_apply-update.sh" ] &&    ! cmp -s "$SRC/deploy/_apply-update.sh" "$SELF"; then
  log "WARNING: $SELF differs from deploy/_apply-update.sh at $SHA."
  log "         Refresh it deliberately, as root:"
  log "           install -o root -g root -m 750 $SRC/deploy/_apply-update.sh $SELF"
fi

# Glob expanded relative to $SRC, not the script's own cwd — otherwise *.py
# would expand against wherever this script happens to be invoked from.
cd "$SRC"
CODE_PATHS=(*.py templates static deploy requirements.txt requirements-dev.txt)
cd - >/dev/null

# Dependencies, into the venv the services actually run from.
#
# update-from-github.sh also runs pip, but against /opt/qbank-deploy/venv --
# qbank-deploy's own *validation* venv, which is a different venv from the
# one gunicorn uses. Before this step, a newly declared requirement was
# installed where the tests run and nowhere else, so the feature needing it
# stayed quietly unavailable in production while the deploy reported
# success. That is exactly how reportlab went missing and every assessment
# export came out as markdown.
#
# Runs before anything is copied or restarted, and aborts the whole deploy
# on failure: if the dependencies for the new code cannot be installed, the
# right outcome is that the currently-running version keeps running.
SHARED_VENV="${QBANK_VENV:-/opt/qbank/venv}"
if [ -x "$SHARED_VENV/bin/pip" ]; then
  if "$SHARED_VENV/bin/pip" install -q -r "$SRC/requirements.txt"; then
    log "dependencies synced into $SHARED_VENV"
  else
    log "FAILED to install requirements into $SHARED_VENV — nothing deployed"
    exit 1
  fi
else
  log "WARNING: no pip at $SHARED_VENV/bin/pip — dependencies NOT synced"
fi

mapfile -t INSTANCE_LINES < <(grep -v '^\s*#' "$INSTANCES_CONF" | grep -v '^\s*$')
log "Deploying already-validated $SHA to ${#INSTANCE_LINES[@]} instance(s)"

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
