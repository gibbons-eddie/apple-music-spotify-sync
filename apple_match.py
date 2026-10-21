"""Match a Spotify track to an Apple Music catalog song (reverse of spotify_match.py).

Same strategy and thresholds as the weekly sync: ISRC lookup first, then a
catalog search scored by spotify_match.score_match.
"""

import logging

from apple_client import AppleClient, song_to_plain
from compare import _clean_artist, _clean_title
from spotify_match import (
    MATCH_THRESHOLD,
    REVIEW_THRESHOLD,
    is_original_release,
    score_match,
)

logger = logging.getLogger(__name__)


def _pick_best_isrc_song(songs: list[dict], source: dict) -> dict:
    preferred = [s for s in songs if is_original_release(song_to_plain(s), source)]
    if preferred:
        preferred.sort(key=lambda s: song_to_plain(s)["release_date"] or "9999")
        return preferred[0]
    logger.warning(
        "No 'original release' Apple candidate for %s — %s; using first ISRC match",
        source.get("name"), source.get("artist"),
    )
    return songs[0]


def _score(source: dict, candidate: dict) -> float:
    # A basic Spotify track list (embed fallback) has no album. Treat the album
    # as matching rather than scoring it 0, so a missing field doesn't push
    # every pick below the review threshold; title, artist and length decide.
    if not source.get("album"):
        source = {**source, "album": candidate.get("album", "")}
    return score_match(source, candidate)


def find_apple_song(spotify_track: dict, client: AppleClient) -> dict | None:
    """spotify_track is a spotify_export track (name, artist, album, duration_ms, isrc).

    Returns {"id", "score", "source", "song", "alternates"?} or None.
    """
    source = {
        "name": spotify_track.get("name", ""),
        "artist": spotify_track.get("artist", ""),
        "album": spotify_track.get("album", ""),
        "duration_ms": spotify_track.get("duration_ms", 0) or 0,
    }

    isrc = (spotify_track.get("isrc") or "").strip()
    if isrc:
        songs = client.songs_by_isrc(isrc)
        if songs:
            best = _pick_best_isrc_song(songs, source)
            return {"id": best["id"], "score": 1.0, "source": "isrc", "song": best}

    term = f"{_clean_title(source['name'])} {_clean_artist(source['artist'])}".strip()
    songs = client.search_songs(term)
    if not songs:
        return None

    preferred = [s for s in songs if is_original_release(song_to_plain(s), source)]
    scored = sorted(
        ((s, _score(source, song_to_plain(s))) for s in (preferred or songs)),
        key=lambda x: x[1],
        reverse=True,
    )
    best, best_score = scored[0]
    if best_score < REVIEW_THRESHOLD:
        return None

    result = {"id": best["id"], "score": best_score, "source": "fuzzy", "song": best}
    if best_score < MATCH_THRESHOLD:
        result["alternates"] = [
            {"id": s["id"], "score": sc, "song": s} for s, sc in scored[1:4]
        ]
    return result
