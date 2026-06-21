#!/usr/bin/env bash
# Root-only stop/start/restart of one managed instance's systemd unit.
#
# Like qbank-rollback.sh, this needs arguments, so both are validated
# against a closed set before anything runs: the verb against a fixed list,
# the instance against the literal names in instances.conf. No service name
# is ever built from unvalidated input.
#
#   qbank-service-ctl.sh <stop|start|restart> <instance-name>
#
# Install: sudo cp deploy/qbank-service-ctl.sh /usr/local/sbin/qbank-service-ctl.sh
#          sudo chown root:root /usr/local/sbin/qbank-service-ctl.sh
#          sudo chmod 750 /usr/local/sbin/qbank-service-ctl.sh
# Sudoers: qbank-admin ALL=(root) NOPASSWD: /usr/local/sbin/qbank-service-ctl.sh

set -euo pipefail

SRC="/opt/qbank-src"
LOG="/var/log/qbank-deploy.log"
INSTANCES_CONF="$SRC/deploy/instances.conf"

log() { echo "$(date -Is) $*" >> "$LOG"; }
die() { log "SERVICE-CTL REJECTED: $*"; echo "$*" >&2; exit 1; }

VERB="${1:-}"
INSTANCE_NAME="${2:-}"
case "$VERB" in
  stop|start|restart) ;;
  *) die "usage: qbank-service-ctl.sh <stop|start|restart> <instance-name>" ;;
esac
[ -n "$INSTANCE_NAME" ] || die "usage: qbank-service-ctl.sh <stop|start|restart> <instance-name>"

svc=""
while IFS=: read -r name label cdir csvc cuser cport; do
  [[ "$name" == \#* ]] && continue
  [ -z "$name" ] && continue
  if [ "$name" = "$INSTANCE_NAME" ]; then
    svc="$csvc"
  fi
done < "$INSTANCES_CONF"
[ -n "$svc" ] || die "unknown instance: $INSTANCE_NAME"

systemctl "$VERB" "$svc"
sleep 1
if [ "$VERB" = "stop" ]; then
  if systemctl is-active --quiet "$svc"; then
    log "$INSTANCE_NAME: stop requested but $svc still active"
    echo "$svc is still active after stop" >&2
    exit 1
  fi
  log "$INSTANCE_NAME: $svc stopped"
  echo "$svc stopped."
else
  if systemctl is-active --quiet "$svc"; then
    log "$INSTANCE_NAME: $svc $VERB OK"
    echo "$svc is active."
  else
    log "$INSTANCE_NAME: $svc $VERB FAILED, check: journalctl -u $svc"
    echo "$svc failed to become active — check journalctl -u $svc" >&2
    exit 1
  fi
fi
