#!/usr/bin/env bash
# Moves the files that make a host *this* host -- the ones neither backup
# mechanism touches -- from an old server to a new one, and verifies them
# at both ends.
#
#   sudo deploy/migrate-secrets.sh --check
#   sudo deploy/migrate-secrets.sh --export <outfile>    # on the OLD host
#   sudo deploy/migrate-secrets.sh --import <infile>     # on the NEW host
#
# The list lives in deploy/secrets-manifest.conf, expanded per instance
# from deploy/instances.conf -- see that file's header for why these are
# excluded from backup-extracted-data.sh / backup-bulk-data.sh, and why
# that exclusion is exactly what makes this script necessary.
#
# --check   Every manifest file present, owned by the right account, at the
#           right mode. Never prints a file's contents. Non-zero exit if
#           anything required is missing or mis-permissioned. Run it on the
#           old host before exporting and on the new host after importing.
#
# --export  Tars the manifest files and encrypts the tarball with `age -p`
#           (or `gpg --symmetric` if age isn't installed). You type the
#           passphrase; it is never stored, and this script has no
#           mode that writes an unencrypted bundle -- the whole point is
#           that this file crosses a network and may sit in /root for a
#           while on both ends.
#
# --import  Decrypts, refuses any archive member that isn't a path the
#           manifest names (so a tampered bundle can't drop a file
#           somewhere else), moves aside anything it would overwrite, then
#           restores. Ownership is re-applied BY NAME from the manifest
#           rather than restored from the archive: system accounts get
#           different numeric UIDs on a freshly provisioned box, so a
#           faithful uid-preserving extract is precisely the wrong thing
#           here -- it would leave every file owned by whatever unrelated
#           account happens to hold that uid on the new host.
#
# Ordering with provision-host.sh: provision first (it creates the accounts
# these files must be owned by), import second, start services third.

set -euo pipefail
umask 077

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$SRC/deploy/secrets-manifest.conf"
INSTANCES_CONF="$SRC/deploy/instances.conf"

MODE=""
TARGET=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check)  MODE=check ;;
    --export) MODE=export; TARGET="${2:?--export needs an output path}"; shift ;;
    --import) MODE=import; TARGET="${2:?--import needs an input path}"; shift ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$MODE" ] || { echo "usage: migrate-secrets.sh --check | --export <file> | --import <file>" >&2; exit 2; }

for f in "$MANIFEST" "$INSTANCES_CONF"; do
  [ -f "$f" ] || { echo "missing $f" >&2; exit 1; }
done

# ---------------------------------------------------------------------------
# Manifest expansion
# ---------------------------------------------------------------------------

# Internal field separator for expanded records. Deliberately not a tab:
# tab is IFS whitespace, so `read` collapses runs of them and empty fields
# (every field but the last on a note: row) silently disappear.
SEP=$'\x1f'

# Parse a single VAR=value out of an .env without sourcing it. The backup
# scripts source theirs; this one runs as root over files being carried
# between machines, so it reads rather than executes them.
env_value() { # file var
  local file="$1" var="$2" val
  [ -f "$file" ] || return 0
  val="$(sed -n "s/^[[:space:]]*${var}[[:space:]]*=[[:space:]]*//p" "$file" | tail -1)"
  val="${val%\"}"; val="${val#\"}"
  val="${val%\'}"; val="${val#\'}"
  printf '%s' "$val"
}

# Emits one record per line: kind SEP path SEP owner SEP mode SEP description
expand_manifest() {
  local line kind scope path owner mode desc epath eowner
  local name label dir svc user port root data_root
  while IFS= read -r line; do
    case "$line" in
      ''|'#'*) continue ;;
      note:*)  printf '%s\n' "note${SEP}${SEP}${SEP}${SEP}${line#note:}"; continue ;;
    esac
    IFS=: read -r kind scope path owner mode desc <<< "$line"
    case "$scope" in
      global)
        printf '%s\n' "${kind}${SEP}${path}${SEP}${owner}${SEP}${mode}${SEP}${desc}"
        ;;
      instance)
        while IFS= read -r entry; do
          IFS=: read -r name label dir svc user port <<< "$entry"
          root="$(dirname "$dir")"
          data_root="$(env_value "$root/.env" DATA_ROOT)"
          [ -n "$data_root" ] || data_root="$dir"
          epath="$(printf '%s' "$path"  | sed -e "s|{root}|$root|g" -e "s|{app_dir}|$dir|g" -e "s|{data_root}|$data_root|g" -e "s|{user}|$user|g")"
          eowner="$(printf '%s' "$owner" | sed -e "s|{user}|$user|g")"
          printf '%s\n' "${kind}${SEP}${epath}${SEP}${eowner}${SEP}${mode}${SEP}[$name] $desc"
        done < <(grep -v '^[[:space:]]*#' "$INSTANCES_CONF" | grep -v '^[[:space:]]*$')
        ;;
      *) echo "manifest: unknown scope '$scope' in: $line" >&2; exit 1 ;;
    esac
  done < "$MANIFEST"
}

mapfile -t ENTRIES < <(expand_manifest)

# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------
if [ "$MODE" = check ]; then
  problems=0
  missing_optional=0
  printf '\nSecret files this host should have (contents are never printed):\n\n'
  for row in "${ENTRIES[@]}"; do
    IFS="$SEP" read -r kind path owner mode desc <<< "$row"
    [ "$kind" = note ] && continue
    if [ ! -e "$path" ]; then
      if [ "$kind" = optional ]; then
        printf '  --  %-45s absent (optional)\n' "$path"
        missing_optional=$((missing_optional + 1))
      else
        printf '  !!  %-45s MISSING\n' "$path"
        printf '      %s\n' "$desc"
        problems=$((problems + 1))
      fi
      continue
    fi
    actual_owner="$(stat -c '%U' "$path")"
    actual_mode="$(stat -c '%a' "$path")"
    if [ "$actual_owner" != "$owner" ] || [ "$actual_mode" != "$mode" ]; then
      printf '  !!  %-45s owner/mode is %s/%s, expected %s/%s\n' \
        "$path" "$actual_owner" "$actual_mode" "$owner" "$mode"
      problems=$((problems + 1))
    else
      printf '  ok  %-45s %s %s\n' "$path" "$owner" "$mode"
    fi
  done

  printf '\nNot files -- confirm you still hold these before decommissioning the old host:\n\n'
  for row in "${ENTRIES[@]}"; do
    IFS="$SEP" read -r kind path owner mode desc <<< "$row"
    [ "$kind" = note ] && printf '  *   %s\n' "$desc"
  done

  printf '\n'
  if [ "$problems" -gt 0 ]; then
    printf '%d problem(s). This host is not ready to serve.\n\n' "$problems"
    exit 1
  fi
  printf 'All required secrets present (%d optional absent).\n\n' "$missing_optional"
  exit 0
fi

# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------
AGE_MAGIC='age-encryption.org/v1'

encrypt_to() { # plaintext_file out_file
  if command -v age >/dev/null; then
    echo "Encrypting with age -- you will be prompted for a passphrase."
    age -p -o "$2" "$1"
  elif command -v gpg >/dev/null; then
    echo "age not installed; encrypting with gpg --symmetric (AES256)."
    gpg --symmetric --cipher-algo AES256 --output "$2" "$1"
  else
    cat >&2 <<'EOF'
Neither `age` nor `gpg` is installed, and this script will not write an
unencrypted bundle of every secret on the box. Install one:
    sudo dnf install age      # or: sudo dnf install gnupg2
EOF
    exit 1
  fi
}

decrypt_to() { # in_file plaintext_file
  if head -c "${#AGE_MAGIC}" "$1" | grep -q "$AGE_MAGIC"; then
    command -v age >/dev/null || { echo "bundle is age-encrypted but age is not installed" >&2; exit 1; }
    age -d -o "$2" "$1"
  else
    command -v gpg >/dev/null || { echo "bundle looks gpg-encrypted but gpg is not installed" >&2; exit 1; }
    gpg --decrypt --output "$2" "$1"
  fi
}

[ "$(id -u)" -eq 0 ] || { echo "--$MODE must run as root (these files are mode 600, owned by service accounts)." >&2; exit 1; }

TMPDIR_SECRETS="$(mktemp -d)"
cleanup() { rm -rf "$TMPDIR_SECRETS"; }
trap cleanup EXIT

# ---------------------------------------------------------------------------
# --export
# ---------------------------------------------------------------------------
if [ "$MODE" = export ]; then
  [ -e "$TARGET" ] && { echo "$TARGET already exists -- refusing to overwrite." >&2; exit 1; }

  members=()
  skipped=0
  for row in "${ENTRIES[@]}"; do
    IFS="$SEP" read -r kind path owner mode desc <<< "$row"
    [ "$kind" = note ] && continue
    if [ ! -e "$path" ]; then
      if [ "$kind" = optional ]; then
        skipped=$((skipped + 1))
        continue
      fi
      echo "required secret missing on this host: $path" >&2
      echo "run --check first; refusing to export an incomplete bundle." >&2
      exit 1
    fi
    members+=("${path#/}")
  done
  [ "${#members[@]}" -gt 0 ] || { echo "nothing to export" >&2; exit 1; }

  printf 'Bundling %d file(s) (%d optional absent):\n' "${#members[@]}" "$skipped"
  printf '  /%s\n' "${members[@]}"

  tar -czf "$TMPDIR_SECRETS/secrets.tar.gz" -C / "${members[@]}"
  encrypt_to "$TMPDIR_SECRETS/secrets.tar.gz" "$TARGET"
  chmod 600 "$TARGET"

  cat <<EOF

Wrote $TARGET ($(stat -c '%s' "$TARGET") bytes, encrypted).

Next:
  scp $TARGET <new-host>:/root/
  # on the new host, AFTER provision-host.sh has created the accounts:
  sudo deploy/migrate-secrets.sh --import /root/$(basename "$TARGET")

Then delete this bundle from both hosts -- it is every secret on the box in
one file, and it has no reason to outlive the migration.
EOF
  exit 0
fi

# ---------------------------------------------------------------------------
# --import
# ---------------------------------------------------------------------------
if [ "$MODE" = import ]; then
  [ -f "$TARGET" ] || { echo "no such bundle: $TARGET" >&2; exit 1; }

  decrypt_to "$TARGET" "$TMPDIR_SECRETS/secrets.tar.gz"

  # Allow-list of exactly what the manifest names, as archive-relative paths.
  allowed=()
  for row in "${ENTRIES[@]}"; do
    IFS="$SEP" read -r kind path owner mode desc <<< "$row"
    [ "$kind" = note ] && continue
    allowed+=("${path#/}")
  done

  mapfile -t members < <(tar -tzf "$TMPDIR_SECRETS/secrets.tar.gz" | grep -v '/$')
  for m in "${members[@]}"; do
    case "$m" in
      /*|*..*) echo "refusing bundle: unsafe member path '$m'" >&2; exit 1 ;;
    esac
    ok=false
    for a in "${allowed[@]}"; do
      [ "$m" = "$a" ] && ok=true && break
    done
    $ok || {
      echo "refusing bundle: member '/$m' is not a path secrets-manifest.conf names." >&2
      echo "Either the bundle was built from a different instances.conf, or it was tampered with." >&2
      exit 1
    }
  done
  printf 'Bundle contains %d manifest-approved file(s).\n\n' "${#members[@]}"

  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  for m in "${members[@]}"; do
    if [ -e "/$m" ]; then
      cp -a "/$m" "/$m.pre-import.$ts"
      printf '  moved aside /%s -> /%s.pre-import.%s\n' "$m" "$m" "$ts"
    fi
  done

  # --no-same-owner is the point, not an oversight: see this script's
  # header on uid drift. Ownership comes from the manifest, below.
  tar -xzf "$TMPDIR_SECRETS/secrets.tar.gz" -C / --no-same-owner "${members[@]}"

  # Ownership failures are reported and counted rather than fatal: aborting
  # mid-loop would leave a half-restored set of secrets with no summary of
  # which ones landed, which is a worse place to debug from than a complete
  # restore plus an explicit list of what needs a manual chown.
  # user:user matches _apply-update.sh's convention -- useradd -r gives each
  # service account a group of the same name.
  restore_problems=0
  for row in "${ENTRIES[@]}"; do
    IFS="$SEP" read -r kind path owner mode desc <<< "$row"
    [ "$kind" = note ] && continue
    [ -e "$path" ] || continue
    if ! id -u "$owner" >/dev/null 2>&1; then
      printf '  !! %-45s account %s does not exist -- run provision-host.sh first\n' "$path" "$owner" >&2
      restore_problems=$((restore_problems + 1))
      continue
    fi
    if ! chown "$owner:$owner" "$path" 2>/dev/null; then
      printf '  !! %-45s chown to %s:%s failed -- fix ownership by hand\n' "$path" "$owner" "$owner" >&2
      restore_problems=$((restore_problems + 1))
      continue
    fi
    chmod "$mode" "$path"
    printf '  restored %-45s %s %s\n' "$path" "$owner" "$mode"
  done

  if [ "$restore_problems" -gt 0 ]; then
    printf '\n%d file(s) restored but not correctly owned -- see above. The app will\n' "$restore_problems" >&2
    printf 'not read a secret it cannot open; fix these before starting services.\n\n' >&2
  fi

  cat <<EOF

Imported. Now:
  sudo deploy/migrate-secrets.sh --check
  # confirm DATA_ROOT in each .env points at where the data actually is on
  # THIS host before starting anything -- the .env came from the old box and
  # its DATA_ROOT is the old box's path.
  shred -u $TARGET    # or rm; it has no reason to outlive the migration
EOF
  [ "$restore_problems" -eq 0 ] || exit 1
  exit 0
fi
