"""Sync driver: Apple Music → Spotify. See ARCHITECTURE.md."""

import sys
import json
import time
import logging
import argparse
from pathlib import Path
from datetime import datetime
from collections import Counter

import spotipy

import review_state
from apple_music import fetch_apple_playlist
from spotify_match import MATCH_THRESHOLD, build_client, find_spotify_track

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).parent
PLAYLISTS_FILE = PROJECT_DIR / "playlists.json"
CACHE_DIR = PROJECT_DIR / "cache"
CACHE_FILE = CACHE_DIR / "track_mapping.json"
REPORTS_DIR = PROJECT_DIR / "reports"
UNMATCHED_FILE = REPORTS_DIR / "unmatched.txt"
NEEDS_REVIEW_FILE = REPORTS_DIR / "needs_review.json"

SEARCH_DELAY_SECONDS = 0.15

# Large-removal guardrail: refuse to remove more than this share of the current
# Spotify playlist in one run (e.g. an upstream Apple Music revert), unless the
# removal is trivially small or --allow-large-removal is passed.
REMOVAL_RATIO_THRESHOLD = 0.15
REMOVAL_ABSOLUTE_FLOOR = 2


class LargeRemovalError(RuntimeError):
    pass


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {}


def _save_cache(cache: dict):
    CACHE_DIR.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True))


def _resolve_tracks(
    apple_tracks: list[dict],
    sp: spotipy.Spotify,
    cache: dict,
    pending: dict,
    playlist_name: str,
    force: bool = False,
) -> tuple[list[str], list[dict], list[dict], int]:
    uris: list[str] = []
    unmatched: list[dict] = []
    uncertain: list[dict] = []
    cache_hits = 0

    for idx, track in enumerate(apple_tracks):
        apple_id = track.get("apple_id", "")
        if not force and apple_id and apple_id in cache:
            uris.append(cache[apple_id])
            cache_hits += 1
            continue

        try:
            match = find_spotify_track(track, sp)
        except spotipy.SpotifyException as e:
            if e.http_status in (401, 403):
                # Credentials or app-registration problem — can't be per-track.
                # Fail fast so Spotify isn't modified based on a broken search.
                raise
            logger.warning("Search error for %s — %s: %s", track["name"], track["artist"], e)
            match = None

        if match:
            uris.append(match["uri"])
            if apple_id:
                cache[apple_id] = match["uri"]
            logger.info(
                "[%s %.2f] %s — %s",
                match["source"], match["score"], track["name"], track["artist"],
            )
            if apple_id:
                _update_pending(pending, apple_id, match, track, playlist_name)
            if match["score"] < MATCH_THRESHOLD:
                uncertain.append({
                    "apple": _apple_context(track, idx, apple_tracks),
                    "picked": _spotify_summary(match["track"], match["score"], match["source"]),
                    "alternates": [
                        _spotify_summary(a["track"], a["score"], "fuzzy")
                        for a in match.get("alternates", [])
                    ],
                })
        else:
            unmatched.append(_apple_context(track, idx, apple_tracks))
            logger.warning("UNMATCHED: %s — %s", track["name"], track["artist"])

        time.sleep(SEARCH_DELAY_SECONDS)

    return uris, unmatched, uncertain, cache_hits


def _update_pending(pending: dict, apple_id: str, match: dict, track: dict, playlist_name: str):
    """Keep pending_review.json in step with a freshly searched (non-cache) match.

    A sub-threshold pick stays pending until /accept or /override; a new
    confident match (e.g. from a --force re-match) clears it.
    """
    if match["score"] >= MATCH_THRESHOLD:
        if pending.pop(apple_id, None):
            logger.info("Pending review cleared by confident re-match: %s", track["name"])
        return
    existing = pending.get(apple_id)
    if existing and existing.get("picked_uri") == match["uri"]:
        return
    pending[apple_id] = {
        "picked_uri": match["uri"],
        "picked_score": round(match["score"], 3),
        "picked_at": review_state.now_iso(),
        "name": track.get("name", ""),
        "artist": track.get("artist", ""),
        "playlist": playlist_name,
    }


def _prune_pending(
    pending: dict,
    dropped: dict,
    cache: dict,
    playlist_name: str,
    apple_tracks: list[dict],
) -> list[str]:
    """Forget pending picks whose track has left this playlist.

    The cache mapping goes too, so if the track is re-added it is matched from
    scratch and surfaces as a brand-new review item. `dropped` records when,
    so stale /accept or /override comments can't resolve the new pick.
    """
    present = {t.get("apple_id") for t in apple_tracks}
    gone = [
        aid for aid, entry in pending.items()
        if entry.get("playlist") == playlist_name and aid not in present
    ]
    for aid in gone:
        entry = pending.pop(aid)
        cache.pop(aid, None)
        dropped[aid] = review_state.now_iso()
        logger.info(
            "Dropped pending review for %s — %s (no longer in %s)",
            entry.get("name", ""), entry.get("artist", ""), playlist_name,
        )
    return gone


def _apple_context(track: dict, idx: int, all_tracks: list[dict]) -> dict:
    def _short(t: dict) -> dict:
        return {"name": t.get("name", ""), "artist": t.get("artist", "")}

    return {
        "name": track.get("name", ""),
        "artist": track.get("artist", ""),
        "album": track.get("album", ""),
        "isrc": track.get("isrc", ""),
        "apple_id": track.get("apple_id", ""),
        "position": idx + 1,
        "total": len(all_tracks),
        "before": [_short(all_tracks[i]) for i in range(max(0, idx - 2), idx)],
        "after": [_short(all_tracks[i]) for i in range(idx + 1, min(len(all_tracks), idx + 3))],
    }


def _spotify_summary(spotify_track: dict, score: float, source: str) -> dict:
    album = spotify_track.get("album", {})
    return {
        "uri": spotify_track.get("uri", ""),
        "name": spotify_track.get("name", ""),
        "artist": ", ".join(a.get("name", "") for a in spotify_track.get("artists", [])),
        "album": album.get("name", ""),
        "album_type": album.get("album_type", ""),
        "release_date": album.get("release_date", ""),
        "score": round(score, 3),
        "source": source,
    }


def _get_current_playlist_uris(sp: spotipy.Spotify, playlist_id: str) -> list[str]:
    info = sp.playlist(playlist_id)
    name = info.get("name", "?")

    uris: list[str] = []
    expected = None
    offset = 0
    while True:
        page = sp.playlist_items(playlist_id, offset=offset, limit=100)
        if expected is None:
            expected = page.get("total", 0)
        items = page.get("items", [])
        if not items:
            break
        for entry in items:
            # Spotify API returns the track under "item" (was "track" in older responses).
            track = entry.get("item") or entry.get("track")
            if track and track.get("uri"):
                uris.append(track["uri"])
        if not page.get("next"):
            break
        offset += 100

    logger.info("Spotify playlist: %s (Spotify reports %d tracks)", name, expected or 0)
    if expected and len(uris) != expected:
        logger.warning(
            "Read mismatch: Spotify reports %d total but we read %d URIs (likely local tracks or unavailable items)",
            expected, len(uris),
        )
    return uris


def _plan_reorder(current: list[str], target: list[str]) -> list[tuple[int, int]]:
    moves: list[tuple[int, int]] = []
    cur = list(current)
    for i, want in enumerate(target):
        if i >= len(cur) or cur[i] == want:
            continue
        try:
            j = cur.index(want, i)
        except ValueError:
            continue
        moves.append((j, i))
        cur.insert(i, cur.pop(j))
    return moves


def _diff_sync_playlist(
    sp: spotipy.Spotify,
    playlist_id: str,
    target_uris: list[str],
    playlist_name: str,
    dry_run: bool = False,
    allow_large_removal: bool = False,
):
    current = _get_current_playlist_uris(sp, playlist_id)
    target_counts = Counter(target_uris)
    current_counts = Counter(current)

    mismatched = {
        uri for uri in set(current_counts) | set(target_counts)
        if current_counts.get(uri, 0) != target_counts.get(uri, 0)
    }

    to_remove_all = [uri for uri in mismatched if uri in current_counts]

    remaining = Counter({u: c for u, c in current_counts.items() if u not in mismatched})
    to_add: list[str] = []
    for uri in target_uris:
        if remaining[uri] > 0:
            remaining[uri] -= 1
        else:
            to_add.append(uri)

    after_diff = [uri for uri in current if uri not in mismatched] + to_add
    reorder_moves = _plan_reorder(after_diff, target_uris)

    unchanged_count = sum(c for u, c in current_counts.items() if u not in mismatched)
    logger.info(
        "Spotify currently: %d | add: %d | remove-all-copies-of: %d URIs | reorder: %d moves | untouched: %d",
        len(current), len(to_add), len(to_remove_all), len(reorder_moves), unchanged_count,
    )

    removal_ratio = len(to_remove_all) / max(len(current), 1)
    if len(to_remove_all) > REMOVAL_ABSOLUTE_FLOOR and removal_ratio > REMOVAL_RATIO_THRESHOLD:
        msg = (
            f"Refusing to remove {len(to_remove_all)}/{len(current)} tracks from {playlist_name} "
            f"({removal_ratio:.1%} exceeds {REMOVAL_RATIO_THRESHOLD:.1%} threshold). "
            "Re-run with --allow-large-removal to proceed, or investigate whether "
            "the Apple Music playlist reverted upstream."
        )
        if allow_large_removal:
            logger.warning("Large-removal guardrail BYPASSED (--allow-large-removal): %s", msg)
        elif dry_run:
            logger.warning("[dry-run] guardrail would abort: %s", msg)
        else:
            raise LargeRemovalError(msg)

    if dry_run:
        logger.info("[dry-run] no changes written")
        return

    if to_remove_all:
        for i in range(0, len(to_remove_all), 100):
            sp.playlist_remove_all_occurrences_of_items(playlist_id, to_remove_all[i:i + 100])
        logger.info("Removed all copies of %d URIs", len(to_remove_all))

    if to_add:
        for i in range(0, len(to_add), 100):
            sp.playlist_add_items(playlist_id, to_add[i:i + 100])
        logger.info("Added %d tracks", len(to_add))

    if reorder_moves:
        for range_start, insert_before in reorder_moves:
            sp.playlist_reorder_items(
                playlist_id,
                range_start=range_start,
                insert_before=insert_before,
            )
            time.sleep(0.1)
        logger.info("Reordered: %d moves", len(reorder_moves))


def _write_unmatched(unmatched: list[dict], playlist_name: str):
    REPORTS_DIR.mkdir(exist_ok=True)
    with open(UNMATCHED_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M} — {playlist_name} ===\n")
        for t in unmatched:
            f.write(f"  {t['name']} — {t['artist']} (album: {t.get('album', '')})\n")


def _write_review_report(playlists: list[dict]):
    REPORTS_DIR.mkdir(exist_ok=True)
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "playlists": playlists,
    }
    NEEDS_REVIEW_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False))


def sync_playlist(
    entry: dict,
    sp: spotipy.Spotify,
    cache: dict,
    pending: dict,
    dropped: dict,
    dry_run: bool = False,
    force: bool = False,
    allow_large_removal: bool = False,
) -> dict:
    name = entry["name"]
    apple_id = entry["apple_id"]
    spotify_id = entry.get("spotify_id")
    if not spotify_id:
        logger.error("No spotify_id configured for %s — skipping", name)
        return {"name": name, "skipped": True}

    logger.info("=" * 60)
    logger.info("Syncing: %s", name)
    logger.info("=" * 60)

    apple_tracks, apple_name = fetch_apple_playlist(apple_id)
    logger.info("Apple Music: %d tracks (%s)", len(apple_tracks), apple_name)

    uris, unmatched, uncertain, cache_hits = _resolve_tracks(
        apple_tracks, sp, cache, pending, name, force=force,
    )
    logger.info(
        "Resolved %d/%d (cache hits: %d, unmatched: %d, uncertain: %d)",
        len(uris), len(apple_tracks), cache_hits, len(unmatched), len(uncertain),
    )

    if unmatched:
        _write_unmatched(unmatched, name)

    _diff_sync_playlist(
        sp, spotify_id, uris, name,
        dry_run=dry_run, allow_large_removal=allow_large_removal,
    )

    # Only after a successful sync: a failed Apple fetch must not look like
    # every pending track left the playlist.
    _prune_pending(pending, dropped, cache, name, apple_tracks)

    return {
        "name": name,
        "apple_id": apple_id,
        "spotify_id": spotify_id,
        "apple_count": len(apple_tracks),
        "resolved": len(uris),
        "unmatched": unmatched,
        "uncertain": uncertain,
    }


def main():
    parser = argparse.ArgumentParser(description="Sync Apple Music → Spotify playlists")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to Spotify or save cache")
    parser.add_argument("--force", action="store_true", help="Ignore cache, re-match everything")
    parser.add_argument("--playlist", help="Sync only the playlist with this name")
    parser.add_argument(
        "--allow-large-removal", action="store_true",
        help=f"Bypass the guard that refuses to remove >{REMOVAL_RATIO_THRESHOLD:.0%} of a playlist",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    playlists = json.loads(PLAYLISTS_FILE.read_text())
    if args.playlist:
        playlists = [p for p in playlists if p["name"] == args.playlist]
        if not playlists:
            logger.error("No playlist named %r in playlists.json", args.playlist)
            return

    sp = build_client()
    cache = _load_cache()
    initial_cache_size = len(cache)
    pending = review_state.load_pending()
    dropped = review_state.load_dropped()

    report_playlists: list[dict] = []
    failed: list[str] = []
    for entry in playlists:
        try:
            fragment = sync_playlist(
                entry, sp, cache, pending, dropped,
                dry_run=args.dry_run, force=args.force,
                allow_large_removal=args.allow_large_removal,
            )
            report_playlists.append(fragment)
        except Exception as e:
            logger.error("Failed syncing %s: %s", entry["name"], e)
            report_playlists.append({"name": entry["name"], "error": str(e)})
            failed.append(entry["name"])

    if not args.dry_run:
        _save_cache(cache)
        logger.info("Cache: %d → %d mappings", initial_cache_size, len(cache))
        review_state.save_pending(pending)
        review_state.save_dropped(dropped)
        logger.info("Pending review: %d", len(pending))

    _write_review_report(report_playlists)

    if failed:
        logger.error("Sync failed for %d playlist(s): %s", len(failed), ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
