"""Merge `/override` comments from the current review issue into the cache.

Runs before `sync.py` in the workflow. Comments on the open
`auto-sync-review` issue that match:

    /override <apple_id> spotify:track:<track_id>

get merged into `cache/track_mapping.json`. Idempotent: re-runs don't touch
the cache if the mapping is already correct, so noisy weekly re-application
is a no-op.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
CACHE_FILE = PROJECT_DIR / "cache" / "track_mapping.json"
ISSUE_LABEL = "auto-sync-review"

# Match anywhere in a comment body. Tolerant of surrounding markdown/prose;
# an explicit `/override` prefix is the guard against accidental hits.
OVERRIDE_RE = re.compile(
    r"/override\s+(\S+)\s+(spotify:track:[A-Za-z0-9]+)"
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


def _fetch_review_comment_bodies(repo: str) -> list[str]:
    issues = json.loads(_gh(
        "issue", "list",
        "--repo", repo,
        "--label", ISSUE_LABEL,
        "--state", "open",
        "--json", "number",
        "--limit", "1",
    ) or "[]")
    if not issues:
        return []
    issue_num = str(issues[0]["number"])
    payload = json.loads(_gh(
        "issue", "view", issue_num,
        "--repo", repo,
        "--json", "comments",
    ))
    return [c.get("body", "") for c in payload.get("comments", [])]


def _parse_overrides(comment_bodies: list[str]) -> dict[str, str]:
    """Last-write-wins if the same apple_id shows up in multiple comments."""
    overrides: dict[str, str] = {}
    for body in comment_bodies:
        for m in OVERRIDE_RE.finditer(body):
            overrides[m.group(1)] = m.group(2)
    return overrides


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print("GITHUB_REPOSITORY not set; skipping override pass.")
        return 0

    comment_bodies = _fetch_review_comment_bodies(repo)
    overrides = _parse_overrides(comment_bodies)
    if not overrides:
        print("No /override commands found.")
        return 0

    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    changed = 0
    for apple_id, spotify_uri in overrides.items():
        if cache.get(apple_id) != spotify_uri:
            print(f"  override: {apple_id} → {spotify_uri}")
            cache[apple_id] = spotify_uri
            changed += 1

    if changed:
        CACHE_FILE.parent.mkdir(exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True))
        print(f"Applied {changed} new override(s) to {CACHE_FILE.name}")
    else:
        print(f"Found {len(overrides)} override(s); all already applied.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
