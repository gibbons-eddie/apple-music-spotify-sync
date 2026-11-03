"""Apply `/override` and `/accept` comments from open review issues.

Runs before `sync.py` in the workflow. Reads (never writes) the comments on
every open `auto-sync-review` issue; closed issues are ignored. Only comments
by the repository owner count. Commands are applied oldest-first, so the
latest one wins:

    /override <apple_id> spotify:track:<track_id>
        The pick is wrong: map the track to this URI in
        `cache/track_mapping.json` and clear its pending-review entry.

    /accept <apple_id>
        The current pick is right: clear its pending-review entry and keep the
        cached URI. Only counts if posted after that pick was made.

With --mode import (the Spotify → Apple import workflow) the same rules apply
to open `auto-import-review` issues, with Spotify track IDs as the key:

    /override spotify:track:<track_id> apple:song:<song_id>
        Updates `cache/reverse_mapping.json`.

    /accept <spotify_track_id>

Commands posted before a pending track was dropped from its playlist (see
review_state.py) are ignored. Idempotent: re-applying the same comments on
every run is a no-op once they have taken effect.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import review_state

PROJECT_DIR = Path(__file__).parent
TRUSTED_ASSOCIATION = "OWNER"


@dataclass(frozen=True)
class Mode:
    """Where one workflow's review commands live and what they change."""
    label: str
    # Group 1/2: /override key and value; group 3: /accept key.
    command_re: re.Pattern
    picked_field: str
    load_cache: Callable[[], dict]
    save_cache: Callable[[dict], None]
    load_pending: Callable[[], dict]
    save_pending: Callable[[dict], None]
    load_dropped: Callable[[], dict]


SYNC_CACHE_FILE = PROJECT_DIR / "cache" / "track_mapping.json"


def _load_sync_cache() -> dict:
    return json.loads(SYNC_CACHE_FILE.read_text()) if SYNC_CACHE_FILE.exists() else {}


def _save_sync_cache(cache: dict):
    SYNC_CACHE_FILE.parent.mkdir(exist_ok=True)
    SYNC_CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True))


def _import_cache_io():
    # Imported lazily: only the import workflow needs the reverse cache.
    import spotify_to_apple
    return spotify_to_apple.load_reverse_cache, (
        lambda c: spotify_to_apple._save_json(spotify_to_apple.REVERSE_CACHE_FILE, c)
    )


# Match anywhere in a comment body. Tolerant of surrounding markdown/prose;
# an explicit `/override` or `/accept` prefix is the guard against accidental hits.
MODES = {
    # Weekly Apple → Spotify sync: /override <apple_id> spotify:track:<id>, /accept <apple_id>
    "sync": Mode(
        label="auto-sync-review",
        command_re=re.compile(
            r"/override\s+`?([^\s`]+)`?\s+`?(spotify:track:[A-Za-z0-9]+)"
            r"|/accept\s+`?([^\s`]+)"
        ),
        picked_field="picked_uri",
        load_cache=_load_sync_cache,
        save_cache=_save_sync_cache,
        load_pending=review_state.load_pending,
        save_pending=review_state.save_pending,
        load_dropped=review_state.load_dropped,
    ),
    # Spotify → Apple import: /override spotify:track:<id> apple:song:<id>, /accept <spotify track id>
    "import": Mode(
        label="auto-import-review",
        command_re=re.compile(
            r"/override\s+`?(?:spotify:track:)?([A-Za-z0-9]{22})`?\s+`?apple:song:(\d+)"
            r"|/accept\s+`?(?:spotify:track:)?([A-Za-z0-9]{22})"
        ),
        picked_field="picked_id",
        load_cache=lambda: _import_cache_io()[0](),
        save_cache=lambda c: _import_cache_io()[1](c),
        load_pending=review_state.load_import_pending,
        save_pending=review_state.save_import_pending,
        load_dropped=review_state.load_import_dropped,
    ),
}


def _gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"gh {' '.join(args)} failed (exit {result.returncode}):", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr,
        )
    return result.stdout.strip()


def _fetch_review_comments(repo: str, label: str) -> list[dict]:
    """All comments on all open review issues, oldest first."""
    issues = json.loads(_gh(
        "issue", "list",
        "--repo", repo,
        "--label", label,
        "--state", "open",
        "--json", "number",
        "--limit", "200",
    ) or "[]")
    comments: list[dict] = []
    for issue in issues:
        payload = json.loads(_gh(
            "issue", "view", str(issue["number"]),
            "--repo", repo,
            "--json", "comments",
        ))
        for c in payload.get("comments", []):
            comments.append({**c, "issue": issue["number"]})
    comments.sort(key=lambda c: c.get("createdAt", ""))
    return comments


def _parse_commands(comments: list[dict], mode: Mode = MODES["sync"]) -> list[dict]:
    """Commands from trusted comments, in posting order."""
    commands: list[dict] = []
    for c in comments:
        body = c.get("body", "")
        if not mode.command_re.search(body):
            continue
        if c.get("authorAssociation") != TRUSTED_ASSOCIATION:
            author = (c.get("author") or {}).get("login", "?")
            print(f"  ignoring command from @{author} on #{c['issue']} (not the repo owner)")
            continue
        for m in mode.command_re.finditer(body):
            if m.group(1):
                cmd = {"kind": "override", "key": m.group(1), "value": m.group(2)}
            else:
                cmd = {"kind": "accept", "key": m.group(3)}
            commands.append({**cmd, "created_at": c["createdAt"], "issue": c["issue"]})
    return commands


def apply_commands(
    commands: list[dict], cache: dict, pending: dict, dropped: dict,
    picked_field: str = "picked_uri",
) -> tuple[int, int]:
    """Mutate cache/pending in place. Returns (cache changes, pending changes)."""
    cache_changed = pending_changed = 0
    for cmd in commands:
        key = cmd["key"]
        posted = review_state.parse_ts(cmd["created_at"])
        where = f"#{cmd['issue']}"

        if key in dropped and posted <= review_state.parse_ts(dropped[key]):
            print(f"  skip {cmd['kind']} {key} ({where}): posted before the track was dropped")
            continue

        if cmd["kind"] == "override":
            if cache.get(key) != cmd["value"]:
                print(f"  override: {key} → {cmd['value']} ({where})")
                cache[key] = cmd["value"]
                cache_changed += 1
            if pending.pop(key, None):
                print(f"  pending cleared by override: {key}")
                pending_changed += 1
            continue

        entry = pending.get(key)
        if not entry:
            continue
        if posted <= review_state.parse_ts(entry["picked_at"]):
            print(f"  skip accept {key} ({where}): posted before the current pick was made")
            continue
        del pending[key]
        print(f"  accept: {key} keeps {entry[picked_field]} ({where})")
        pending_changed += 1
    return cache_changed, pending_changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply /override and /accept review comments")
    parser.add_argument("--mode", choices=sorted(MODES), default="sync",
                        help="sync: weekly Apple → Spotify reviews; import: Spotify → Apple imports")
    mode = MODES[parser.parse_args().mode]

    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print("GITHUB_REPOSITORY not set; skipping override pass.")
        return 0

    commands = _parse_commands(_fetch_review_comments(repo, mode.label), mode)
    if not commands:
        print("No /override or /accept commands found.")
        return 0

    cache = mode.load_cache()
    pending = mode.load_pending()
    cache_changed, pending_changed = apply_commands(
        commands, cache, pending, mode.load_dropped(), mode.picked_field,
    )

    if cache_changed:
        mode.save_cache(cache)
    if pending_changed:
        mode.save_pending(pending)
    print(
        f"Found {len(commands)} command(s): {cache_changed} new cache change(s), "
        f"{pending_changed} pending entr{'y' if pending_changed == 1 else 'ies'} resolved."
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
