# Server Migration Runbook

**Purpose.** Move a running Sci-Oly question-bank server from one machine to
another with no data loss and a short, controlled outage.

**Audience.** The person with root on both machines. No application
knowledge is required, but you must be comfortable with `ssh`, `sudo` and
`systemctl`.

**Time required.** Two to four hours, most of it waiting on data transfer.
The user-visible outage is only the final cut-over — typically under
fifteen minutes.

**Related documents.** `README.md` explains *why* the deployment is shaped
this way; `HOWTO.md` covers day-to-day use by role; `spec.md` §18 covers the
design rationale behind the provisioning and secret-transfer scripts. This
document is the ordered checklist. Where they disagree, trust the scripts
in `deploy/` — they are the executable source of truth.

---

## 1. The one idea that makes this migration safe

The server is made of **three separable things**. Migrating means moving
each one deliberately:

| Layer | What it is | How it moves |
|---|---|---|
| **Code** | This Git repository | `git clone`, then `deploy/provision-host.sh` |
| **Identity** | `.env` files, `auth_users.json`, password hashes | `deploy/migrate-secrets.sh --export` / `--import` |
| **Data** | Event PDFs, extracted questions, seasons, rosters, student responses | `restic restore` + the databank Git repo |

> **Read this before anything else.** The two automated backup pipelines
> deliberately **exclude** the identity layer, because they are unattended
> jobs writing to GitHub and S3 and those files are too sensitive to send
> there. A host rebuilt from backups alone therefore has all of its data and
> none of its identity: it will either refuse to start or start and log
> every user out. Section 4 is the step that closes that gap. Do not skip it.

---

## 2. Inventory the current machine

Run everything in this section **on the old host**. Record the output; you
will need it in Section 5 and to verify the migration in Section 8.

### 2.1 Confirm the secrets are all present and readable

```bash
sudo deploy/migrate-secrets.sh --check
```

This prints every file that makes this host *this* host — its presence,
owner and permissions, never its contents. It exits non-zero if anything
required is missing or mis-permissioned. **Resolve any failure before
continuing.**

Below that list it prints a second list: secrets that are **not files** and
therefore cannot be exported by any script. Read it carefully. In
particular:

> **The restic repository password.** If it exists only in
> `/opt/qbank/backup/.env` on a machine you are about to decommission, then
> every S3 snapshot becomes permanently undecryptable the moment that
> machine is wiped. Confirm it is in a password manager **now**, before you
> go any further.

### 2.2 Record the current topology

```bash
cat deploy/instances.conf                      # which instances exist
systemctl list-units 'qbank*' 'admin-app*' --all
ss -lntp | grep -E '5000|5001|5002'            # which ports are in use
for f in /opt/*/.env; do echo "== $f"; sudo grep -E '^(DATA_ROOT|APPLICATION_ROOT)=' "$f"; done
df -h                                          # how much data you are moving
```

### 2.3 Record a parity baseline

You will compare against these numbers after the migration. Nothing
automates this comparison.

- Open each school's landing page and write down the **event count** and the
  **question count for every event**.
- Note which systemd units are active.
- Note the current `--threads` value per instance:
  ```bash
  systemctl show qbank.service --property=ExecStart --value
  ```
  Thread counts set through the admin app live in a systemd drop-in, not in
  any `.env`, and are **not** carried across by `migrate-secrets.sh`. Re-apply
  them by hand on the new host.

---

## 3. Prepare the new machine

### 3.1 Base operating system

Install a clean RHEL (or compatible) system. The current production host
runs **RHEL 10.2**. Give the machine a static or DHCP-reserved LAN address
so it cannot drift.

### 3.2 Get the repository onto the box

```bash
sudo dnf install -y git
sudo git clone <your-repo-url> /opt/qbank-src-bootstrap
cd /opt/qbank-src-bootstrap
```

Any location works for this bootstrap copy; provisioning creates the
canonical one at `/opt/qbank-src`.

### 3.3 Review the provisioning plan, then run it

```bash
sudo deploy/provision-host.sh --dry-run     # read the entire plan first
sudo deploy/provision-host.sh               # then run it for real
```

The script is **idempotent** — re-run it as often as you like, after a
partial failure or after adding a school to `instances.conf`. Every step
checks before it creates, and it reports each item as created or already
present.

#### What provisioning creates

**System accounts** (all `-r` system accounts; the instance and admin
accounts are `/sbin/nologin`):

| Account | Home | Shell | Purpose |
|---|---|---|---|
| `qbank` | `/opt/qbank` | `nologin` | Runs the NCMS instance; owns its data |
| `qbank-chs` | `/opt/qbank-chs` | `nologin` | Runs the CHS instance; owns its data |
| `qbank-admin` | `/opt/qbank-admin` | `nologin` | Runs the admin app. Added to the `systemd-journal` group for read-only log access |
| `qbank-deploy` | `/opt/qbank-deploy` | `/bin/bash` | Fetches and validates code updates from GitHub |

One instance account is created per row in `deploy/instances.conf`. If you
are adding or renaming schools, edit that file **before** provisioning.

**Directories:**

| Path | Owner | Mode | Contents |
|---|---|---|---|
| `/opt/qbank/app` | `qbank` | 750 | NCMS application code |
| `/opt/qbank-chs/app` | `qbank-chs` | 750 | CHS application code |
| `/opt/qbank-admin/app` | `qbank-admin` | 750 | Admin application code |
| `/opt/qbank/venv` | `root` | — | Shared Python virtualenv for all instances |
| `/opt/qbank-deploy` | `qbank-deploy` | 750 | Update script and its own venv |
| `/opt/qbank-src` | `qbank-deploy` | 750 | Canonical Git clone that updates are pulled into |
| `/opt/qbank-backups` | `root` | 750 | Pre-update backups, used by rollback |
| `/opt/qbank/backup` | `qbank` | 700 | Backup-destination credentials (`.env`) |

**Log files:** `/var/log/qbank-deploy.log`, `/var/log/qbank-admin-actions.log`,
`/var/log/qbank-backup.log`.

**Root-owned action scripts** in `/usr/local/sbin/` (mode 750), each reachable
through one narrowly scoped `NOPASSWD` sudoers rule:
`qbank-apply-update.sh`, `qbank-service-ctl.sh`, `qbank-rollback.sh`,
`qbank-set-threads.sh`.

**Packages:** `python3`, `python3-pip`, `git`, `rsync`, `tar`, `libcap`,
`libreoffice-headless` (required for `.docx`/`.doc` ingestion), `restic`.

**systemd units:** one per instance, generated from `deploy/qbank.service` as
a template, plus `admin-app.service`. Units are **enabled but not started** —
without secrets they would only crash-loop.

**Firewall:** ports 80 and 443 opened. Port 80 is required as well as 443,
because Caddy's Let's Encrypt HTTP-01 challenge uses it.

#### What provisioning deliberately does *not* do

Each of these needs a human decision or a secret:

- **It does not download the Caddy binary.** If `/usr/local/bin/caddy` already
  exists, the script installs the Caddyfile and sets the port-binding
  capability. If not, it prints the steps and moves on. This script never
  fetches a binary from the internet and runs it as root.
- **It does not write any `.env`, `auth_users.json`, or password hash.**
- **It does not start any service.**
- **It does not configure DNS, the router port-forward, or backup cron jobs.**

---

## 4. Move the secrets

Provision **first** — this step applies ownership by account name, and those
accounts must already exist.

### 4.1 Export from the old host

```bash
sudo deploy/migrate-secrets.sh --export /root/qbank-secrets.age
```

You will be prompted for a passphrase. The bundle is encrypted with `age -p`,
or `gpg --symmetric` if `age` is not installed. There is **no mode that
writes an unencrypted bundle** — this file crosses a network and may sit in
`/root` on both machines for a while.

Choose a strong passphrase and transfer it out of band (a password manager,
not the same channel as the file).

### 4.2 Transfer

```bash
scp /root/qbank-secrets.age root@<new-host>:/root/
```

### 4.3 Import on the new host

```bash
sudo deploy/migrate-secrets.sh --import /root/qbank-secrets.age
```

The import is deliberately defensive:

- It **refuses any archive member** that is not a path the manifest names, so
  a tampered bundle cannot write files elsewhere.
- It **moves aside** anything it would overwrite.
- It re-applies ownership **by account name**, not by numeric UID. System
  accounts get different UIDs on a freshly provisioned machine, so a
  faithful UID-preserving extract would be exactly wrong — it would leave
  every file owned by whatever unrelated account happens to hold that number.

### 4.4 What the bundle contains

| File | Owner | Mode | Why it matters |
|---|---|---|---|
| `<instance-root>/.env` | instance user | 600 | `FLASK_SECRET_KEY` (sessions survive restarts), `ANTHROPIC_API_KEY`, `APPLICATION_ROOT`, `DATA_ROOT`, `SESSION_COOKIE_SECURE` |
| `<data-root>/auth_users.json` | instance user | 600 | Every account: usernames, roles, password hashes |
| `<app-dir>/.scioly_cookies.json` | instance user | 600 | Cached scioly.org bypass cookies (optional; ~7-day life) |
| `/opt/qbank-admin/.env` | `qbank-admin` | 600 | `ADMIN_PASSWORD_HASH` and the admin app's own `FLASK_SECRET_KEY` |
| `/opt/qbank/backup/.env` | `qbank` | 600 | GitHub PAT, AWS keys, restic repository password |

Note that `auth_users.json` follows **`DATA_ROOT`**, not the application
directory — on a migrated instance it is not under `/opt/qbank/app`.

### 4.5 Verify

```bash
sudo deploy/migrate-secrets.sh --check
```

Every required file must be present, correctly owned, and correctly
permissioned before you continue.

---

## 5. Restore the data

Provisioning installs code, not content. Restore each instance's data
separately.

### 5.1 Bulk data — PDFs, images, texts, textbooks, student responses

Source the backup credentials, then restore from S3:

```bash
set -a; . /opt/qbank/backup/.env; set +a
restic snapshots                       # confirm you can read the repository
```

> **restic records absolute paths.** `--target` is a prefix, not a
> destination — restoring straight into the new `DATA_ROOT` would nest the
> data one level deeper than you want. Restore into a staging directory,
> confirm the layout, then move it into place.

```bash
sudo mkdir -p /restore
sudo restic restore latest --target /restore

# Inspect what landed. The data sits under the OLD host's DATA_ROOT path,
# e.g. /restore/data/qbank/ncms/ or /restore/opt/qbank/app/
sudo find /restore -maxdepth 4 -type d | head -20

# Then move it to the new location (note the trailing slashes)
sudo rsync -a /restore/<old-data-root>/ /data/qbank/ncms/
```

### 5.2 Extracted data — questions and generated markdown

Clone the private databank repository and copy its contents into the same
`DATA_ROOT`:

```bash
git clone <databank-repo-url> /tmp/databank
sudo rsync -a /tmp/databank/ /data/qbank/ncms/
```

### 5.3 Fix ownership

```bash
sudo chown -R qbank:qbank /data/qbank/ncms
sudo chown -R qbank-chs:qbank-chs /data/qbank/chs
```

### 5.4 Point each instance at its data

> **This is the single most common way a migration ends with a server that
> starts cleanly and shows zero questions.** `DATA_ROOT` in each `.env` came
> from the old host and still contains the old host's path.

Open each instance's `.env` and confirm `DATA_ROOT` matches where you
actually put the data:

```bash
sudo -e /opt/qbank/.env          # DATA_ROOT=/data/qbank/ncms
sudo -e /opt/qbank-chs/.env      # DATA_ROOT=/data/qbank/chs
```

Repeat for every instance in `deploy/instances.conf`.

---

## 6. Reverse proxy and DNS

### 6.1 Install Caddy

Provisioning does not fetch the binary. Install Caddy yourself, place it at
`/usr/local/bin/caddy`, then re-run `provision-host.sh` — it will detect the
binary, install the Caddyfile and set the capability. Or do it by hand:

```bash
sudo setcap cap_net_bind_service=+ep /usr/local/bin/caddy
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
```

The capability is what lets Caddy bind ports 80 and 443 **without running as
root**.

### 6.2 Check the Caddyfile

The domain in `deploy/Caddyfile` is **hardcoded**. Set it to your own public
domain. Each school is mounted under its own path, matching the
`APPLICATION_ROOT` set in that instance's `.env`:

| Path | Backend |
|---|---|
| `/testbank/<school-a>*` | `127.0.0.1:<port-a>` |
| `/testbank/<school-b>*` | `127.0.0.1:<port-b>` |
| `/testbank/admin*` | `127.0.0.1:<admin-port>` |

> Do **not** change these `handle` blocks to `handle_path`. The blocks forward
> the path *without* stripping the prefix, because the application's
> `_PrefixMiddleware` expects the full path including it.

### 6.3 Do not point DNS at the new host yet

Caddy cannot obtain a certificate until DNS resolves to this machine and
ports 80 and 443 reach it. That is a **cut-over** step (Section 9), not a
preparation step. Verify everything else first.

---

## 7. Start the services

```bash
sudo systemctl start qbank.service
sudo systemctl start qbank-chs.service
sudo systemctl start admin-app.service
sudo systemctl start caddy
```

Re-apply any per-instance thread count you recorded in Section 2.3:

```bash
sudo /usr/local/sbin/qbank-set-threads.sh ncms 8
```

---

## 8. Verify before cutting over

**"The service started" is not the same as "it works."** Nothing automates
this comparison — work through it by hand.

| Check | Command or action | Expected |
|---|---|---|
| Units active | `systemctl status qbank qbank-chs admin-app caddy` | All `active (running)` |
| Ports listening | `ss -lntp \| grep -E '5000\|5001\|5002'` | One gunicorn per instance |
| Secrets intact | `sudo deploy/migrate-secrets.sh --check` | Exits zero |
| App responds | `curl -sI localhost:<port-a>/testbank/<school-a>/login` | `200` or `302` |
| **Event count** | Open each school's landing page | Matches your Section 2.3 baseline |
| **Question count per event** | Same page | Matches the baseline, event by event |
| Login works | Sign in as a coach | Succeeds with the existing password |
| Assessments intact | Open the Assessments dashboard | Seasons, windows and assessments all present |
| Student data intact | Open Scores for the current season | Scores match the old host |
| Word ingestion | `which soffice` | Present (needed for `.docx`/`.doc` uploads) |

If question counts differ, stop. The usual cause is a `DATA_ROOT` that still
points at the old host's path (Section 5.4), or a restore that has not
finished.

---

## 9. Cut over

Only once the new machine genuinely serves.

1. **Freeze the old host.** Anything a coach saves on the old machine after
   your final data sync is lost when you switch. Either stop the old
   instances before the last sync, or plan to re-sync and accept a short
   outage:
   ```bash
   sudo systemctl stop qbank qbank-chs admin-app     # on the OLD host
   ```
2. **Re-sync any data** that changed since your first restore.
3. **Move the network.** On the router, forward WAN ports 80 and 443 to the
   new machine's LAN address.
4. **Update Dynamic DNS.** If the gateway's DDNS client is pinned to a
   particular host, repoint it. This has silently drifted before — verify
   the domain resolves to the current public IP rather than assuming.
5. **Watch the certificate issue.** Caddy requests one automatically on the
   first request once DNS and forwarding are correct:
   ```bash
   sudo journalctl -u caddy -f
   ```
6. **Confirm from outside the network** — use mobile data, not the LAN — that
   each school's URL loads over HTTPS.

---

## 10. Restore the backup schedule

Backups do not migrate themselves. Recreate the cron entries **as each
instance user**, once the destinations are reachable from the new host.

Pass the instance's own `.env` as the trailing argument so `DATA_ROOT`
resolves correctly — without it, both scripts back up the now-empty
application directory:

```bash
# every 1-2 hours: extracted questions and markdown, into the databank repo
deploy/backup-extracted-data.sh /opt/qbank/app /opt/qbank/databank \
    /opt/qbank/backup/.env /opt/qbank/.env

# nightly: bulk binary data to S3 via restic
deploy/backup-bulk-data.sh /opt/qbank/app /opt/qbank/backup/.env /opt/qbank/.env
```

Verify within twenty-four hours:

```bash
tail /var/log/qbank-backup.log
set -a; . /opt/qbank/backup/.env; set +a && restic snapshots
```

A backup that has never been restored is not a verified backup. Schedule a
test restore.

---

## 11. Decommission the old machine

**Keep the old host installed but stopped for at least one week.** It is your
rollback, and it costs nothing to keep.

When you are confident:

1. Destroy the transfer bundle on **both** machines:
   ```bash
   sudo shred -u /root/qbank-secrets.age
   ```
2. Rotate anything exposed during the move — the bundle passphrase at
   minimum.
3. Only then wipe the old machine.

> Do **not** rotate the restic repository password as part of this cleanup.
> Rotating it means re-encrypting or losing access to every existing
> snapshot. Confirm you need nothing from the old snapshots first.

---

## 12. Rolling back

If the new machine fails after cut-over and you have not wiped the old one:

1. Move the router's port-forward back to the old machine's LAN address.
2. Repoint Dynamic DNS if it was changed.
3. Start the old services:
   ```bash
   sudo systemctl start qbank qbank-chs admin-app caddy
   ```

Anything students or coaches saved on the **new** machine after cut-over does
not exist on the old one. Export it before rolling back if it matters.

---

## 13. Troubleshooting

| Symptom | Most likely cause | Fix |
|---|---|---|
| App starts, zero questions everywhere | `DATA_ROOT` still points at the old host's path | Section 5.4 |
| Everyone is logged out after every restart | `FLASK_SECRET_KEY` missing from the instance `.env` | Re-import secrets (Section 4) |
| Service crash-loops immediately | No `.env`, or `SESSION_COOKIE_SECURE=true` with no `FLASK_SECRET_KEY` | `journalctl -u qbank -n 50`; the startup check names the missing variable |
| Nobody can log in, but the app runs | `auth_users.json` missing or under the wrong path | It follows `DATA_ROOT`, not the app dir |
| No certificate; browser warns | DNS not pointing here, or port 80 not forwarded | Section 6.3 and 9; check `journalctl -u caddy` |
| 404 on every school URL | `APPLICATION_ROOT` and the Caddyfile `handle` block disagree | Compare the `.env` value with `/etc/caddy/Caddyfile` |
| `.docx` uploads fail, or archive Word previews show an error | `libreoffice-headless` not installed | `sudo dnf install libreoffice-headless`. If the distribution no longer ships it (recent RHEL), install LibreOffice via Flatpak or a container and set `SOFFICE_BIN=<path-to-binary>` in that instance's `.env` |
| Admin app rejects the password | `ADMIN_PASSWORD_HASH` missing from `/opt/qbank-admin/.env` | Re-import secrets |
| `restic` cannot read the repository | Wrong repository password, or AWS keys not sourced | `set -a; . /opt/qbank/backup/.env; set +a` |
| Thread count reverted to default | Set via a systemd drop-in, which is host-local and not migrated | `qbank-set-threads.sh <instance> <n>` |

---

## 14. Reference

### Deployment values template

Fill these in for your own environment.

| Item | Value |
|---|---|
| OS | `<distribution/version>` |
| Hostname | `<server-hostname>` |
| LAN address | `<lan-ip>` (DHCP-reserved) |
| Gateway | `<router/gateway model>` |
| Public domain | `<public-domain>` (DNS provider of your choice) |
| Reverse proxy | Caddy, static binary at `/usr/local/bin/caddy` |
| Primary instance | `https://<domain>/testbank/<school-a>/` · `<app-dir-a>` · user `<user-a>` · `127.0.0.1:<port-a>` · `<service-a>.service` |
| Secondary instance | `https://<domain>/testbank/<school-b>/` · `<app-dir-b>` · user `<user-b>` · `127.0.0.1:<port-b>` · `<service-b>.service` |
| Admin app | `https://<domain>/testbank/admin/` · `<admin-app-dir>` · user `<admin-user>` · `127.0.0.1:<admin-port>` · `<admin-service>.service` |
| Shared venv | `<shared-venv-path>` |
| Data locations | `<data-root-a>`, `<data-root-b>` |

### Scripts used in this runbook

| Script | Role |
|---|---|
| `deploy/provision-host.sh` | Builds a bare host into a working server. Idempotent. `--dry-run` shows the plan |
| `deploy/migrate-secrets.sh` | `--check` / `--export` / `--import` the identity layer |
| `deploy/secrets-manifest.conf` | The list of what counts as a secret, including non-file items |
| `deploy/instances.conf` | Single source of truth for which instances exist |
| `deploy/qbank.service` | systemd unit template; per-instance units are generated from it |
| `deploy/Caddyfile` | Reverse proxy and automatic HTTPS |
| `deploy/backup-extracted-data.sh` | Questions and markdown → private Git repository |
| `deploy/backup-bulk-data.sh` | PDFs, images and responses → S3 via restic |
| `deploy/migrate-data-root.sh` | Moves one instance's data to a new `DATA_ROOT` on the **same** machine — not host-to-host |
| `deploy/qbank-set-threads.sh` | Adjusts a running instance's gunicorn thread count |

### A note on `--workers`

Every generated unit runs `gunicorn --workers 1`. **This is load-bearing, not
a default to tune.** The application's per-event state lock and its
background-job queue are both in-process designs that are only correct within
a single worker process. `--threads` is the safe knob if you move to a more
powerful machine; raising `--workers` requires redesigning both mechanisms
first. See `README.md` for the full explanation.

### Post-migration checklist

- [ ] `migrate-secrets.sh --check` passes on the new host
- [ ] Event and question counts match the pre-migration baseline
- [ ] A coach can sign in with an existing password
- [ ] Seasons, assessment windows and student scores are all present
- [ ] HTTPS works from outside the local network
- [ ] Thread counts re-applied per instance
- [ ] Backup cron entries recreated and verified in the log
- [ ] Transfer bundle shredded on both machines
- [ ] Old machine stopped but retained for at least one week
