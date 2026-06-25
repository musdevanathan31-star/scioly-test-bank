"""
Authentication and role-based access for the shared, multi-user deployment.

Three roles:
    coach     — full admin, implicit access to every event, plus user
                management and shared-textbook management.
    volunteer — edit access only to the events a coach has assigned them.
    student   — no question-bank access at all (enforced in review_app.py's
                _select_event); scoped instead to the season-long testing
                workflow (seasons.py/testing.py) via a per-season roster,
                never via the `events` field below (that field keeps
                meaning exactly what it means for a volunteer — bank-edit
                access — a deliberately separate, unrelated mechanism).

Stored as a flat JSON file at the repo root (`auth_users.json`), same
pattern as `events.py`'s `events_custom.json` — no DB, atomic tempfile+
os.replace writes, one lock for concurrent writers.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

REPO_ROOT = Path(__file__).parent
# Mirrors events.py's DATA_ROOT resolution independently (this module
# deliberately doesn't import events.py — keeping these two small, easily
# duplicated constants out of an import dependency between otherwise
# unrelated modules) — see events.py's DATA_ROOT docstring for the full
# rationale. Defaults to REPO_ROOT, so existing deployments need no config.
DATA_ROOT = Path(os.environ.get("DATA_ROOT") or REPO_ROOT)
USERS_FILE = DATA_ROOT / "auth_users.json"

_users_lock = threading.Lock()

# 2-32 chars, must start with a letter — same shape as events.py's slug rule.
_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

ROLES = ("coach", "volunteer", "student")


@dataclass(frozen=True)
class User:
    username: str
    password_hash: str
    role: str                      # "coach" | "volunteer"
    events: tuple[str, ...] = ()   # ignored for coaches (implicit all-access)
    # Soft-delete flag. The web app never removes an account outright (see
    # disable_user/enable_user) — disabling just blocks login and kicks any
    # active session, fully reversible. Real removal is delete_user(), only
    # ever called from an operator CLI, never from review_app.py's routes.
    disabled: bool = False
    # Friendlier label shown in UI chrome (header greeting, user-management
    # table) instead of the bare username — purely cosmetic, never used as
    # an identifier anywhere data is keyed or audited (lastEditedBy etc.
    # keep storing `username`, which doesn't change if this does).
    display_name: str = ""

    def can_access(self, slug: str) -> bool:
        return self.role == "coach" or slug in self.events


def _load() -> dict[str, User]:
    # Reads and writes share the same lock (not just writes) — on Windows,
    # os.replace() targeting a file another thread currently has open for
    # read can fail with PermissionError. Matters more now that
    # create_users_bulk() (seasons.py's CSV import) calls create_user() —
    # i.e. _load()-then-_save() — in a tight loop.
    with _users_lock:
        if not USERS_FILE.exists():
            return {}
        try:
            data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    users: dict[str, User] = {}
    for username, d in data.items():
        try:
            users[username] = User(
                username=username,
                password_hash=d["password_hash"],
                role=d.get("role", "volunteer"),
                events=tuple(d.get("events") or ()),
                disabled=bool(d.get("disabled", False)),
                display_name=d.get("display_name", ""),
            )
        except Exception:
            continue
    return users


def _save(users: dict[str, User]) -> None:
    with _users_lock:
        data = {
            u.username: {
                "password_hash": u.password_hash,
                "role": u.role,
                "events": list(u.events),
                "disabled": u.disabled,
                "display_name": u.display_name,
            }
            for u in users.values()
        }
        # Atomic write: tempfile + os.replace, same as events.py's
        # _save_custom_events — a crash mid-write never corrupts the file.
        tmp = USERS_FILE.with_suffix(USERS_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, USERS_FILE)


def load_users() -> dict[str, User]:
    return _load()


def get_user(username: str) -> User | None:
    return _load().get((username or "").strip().lower())


def verify_login(username: str, password: str) -> User | None:
    user = get_user(username)
    if user is None or user.disabled or not check_password_hash(user.password_hash, password):
        return None
    return user


def user_can_access_event(user: User, slug: str) -> bool:
    return user.can_access(slug)


def is_student(user: User | None) -> bool:
    return user is not None and user.role == "student"


def create_user(
    username: str,
    password: str,
    role: str,
    events: list[str] | None = None,
) -> User:
    username = (username or "").strip().lower()
    if not _USERNAME_RE.match(username):
        raise ValueError(
            "username must start with a letter and contain only lowercase "
            "letters, digits, and underscores (2-32 chars)"
        )
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    users = _load()
    if username in users:
        raise ValueError(f"username {username!r} already exists")
    user = User(
        username=username,
        password_hash=generate_password_hash(password),
        role=role,
        events=tuple(events or ()),
    )
    users[username] = user
    _save(users)
    return user


def slugify_username(display_name: str) -> str:
    """Derive a candidate username from a free-text display name (which may
    contain spaces, mixed case, punctuation) for the Club Management CSV
    bulk-import — same character rules as _USERNAME_RE, but repaired rather
    than rejected: lowercase, strip everything outside [a-z0-9], prefix with
    "s" if the result would start with a digit, fall back to "student" if
    nothing alphanumeric survives, truncate to the 32-char limit."""
    slug = re.sub(r"[^a-z0-9]", "", (display_name or "").lower())
    if not slug:
        slug = "student"
    if not slug[0].isalpha():
        slug = "s" + slug
    return slug[:32]


def generate_unique_username(display_name: str) -> str:
    """slugify_username(), then append 2, 3, ... until it doesn't collide
    with an existing account. Used by the CSV bulk-import when a row's
    username column is left blank."""
    base = slugify_username(display_name)
    existing = load_users()
    if base not in existing:
        return base
    n = 2
    while True:
        candidate = f"{base}{n}"[:32]
        if candidate not in existing:
            return candidate
        n += 1


def generate_password(school: str, season_id: str, username: str) -> str:
    """The literal "school+year+name" formula for an auto-generated student
    password (CSV bulk-import, blank password column). Uses the already-
    deduplicated username rather than the raw display name as the "name"
    component, so two same-named students never get the same password.
    `season_id+username` alone is already >= 8 chars even when `school`
    (the SCHOOL_NAME env var) is unset, satisfying create_user's minimum."""
    return f"{school}{season_id}{username}".lower()


def create_users_bulk(rows: list[dict], season_id: str = "") -> dict:
    """Bulk-create student accounts from parsed CSV rows (Club Management's
    CSV upload). Each row: {"display_name": str, "username": str (optional),
    "password": str (optional)}. A blank username/password is filled in via
    generate_unique_username()/generate_password() — `season_id` is the
    season the upload is happening into (used only as the "year" component
    of a generated password; auth.py has no concept of seasons otherwise,
    deliberately, per the module docstring). Continues past a per-row
    failure (e.g. an explicit duplicate username) rather than aborting the
    whole batch, so one bad row in a large CSV doesn't block the rest —
    mirrors this app's general "report partial failure, don't discard
    partial success" stance (e.g. publish()'s skipped-question list
    elsewhere in this codebase).

    Returns {"created": [{"username","password","display_name"}],
             "errors": [{"row": int, "reason": str}]} — `password` is the
    plaintext value only for rows where one was just generated (never the
    hash), shown once to the coach in the upload results table; an
    explicitly-supplied password is echoed back too so the coach can
    confirm what they typed, matching the same one-time-display courtesy.
    """
    school = os.environ.get("SCHOOL_NAME", "")
    created: list[dict] = []
    errors: list[dict] = []
    for i, row in enumerate(rows):
        display_name = (row.get("display_name") or "").strip()
        if not display_name:
            errors.append({"row": i, "reason": "display_name is required"})
            continue
        username = (row.get("username") or "").strip().lower()
        if not username:
            username = generate_unique_username(display_name)
        password = (row.get("password") or "").strip()
        if not password:
            password = generate_password(school, season_id, username)
        try:
            create_user(username, password, role="student")
        except ValueError as e:
            errors.append({"row": i, "reason": str(e)})
            continue
        # "row" lets callers (e.g. review_app.py's CSV-upload route) map a
        # created account back to that row's other columns (e.g. an
        # "events" column this function doesn't know about) without relying
        # on positional alignment with the input list, which would break as
        # soon as any earlier row fails and goes to `errors` instead.
        created.append({"row": i, "username": username, "password": password, "display_name": display_name})
    return {"created": created, "errors": errors}


def update_user(
    username: str,
    role: str | None = None,
    events: list[str] | None = None,
    disabled: bool | None = None,
) -> User:
    users = _load()
    existing = users.get(username)
    if existing is None:
        raise ValueError(f"unknown user {username!r}")
    updated = User(
        username=existing.username,
        password_hash=existing.password_hash,
        role=role if role is not None else existing.role,
        events=tuple(events) if events is not None else existing.events,
        disabled=disabled if disabled is not None else existing.disabled,
        display_name=existing.display_name,
    )
    users[username] = updated
    _save(users)
    return updated


class WrongPasswordError(ValueError):
    """Raised by change_own_password() when current_password doesn't match
    — kept distinct from the generic ValueError used for a malformed new
    password so callers (the route) can tell "you typed your own current
    password wrong" apart from "your new password is too short" and respond
    with the right HTTP status (403 vs 400) for each."""


def change_own_password(username: str, current_password: str, new_password: str) -> User:
    """Self-service password change — requires the caller to already know
    their current password (re-checked here via check_password_hash, the
    same function verify_login() uses), mirroring this codebase's existing
    password-reconfirmation pattern for sensitive self-service actions
    (admin_app.py's Update/Restart/Rollback/Set-threads gate). Unlike
    update_user() (coach-only, never touches password_hash by design), this
    is the one path that can change a password outside the CLI."""
    users = _load()
    existing = users.get(username)
    if existing is None:
        raise ValueError(f"unknown user {username!r}")
    if not check_password_hash(existing.password_hash, current_password):
        raise WrongPasswordError("current password is incorrect")
    if not new_password or len(new_password) < 8:
        raise ValueError("new password must be at least 8 characters")
    updated = User(
        username=existing.username,
        password_hash=generate_password_hash(new_password),
        role=existing.role,
        events=existing.events,
        disabled=existing.disabled,
        display_name=existing.display_name,
    )
    users[username] = updated
    _save(users)
    return updated


def set_display_name(username: str, display_name: str) -> User:
    """Self-service display-name change — no current-password check, since
    this is a cosmetic UI label, not a security-sensitive field (see
    User.display_name's docstring)."""
    users = _load()
    existing = users.get(username)
    if existing is None:
        raise ValueError(f"unknown user {username!r}")
    updated = User(
        username=existing.username,
        password_hash=existing.password_hash,
        role=existing.role,
        events=existing.events,
        disabled=existing.disabled,
        display_name=(display_name or "").strip()[:80],
    )
    users[username] = updated
    _save(users)
    return updated


def disable_user(username: str) -> User:
    """Block login and force re-auth on any active session (see
    review_app.py's before_request) without touching the account or any
    event data. Reversible via enable_user."""
    return update_user(username, disabled=True)


def enable_user(username: str) -> User:
    """Reverse disable_user."""
    return update_user(username, disabled=False)


def delete_user(username: str) -> None:
    """Permanently remove an account from auth_users.json. Only ever call
    this from an operator CLI/script run directly on the server — the web
    app uses disable_user/enable_user instead, never this."""
    users = _load()
    if username not in users:
        raise ValueError(f"unknown user {username!r}")
    del users[username]
    _save(users)


def bootstrap_first_coach() -> None:
    """CLI helper: `python auth.py --create-coach`.

    Lets the very first account be created without needing to already be
    logged in (a fresh `auth_users.json` has no coach who could otherwise
    use the in-app admin UI to create one).
    """
    if _load():
        print("auth_users.json already has accounts — refusing to bootstrap again.")
        print("Log in as an existing coach and use the admin UI instead.")
        return
    username = input("Coach username: ").strip().lower()
    password = getpass.getpass("Coach password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords don't match.")
        return
    try:
        create_user(username, password, role="coach")
    except ValueError as e:
        print(f"Error: {e}")
        return
    print(f"Created coach account {username!r}.")


if __name__ == "__main__":
    if "--create-coach" in sys.argv:
        bootstrap_first_coach()
    else:
        print("Usage: python auth.py --create-coach")
