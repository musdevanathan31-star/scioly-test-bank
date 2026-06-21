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
# no special access to anything else on the box). The one moment that needs
# root (pip install into the shared venv, rsyncing into qbank/qbank-chs
# owned directories, restarting systemd units) is delegated to
# qbank-apply-update.sh, a fixed, non-parameterized, root-owned script in a
# standard sbin location — the NOPASSWD grant above covers exactly that one
# script path, nothing else. qbank-deploy can never use it to run arbitrary
# root commands.

set -euo pipefail

REPO_URL="https://github.com/musdevanathan31-star/scioly-test-bank.git"
REF="${1:-main}"
SRC="/opt/qbank-src"
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

sudo /usr/local/sbin/qbank-apply-update.sh
echo "Done — see $LOG for per-instance results."
