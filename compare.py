"""
Compare Apple Music and Spotify playlists to find differences.

Reads an Apple Music CSV (auto-fetched or pre-exported) and a Spotify CSV
(user-provided export), fuzzy-matches tracks, and outputs a diff report
showing what's missing from each side.

Usage:
    python compare.py <apple_csv> <spotify_csv>
    python compare.py --fetch <apple_playlist_id> <spotify_csv>
"""

import csv
import re
import sys
import logging
import argparse
import unicodedata
from pathlib import Path
from datetime import datetime
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

MATCH_THRESHOLD = 0.82

# --- Normalization (same logic as the original spotify_match.py) ---

TITLE_JUNK_RE = re.compile(
    r"""
    \s*[\-–—]\s*
    (?:
        (?:\d{4}\s+)?
        remaster(?:ed)?
        (?:\s+\d{4})?
        (?:\s+version)?
    |
        remasterizado\s+\d{4}
    |
        radio\s+edit
    |
        single\s+version
    |
        deluxe(?:\s+edition)?
    |
        bonus\s+track
    |
        original\s+mix
    |
        .*remaster.*
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

PAREN_JUNK_RE = re.compile(
    r"""
    \s*[(\[]
    (?:
        (?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?
    |
        remasterizado\s+\d{4}
    |
        radio\s+edit
    |
        single\s+version
    |
        deluxe(?:\s+edition)?
    |
        bonus\s+track
    |
        original\s+mix
    |
        .*remaster.*
    )
    \s*[)\]]
    """,
    re.IGNORECASE | re.VERBOSE,
)

FEAT_RE = re.compile(
    r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s+[^)\]]+[)\]]",
    re.IGNORECASE,
)

FEAT_INLINE_RE = re.compile(
    r"\s+(?:feat\.?|ft\.?|featuring)\s+.+$",
    re.IGNORECASE,
)


def _normalize_text(text: str) -> str:
    """Lowercase, strip diacritics, collapse whitespace."""
    text = text.lower().strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"\s+", " ", text)
    return text


def _clean_title(title: str) -> str:
    """Strip remaster/edit/version suffixes and feat. credits."""
    title = FEAT_RE.sub("", title)
    title = FEAT_INLINE_RE.sub("", title)
    title = PAREN_JUNK_RE.sub("", title)
    title = TITLE_JUNK_RE.sub("", title)
    return title.strip()


def _clean_artist(artist: str) -> str:
    """Normalize artist name for comparison."""
    artist = re.sub(r"\s*&\s*", " and ", artist)
    artist = FEAT_INLINE_RE.sub("", artist)
    artist = FEAT_RE.sub("", artist)
    return artist.strip()


def _score_match(track_a: dict, track_b: dict) -> float:
    """
    Score how similar two tracks are. Returns 0.0–1.0.
    Weights: title 40%, artist 35%, album 25%
    """
    a_title = _normalize_text(_clean_title(track_a["name"]))
    b_title = _normalize_text(_clean_title(track_b["name"]))
    title_score = fuzz.token_set_ratio(a_title, b_title) / 100.0

    a_artist = _normalize_text(_clean_artist(track_a["artist"]))
    b_artist = _normalize_text(_clean_artist(track_b["artist"]))
    artist_score = fuzz.token_set_ratio(a_artist, b_artist) / 100.0

    a_album = _normalize_text(_clean_title(track_a.get("album", "")))
    b_album = _normalize_text(_clean_title(track_b.get("album", "")))
    album_score = fuzz.token_set_ratio(a_album, b_album) / 100.0

    return (0.40 * title_score) + (0.35 * artist_score) + (0.25 * album_score)


# --- CSV reading ---

def _get_field(row: dict, *candidates: str) -> str:
    """Try multiple column name variants and return the first non-empty match."""
    for key in candidates:
        val = row.get(key, "").strip()
        if val:
            return val
    return ""


def _read_csv(path: Path) -> list[dict]:
    """
    Read a playlist CSV. Handles multiple column naming conventions:
    - Our Apple Music export: name, artist, album
    - Exportify: Track Name, Artist Name(s), Album Name
    """
    tracks = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tracks.append({
                "name": _get_field(row, "name", "Track Name", "track_name"),
                "artist": _get_field(row, "artist", "Artist Name(s)", "Artist Name", "artist_name"),
                "album": _get_field(row, "album", "Album Name", "album_name"),
                "isrc": _get_field(row, "isrc", "ISRC"),
            })
    return tracks


# --- Comparison ---

def compare_playlists(apple_tracks: list[dict], spotify_tracks: list[dict]) -> dict:
    """
    Compare two track lists and return matched/unmatched results.

    Strategy:
    1. ISRC exact match first (when both sides have ISRCs)
    2. Fuzzy match for the rest (>= 0.82 threshold)

    Returns dict with:
        matched: list of (apple_track, spotify_track, score)
        missing_from_spotify: list of apple tracks not found in spotify
        missing_from_apple: list of spotify tracks not found in apple
    """
    matched = []
    missing_from_spotify = []
    spotify_matched = set()

    # Build ISRC lookup for Spotify tracks
    spotify_by_isrc: dict[str, int] = {}
    for i, sp_track in enumerate(spotify_tracks):
        isrc = sp_track.get("isrc", "").strip()
        if isrc:
            spotify_by_isrc[isrc] = i

    # Pass 1: ISRC exact matches
    isrc_matched_apple = set()
    for ai, apple_track in enumerate(apple_tracks):
        isrc = apple_track.get("isrc", "").strip()
        if isrc and isrc in spotify_by_isrc:
            si = spotify_by_isrc[isrc]
            if si not in spotify_matched:
                matched.append((apple_track, spotify_tracks[si], 1.0))
                spotify_matched.add(si)
                isrc_matched_apple.add(ai)

    logger.info("ISRC pass: %d exact matches", len(isrc_matched_apple))

    # Pass 2: Fuzzy match for remaining tracks
    for ai, apple_track in enumerate(apple_tracks):
        if ai in isrc_matched_apple:
            continue

        best_score = 0.0
        best_idx = -1

        for i, sp_track in enumerate(spotify_tracks):
            if i in spotify_matched:
                continue
            score = _score_match(apple_track, sp_track)
            if score > best_score:
                best_score = score
                best_idx = i

        if best_score >= MATCH_THRESHOLD and best_idx >= 0:
            matched.append((apple_track, spotify_tracks[best_idx], best_score))
            spotify_matched.add(best_idx)
        else:
            missing_from_spotify.append(apple_track)

    # Anything in Spotify not matched to an Apple track
    missing_from_apple = [
        sp_track for i, sp_track in enumerate(spotify_tracks)
        if i not in spotify_matched
    ]

    return {
        "matched": matched,
        "missing_from_spotify": missing_from_spotify,
        "missing_from_apple": missing_from_apple,
    }


DJ_MIX_RE = re.compile(
    r"""
    \bDJ\s+Mix\b
    | \bMixed\b
    | \bBoiler\s+Room\b
    | \bLive\s+(?:at|from|in)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _is_dj_mix(track: dict) -> bool:
    """Check if a track is a DJ mix based on name or album."""
    text = f"{track.get('name', '')} {track.get('album', '')}"
    return bool(DJ_MIX_RE.search(text))


def write_report(results: dict, output_path: Path, playlist_name: str = ""):
    """Write a human-readable diff report."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    header = f"Playlist Comparison: {playlist_name}" if playlist_name else "Playlist Comparison Report"

    # Split missing-from-spotify into DJ mixes vs regular tracks
    add_regular = [t for t in results["missing_from_spotify"] if not _is_dj_mix(t)]
    add_dj_mix = [t for t in results["missing_from_spotify"] if _is_dj_mix(t)]

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"{header}\n")
        f.write(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"Match threshold: {MATCH_THRESHOLD}\n")
        f.write("=" * 60 + "\n\n")

        # Summary
        f.write(f"Matched:                {len(results['matched'])}\n")
        f.write(f"Add to Spotify:         {len(add_regular)}\n")
        f.write(f"Remove from Spotify:    {len(results['missing_from_apple'])}\n")
        if add_dj_mix:
            f.write(f"DJ Mixes (skip):        {len(add_dj_mix)}\n")
        f.write("\n")

        # Missing from Spotify — need to add
        if add_regular:
            f.write("=" * 60 + "\n")
            f.write("ADD TO SPOTIFY\n")
            f.write("=" * 60 + "\n\n")
            for i, track in enumerate(add_regular, 1):
                f.write(f"  {i}. {track['name']} — {track['artist']}\n")
                f.write(f"     Album: {track.get('album', 'N/A')}\n\n")

        # On Spotify but not Apple Music — need to remove
        if results["missing_from_apple"]:
            f.write("=" * 60 + "\n")
            f.write("REMOVE FROM SPOTIFY\n")
            f.write("=" * 60 + "\n\n")
            for i, track in enumerate(results["missing_from_apple"], 1):
                f.write(f"  {i}. {track['name']} — {track['artist']}\n")
                f.write(f"     Album: {track.get('album', 'N/A')}\n\n")

        # DJ Mixes — flagged separately, can't match on Spotify
        if add_dj_mix:
            f.write("=" * 60 + "\n")
            f.write("DJ MIXES / LIVE SETS (not available on Spotify)\n")
            f.write("=" * 60 + "\n\n")
            for i, track in enumerate(add_dj_mix, 1):
                f.write(f"  {i}. {track['name']} — {track['artist']}\n")
                f.write(f"     Album: {track.get('album', 'N/A')}\n\n")

        # Matched tracks
        f.write("=" * 60 + "\n")
        f.write(f"MATCHED ({len(results['matched'])} tracks)\n")
        f.write("=" * 60 + "\n\n")
        for apple, spotify, score in results["matched"]:
            f.write(f"  [{score:.2f}] {apple['name']} — {apple['artist']}\n")


def _slugify(name: str) -> str:
    """Convert a name to a safe filename slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "_", slug)
    return slug.strip("_")


def main():
    parser = argparse.ArgumentParser(
        description="Compare Apple Music and Spotify playlists"
    )
    parser.add_argument(
        "--fetch",
        metavar="APPLE_PLAYLIST_ID",
        help="Fetch Apple Music playlist live instead of reading a CSV",
    )
    parser.add_argument(
        "apple_csv",
        nargs="?",
        help="Path to Apple Music playlist CSV (not needed if --fetch is used)",
    )
    parser.add_argument(
        "spotify_csv",
        help="Path to Spotify playlist CSV export",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Output report path (default: reports/<playlist_name>_diff.txt)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Load Apple Music tracks
    playlist_name = ""
    if args.fetch:
        from apple_music import fetch_apple_playlist
        logger.info("Fetching Apple Music playlist %s...", args.fetch)
        apple_tracks, playlist_name = fetch_apple_playlist(args.fetch)
    elif args.apple_csv:
        apple_tracks = _read_csv(Path(args.apple_csv))
        # Derive playlist name from CSV filename
        playlist_name = Path(args.apple_csv).stem.replace("_", " ").title()
        logger.info("Loaded %d Apple Music tracks from %s", len(apple_tracks), args.apple_csv)
    else:
        parser.error("Either provide an Apple Music CSV or use --fetch <playlist_id>")
        return

    # Load Spotify tracks
    spotify_tracks = _read_csv(Path(args.spotify_csv))
    logger.info("Loaded %d Spotify tracks from %s", len(spotify_tracks), args.spotify_csv)

    # Compare
    logger.info("Comparing %d Apple Music tracks vs %d Spotify tracks...",
                len(apple_tracks), len(spotify_tracks))
    results = compare_playlists(apple_tracks, spotify_tracks)

    # Output
    if args.output:
        output_path = Path(args.output)
    else:
        slug = _slugify(playlist_name) if playlist_name else "playlist"
        output_path = Path(f"reports/{slug}_diff.txt")

    write_report(results, output_path, playlist_name=playlist_name)

    # Split DJ mixes from regular adds
    add_regular = [t for t in results["missing_from_spotify"] if not _is_dj_mix(t)]
    add_dj_mix = [t for t in results["missing_from_spotify"] if _is_dj_mix(t)]

    # Print summary to terminal
    if playlist_name:
        print(f"\n  Playlist: {playlist_name}")
    print(f"{'=' * 50}")
    print(f"  Matched:              {len(results['matched'])}")
    print(f"  Add to Spotify:       {len(add_regular)}")
    print(f"  Remove from Spotify:  {len(results['missing_from_apple'])}")
    if add_dj_mix:
        print(f"  DJ Mixes (skip):      {len(add_dj_mix)}")
    print(f"{'=' * 50}")
    print(f"\nFull report: {output_path}")

    if add_regular:
        print(f"\nADD TO SPOTIFY:")
        for track in add_regular:
            print(f"  + {track['name']} — {track['artist']}")

    if results["missing_from_apple"]:
        print(f"\nREMOVE FROM SPOTIFY:")
        for track in results["missing_from_apple"]:
            print(f"  - {track['name']} — {track['artist']}")

    if add_dj_mix:
        print(f"\nDJ MIXES / LIVE SETS (skip):")
        for track in add_dj_mix:
            print(f"  ~ {track['name']} — {track['artist']}")


if __name__ == "__main__":
    main()
