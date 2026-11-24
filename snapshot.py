#!/usr/bin/python3
"""Snapshot the TV schedule into guide.json and push when it changes."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Jerusalem")
REPO = Path(__file__).resolve().parent
GUIDE_PATH = REPO / "guide.json"
LOCK_PATH = Path.home() / ".cache/tvprog-guide/snapshot.lock"
CHANNEL_MIN = 100
CHANNEL_MAX = 198
EXCLUDED = {199, 900}
WINDOW_HOURS = 48
TIMEOUT = 30


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def tunarr_base() -> str:
    url = os.environ.get("TUNARR_URL", "").strip().rstrip("/")
    if not url:
        die("TUNARR_URL is not set")
    return url


def get_json(base: str, path: str):
    try:
        with urllib.request.urlopen(base + path, timeout=TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        die(f"request failed ({exc.code}) for {path}")
    except Exception as exc:
        die(f"request failed for {path}: {type(exc).__name__}")


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def iso_local(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=TZ).isoformat(timespec="seconds")


def acquire_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        die("another snapshot is already running", 0)
    return handle


def included(number) -> bool:
    try:
        value = int(number)
    except (TypeError, ValueError):
        return False
    if value in EXCLUDED:
        return False
    return CHANNEL_MIN <= value <= CHANNEL_MAX


def title_index(programs: list) -> dict:
    index = {}
    for item in programs:
        if item.get("type") != "content":
            continue
        program = item.get("program") if isinstance(item.get("program"), dict) else {}
        pid = item.get("id") or program.get("uuid") or program.get("id")
        if not pid:
            continue
        title = (program.get("title") or item.get("title") or "").strip() or "Untitled"
        kind = program.get("type") if program.get("type") in {"episode", "movie"} else "content"
        rec = {"title": title, "kind": kind}
        show = None
        if isinstance(program.get("show"), dict):
            show = (program["show"].get("title") or "").strip()
        if show and show.lower() != title.lower():
            rec["show"] = show
        year = program.get("year")
        if isinstance(year, int) and year > 0:
            rec["year"] = year
        index[pid] = rec
    return index


def from_guide_item(item: dict, titles: dict) -> dict | None:
    lineup = item.get("lineupItem") if isinstance(item.get("lineupItem"), dict) else {}
    start = item.get("startTimeMs")
    duration = lineup.get("durationMs")
    if not isinstance(start, (int, float)) or not isinstance(duration, (int, float)):
        return None
    start_ms = int(start)
    end_ms = int(start + duration)
    typ = lineup.get("type")
    if typ in {"offline", "flex"}:
        return {
            "title": "Off air",
            "kind": "off",
            "start_ms": start_ms,
            "end_ms": end_ms,
            "start": iso_local(start_ms),
            "end": iso_local(end_ms),
        }
    meta = titles.get(lineup.get("id")) or {}
    rec = {
        "title": meta.get("title") or "Untitled",
        "kind": meta.get("kind") or "content",
        "start_ms": start_ms,
        "end_ms": end_ms,
        "start": iso_local(start_ms),
        "end": iso_local(end_ms),
    }
    if meta.get("show"):
        rec["show"] = meta["show"]
    if meta.get("year"):
        rec["year"] = meta["year"]
    return rec


def overlaps(program: dict, start_ms: int, end_ms: int) -> bool:
    return program["end_ms"] > start_ms and program["start_ms"] < end_ms


def schedule_payload(data: dict) -> dict:
    # Compare only the lineup. The requested window slides with "now",
    # so including it would push a new commit every run.
    return {"timezone": data.get("timezone"), "channels": data.get("channels")}


def git(*args, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=REPO,
        check=check,
        text=True,
        capture_output=True,
    )


def maybe_push(payload: dict) -> str:
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    previous = None
    if GUIDE_PATH.exists():
        try:
            previous = json.loads(GUIDE_PATH.read_text())
        except json.JSONDecodeError:
            previous = None
    changed = previous is None or schedule_payload(previous) != schedule_payload(payload)
    if not changed:
        print("guide unchanged, not pushing")
        return "unchanged"
    GUIDE_PATH.write_text(text)
    if os.environ.get("SNAPSHOT_PUSH", "1") not in {"1", "true", "yes"}:
        print("wrote guide.json (push disabled)")
        return "written"
    git("add", "guide.json")
    staged = git("diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        print("guide.json already committed")
        return "unchanged"
    commit = git("-c", "commit.gpgsign=false", "commit", "-m", "Update guide snapshot", check=False)
    if commit.returncode != 0:
        die((commit.stderr or commit.stdout or "git commit failed").strip())
    pushed = git("push", "origin", "HEAD", check=False)
    if pushed.returncode != 0:
        die((pushed.stderr or pushed.stdout or "git push failed").strip())
    print("pushed guide snapshot")
    return "pushed"


def main() -> None:
    acquire_lock()
    base = tunarr_base()
    now = datetime.now(TZ)
    start = now
    end = now + timedelta(hours=WINDOW_HOURS)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    guide_q = (
        f"dateFrom={urllib.parse.quote(iso_utc(start))}"
        f"&dateTo={urllib.parse.quote(iso_utc(end))}"
    )
    lineup_q = (
        f"from={urllib.parse.quote(iso_utc(start))}"
        f"&to={urllib.parse.quote(iso_utc(end))}"
    )

    channels = get_json(base, "/api/channels")
    if not isinstance(channels, list):
        die("channels response was not a list")

    out = []
    for channel in sorted(
        (item for item in channels if isinstance(item, dict)),
        key=lambda item: int(item.get("number") or 0),
    ):
        number = channel.get("number")
        if not included(number):
            continue
        channel_id = channel.get("id")
        name = channel.get("name") or f"Channel {number}"
        if not channel_id:
            print(f"skipping {number}: no id", file=sys.stderr)
            continue
        guide = get_json(base, f"/api/guide/channels/{channel_id}?{guide_q}")
        lineup = get_json(base, f"/api/channels/{channel_id}/lineup?{lineup_q}")
        if not isinstance(guide, list):
            die(f"guide for {number} was not a list")
        programs = lineup.get("programs") if isinstance(lineup, dict) else []
        titles = title_index(programs if isinstance(programs, list) else [])
        items = []
        for entry in guide:
            rec = from_guide_item(entry, titles)
            if rec and overlaps(rec, start_ms, end_ms):
                items.append(rec)
        items.sort(key=lambda item: (item["start_ms"], item["end_ms"]))
        out.append({"number": int(number), "name": name, "programs": items})

    if not out:
        die("no channels in range 100-198")

    generated = datetime.now(TZ)
    payload = {
        "generated_at": generated.isoformat(timespec="seconds"),
        "generated_at_ms": int(generated.timestamp() * 1000),
        "timezone": "Asia/Jerusalem",
        "window": {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "start": iso_local(start_ms),
            "end": iso_local(end_ms),
        },
        "channels": out,
    }
    status = maybe_push(payload)
    total = sum(len(channel["programs"]) for channel in out)
    print(
        f"{status}: {len(out)} channels, {total} programs, "
        f"generated {payload['generated_at']}"
    )


if __name__ == "__main__":
    main()
