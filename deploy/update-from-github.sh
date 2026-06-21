#!/usr/bin/env bash
# Pulls the latest code from the public repo and deploys it to every
# instance on this box (NCMS, CHS, ...). Run by hand when you're ready —
# deliberately not on a cron/timer (see deploy/Caddyfile's neighbour
# comments and spec.md for the reasoning: auto-deploying unreviewed pushes
# adds blast-radius without saving meaningful effort at this scale).
#
#   deploy/update-from-github.sh [ref]      # ref defaults to "main"
#
# One-time setup on the server:
#   sudo useradd -r -m -d /opt/qbank-deploy -s /bin/bash qbank-deploy
#   sudo -u qbank-deploy git clone https://github.com/musdevanathan31-star/scioly-test-bank.git /opt/qbank-src
#   sudo cp deploy/_apply-update.sh /usr/local/sbin/qbank-apply-update.sh
#   sudo chown root:root /usr/local/sbin/qbank-apply-update.sh && sudo chmod 750 /usr/local/sbin/qbank-apply-update.sh
#   echo 'qbank-deploy ALL=(root) NOPASSWD: /usr/local/sbin/qbank-apply-update.sh' | sudo tee /etc/sudoers.d/qbank-deploy
#   sudo chmod 440 /etc/sudoers.d/qbank-deploy
#
# This script itself runs as qbank-deploy with no special privileges — it
# only fetches into a clone qbank-deploy already owns (/opt/qbank-src, a
# plain directory outside any other account's home, so qbank-deploy needs
# no special access to anything else on the box), and validates that fetch
# using its own dedicated venv (/opt/qbank-deploy/venv — deliberately
# separate from the shared /opt/qbank/venv that actually serves live
# traffic, so this validation step never needs write access to anything
# another account owns). The one moment that needs root — rsyncing
# already-validated code into qbank/qbank-chs-owned directories and
# restarting systemd units — is delegated to qbank-apply-update.sh, a
# fixed, non-parameterized, root-owned script in a standard sbin location —
# the NOPASSWD grant above covers exactly that one script path, nothing
# else. qbank-deploy can never use it to run arbitrary root commands.
#
# Validating here (as the unprivileged qbank-deploy account) rather than
# inside qbank-apply-update.sh (root) is load-bearing, not stylistic: this
# is the step that executes arbitrary code from whatever was just fetched
# (pip's build-script hooks, pytest collection/execution) — running that as
# root would mean anyone who can land a malicious commit or dependency on
# the tracked branch gets root code execution the moment this script runs.
# By the time qbank-apply-update.sh (root) ever touches the fetched code,
# it has already passed py_compile + the test suite as a low-privilege user.

set -euo pipefail

REPO_URL="https://github.com/musdevanathan31-star/scioly-test-bank.git"
REF="${1:-main}"
SRC="/opt/qbank-src"
VENV="/opt/qbank-deploy/venv"
LOG="/var/log/qbank-deploy.log"

log() { echo "$(date -Is) $*" >> "$LOG"; }

if [ ! -d "$SRC/.git" ]; then
  git clone "$REPO_URL" "$SRC"
else
  git -C "$SRC" fetch origin
fi
git -C "$SRC" checkout "$REF"
git -C "$SRC" reset --hard "origin/$REF" 2>/dev/null || true   # no-op for a pinned tag/SHA
SHA=$(git -C "$SRC" rev-parse --short HEAD)
log "Fetched $REF @ $SHA"

"$VENV/bin/pip" install -q -r "$SRC/requirements.txt"
# -B / no:cacheprovider: this clone is qbank-deploy's own, but it was the
# *root*-run validation before this fix that first left root-owned
# __pycache__/.pytest_cache entries in it (qbank-deploy then can't
# overwrite them on a later run). Not writing either cache here at all
# sidesteps that whole class of ownership drift permanently, rather than
# just cleaning it up once.
PYTHONDONTWRITEBYTECODE=1 "$VENV/bin/python" -B -m py_compile "$SRC"/*.py
if ! PYTHONDONTWRITEBYTECODE=1 "$VENV/bin/python" -B -m pytest "$SRC/tests" -q -p no:cacheprovider; then
  log "FAILED validation at $SHA — nothing deployed"
  exit 1
fi
log "Validated $SHA as qbank-deploy — applying as root"

sudo /usr/local/sbin/qbank-apply-update.sh
echo "Done — see $LOG for per-instance results."
