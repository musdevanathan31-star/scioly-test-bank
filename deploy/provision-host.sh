#!/usr/bin/env bash
# Provisions a bare RHEL host into a working Sci-Oly question-bank server:
# system accounts, directory tree, venvs, systemd units, the root-owned
# action scripts the admin app drives, and the narrow sudoers grants that
# let it drive them. Formalizes the by-hand bring-up that README's
# "Deploying", "Running multiple independent instances" and "Production
# deployment (current state)" sections describe in prose, so moving to a
# different box is a runbook step rather than an archaeology exercise.
#
#   sudo deploy/provision-host.sh [--dry-run] [--no-packages] [--no-firewall]
#                                 [--src <repo-dir>] [--venv <path>]
#
# Idempotent by construction: every step checks before it creates, so
# re-running after a partial failure -- or after adding a school to
# instances.conf -- is safe and does only the missing work.
#
# Instances come from deploy/instances.conf, the same single source of
# truth _apply-update.sh, qbank-rollback.sh, qbank-service-ctl.sh and
# admin_app.py already read, so adding a school never means editing this
# script. The admin app is a separate stanza below rather than an
# instances.conf row on purpose: every consumer of that file treats each
# row as a review-app instance to update/restart/roll back, and the admin
# app is none of those things to itself.
#
# DELIBERATELY NOT DONE HERE. Each of these needs either a human decision
# or a secret, and this script prints them as a closing checklist instead
# of guessing -- the same manual-over-automatic stance as the GitHub-update
# mechanism (see update-from-github.sh's header for that reasoning):
#   - Downloading the Caddy binary. If /usr/local/bin/caddy is already
#     present this installs the Caddyfile and sets the port-binding
#     capability; if it isn't, it prints the steps and moves on. This
#     script never fetches a binary off the internet and runs it.
#   - Writing any .env, auth_users.json, or password hash. Use
#     deploy/migrate-secrets.sh --import to bring those from the old host,
#     or the closing checklist for a greenfield box.
#   - Starting the services. Units are enabled but not started: without
#     the secrets above they would only crash-loop.
#   - DNS, the router's port-forward, and the backup cron lines.

set -euo pipefail

DRY_RUN=false
DO_PACKAGES=true
DO_FIREWALL=true
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_VENV="/opt/qbank/venv"

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)     DRY_RUN=true ;;
    --no-packages) DO_PACKAGES=false ;;
    --no-firewall) DO_FIREWALL=false ;;
    --src)         SRC="${2:?--src needs a path}"; shift ;;
    --venv)        SHARED_VENV="${2:?--venv needs a path}"; shift ;;
    -h|--help)     sed -n '2,39p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

INSTANCES_CONF="$SRC/deploy/instances.conf"
UNIT_TEMPLATE="$SRC/deploy/qbank.service"
DEPLOY_USER="qbank-deploy"
DEPLOY_HOME="/opt/qbank-deploy"
DEPLOY_SRC="/opt/qbank-src"
ADMIN_USER="qbank-admin"
ADMIN_HOME="/opt/qbank-admin"
BACKUP_ROOT="/opt/qbank-backups"
PKGS=(python3 python3-pip git rsync tar libcap libreoffice-headless restic)

# Code-only allow-list, identical to _apply-update.sh's -- if the two ever
# disagree, a freshly provisioned box differs from an updated one.
CODE_PATHS=(*.py templates static deploy requirements.txt requirements-dev.txt)

CREATED=()
SKIPPED=()

section() { printf '\n== %s\n' "$*"; }
say()     { printf '   %s\n' "$*"; }
made()    { CREATED+=("$*"); printf '   + %s\n' "$*"; }
kept()    { SKIPPED+=("$*"); printf '   . %s (already present)\n' "$*"; }
warn()    { printf '   ! %s\n' "$*" >&2; }

run() {
  if $DRY_RUN; then
    printf '   [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
section "Preflight"

if [ "$(id -u)" -ne 0 ] && ! $DRY_RUN; then
  echo "provision-host.sh must run as root (it creates users, units and sudoers files)." >&2
  exit 1
fi
for f in "$INSTANCES_CONF" "$UNIT_TEMPLATE" "$SRC/deploy/admin-app.service" "$SRC/requirements.txt"; do
  [ -f "$f" ] || { echo "missing $f -- is --src pointing at a full repo checkout?" >&2; exit 1; }
done
for cmd in useradd install rsync python3 systemctl; do
  if ! command -v "$cmd" >/dev/null; then
    # Under --dry-run these are only ever printed, never invoked, so a
    # missing one is not a reason to refuse -- previewing the plan from a
    # dev machine (which has no useradd/systemctl) is a legitimate use.
    if $DRY_RUN; then
      warn "not found: $cmd (dry run -- would be required for a real run)"
    else
      echo "required command not found: $cmd" >&2
      exit 1
    fi
  fi
done
say "source repo: $SRC"
say "shared venv: $SHARED_VENV"
$DRY_RUN && say "DRY RUN -- nothing will be changed"

mapfile -t INSTANCE_LINES < <(grep -v '^[[:space:]]*#' "$INSTANCES_CONF" | grep -v '^[[:space:]]*$')
[ "${#INSTANCE_LINES[@]}" -gt 0 ] || { echo "no instances in $INSTANCES_CONF" >&2; exit 1; }
say "instances: ${#INSTANCE_LINES[@]} from $(basename "$INSTANCES_CONF")"

IFS=: read -r _n _l _d _s FIRST_USER _p <<< "${INSTANCE_LINES[0]}"

# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------
section "System packages"

if $DO_PACKAGES; then
  if command -v dnf >/dev/null; then
    # libreoffice-headless is only needed for .docx/.doc ingestion and
    # restic only for the bulk backups, but both are cheap to install now
    # and annoying to discover missing later (see requirements.txt's notes).
    run dnf install -y "${PKGS[@]}"
    made "packages: ${PKGS[*]}"
  else
    warn "no dnf on this host -- install these yourself: ${PKGS[*]}"
  fi
else
  say "skipped (--no-packages)"
fi

# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
section "System accounts"

ensure_user() { # name home shell
  local name="$1" home="$2" shell="$3"
  if id -u "$name" >/dev/null 2>&1; then
    kept "user $name"
  else
    run useradd -r -m -d "$home" -s "$shell" "$name"
    made "user $name (home $home)"
  fi
}

for entry in "${INSTANCE_LINES[@]}"; do
  IFS=: read -r name label dir svc user port <<< "$entry"
  ensure_user "$user" "$(dirname "$dir")" /sbin/nologin
done

# The admin app reads journals for its console view. Group membership is
# what grants that -- read-only, and deliberately not a sudo grant.
ensure_user "$ADMIN_USER" "$ADMIN_HOME" /sbin/nologin
if id -nG "$ADMIN_USER" 2>/dev/null | tr ' ' '\n' | grep -qx systemd-journal; then
  kept "$ADMIN_USER in systemd-journal"
else
  run usermod -aG systemd-journal "$ADMIN_USER"
  made "$ADMIN_USER added to systemd-journal (read-only journal access)"
fi

# The deploy account needs a real shell: admin_app.py invokes its update
# script via "sudo -u qbank-deploy /opt/qbank-deploy/update-from-github.sh".
ensure_user "$DEPLOY_USER" "$DEPLOY_HOME" /bin/bash

# ---------------------------------------------------------------------------
# Directories and log files
# ---------------------------------------------------------------------------
section "Directories and logs"

ensure_dir() { # path owner mode
  local path="$1" owner="$2" mode="$3"
  if [ -d "$path" ]; then
    kept "dir $path"
  else
    run install -d -o "$owner" -g "$owner" -m "$mode" "$path"
    made "dir $path ($owner, $mode)"
  fi
}

ensure_log() { # path owner mode
  local path="$1" owner="$2" mode="$3"
  if [ -f "$path" ]; then
    kept "log $path"
  else
    # Created up front on purpose: these are appended to by non-root
    # accounts (qbank-deploy, qbank-admin, and the instance user via cron)
    # that cannot create a file in /var/log themselves, so a missing file
    # here is a first-run failure inside a script that otherwise looks fine.
    run install -o "$owner" -g "$owner" -m "$mode" /dev/null "$path"
    made "log $path ($owner)"
  fi
}

for entry in "${INSTANCE_LINES[@]}"; do
  IFS=: read -r name label dir svc user port <<< "$entry"
  ensure_dir "$(dirname "$dir")" "$user" 750
  ensure_dir "$dir" "$user" 750
done

ensure_dir "$ADMIN_HOME" "$ADMIN_USER" 750
ensure_dir "$ADMIN_HOME/app" "$ADMIN_USER" 750
ensure_dir "$DEPLOY_HOME" "$DEPLOY_USER" 750
ensure_dir "$BACKUP_ROOT" root 750

# Backup-destination credentials live apart from every app .env so an
# app-level bug can never read them (see README's "Backups").
ensure_dir "/opt/qbank/backup" "$FIRST_USER" 700

ensure_log /var/log/qbank-deploy.log "$DEPLOY_USER" 644
ensure_log /var/log/qbank-admin-actions.log "$ADMIN_USER" 640
ensure_log /var/log/qbank-backup.log "$FIRST_USER" 644

# ---------------------------------------------------------------------------
# Application code
# ---------------------------------------------------------------------------
section "Application code"

sync_code() { # dest owner
  local dest="$1" owner="$2" p
  cd "$SRC"
  for p in "${CODE_PATHS[@]}"; do
    [ -e "$p" ] || continue
    run rsync -a --delete "$SRC/$p" "$dest/"
  done
  cd - >/dev/null
  run chown -R "$owner:$owner" "$dest"
  made "code -> $dest ($owner)"
}

for entry in "${INSTANCE_LINES[@]}"; do
  IFS=: read -r name label dir svc user port <<< "$entry"
  sync_code "$dir" "$user"
done
sync_code "$ADMIN_HOME/app" "$ADMIN_USER"

# ---------------------------------------------------------------------------
# Virtualenvs
# ---------------------------------------------------------------------------
section "Virtualenvs"

# The shared venv stays root-owned: nothing writes to it at runtime, and an
# instance account that cannot modify its own interpreter is one less way
# for an app-level bug to become persistence. Dependency bumps therefore
# need root -- see the closing checklist.
if [ -x "$SHARED_VENV/bin/python" ]; then
  kept "venv $SHARED_VENV"
else
  run python3 -m venv "$SHARED_VENV"
  made "venv $SHARED_VENV"
fi
run "$SHARED_VENV/bin/pip" install -q -r "$SRC/requirements.txt"
say "dependencies installed into $SHARED_VENV"

# The deploy account's own venv is deliberately separate: update-from-github.sh
# executes freshly fetched code (pip build hooks, pytest collection) inside
# it, and must never be able to write to the venv serving live traffic.
if [ -x "$DEPLOY_HOME/venv/bin/python" ]; then
  kept "venv $DEPLOY_HOME/venv"
else
  run runuser -u "$DEPLOY_USER" -- python3 -m venv "$DEPLOY_HOME/venv"
  made "venv $DEPLOY_HOME/venv ($DEPLOY_USER-owned)"
fi
run runuser -u "$DEPLOY_USER" -- "$DEPLOY_HOME/venv/bin/pip" install -q -r "$SRC/requirements.txt"

# ---------------------------------------------------------------------------
# Update mechanism
# ---------------------------------------------------------------------------
section "Update mechanism"

if [ -d "$DEPLOY_SRC/.git" ]; then
  kept "clone $DEPLOY_SRC"
else
  REPO_URL="$(git -C "$SRC" remote get-url origin 2>/dev/null || true)"
  if [ -z "$REPO_URL" ]; then
    REPO_URL="$(sed -n 's/^REPO_URL="\(.*\)"$/\1/p' "$SRC/deploy/update-from-github.sh" | head -1)"
  fi
  if [ -n "$REPO_URL" ]; then
    run runuser -u "$DEPLOY_USER" -- git clone "$REPO_URL" "$DEPLOY_SRC"
    made "clone $DEPLOY_SRC from $REPO_URL"
  else
    warn "could not determine the repo URL -- clone it yourself:"
    warn "  sudo -u $DEPLOY_USER git clone <url> $DEPLOY_SRC"
  fi
fi

# admin_app.py invokes this at a fixed path in the deploy account's home
# (UPDATE_SCRIPT in admin_app.py), not out of the repo checkout.
run install -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 750 \
  "$SRC/deploy/update-from-github.sh" "$DEPLOY_HOME/update-from-github.sh"
made "$DEPLOY_HOME/update-from-github.sh"

# Root-owned action scripts. 750 root:root is what makes the NOPASSWD
# grants below safe: the granted accounts can execute these through sudo
# but can never edit them, so the fixed contents bound what they can do.
install_sbin() { # src dest
  run install -o root -g root -m 750 "$1" "$2"
  made "$2 (root:root 750)"
}
install_sbin "$SRC/deploy/_apply-update.sh"      /usr/local/sbin/qbank-apply-update.sh
install_sbin "$SRC/deploy/qbank-service-ctl.sh"  /usr/local/sbin/qbank-service-ctl.sh
install_sbin "$SRC/deploy/qbank-rollback.sh"     /usr/local/sbin/qbank-rollback.sh
install_sbin "$SRC/deploy/qbank-set-threads.sh"  /usr/local/sbin/qbank-set-threads.sh

# ---------------------------------------------------------------------------
# Sudoers
# ---------------------------------------------------------------------------
section "Sudoers grants"

install_sudoers() { # name content
  local name="$1" content="$2" dest tmp
  dest="/etc/sudoers.d/$name"
  if $DRY_RUN; then
    printf '   [dry-run] write %s:\n' "$dest"
    printf '%s\n' "$content" | sed 's/^/       /'
    return
  fi
  tmp="$(mktemp)"
  printf '%s\n' "$content" > "$tmp"
  # Validate before installing: a malformed sudoers file can lock every
  # account out of sudo, including the one that would fix it.
  if ! visudo -c -q -f "$tmp"; then
    rm -f "$tmp"
    echo "refusing to install invalid sudoers file $dest" >&2
    exit 1
  fi
  install -o root -g root -m 440 "$tmp" "$dest"
  rm -f "$tmp"
  made "$dest"
}

install_sudoers qbank-deploy \
"$DEPLOY_USER ALL=(root) NOPASSWD: /usr/local/sbin/qbank-apply-update.sh"

install_sudoers qbank-admin \
"$ADMIN_USER ALL=(root) NOPASSWD: /usr/local/sbin/qbank-service-ctl.sh
$ADMIN_USER ALL=(root) NOPASSWD: /usr/local/sbin/qbank-rollback.sh
$ADMIN_USER ALL=(root) NOPASSWD: /usr/local/sbin/qbank-set-threads.sh
$ADMIN_USER ALL=($DEPLOY_USER) NOPASSWD: $DEPLOY_HOME/update-from-github.sh"

# ---------------------------------------------------------------------------
# systemd units
# ---------------------------------------------------------------------------
section "systemd units"

install_unit() { # unit_name content_file what
  local dest="/etc/systemd/system/$1" content="$2" what="$3"
  if $DRY_RUN; then
    printf '   [dry-run] install unit %s (%s)\n' "$dest" "$what"
    return
  fi
  if [ -f "$dest" ] && cmp -s "$content" "$dest"; then
    kept "unit $1"
  else
    [ -f "$dest" ] && cp -a "$dest" "$dest.pre-provision.$(date -u +%Y%m%dT%H%M%SZ)"
    install -o root -g root -m 644 "$content" "$dest"
    made "unit $1 ($what)"
  fi
}

# Generated from deploy/qbank.service rather than written out here, so the
# gunicorn flags (--workers 1 is load-bearing; see that file's header) live
# in exactly one place and a second school can never silently drift from
# the first. For the first instance the result is byte-identical to the
# template, which is a useful sanity property.
for entry in "${INSTANCE_LINES[@]}"; do
  IFS=: read -r name label dir svc user port <<< "$entry"
  env_file="$(dirname "$dir")/.env"
  tmp_unit="$(mktemp)"
  {
    printf '# GENERATED by deploy/provision-host.sh from deploy/qbank.service\n'
    printf '# for instance "%s" (%s). Edit the template and re-run, not this file.\n' "$name" "$label"
    sed -e "s|^Description=.*|Description=Sci-Oly question-bank app -- $label (gunicorn)|" \
        -e "s|^User=.*|User=$user|" \
        -e "s|^Group=.*|Group=$user|" \
        -e "s|^WorkingDirectory=.*|WorkingDirectory=$dir|" \
        -e "s|^EnvironmentFile=.*|EnvironmentFile=$env_file|" \
        -e "s|--bind 127\.0\.0\.1:[0-9]*|--bind 127.0.0.1:$port|" \
        -e "s|^ExecStart=[^ ]*/bin/gunicorn|ExecStart=$SHARED_VENV/bin/gunicorn|" \
        "$UNIT_TEMPLATE"
  } > "$tmp_unit"

  # Cheap guard against the template being restructured out from under
  # these anchored substitutions: a unit missing any of these is broken in
  # a way systemd would only report at start time.
  for required in "^User=$user$" "^WorkingDirectory=$dir$" "^EnvironmentFile=$env_file$" "127.0.0.1:$port"; do
    grep -q "$required" "$tmp_unit" || {
      rm -f "$tmp_unit"
      echo "generated unit for '$name' lacks '$required' -- has deploy/qbank.service changed shape?" >&2
      exit 1
    }
  done

  install_unit "$svc" "$tmp_unit" "from template, port $port"
  rm -f "$tmp_unit"
done

install_unit admin-app.service "$SRC/deploy/admin-app.service" "verbatim"

run systemctl daemon-reload
for entry in "${INSTANCE_LINES[@]}"; do
  IFS=: read -r name label dir svc user port <<< "$entry"
  run systemctl enable "$svc"
done
run systemctl enable admin-app.service
# Enabled, not started -- see this script's header. Nothing here has an
# .env yet, so starting now would produce a crash loop and a misleading
# "it doesn't work" first impression.
say "units enabled (not started -- they have no .env yet)"

# ---------------------------------------------------------------------------
# Caddy
# ---------------------------------------------------------------------------
section "Reverse proxy (Caddy)"

if [ -x /usr/local/bin/caddy ]; then
  run setcap cap_net_bind_service=+ep /usr/local/bin/caddy
  made "cap_net_bind_service on /usr/local/bin/caddy (binds 80/443 without root)"
  if [ -f /etc/caddy/Caddyfile ] && cmp -s "$SRC/deploy/Caddyfile" /etc/caddy/Caddyfile; then
    kept "/etc/caddy/Caddyfile"
  else
    run install -d -o root -g root -m 755 /etc/caddy
    if [ -f /etc/caddy/Caddyfile ]; then
      run cp -a /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.pre-provision.$(date -u +%Y%m%dT%H%M%SZ)"
    fi
    run install -o root -g root -m 644 "$SRC/deploy/Caddyfile" /etc/caddy/Caddyfile
    made "/etc/caddy/Caddyfile (the domain is hardcoded in it -- edit for a new domain)"
  fi
else
  warn "/usr/local/bin/caddy not found -- this script does not download binaries."
  warn "Install the static binary yourself, then re-run this script to get the"
  warn "capability and Caddyfile installed."
fi

# ---------------------------------------------------------------------------
# Firewall
# ---------------------------------------------------------------------------
section "Firewall"

if $DO_FIREWALL && command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  # 80 as well as 443: Caddy's Let's Encrypt HTTP-01 challenge needs it
  # reachable, not just the TLS port (see README's home-network notes).
  run firewall-cmd --permanent --add-service=http
  run firewall-cmd --permanent --add-service=https
  run firewall-cmd --reload
  made "firewalld: http + https opened"
else
  say "skipped (--no-firewall, or firewalld not running) -- 80/443 must still reach this box"
fi

# ---------------------------------------------------------------------------
# Closing checklist
# ---------------------------------------------------------------------------
START_LINES=""
for entry in "${INSTANCE_LINES[@]}"; do
  IFS=: read -r name label dir svc user port <<< "$entry"
  START_LINES="${START_LINES}        sudo systemctl start $svc"$'\n'
done

cat <<EOF

============================================================
Provisioning complete: ${#CREATED[@]} change(s), ${#SKIPPED[@]} already in place.

WHAT'S LEFT -- none of it is safe to guess, so none of it is automated:

 1. Secrets. Nothing here wrote an .env, auth_users.json, or password
    hash. From the old host:
        sudo deploy/migrate-secrets.sh --export /root/qbank-secrets.age
    scp it across, then on this box:
        sudo deploy/migrate-secrets.sh --import /root/qbank-secrets.age
    Greenfield instead? Write each instance's .env from .env.example (a
    fresh FLASK_SECRET_KEY per instance: openssl rand -hex 32), write
    $ADMIN_HOME/.env with ADMIN_PASSWORD_HASH, then bootstrap the first
    account: $SHARED_VENV/bin/python auth.py --create-coach

 2. Verify what landed, before trusting it:
        sudo deploy/migrate-secrets.sh --check

 3. Data. This provisions code, not content. Restore each instance's
    DATA_ROOT (restic for bulk, the databank git repo for extracted
    JSON/markdown) and make sure DATA_ROOT in each .env points at where
    you actually put it.

 4. Start the services:
${START_LINES}        sudo systemctl start admin-app.service caddy

 5. Caddy: the domain in /etc/caddy/Caddyfile is hardcoded. Point DNS at
    this box and forward WAN 80+443 to it before expecting a certificate.

 6. Backup cron (as the instance user), once destinations exist -- see
    README's "Backups". Pass the instance .env as the trailing argument
    so DATA_ROOT resolves.

 7. Dependency bumps need root now, since the shared venv is root-owned:
        sudo $SHARED_VENV/bin/pip install -r <app-dir>/requirements.txt

NOT COVERED by this script or migrate-secrets.sh: a parity check between
the old and new host (unit states, listening ports, event and question
counts) before you move the port-forward. Compare those by hand.
============================================================
EOF
