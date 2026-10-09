"""Persistent review state shared by sync.py, apply_overrides.py and report_to_issue.py.

cache/pending_review.json — sub-threshold picks awaiting /accept or /override:

    {"<apple_id>": {"picked_uri": ..., "picked_score": 0.78, "picked_at": ...,
                    "name": ..., "artist": ..., "playlist": ...}}

cache/dropped_review.json — pending tracks that left their playlist before being
resolved, mapped to when they were dropped. Their cache mapping is deleted so a
re-add is matched from scratch, and any /accept or /override posted before the
drop no longer applies to them:

    {"<apple_id>": "<dropped_at>"}

Timestamps are UTC ISO-8601 so they compare cleanly with GitHub's createdAt.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "cache"
PENDING_FILE = CACHE_DIR / "pending_review.json"
DROPPED_FILE = CACHE_DIR / "dropped_review.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _save(path: Path, data: dict):
    CACHE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))


def load_pending() -> dict:
    return _load(PENDING_FILE)


def save_pending(pending: dict):
    _save(PENDING_FILE, pending)


def load_dropped() -> dict:
    return _load(DROPPED_FILE)


def save_dropped(dropped: dict):
    _save(DROPPED_FILE, dropped)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(ts: str) -> datetime:
    """Parse our timestamps and GitHub's `...Z` ones into aware UTC datetimes."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
