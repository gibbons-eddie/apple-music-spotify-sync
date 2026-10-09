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

Commands posted before a pending track was dropped from its playlist (see
review_state.py) are ignored. Idempotent: re-applying the same comments on
every run is a no-op once they have taken effect.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import review_state

PROJECT_DIR = Path(__file__).parent
CACHE_FILE = PROJECT_DIR / "cache" / "track_mapping.json"
ISSUE_LABEL = "auto-sync-review"
TRUSTED_ASSOCIATION = "OWNER"

# Match anywhere in a comment body. Tolerant of surrounding markdown/prose;
# an explicit `/override` or `/accept` prefix is the guard against accidental hits.
COMMAND_RE = re.compile(
    r"/override\s+`?([^\s`]+)`?\s+`?(spotify:track:[A-Za-z0-9]+)"
    r"|/accept\s+`?([^\s`]+)"
)


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


def _fetch_review_comments(repo: str) -> list[dict]:
    """All comments on all open review issues, oldest first."""
    issues = json.loads(_gh(
        "issue", "list",
        "--repo", repo,
        "--label", ISSUE_LABEL,
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


def _parse_commands(comments: list[dict]) -> list[dict]:
    """Commands from trusted comments, in posting order."""
    commands: list[dict] = []
    for c in comments:
        body = c.get("body", "")
        if not COMMAND_RE.search(body):
            continue
        if c.get("authorAssociation") != TRUSTED_ASSOCIATION:
            author = (c.get("author") or {}).get("login", "?")
            print(f"  ignoring command from @{author} on #{c['issue']} (not the repo owner)")
            continue
        for m in COMMAND_RE.finditer(body):
            if m.group(1):
                cmd = {"kind": "override", "apple_id": m.group(1), "uri": m.group(2)}
            else:
                cmd = {"kind": "accept", "apple_id": m.group(3)}
            commands.append({**cmd, "created_at": c["createdAt"], "issue": c["issue"]})
    return commands


def apply_commands(
    commands: list[dict], cache: dict, pending: dict, dropped: dict,
) -> tuple[int, int]:
    """Mutate cache/pending in place. Returns (cache changes, pending changes)."""
    cache_changed = pending_changed = 0
    for cmd in commands:
        apple_id = cmd["apple_id"]
        posted = review_state.parse_ts(cmd["created_at"])
        where = f"#{cmd['issue']}"

        if apple_id in dropped and posted <= review_state.parse_ts(dropped[apple_id]):
            print(f"  skip {cmd['kind']} {apple_id} ({where}): posted before the track was dropped")
            continue

        if cmd["kind"] == "override":
            if cache.get(apple_id) != cmd["uri"]:
                print(f"  override: {apple_id} → {cmd['uri']} ({where})")
                cache[apple_id] = cmd["uri"]
                cache_changed += 1
            if pending.pop(apple_id, None):
                print(f"  pending cleared by override: {apple_id}")
                pending_changed += 1
            continue

        entry = pending.get(apple_id)
        if not entry:
            continue
        if posted <= review_state.parse_ts(entry["picked_at"]):
            print(f"  skip accept {apple_id} ({where}): posted before the current pick was made")
            continue
        del pending[apple_id]
        print(f"  accept: {apple_id} keeps {entry['picked_uri']} ({where})")
        pending_changed += 1
    return cache_changed, pending_changed


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print("GITHUB_REPOSITORY not set; skipping override pass.")
        return 0

    commands = _parse_commands(_fetch_review_comments(repo))
    if not commands:
        print("No /override or /accept commands found.")
        return 0

    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    pending = review_state.load_pending()
    dropped = review_state.load_dropped()
    cache_changed, pending_changed = apply_commands(commands, cache, pending, dropped)

    if cache_changed:
        CACHE_FILE.parent.mkdir(exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True))
    if pending_changed:
        review_state.save_pending(pending)
    print(
        f"Found {len(commands)} command(s): {cache_changed} new cache change(s), "
        f"{pending_changed} pending entr{'y' if pending_changed == 1 else 'ies'} resolved."
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
