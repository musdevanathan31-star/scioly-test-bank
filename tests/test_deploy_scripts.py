"""
Properties of the deployment scripts that are invisible until production.

These are greps, not executions — the scripts run as root against a
provisioned RHEL host and cannot be exercised here. That limits what can be
checked, but the things checked are the ones that have actually gone wrong:
a deploy that reports success while the running app never received something
it needed.

Run with: `python -m pytest tests/test_deploy_scripts.py -q`
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SERVING_VENV = "/opt/qbank/venv"


def _read(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def test_the_deploy_installs_requirements_into_the_serving_venv():
    """The one that bit us.

    update-from-github.sh runs pip against /opt/qbank-deploy/venv — the
    unprivileged validation venv — while gunicorn runs from
    /opt/qbank/venv. So a newly declared dependency was installed where the
    tests run and nowhere else, and the deploy still reported success. Every
    assessment export silently fell back to markdown because reportlab was
    never installed where the app could see it.
    """
    apply_sh = _read("_apply-update.sh")
    assert "pip" in apply_sh and "requirements.txt" in apply_sh, (
        "_apply-update.sh must install requirements into the venv the "
        "services actually run from — the validation venv is a different one")


def test_a_failed_dependency_install_stops_the_deploy():
    apply_sh = _read("_apply-update.sh")
    # Anchor on the pip invocation, not the first mention of the filename --
    # requirements.txt also appears in the code-sync allow-list above it.
    install = apply_sh[apply_sh.index("bin/pip"):]
    # Deploying code whose dependencies could not be installed is worse than
    # not deploying: the currently-running version is at least working.
    assert "exit 1" in install[:600], (
        "a failed requirements install must abort before anything is "
        "copied or restarted")


def test_every_service_runs_from_the_venv_the_deploy_updates():
    """If a unit ever pointed somewhere else, that instance would silently
    stop receiving dependency updates while still deploying code fine."""
    for unit in sorted(DEPLOY.glob("*.service")):
        text = unit.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("ExecStart="):
                assert SERVING_VENV in line, f"{unit.name}: {line}"


def test_optional_runtime_deps_are_declared():
    """reportlab and genanki back real features and are guarded at runtime,
    which made them easy to leave undeclared — and undeclared meant never
    installed on any server."""
    reqs = (DEPLOY.parent / "requirements.txt").read_text(encoding="utf-8")
    for pkg in ("reportlab", "genanki"):
        assert re.search(rf"^{pkg}\b", reqs, re.M), f"{pkg} missing"


def test_the_archive_is_excluded_from_restic_but_not_from_migration():
    """Deliberate asymmetry, and easy to "tidy up" wrongly: a second copy of
    the archive lives in Google Drive, so it stays out of the nightly S3
    snapshot — but a host move should rsync it locally rather than
    re-download 65GB."""
    bulk = _read("backup-bulk-data.sh")
    migrate = _read("migrate-data-root.sh")
    assert "tournament_archive" in bulk
    assert "tournament_archive" not in migrate.replace("#", "")


def test_the_archive_event_map_reaches_the_git_backup():
    """Hand-curated, tiny, and caught by neither mechanism by default:
    backup-bulk-data.sh iterates '*/' so it skips top-level files."""
    assert "archive_event_map.json" in _read("backup-extracted-data.sh")


def test_the_privileged_script_never_updates_itself():
    """The privilege boundary this whole split exists for.

    qbank-apply-update.sh runs as root under a NOPASSWD grant naming its
    exact path. If it refreshed itself from the fetched repo, anyone able to
    land a commit on the tracked branch would get root on the next deploy —
    which is precisely what keeping the privileged half a fixed, separate
    file prevents.
    """
    apply_sh = _read("_apply-update.sh")
    for line in apply_sh.splitlines():
        stripped = line.strip()
        # Comments and log/echo lines print the refresh command on purpose —
        # a human running it as root is the supported path. Only an actual
        # invocation would be the problem.
        if stripped.startswith(("#", "log ", "echo ")):
            continue
        if re.match(r"(cp|install|rsync)\b.*deploy/_apply-update\.sh", stripped):
            pytest.fail(f"self-update would break the privilege boundary: {stripped}")


def test_both_installed_scripts_warn_when_they_fall_behind():
    """provision-host.sh installs these two once and no deploy refreshes
    them, so a fix to deploy/*.sh reaches the instance directories while the
    copies that actually run stay as they were. Silent, and indistinguishable
    from the fix being applied."""
    for name in ("_apply-update.sh", "update-from-github.sh"):
        text = _read(name)
        assert "cmp -s" in text, f"{name} does not detect its own drift"
        assert "BASH_SOURCE" in text, f"{name} does not compare against itself"
