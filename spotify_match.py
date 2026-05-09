"""Resolve Apple Music tracks to Spotify URIs. See ARCHITECTURE.md."""

import os
import re
import base64
import logging
from pathlib import Path

import requests
import spotipy
from rapidfuzz import fuzz

from compare import _clean_title, _clean_artist, _normalize_text

logger = logging.getLogger(__name__)

MATCH_THRESHOLD = 0.82
REVIEW_THRESHOLD = 0.65


def _load_env():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _refresh_access_token() -> str:
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN")
    if not all([client_id, client_secret, refresh_token]):
        raise RuntimeError(
            "Missing SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, or "
            "SPOTIFY_REFRESH_TOKEN in env (.env or shell)."
        )
    auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        headers={"Authorization": f"Basic {auth_header}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def build_client() -> spotipy.Spotify:
    _load_env()
    return spotipy.Spotify(auth=_refresh_access_token())


_COMPILATION_RE = re.compile(
    r"""\b(
        greatest\s+hits |
        best\s+of |
        anthology |
        collection |
        the\s+essential |
        now\s+that.s\s+what |
        compilation |
        # Generic VA mix-tape names
        workout |
        chill |
        bangers |
        playlist
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)

_LIVE_RE = re.compile(
    r"\b(live(\s+(at|from|in))?|unplugged|in\s+concert)\b",
    re.IGNORECASE,
)

_SOUNDTRACK_RE = re.compile(
    r"""\b(
        soundtrack |
        original\s+motion\s+picture |
        music\s+from(\s+the\s+motion\s+pictures?)? |
        \bo\.?\s*s\.?\s*t\b |
        original\s+score
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)

ALBUM_NAME_FUZZ_THRESHOLD = 60


def _is_original_release(spotify_track: dict, apple_track: dict) -> bool:
    album = spotify_track.get("album", {})
    album_name = album.get("name", "")
    album_type = album.get("album_type", "")
    album_artists = [a.get("name", "") for a in album.get("artists", [])]
    apple_album = apple_track.get("album", "")

    apple_is_soundtrack = bool(_SOUNDTRACK_RE.search(apple_album))
    spotify_is_soundtrack = bool(_SOUNDTRACK_RE.search(album_name))
    if apple_is_soundtrack != spotify_is_soundtrack:
        return False
    if apple_is_soundtrack and spotify_is_soundtrack:
        return True

    if album_type == "compilation":
        return False

    if any(a.lower() == "various artists" for a in album_artists):
        return False

    if _COMPILATION_RE.search(album_name):
        return False

    spotify_is_live = bool(
        _LIVE_RE.search(spotify_track.get("name", ""))
        or _LIVE_RE.search(album_name)
    )
    apple_is_live = bool(
        _LIVE_RE.search(apple_track.get("name", ""))
        or _LIVE_RE.search(apple_album)
    )
    if spotify_is_live != apple_is_live:
        return False

    if apple_album and album_name:
        a = _normalize_text(_clean_title(apple_album))
        s = _normalize_text(_clean_title(album_name))
        if a and s and fuzz.token_set_ratio(a, s) < ALBUM_NAME_FUZZ_THRESHOLD:
            return False

    return True


def _pick_best_isrc_candidate(items: list[dict], apple_track: dict) -> dict | None:
    if not items:
        return None
    preferred = [t for t in items if _is_original_release(t, apple_track)]
    if preferred:
        preferred.sort(key=lambda t: t.get("album", {}).get("release_date", "9999"))
        return preferred[0]
    logger.warning(
        "No 'original release' candidate for %s — %s; falling back to first match",
        apple_track.get("name"), apple_track.get("artist"),
    )
    return items[0]


def _score_match(apple_track: dict, spotify_track: dict) -> float:
    a_title = _normalize_text(_clean_title(apple_track["name"]))
    s_title = _normalize_text(_clean_title(spotify_track["name"]))
    title_score = fuzz.token_set_ratio(a_title, s_title) / 100.0

    a_artist = _normalize_text(_clean_artist(apple_track["artist"]))
    s_artist = _normalize_text(_clean_artist(
        ", ".join(a["name"] for a in spotify_track.get("artists", []))
    ))
    artist_score = fuzz.token_set_ratio(a_artist, s_artist) / 100.0

    a_album = _normalize_text(_clean_title(apple_track.get("album", "")))
    s_album = _normalize_text(_clean_title(spotify_track.get("album", {}).get("name", "")))
    album_score = fuzz.token_set_ratio(a_album, s_album) / 100.0

    a_dur = apple_track.get("duration_ms", 0) or 0
    s_dur = spotify_track.get("duration_ms", 0) or 0
    if a_dur and s_dur:
        delta = abs(a_dur - s_dur)
        duration_score = max(0.0, 1.0 - delta / 5000.0)
    else:
        duration_score = 0.5

    return (
        0.40 * title_score
        + 0.35 * artist_score
        + 0.15 * album_score
        + 0.10 * duration_score
    )


def find_spotify_track(
    apple_track: dict,
    sp: spotipy.Spotify,
) -> dict | None:
    isrc = (apple_track.get("isrc") or "").strip()
    if isrc:
        results = sp.search(q=f"isrc:{isrc}", type="track", limit=10)
        items = results.get("tracks", {}).get("items", [])
        if items:
            best = _pick_best_isrc_candidate(items, apple_track)
            if best:
                return {
                    "uri": best["uri"],
                    "score": 1.0,
                    "source": "isrc",
                    "track": best,
                }

    title = _clean_title(apple_track["name"])
    artist = _clean_artist(apple_track["artist"])
    query = f"track:{title} artist:{artist}"
    try:
        results = sp.search(q=query, type="track", limit=10)
    except spotipy.SpotifyException as e:
        logger.warning("Search failed for %s: %s", query, e)
        return None

    items = results.get("tracks", {}).get("items", [])
    if not items:
        return None

    preferred = [t for t in items if _is_original_release(t, apple_track)]
    candidates = preferred or items

    scored = sorted(
        ((cand, _score_match(apple_track, cand)) for cand in candidates),
        key=lambda x: x[1],
        reverse=True,
    )
    if not scored or scored[0][1] < REVIEW_THRESHOLD:
        return None

    best_track, best_score = scored[0]
    result = {
        "uri": best_track["uri"],
        "score": best_score,
        "source": "fuzzy",
        "track": best_track,
    }

    if best_score < MATCH_THRESHOLD:
        result["alternates"] = [
            {"uri": t["uri"], "score": s, "track": t}
            for t, s in scored[1:4]
        ]

    return result


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Debug a single Apple → Spotify match")
    parser.add_argument("name", help="Track title")
    parser.add_argument("artist", help="Artist")
    parser.add_argument("--album", default="", help="Album name (optional)")
    parser.add_argument("--isrc", default="", help="ISRC (optional, enables ISRC search)")
    parser.add_argument("--duration-ms", type=int, default=0, help="Duration in ms")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    sp = build_client()
    apple_track = {
        "name": args.name,
        "artist": args.artist,
        "album": args.album,
        "isrc": args.isrc,
        "duration_ms": args.duration_ms,
    }
    match = find_spotify_track(apple_track, sp)
    if not match:
        print("NO MATCH")
        return
    t = match["track"]
    artists = ", ".join(a["name"] for a in t.get("artists", []))
    album = t.get("album", {})
    print(f"\n[{match['source']} {match['score']:.2f}] {t['name']} — {artists}")
    print(f"  album: {album.get('name')} ({album.get('album_type')}, {album.get('release_date')})")
    print(f"  uri:   {match['uri']}")


if __name__ == "__main__":
    main()
