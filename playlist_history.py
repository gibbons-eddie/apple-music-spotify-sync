"""Synced-playlist history and revert alerts. Alert-only: never blocks a sync.

Motivation: in September both the Apple Music and the Spotify playlist went
back to a weeks-old version on their own, days after the last sync run. The
sync can't prevent that, but it can notice and leave a record behind.

cache/playlist_history.json keeps, per playlist, the last MAX_VERSIONS distinct
versions the sync wrote (Apple track list + the Spotify URIs it targeted):

    {"<playlist name>": [
        {"first_synced_at": ..., "last_synced_at": ..., "run": "142",
         "tracks": [{"apple_id": ..., "name": ..., "artist": ..., "uri": ...}]}
    ]}

Each run compares what it finds against that history:
  - apple_revert: the Apple playlist now closely matches an *older* recorded
    version rather than the latest one.
  - spotify_drift: the Spotify playlist no longer holds what the last run
    wrote, i.e. it changed outside the sync (flagged as a revert when it
    matches an older recorded version).
"""

import json
import os
from pathlib import Path

import review_state

HISTORY_FILE = Path(__file__).parent / "cache" / "playlist_history.json"
MAX_VERSIONS = 12
# Share of tracks two versions must have in common to count as "the same
# version" for revert matching (tolerates a track or two of noise).
REVERT_SIMILARITY = 0.9


def load() -> dict:
    return json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() else {}


def save(history: dict):
    HISTORY_FILE.parent.mkdir(exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))


def _similarity(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / max(len(a), len(b))


def _apple_ids(version: dict) -> set[str]:
    return {t["apple_id"] for t in version["tracks"] if t.get("apple_id")}


def _uris(version: dict) -> set[str]:
    return {t["uri"] for t in version["tracks"] if t.get("uri")}


def _best_older_match(versions: list[dict], current: set, key) -> tuple[dict | None, float]:
    """Closest version other than the latest, if it beats the latest."""
    latest_sim = _similarity(current, key(versions[-1]))
    best, best_sim = None, 0.0
    for v in versions[:-1]:
        sim = _similarity(current, key(v))
        if sim > best_sim:
            best, best_sim = v, sim
    if best and best_sim >= REVERT_SIMILARITY and best_sim > latest_sim:
        return best, best_sim
    return None, latest_sim


def _span(version: dict) -> str:
    first, last = version["first_synced_at"][:10], version["last_synced_at"][:10]
    return first if first == last else f"{first} to {last}"


def _label(names: dict, keys) -> list[dict]:
    return [names.get(k, {"name": "?", "artist": "?", "id": k}) for k in sorted(keys)]


def detect(
    history: dict,
    playlist_name: str,
    apple_tracks: list[dict],
    spotify_current: list[str],
) -> list[dict]:
    versions = history.get(playlist_name, [])
    if not versions:
        return []
    latest = versions[-1]
    alerts: list[dict] = []

    # Names for anything we've ever recorded, so alerts are readable.
    by_apple: dict[str, dict] = {}
    by_uri: dict[str, dict] = {}
    for v in versions:
        for t in v["tracks"]:
            label = {"name": t.get("name", "?"), "artist": t.get("artist", "?")}
            if t.get("apple_id"):
                by_apple[t["apple_id"]] = {**label, "id": t["apple_id"]}
            if t.get("uri"):
                by_uri[t["uri"]] = {**label, "id": t["uri"]}
    for t in apple_tracks:
        if t.get("apple_id"):
            by_apple[t["apple_id"]] = {"name": t.get("name", "?"), "artist": t.get("artist", "?"), "id": t["apple_id"]}

    apple_now = {t["apple_id"] for t in apple_tracks if t.get("apple_id")}
    if apple_now != _apple_ids(latest):
        match, sim = _best_older_match(versions, apple_now, _apple_ids)
        if match:
            alerts.append({
                "kind": "apple_revert",
                "summary": (
                    f"Apple Music playlist matches the version synced "
                    f"{_span(match)} "
                    f"({sim:.0%} of tracks), not the one synced last on "
                    f"{latest['last_synced_at'][:10]}. Possible upstream revert."
                ),
                "matched_version": {k: match[k] for k in ("first_synced_at", "last_synced_at", "run")},
                "last_version": {k: latest[k] for k in ("first_synced_at", "last_synced_at", "run")},
                "added": _label(by_apple, apple_now - _apple_ids(latest)),
                "removed": _label(by_apple, _apple_ids(latest) - apple_now),
            })

    spotify_now = set(spotify_current)
    if spotify_now != _uris(latest):
        match, sim = _best_older_match(versions, spotify_now, _uris)
        if match:
            summary = (
                f"Spotify playlist matches the version synced "
                f"{_span(match)} "
                f"({sim:.0%} of tracks), not what the last run wrote on "
                f"{latest['last_synced_at'][:10]}. Possible upstream revert."
            )
        else:
            summary = (
                f"Spotify playlist changed outside the sync since the last run "
                f"on {latest['last_synced_at'][:10]}."
            )
        alerts.append({
            "kind": "spotify_drift",
            "summary": summary,
            "matched_version": {k: match[k] for k in ("first_synced_at", "last_synced_at", "run")} if match else None,
            "last_version": {k: latest[k] for k in ("first_synced_at", "last_synced_at", "run")},
            "added": _label(by_uri, spotify_now - _uris(latest)),
            "removed": _label(by_uri, _uris(latest) - spotify_now),
        })
    return alerts


def record(
    history: dict,
    playlist_name: str,
    apple_tracks: list[dict],
    cache: dict,
    target_uris: list[str],
):
    """Append what this run synced, or refresh last_synced_at if unchanged.

    A track's uri is recorded only if it was actually written to Spotify, so a
    stale cache entry for an unmatched track can't fake drift next run.
    """
    written = set(target_uris)
    now = review_state.now_iso()
    run = os.environ.get("GITHUB_RUN_NUMBER", "")
    tracks = [
        {
            "apple_id": t.get("apple_id", ""),
            "name": t.get("name", ""),
            "artist": t.get("artist", ""),
            "uri": u if (u := cache.get(t.get("apple_id", ""), "")) in written else "",
        }
        for t in apple_tracks
    ]
    versions = history.setdefault(playlist_name, [])
    if versions and versions[-1]["tracks"] == tracks:
        versions[-1]["last_synced_at"] = now
        return
    versions.append({"first_synced_at": now, "last_synced_at": now, "run": run, "tracks": tracks})
    del versions[:-MAX_VERSIONS]
