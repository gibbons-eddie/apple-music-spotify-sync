"""Import a Spotify playlist into your Apple Music library (one-way, append-only).

    python3 spotify_to_apple.py <spotify playlist link> [--dry-run]

First run for a link creates an Apple Music library playlist with the same name
and an "Imported from Spotify playlist <id>" marker in its description. Later
runs find it again (cache/imports.json, then the marker) and append the songs
it's missing. Apple's public API can't remove or reorder songs, so songs to
remove and songs out of order are listed in the review issue to fix by hand.

--save-text also saves the Spotify playlist as a readable .txt and a .csv under
exports/ (git-ignored), e.g. to keep a copy of a playlist you're backing up.

--dry-run matches everything and writes the review report, but never writes to
the Apple library. It works without APPLE_MUSIC_USER_TOKEN; the library is then
not checked.
"""

import csv
import sys
import json
import time
import logging
import argparse
from pathlib import Path
from datetime import datetime

import requests
import spotipy
from rapidfuzz import fuzz

import review_state
from apple_client import (
    AppleAuthError,
    AppleClient,
    library_track_catalog_id,
    library_track_plain,
    song_to_plain,
)
from apple_match import find_apple_song
from compare import _clean_artist, _clean_title, _normalize_text
from spotify_export import _extract_playlist_id, _slugify, fetch_spotify_playlist
from spotify_match import MATCH_THRESHOLD

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).parent
CACHE_DIR = PROJECT_DIR / "cache"
FORWARD_CACHE_FILE = CACHE_DIR / "track_mapping.json"
REVERSE_CACHE_FILE = CACHE_DIR / "reverse_mapping.json"
IMPORTS_FILE = CACHE_DIR / "imports.json"
REPORT_FILE = PROJECT_DIR / "reports" / "import_review.json"
# Local text backups of each imported Spotify playlist (git-ignored).
EXPORTS_DIR = PROJECT_DIR / "exports"

SEARCH_DELAY_SECONDS = 0.15
MARKER_PREFIX = "Imported from Spotify playlist"


class ImportStopped(RuntimeError):
    """A run-stopping problem to show in the review issue."""


def marker_key(spotify_playlist_id: str) -> str:
    """The part of the description marker that identifies the import."""
    return f"{MARKER_PREFIX} {spotify_playlist_id}"


def marker(spotify_playlist_id: str) -> str:
    return f"{marker_key(spotify_playlist_id)} by apple-music-spotify-sync."


# --- state -------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _save_json(path: Path, data: dict):
    CACHE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))


def load_reverse_cache() -> dict:
    """Spotify track ID → Apple catalog song ID.

    On first use it is seeded from the weekly sync's Apple → Spotify mappings,
    flipped, so songs already on your synced playlists match instantly.
    """
    if REVERSE_CACHE_FILE.exists():
        return _load_json(REVERSE_CACHE_FILE)
    seeded: dict[str, str] = {}
    for apple_id, uri in sorted(_load_json(FORWARD_CACHE_FILE).items()):
        if uri.startswith("spotify:track:"):
            seeded.setdefault(uri.rsplit(":", 1)[-1], apple_id)
    logger.info("Seeded reverse cache with %d mappings from track_mapping.json", len(seeded))
    return seeded


# --- reading Spotify -------------------------------------------------------------

def _spotify_api_read(playlist_id: str) -> tuple[list[dict], str]:
    """Full details (album, ISRC) via this repo's own Spotify app, as the weekly sync uses."""
    from spotify_match import _load_env, _refresh_access_token

    _load_env()
    # No retries: a rate limit should fall back to the embed page at once, not
    # sleep for the hours a Retry-After header can ask for.
    sp = spotipy.Spotify(
        auth=_refresh_access_token(), retries=0, status_retries=0, requests_timeout=15,
    )
    name = sp.playlist(playlist_id, fields="name").get("name", playlist_id)
    tracks: list[dict] = []
    page = sp.playlist_items(playlist_id, limit=100)
    while page:
        for entry in page.get("items", []):
            # Spotify returns the track under "item" (was "track" in older responses).
            t = entry.get("item") or entry.get("track")
            if not t or t.get("type") != "track":
                continue
            tracks.append({
                "name": t.get("name", ""),
                "artist": ", ".join(a.get("name", "") for a in t.get("artists", [])),
                "album": (t.get("album") or {}).get("name", ""),
                "duration_ms": t.get("duration_ms", 0) or 0,
                "isrc": (t.get("external_ids") or {}).get("isrc", ""),
                "spotify_id": t.get("id") or "",
                "spotify_url": (t.get("external_urls") or {}).get("spotify", ""),
            })
        page = sp.next(page) if page.get("next") else None
    return tracks, name


def read_spotify_playlist(url: str) -> tuple[list[dict], str, bool]:
    """Returns (tracks, name, basic).

    Reads through this repo's Spotify app first (full details). If that's not
    possible (no credentials locally, Spotify refuses its app access to
    Spotify-made playlists such as Your Top Songs, or rate-limits it), falls
    back to the public embed page's basic track list: title, artist and
    length, no album or ISRC.
    """
    playlist_id = _extract_playlist_id(url)
    try:
        tracks, name = _spotify_api_read(playlist_id)
        return tracks, name, False
    except spotipy.SpotifyException as e:
        reason = f"Spotify answered {e.http_status} to the app's playlist read"
    except (RuntimeError, requests.RequestException) as e:
        reason = f"the app's playlist read wasn't possible ({e})"
    logger.warning(
        "%s; using the embed page's basic track list (no album or ISRC, so matching "
        "relies on title, artist and length)", reason,
    )
    tracks, name = fetch_spotify_playlist(url, embed_only=True)
    return tracks, name, True


def write_backup(tracks: list[dict], name: str, playlist_id: str) -> list[Path]:
    """Save the Spotify playlist as a readable .txt and a .csv under exports/."""
    EXPORTS_DIR.mkdir(exist_ok=True)
    stem = EXPORTS_DIR / f"{_slugify(name) or 'playlist'}_{playlist_id}"
    url = f"https://open.spotify.com/playlist/{playlist_id}"

    def length(ms: int) -> str:
        secs = round((ms or 0) / 1000)
        return f"{secs // 60}:{secs % 60:02d}" if secs else ""

    lines = [name, url, f"Saved {datetime.now():%Y-%m-%d} from Spotify · {len(tracks)} songs", ""]
    width = len(str(len(tracks)))
    for i, t in enumerate(tracks, 1):
        extras = " · ".join(b for b in (t.get("album", ""), length(t.get("duration_ms", 0))) if b)
        lines.append(f"{i:>{width}}. {t.get('name', '')} — {t.get('artist', '')}" + (f" · {extras}" if extras else ""))
    txt = stem.with_suffix(".txt")
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8")

    csv_path = stem.with_suffix(".csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["position", "name", "artist", "album", "length", "isrc", "spotify_url"])
        for i, t in enumerate(tracks, 1):
            w.writerow([i, t.get("name", ""), t.get("artist", ""), t.get("album", ""),
                        length(t.get("duration_ms", 0)), t.get("isrc", ""), t.get("spotify_url", "")])
    logger.info("Saved backup: %s and %s", txt, csv_path)
    return [txt, csv_path]


# --- matching ------------------------------------------------------------------

def _spotify_context(track: dict, idx: int, total: int) -> dict:
    return {
        "name": track.get("name", ""),
        "artist": track.get("artist", ""),
        "album": track.get("album", ""),
        "isrc": track.get("isrc", ""),
        "spotify_id": track.get("spotify_id", ""),
        "position": idx + 1,
        "total": total,
    }


def _apple_summary(song: dict, score: float, source: str) -> dict:
    p = song_to_plain(song)
    return {
        "id": song.get("id", ""),
        "name": p["name"],
        "artist": p["artist"],
        "album": p["album"],
        "release_date": p["release_date"],
        "score": round(score, 3),
        "source": source,
    }


def _update_pending(pending: dict, track_id: str, match: dict, track: dict, playlist_id: str):
    if match["score"] >= MATCH_THRESHOLD:
        pending.pop(track_id, None)
        return
    existing = pending.get(track_id)
    if existing and existing.get("picked_id") == match["id"]:
        return
    pending[track_id] = {
        "picked_id": match["id"],
        "picked_score": round(match["score"], 3),
        "picked_at": review_state.now_iso(),
        "name": track.get("name", ""),
        "artist": track.get("artist", ""),
        "playlist": playlist_id,
    }


def resolve_tracks(
    tracks: list[dict],
    client: AppleClient,
    reverse: dict,
    pending: dict,
    playlist_id: str,
) -> tuple[list[tuple[dict, str]], list[dict], list[dict], list[dict], int]:
    """Returns ([(spotify track, apple song id)] in order, unmatched, uncertain,
    matched-via-featured-artists, cache hits)."""
    matched: list[tuple[dict, str]] = []
    unmatched: list[dict] = []
    uncertain: list[dict] = []
    featured: list[dict] = []
    hits = 0

    for idx, track in enumerate(tracks):
        tid = track.get("spotify_id", "")
        if tid and tid in reverse:
            matched.append((track, reverse[tid]))
            hits += 1
            continue
        if not tid:
            # Local files and unavailable items have no Spotify ID or catalog equivalent.
            unmatched.append(_spotify_context(track, idx, len(tracks)))
            continue

        match = find_apple_song(track, client)
        if match:
            matched.append((track, match["id"]))
            reverse[tid] = match["id"]
            _update_pending(pending, tid, match, track, playlist_id)
            logger.info("[%s %.2f] %s — %s", match["source"], match["score"], track["name"], track["artist"])
            if match.get("via_featured"):
                featured.append({
                    "spotify": _spotify_context(track, idx, len(tracks)),
                    "picked": _apple_summary(match["song"], match["score"], match["source"]),
                    "score_without_featured": round(match["raw_score"], 3),
                })
            if match["score"] < MATCH_THRESHOLD:
                uncertain.append({
                    "spotify": _spotify_context(track, idx, len(tracks)),
                    "picked": _apple_summary(match["song"], match["score"], match["source"]),
                    "alternates": [
                        _apple_summary(a["song"], a["score"], "fuzzy")
                        for a in match.get("alternates", [])
                    ],
                })
        else:
            unmatched.append(_spotify_context(track, idx, len(tracks)))
            logger.warning("UNMATCHED: %s — %s", track["name"], track["artist"])
        time.sleep(SEARCH_DELAY_SECONDS)

    return matched, unmatched, uncertain, featured, hits


def prune_pending(pending: dict, dropped: dict, reverse: dict, playlist_id: str, tracks: list[dict]):
    """Forget pending picks whose track left this Spotify playlist, so a re-add
    is matched from scratch (same rule as the weekly sync)."""
    present = {t.get("spotify_id") for t in tracks}
    for tid in [t for t, e in pending.items() if e.get("playlist") == playlist_id and t not in present]:
        entry = pending.pop(tid)
        reverse.pop(tid, None)
        dropped[tid] = review_state.now_iso()
        logger.info("Dropped pending review for %s — %s (no longer in the playlist)",
                    entry.get("name", ""), entry.get("artist", ""))


# --- the Apple playlist ----------------------------------------------------------

def _description(playlist: dict) -> str:
    d = playlist.get("attributes", {}).get("description", {})
    return d.get("standard", "") if isinstance(d, dict) else str(d or "")


def find_existing_playlist(
    client: AppleClient, imports: dict, playlist_id: str, name: str,
) -> dict | None:
    record = imports.get(playlist_id)
    if record:
        found = client.library_playlist(record["apple_playlist_id"])
        if found:
            return found
        logger.warning("Recorded Apple playlist %s is gone; looking it up again", record["apple_playlist_id"])

    library = client.library_playlists()
    for p in library:
        if marker_key(playlist_id) in _description(p):
            return p

    clashes = [p for p in library if p.get("attributes", {}).get("name", "") == name]
    if clashes:
        raise ImportStopped(
            f"Your Apple Music library already has a playlist named \"{name}\" that this tool "
            "didn't create, so it was left alone. Rename it, or, to adopt it, add this line "
            f"to its description and re-run: {marker(playlist_id)}"
        )
    return None


def _same_song(track: dict, apple: dict) -> bool:
    """Fallback identity check when a library entry's catalog ID differs from
    the one we added (Apple sometimes maps to another release of the song)."""
    a = _normalize_text(_clean_title(track.get("name", "")))
    b = _normalize_text(_clean_title(apple.get("name", "")))
    if not a or a != b:
        return False
    return fuzz.token_set_ratio(
        _normalize_text(_clean_artist(track.get("artist", ""))),
        _normalize_text(_clean_artist(apple.get("artist", ""))),
    ) >= 90


def _longest_increasing(seq: list[int]) -> set[int]:
    """Indexes into seq of one longest strictly increasing subsequence."""
    import bisect
    tails: list[int] = []
    tail_idx: list[int] = []
    prev = [-1] * len(seq)
    for i, v in enumerate(seq):
        k = bisect.bisect_left(tails, v)
        if k == len(tails):
            tails.append(v)
            tail_idx.append(i)
        else:
            tails[k] = v
            tail_idx[k] = i
        prev[i] = tail_idx[k - 1] if k else -1
    keep: set[int] = set()
    i = tail_idx[-1] if tail_idx else -1
    while i != -1:
        keep.add(i)
        i = prev[i]
    return keep


def plan_update(targets: list[tuple[dict, str]], current: list[dict]) -> dict:
    """Compare the wanted songs (in Spotify order) with the Apple playlist.

    Returns songs to append, songs to remove by hand, and the fewest songs to
    drag so the order matches Spotify once the appends have landed.
    """
    entries = [
        {"catalog_id": library_track_catalog_id(t), **library_track_plain(t), "claimed": None}
        for t in current
    ]
    by_id: dict[str, list[int]] = {}
    for i, e in enumerate(entries):
        if e["catalog_id"]:
            by_id.setdefault(e["catalog_id"], []).append(i)

    to_add: list[tuple[int, dict, str]] = []
    for ti, (track, song_id) in enumerate(targets):
        slot = next((i for i in by_id.get(song_id, []) if entries[i]["claimed"] is None), None)
        if slot is None:
            slot = next(
                (i for i, e in enumerate(entries) if e["claimed"] is None and _same_song(track, e)),
                None,
            )
        if slot is None:
            to_add.append((ti, track, song_id))
        else:
            entries[slot]["claimed"] = ti

    remove = [
        {"name": e["name"], "artist": e["artist"], "apple_position": i + 1}
        for i, e in enumerate(entries) if e["claimed"] is None
    ]

    # Order after the append: kept entries in place, new songs at the end.
    final = [(i + 1, e["claimed"]) for i, e in enumerate(entries) if e["claimed"] is not None]
    final += [(len(entries) + n + 1, ti) for n, (ti, _, _) in enumerate(to_add)]
    keep = _longest_increasing([ti for _, ti in final])
    out_of_order = [
        {
            "name": targets[ti][0].get("name", ""),
            "artist": targets[ti][0].get("artist", ""),
            "spotify_position": ti + 1,
            "apple_position": pos,
        }
        for k, (pos, ti) in enumerate(final) if k not in keep
    ]
    return {
        "to_add": [(track, song_id) for _, track, song_id in to_add],
        "remove_by_hand": remove,
        "out_of_order": sorted(out_of_order, key=lambda m: m["spotify_position"]),
    }


def _unique_targets(matched: list[tuple[dict, str]]) -> list[tuple[dict, str]]:
    seen: set[str] = set()
    out = []
    for track, song_id in matched:
        if song_id not in seen:
            seen.add(song_id)
            out.append((track, song_id))
    return out


def _named(pairs: list[tuple[dict, str]]) -> list[dict]:
    return [{"name": t.get("name", ""), "artist": t.get("artist", ""), "apple_id": s} for t, s in pairs]


# --- run -------------------------------------------------------------------------

def run(url: str, dry_run: bool, save_text: bool = False) -> dict:
    playlist_id = _extract_playlist_id(url)
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "spotify_playlist": {"id": playlist_id, "name": playlist_id,
                             "url": f"https://open.spotify.com/playlist/{playlist_id}"},
    }
    reverse = load_reverse_cache()
    pending = review_state.load_import_pending()
    dropped = review_state.load_import_dropped()
    imports = _load_json(IMPORTS_FILE)

    try:
        tracks, sp_name, basic = read_spotify_playlist(url)
        logger.info("Spotify: %d tracks (%s)", len(tracks), sp_name)
        report["spotify_playlist"]["name"] = sp_name
        report["spotify_playlist"]["basic_info"] = basic
        report["total"] = len(tracks)
        if save_text:
            report["backup"] = [str(p.relative_to(PROJECT_DIR)) for p in write_backup(tracks, sp_name, playlist_id)]
        client = AppleClient()

        matched, unmatched, uncertain, featured, hits = resolve_tracks(
            tracks, client, reverse, pending, playlist_id,
        )
        logger.info("Resolved %d/%d (cache hits: %d, unmatched: %d, uncertain: %d)",
                    len(matched), len(tracks), hits, len(unmatched), len(uncertain))
        report.update(resolved=len(matched), unmatched=unmatched, uncertain=uncertain, featured=featured)
        targets = _unique_targets(matched)

        if not client.has_library_access:
            if not dry_run:
                raise AppleAuthError(
                    "APPLE_MUSIC_USER_TOKEN is not set, so the Apple library can't be written. "
                    "Run with --dry-run to only match, or add the token."
                )
            logger.warning("No APPLE_MUSIC_USER_TOKEN: dry run without checking the Apple library")
            report["apple_playlist"] = {"action": "not_checked", "name": sp_name}
            report["added"] = _named(targets)
        else:
            existing = find_existing_playlist(client, imports, playlist_id, sp_name)
            if existing is None:
                report["apple_playlist"] = {"action": "would_create" if dry_run else "created", "name": sp_name}
                report["added"] = _named(targets)
                if not dry_run:
                    apple_id = client.create_library_playlist(
                        sp_name, marker(playlist_id), [s for _, s in targets],
                    )
                    report["apple_playlist"]["id"] = apple_id
                    imports[playlist_id] = {
                        "apple_playlist_id": apple_id, "name": sp_name,
                        "created_at": review_state.now_iso(),
                    }
                logger.info("%s Apple playlist '%s' with %d songs",
                            "Would create" if dry_run else "Created", sp_name, len(targets))
            else:
                apple_id = existing["id"]
                current = client.library_playlist_tracks(apple_id)
                plan = plan_update(targets, current)
                report["apple_playlist"] = {
                    "action": "would_update" if dry_run else "updated", "id": apple_id,
                    "name": existing.get("attributes", {}).get("name", sp_name),
                }
                report["added"] = _named(plan["to_add"])
                report["remove_by_hand"] = plan["remove_by_hand"]
                report["out_of_order"] = plan["out_of_order"]
                if not dry_run and plan["to_add"]:
                    client.append_tracks(apple_id, [s for _, s in plan["to_add"]])
                logger.info(
                    "Apple playlist: %d current | %s %d | remove by hand: %d | out of order: %d",
                    len(current), "would append" if dry_run else "appended", len(plan["to_add"]),
                    len(plan["remove_by_hand"]), len(plan["out_of_order"]),
                )
                if not dry_run:
                    imports.setdefault(playlist_id, {}).update(apple_playlist_id=apple_id, name=sp_name)
            if not dry_run:
                imports[playlist_id]["last_run_at"] = review_state.now_iso()

        prune_pending(pending, dropped, reverse, playlist_id, tracks)
    except (AppleAuthError, ImportStopped) as e:
        logger.error("Import failed: %s", e)
        report["error"] = str(e)
    except Exception as e:
        logger.exception("Import failed")
        report["error"] = f"{type(e).__name__}: {e}"
    finally:
        _save_json(REVERSE_CACHE_FILE, reverse)
        review_state.save_import_pending(pending)
        review_state.save_import_dropped(dropped)
        if not dry_run:
            _save_json(IMPORTS_FILE, imports)

    report["pending"] = pending_for(pending, playlist_id, report)
    REPORT_FILE.parent.mkdir(exist_ok=True)
    REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def pending_for(pending: dict, playlist_id: str, report: dict) -> dict:
    """This playlist's pending picks, minus ones already shown as uncertain this run."""
    shown = {u["spotify"]["spotify_id"] for u in report.get("uncertain", [])}
    return {t: e for t, e in pending.items() if e.get("playlist") == playlist_id and t not in shown}


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a Spotify playlist into Apple Music")
    parser.add_argument("url", help="Spotify playlist link or ID")
    parser.add_argument("--dry-run", action="store_true", help="Match and report only; no Apple writes")
    parser.add_argument("--save-text", action="store_true",
                        help="Also save the Spotify playlist as .txt and .csv under exports/")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    report = run(args.url, args.dry_run, args.save_text)
    return 1 if report.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
