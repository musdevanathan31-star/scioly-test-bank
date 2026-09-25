#!/usr/bin/env python3
"""
Operator CLI for one instance's season setup: inspect it, reset it for a new
season, and stage a week of practice tests from question bundles.

Runs ON THE SERVER, against exactly one instance named in
deploy/instances.conf, as that instance's own system user (so every file it
writes keeps the ownership the running app expects). Every command is a dry
run unless given --apply; with --apply it first refuses to run while the
instance's service is active (the app's state locks are in-process only, so
a second writer could lose updates), then writes a backup of every JSON
state file before changing anything.

    # everything below is a dry run until --apply is added
    sudo -u qbank /opt/qbank/venv/bin/python /opt/qbank/app/season_admin.py \\
        --instance ncms inspect Week_01_all_events_2027.zip

    systemctl stop qbank.service
    sudo -u qbank ... --instance ncms reset --season 2027 \\
        --events-from Week_01_all_events_2027.zip --apply
    sudo -u qbank ... --instance ncms stage-week Week_01_all_events_2027.zip \\
        --season 2027 --label "Week 1" --date 2026-09-30 --start 12:00 --end 16:00 \\
        --tz America/New_York --go-live --apply
    systemctl start qbank.service

Commands
  inspect [BUNDLES]   events, question counts (PDF-extracted vs other), seasons,
                      rosters, windows, tests, responses; with BUNDLES, how each
                      bundle's event maps onto this instance's events.
  reset               clean the question bank and wipe every season (with its
                      windows, tests and responses), then create --season with
                      the events named by --events-from, registering any the
                      instance doesn't have. See reset() for exactly what goes.
  stage-week BUNDLES  import each event's bundle into that event's bank, then
                      create one window and, per event, keep all of the
                      bundle's questions, publish, and (--go-live) go live.

BUNDLES is a zip of per-event bundle zips (the practice-test generator's
weekly "all events" file), a single bundle zip, or a directory of them.
Re-running stage-week with the same files is safe: questions an earlier run
imported are recognised by the bundle's digest and reused, and tests that
are already published are left alone.

See spec.md "Season setup CLI" and HOWTO.md "Setting up a season from the
command line".
"""
from __future__ import annotations

import argparse
import contextlib
import getpass
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OTHER_GENERAL = "Other / General"


# ---------------------------------------------------------------------------
# Instance resolution (runs before any app module is imported: events.py and
# friends read DATA_ROOT at import time)
# ---------------------------------------------------------------------------

@dataclass
class Instance:
    name: str
    label: str
    app_dir: Path
    service: str
    user: str
    env_file: Path


def read_instances(conf: Path) -> dict[str, Instance]:
    out: dict[str, Instance] = {}
    for line in conf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # name:label:app_dir:service:user:port — split from both ends so a
        # path containing ":" (a Windows drive, in tests) stays whole.
        parts = line.split(":")
        name, label = parts[0], parts[1]
        service, user = parts[-3], parts[-2]
        app_dir = Path(":".join(parts[2:-3]))
        out[name] = Instance(name, label, app_dir, service, user, app_dir.parent / ".env")
    return out


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        env[k] = v
    return env


def service_active(service: str) -> bool | None:
    """True/False from systemctl, None when systemctl isn't available."""
    if shutil.which("systemctl") is None:
        return None
    r = subprocess.run(["systemctl", "is-active", "--quiet", service])
    return r.returncode == 0


# ---------------------------------------------------------------------------
# Bundles
# ---------------------------------------------------------------------------

@dataclass
class EventBundle:
    event_name: str
    path: Path
    bundle: object                       # bundle_import.Bundle
    topics: list[str] = field(default_factory=list)


def load_bundles(src: Path, workdir: Path) -> list[EventBundle]:
    import bundle_import
    paths: list[Path] = []
    if src.is_dir():
        paths = sorted(p for p in src.iterdir() if p.suffix.lower() == ".zip")
    else:
        with zipfile.ZipFile(src) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if any(Path(n).name == "manifest.json" for n in names):
                paths = [src]                                   # one bundle
            else:
                for n in names:
                    if n.lower().endswith(".zip") and not n.startswith("__MACOSX/"):
                        dest = workdir / Path(n).name
                        dest.write_bytes(zf.read(n))
                        paths.append(dest)
    out = []
    for p in sorted(paths):
        b = bundle_import.open_bundle(p)
        name = str(b.manifest.get("event") or "").strip()
        if not name:
            raise SystemExit(f"{p.name}: manifest has no \"event\" name")
        topics = list(dict.fromkeys(str(q.get("topic") or "").strip()
                                    for q in b.manifest.get("questions") or []
                                    if isinstance(q, dict) and str(q.get("topic") or "").strip()))
        out.append(EventBundle(name, p, b, topics))
    if not out:
        raise SystemExit(f"no bundles found in {src}")
    return out


def _norm(s: str) -> str:
    s = str(s or "").lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]", "", s)


def _norm_loose(s: str) -> str:
    """Also drops the word "and": "Anatomy and Physiology" == slug anatomy_physiology."""
    s = str(s or "").lower().replace("&", " ")
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\band\b", " ", s))


def map_events(bundles: list[EventBundle]) -> list[tuple[EventBundle, str, bool]]:
    """[(bundle, slug, exists)] — matched by name/slug/event_match, ignoring
    case, punctuation and the word "and"; unmatched ones get a new slug."""
    import events as events_mod
    exact: dict[str, str] = {}
    loose: dict[str, str] = {}
    for slug, ev in events_mod.EVENTS.items():
        for key in [ev.name, slug, *ev.event_match]:
            exact.setdefault(_norm(key), slug)
            loose.setdefault(_norm_loose(key), slug)
    out = []
    for b in bundles:
        slug = exact.get(_norm(b.event_name)) or loose.get(_norm_loose(b.event_name))
        if slug:
            out.append((b, slug, True))
        else:
            new = re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", b.event_name.lower())).strip("_")
            if not re.match(r"^[a-z]", new):
                new = "e_" + new
            out.append((b, new, False))
    return out


# ---------------------------------------------------------------------------
# Safety rails
# ---------------------------------------------------------------------------

def backup_state(data_root: Path, tag: str) -> Path:
    """Tar every JSON state file (root-level JSON, each event's state/jobs
    index, custom events, response directories) — not PDFs or images,
    which none of these commands touch."""
    dest_dir = data_root / ".season_admin_backups"
    dest_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = dest_dir / f"{stamp}-{tag}.tar.gz"
    with tarfile.open(dest, "w:gz") as tar:
        for p in sorted(data_root.glob("*.json")):
            tar.add(p, arcname=p.name)
        for p in sorted(data_root.glob("*/.qbank_state.json")):
            tar.add(p, arcname=str(p.relative_to(data_root)))
        for d in ("assessment_responses", "test_responses"):
            if (data_root / d).is_dir():
                tar.add(data_root / d, arcname=d)
    return dest


def say(msg: str = "") -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _bank_counts(slug: str) -> dict:
    import build_question_bank as bqb
    bqb.set_event(slug)
    if not bqb.EVENT.state_file.exists():
        return {"pdf_buckets": 0, "pdf_questions": 0, "other_questions": 0}
    st = bqb._load_state()
    qs = st.get("questions") or {}
    pdf = {k: v for k, v in qs.items() if not k.startswith("_")}
    return {"pdf_buckets": len(pdf), "pdf_questions": sum(len(v or []) for v in pdf.values()),
            "other_questions": sum(len(v or []) for k, v in qs.items() if k.startswith("_"))}


def cmd_inspect(args, data_root: Path) -> None:
    import events as events_mod
    import seasons as seasons_mod
    import assessments as assessments_mod
    say(f"DATA_ROOT: {data_root}")
    say("\nEvents:")
    for slug, ev in sorted(events_mod.EVENTS.items()):
        c = _bank_counts(slug)
        flags = ",".join(f for f, on in (("builtin", events_mod.is_builtin(slug)), ("archived", ev.archived),
                                         ("build", ev.has_build)) if on)
        say(f"  {slug:28} {ev.name[:30]:30} {flags:18} PDF: {c['pdf_questions']:5} in {c['pdf_buckets']:3} PDF(s)"
            f"   other: {c['other_questions']:5}")
    seasons = seasons_mod.load_seasons()
    windows = assessments_mod.load_windows()
    tests = assessments_mod.load_assessments()
    say(f"\nSeasons: {len(seasons)}")
    for sid, s in seasons.items():
        rostered = {slug: len(seasons_mod.get_roster(sid, slug)) for slug in s.event_slugs}
        say(f"  {sid!r} current={s.is_current} events={len(s.event_slugs)} "
            f"rostered students per event: {rostered}")
    responses = sum(len(assessments_mod.get_responses_for_assessment(a)) for a in tests)
    say(f"Windows: {len(windows)}   Tests: {len(tests)}   Student responses: {responses}")
    if args.bundles:
        with tempfile.TemporaryDirectory() as tmp:
            bundles = load_bundles(Path(args.bundles), Path(tmp))
            _print_mapping(map_events(bundles))


def _print_mapping(mapping) -> None:
    say(f"\nBundle → event ({len(mapping)}):")
    for b, slug, exists in mapping:
        n = len(b.bundle.manifest.get("questions") or [])
        say(f"  {b.event_name[:32]:32} → {slug:28} {'' if exists else '(NEW — will be registered)'}  {n} questions")


def _clean_bank(keep_general: bool, apply: bool) -> dict:
    """Per event: drop every PDF-extracted question (buckets not starting
    with "_") with its annotations; in the synthetic buckets keep only
    questions with a gradeable answer and a topic. PDFs, images and the
    vision cache are left alone."""
    import events as events_mod
    import build_question_bank as bqb
    totals = {"pdf_removed": 0, "incomplete_removed": 0, "kept": 0}
    for slug in sorted(events_mod.EVENTS):
        bqb.set_event(slug)
        if not bqb.EVENT.state_file.exists():
            continue
        ctx = bqb._state_transaction() if apply else contextlib.nullcontext(bqb._load_state())
        with ctx as st:
            qs = st.setdefault("questions", {})
            pdf_keys = [k for k in qs if not k.startswith("_")]
            pdf_n = sum(len(qs[k] or []) for k in pdf_keys)
            dropped, kept = 0, 0
            reasons: dict[str, int] = {}
            for bucket in [k for k in qs if k.startswith("_")]:
                keep = []
                for q in qs[bucket] or []:
                    ok, why = bqb.question_gradeability(q)
                    topic = str(q.get("topic") or "").strip()
                    if not ok:
                        reasons[why] = reasons.get(why, 0) + 1
                    elif not topic or (topic == OTHER_GENERAL and not keep_general):
                        ok, why = False, "no topic (Other / General)"
                        reasons[why] = reasons.get(why, 0) + 1
                    if ok:
                        keep.append(q)
                dropped += len(qs[bucket] or []) - len(keep)
                kept += len(keep)
                if apply:
                    qs[bucket] = keep
            if apply:
                for k in pdf_keys:
                    qs.pop(k, None)
                    (st.get("annotations") or {}).pop(k, None)
                    (st.get("manual") or {}).pop(k, None)
        if pdf_n or dropped or kept:
            why = "; ".join(f"{n} {r}" for r, n in sorted(reasons.items())) or "-"
            say(f"  {slug:28} PDF-extracted removed: {pdf_n:5}   incomplete removed: {dropped:4} ({why})   kept: {kept}")
        totals["pdf_removed"] += pdf_n
        totals["incomplete_removed"] += dropped
        totals["kept"] += kept
    return totals


def cmd_reset(args, data_root: Path) -> None:
    import events as events_mod
    import seasons as seasons_mod
    import assessments as assessments_mod
    import deletion

    with tempfile.TemporaryDirectory() as tmp:
        bundles = load_bundles(Path(args.events_from), Path(tmp))
        mapping = map_events(bundles)
    _print_mapping(mapping)

    old = seasons_mod.load_seasons()
    carried: dict[str, list[str]] = {}
    if args.copy_rosters_from:
        if args.copy_rosters_from not in old:
            raise SystemExit(f"--copy-rosters-from: no season {args.copy_rosters_from!r} "
                             f"(have: {', '.join(old) or 'none'})")
        src = old[args.copy_rosters_from]
        carried = {slug: seasons_mod.get_roster(src.season_id, slug) for slug in src.event_slugs}

    if args.apply:
        say(f"\nBackup: {backup_state(data_root, 'reset')}")

    say(f"\nQuestion bank ({'cleaning' if args.apply else 'would clean'}):")
    totals = _clean_bank(args.keep_general, args.apply)
    say(f"  total: {totals['pdf_removed']} PDF-extracted and {totals['incomplete_removed']} incomplete "
        f"question(s) {'removed' if args.apply else 'to remove'}; {totals['kept']} kept")

    windows = assessments_mod.load_windows()
    tests = assessments_mod.load_assessments()
    responses = sum(len(assessments_mod.get_responses_for_assessment(a)) for a in tests)
    say(f"\nSeasons {'deleted' if args.apply else 'to delete'}: {', '.join(old) or 'none'} "
        f"({len(windows)} window(s), {len(tests)} test(s), {responses} student response(s))")
    if args.apply:
        for sid in list(old):
            deletion.delete_season(sid)
        for wid in list(assessments_mod.load_windows()):          # windows outside any season
            deletion.delete_assessment_window(wid)
        for aid in list(assessments_mod.load_assessments()):      # tests outside any window
            deletion.delete_assessment(aid)

    for b, slug, exists in mapping:
        ev = events_mod.EVENTS.get(slug)
        if not exists:
            say(f"  register event {slug} ({b.event_name}) with {len(b.topics)} topic(s)")
            if args.apply:
                events_mod.add_custom_event(slug, b.event_name, event_match=[b.event_name], topics=b.topics)
        elif ev is not None and ev.archived:
            say(f"  unarchive event {slug}")
            if args.apply:
                events_mod.unarchive_custom_event(slug)

    slugs = list(dict.fromkeys(slug for _b, slug, _e in mapping))
    say(f"\nSeason {args.season!r}: {'created' if args.apply else 'would be created'} with {len(slugs)} event(s)")
    if args.apply:
        seasons_mod.create_season(args.season, label=args.season, event_slugs=slugs, created_by="season_admin")
    for slug in slugs:
        if carried.get(slug):
            say(f"  roster {slug}: {len(carried[slug])} student(s) copied from {args.copy_rosters_from}")
            if args.apply:
                seasons_mod.set_roster(args.season, slug, carried[slug])
    empty = [s for s in slugs if not carried.get(s)]
    if empty:
        say(f"  NOTE: {len(empty)} event(s) have no students rostered for {args.season} yet — "
            f"add them on the Club page or students won't see these tests: {', '.join(empty)}")
    say("\n" + ("Done." if args.apply else "Dry run — nothing changed. Add --apply to do it."))


def _window_times(args) -> tuple[str, str]:
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(args.tz)
    opens = datetime.fromisoformat(f"{args.date}T{args.start}").replace(tzinfo=tz)
    closes = datetime.fromisoformat(f"{args.date}T{args.end}").replace(tzinfo=tz)
    if closes <= opens:
        raise SystemExit("--end must be after --start")
    return (opens.astimezone(timezone.utc).isoformat(timespec="seconds"),
            closes.astimezone(timezone.utc).isoformat(timespec="seconds"))


def cmd_stage_week(args, data_root: Path) -> None:
    import events as events_mod
    import seasons as seasons_mod
    import assessments as assessments_mod
    import build_question_bank as bqb
    import bundle_import

    season = seasons_mod.get_season(args.season)
    if season is None:
        raise SystemExit(f"no season {args.season!r} — run `reset` first")
    opens_at, closes_at = _window_times(args)
    say(f"Window {args.label!r}: {args.date} {args.start}–{args.end} {args.tz} "
        f"= {opens_at} → {closes_at} (UTC)")
    # Checked before anything is written, so a mistyped time never leaves a
    # half-done run behind.
    same_label = [w for w in assessments_mod.windows_for_season(args.season) if w.label == args.label]
    existing_window = same_label[0] if same_label else None
    if existing_window is not None and \
            (existing_window.opens_at, existing_window.closes_at) != (opens_at, closes_at):
        raise SystemExit(f"a window labelled {args.label!r} already exists with different times "
                         f"({existing_window.opens_at} → {existing_window.closes_at}); use another --label")

    with tempfile.TemporaryDirectory() as tmp:
        bundles = load_bundles(Path(args.bundles), Path(tmp))
        mapping = map_events(bundles)
        _print_mapping(mapping)
        missing = [(b, s) for b, s, e in mapping if not e]
        if missing and not args.register_missing:
            raise SystemExit("unknown event(s): " + ", ".join(b.event_name for b, _ in missing)
                             + " — run `reset`, or pass --register-missing")
        if args.apply:
            say(f"\nBackup: {backup_state(data_root, 'stage-week')}")
            for b, slug in missing:
                events_mod.add_custom_event(slug, b.event_name, event_match=[b.event_name], topics=b.topics)
        slugs = list(dict.fromkeys(slug for _b, slug, _e in mapping))
        add_to_season = [s for s in slugs if s not in season.event_slugs]
        if add_to_season:
            say(f"  add to season {args.season}: {', '.join(add_to_season)}")
            if args.apply:
                seasons_mod.update_season_events(args.season, list(season.event_slugs) + add_to_season)

        say(f"\nImport ({'importing' if args.apply else 'would import'}):")
        kept_by_slug: dict[str, list[dict]] = {}
        for b, slug, _e in mapping:
            topics_added = events_mod.extend_event_topics(slug, b.topics) if args.apply else \
                [t for t in b.topics if slug in events_mod.EVENTS and not events_mod.is_builtin(slug)
                 and t not in events_mod.EVENTS[slug].topics]
            bqb.set_event(slug)
            bucket = f"_generated_{slug}.pdf"
            if args.apply:
                with contextlib.redirect_stdout(io.StringIO()):
                    res = bundle_import.run_import(
                        b.bundle, filename=b.path.name, bucket=bucket,
                        mark_validated=not args.no_validate, next_number=bqb.next_global_q_number,
                        should_cancel=lambda: False, on_progress=lambda **kw: None,
                        dedup=False, keep_topics=True)
                refs = res["bundle_questions"]
                added, reused = res["added"], len(res.get("already_imported") or [])
                problems = res["invalid"]
                issues = res["issues"]
            else:
                names = {r: None for r in bundle_import._all_image_refs(b.bundle.manifest)}
                plan = bundle_import._plan(b.bundle, bqb._load_state(), b.path.name, names,
                                           dedup=False, keep_topics=True)
                refs = plan["already_imported"] + [{"id": q["import_meta"]["id"]} for q in plan["accepted"]]
                added, reused = len(plan["accepted"]), len(plan["already_imported"])
                problems, issues = plan["invalid"], plan["issues"]
            order = {str(q.get("id")): i for i, q in enumerate(b.bundle.manifest.get("questions") or [])}
            refs = sorted(refs, key=lambda r: order.get(str(r.get("id")), 10**6))
            kept_by_slug[slug] = [{"bucket": r.get("bucket", bucket), "number": r.get("number"),
                                   "max_points": 1.0} for r in refs]
            extra = f"  new topics: {len(topics_added)}" if topics_added else ""
            say(f"  {slug:28} {added:3} new, {reused:3} already imported, {len(problems)} invalid, "
                f"{len(issues)} with notes{extra}")
            for p in problems:
                say(f"      ! {p['id']}: {p['reason']}")

    if existing_window is not None:
        window = existing_window
        say(f"\nWindow {args.label!r} already exists — reusing it")
        if args.apply:
            missing_slugs = [s for s in slugs if s not in window.event_slugs]
            if missing_slugs:
                assessments_mod.update_window(window.window_id,
                                              event_slugs=list(window.event_slugs) + missing_slugs)
    else:
        say(f"\nWindow {args.label!r}: {'created' if args.apply else 'would be created'}")
        window = assessments_mod.create_window(args.season, opens_at, closes_at, slugs, label=args.label,
                                               created_by="season_admin") if args.apply else None

    say(f"\nTests ({'staging' if args.apply else 'would stage'}; "
        f"{'publish + go live' if args.go_live else 'publish only'}):")
    for slug in slugs:
        kept = kept_by_slug.get(slug) or []
        roster = len(seasons_mod.get_roster(args.season, slug))
        note = "" if roster else "   (no students rostered yet)"
        if not args.apply:
            say(f"  {slug:28} {len(kept):3} question(s){note}")
            continue
        a = assessments_mod.get_assessment_for(window.window_id, slug, "exam")
        if a.status != "preparing":
            say(f"  {slug:28} already {a.status} — left alone{note}")
            continue
        assessments_mod.update_assessment_kept(a.assessment_id, kept, edited_by="season_admin")
        pub = assessments_mod.publish_assessment(a.assessment_id, published_by="season_admin")
        skipped = pub.get("skipped") or []
        status = "published"
        if args.go_live:
            assessments_mod.go_live_assessment(a.assessment_id, live_by="season_admin")
            status = "live"
        say(f"  {slug:28} {len(kept) - len(skipped):3} question(s), {status}"
            f"{f', {len(skipped)} skipped' if skipped else ''}{note}")
    say("\n" + ("Done." if args.apply else "Dry run — nothing changed. Add --apply to do it."))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--instance", help="instance name from deploy/instances.conf (e.g. ncms)")
    target.add_argument("--data-root", help="operate on this DATA_ROOT directly (development/testing)")
    ap.add_argument("--conf", default=str(HERE / "deploy" / "instances.conf"))
    ap.add_argument("--allow-running-service", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--allow-any-user", action="store_true", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("inspect")
    p.add_argument("bundles", nargs="?")

    p = sub.add_parser("reset")
    p.add_argument("--season", required=True)
    p.add_argument("--events-from", required=True, help="bundles whose events make up the season")
    p.add_argument("--copy-rosters-from", help="carry this old season's rosters into the new one")
    p.add_argument("--keep-general", action="store_true",
                   help="keep complete questions whose topic is 'Other / General'")
    p.add_argument("--apply", action="store_true")

    p = sub.add_parser("stage-week")
    p.add_argument("bundles")
    p.add_argument("--season", required=True)
    p.add_argument("--label", required=True, help='window label, e.g. "Week 1"')
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--start", required=True, help="HH:MM, local to --tz")
    p.add_argument("--end", required=True, help="HH:MM, local to --tz")
    p.add_argument("--tz", default="America/New_York")
    p.add_argument("--go-live", action="store_true")
    p.add_argument("--register-missing", action="store_true",
                   help="register events the instance doesn't have yet")
    p.add_argument("--no-validate", action="store_true",
                   help="don't mark imported questions validated")
    p.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    if args.instance:
        instances = read_instances(Path(args.conf))
        if args.instance not in instances:
            raise SystemExit(f"no instance {args.instance!r} in {args.conf} (have: {', '.join(instances)})")
        inst = instances[args.instance]
        if getpass.getuser() != inst.user and not args.allow_any_user:
            raise SystemExit(f"run this as the instance's user so file ownership stays right:\n"
                             f"  sudo -u {inst.user} {sys.executable} {' '.join(sys.argv)}")
        env = load_env_file(inst.env_file) if inst.env_file.exists() else {}
        os.environ.update(env)
        os.environ.setdefault("DATA_ROOT", str(inst.app_dir))
        say(f"Instance {inst.name} ({inst.label}) — service {inst.service}")
        if getattr(args, "apply", False) and not args.allow_running_service:
            active = service_active(inst.service)
            if active:
                raise SystemExit(f"{inst.service} is running. Stop it first (systemctl stop {inst.service}) "
                                 f"— the app's state locks only work inside one process.")
            if active is None:
                say("WARNING: systemctl not found — can't confirm the service is stopped")
    else:
        os.environ["DATA_ROOT"] = str(Path(args.data_root).resolve())

    sys.path.insert(0, str(HERE))
    import events as events_mod
    data_root = Path(events_mod.DATA_ROOT)
    {"inspect": cmd_inspect, "reset": cmd_reset, "stage-week": cmd_stage_week}[args.cmd](args, data_root)


if __name__ == "__main__":
    main()
