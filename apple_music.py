"""Fetch all tracks from a public Apple Music playlist via amp-api and export to CSV."""

import csv
import re
import time
import logging
from pathlib import Path
import requests

logger = logging.getLogger(__name__)

AM_BASE = "https://amp-api.music.apple.com"
AM_WEB = "https://music.apple.com"
JS_FILE_RE = re.compile(r"/assets/index-legacy[~\-][^/]+\.js")
TOKEN_RE = re.compile(r'eyJh[^"]+')

HEADERS_TEMPLATE = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Origin": "https://music.apple.com",
}


def _scrape_apple_token() -> str:
    """Scrape Apple's privileged JWT from their web player JS bundle."""
    sess = requests.Session()
    sess.headers.update(HEADERS_TEMPLATE)

    # Fetch the main page to find the JS bundle URL
    resp = sess.get(AM_WEB)
    resp.raise_for_status()
    match = JS_FILE_RE.search(resp.text)
    if not match:
        raise RuntimeError(
            "Could not find index-legacy JS bundle in music.apple.com HTML. "
            "Apple may have changed their bundle naming."
        )
    js_url = AM_WEB + match.group(0)

    # Fetch the JS bundle and extract the JWT
    resp = sess.get(js_url)
    resp.raise_for_status()
    match = TOKEN_RE.search(resp.text)
    if not match:
        raise RuntimeError(
            "Could not find JWT in JS bundle. Apple may have changed token embedding."
        )
    token = match.group(0)
    logger.info("Scraped Apple Music token (length=%d, prefix=%s...)", len(token), token[:20])
    return token


def _slugify(name: str) -> str:
    """Convert a playlist name to a safe filename slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)   # strip non-alphanumeric
    slug = re.sub(r"[\s_-]+", "_", slug)   # collapse whitespace/dashes to underscore
    return slug.strip("_")


def fetch_apple_playlist(playlist_id: str) -> tuple[list[dict], str]:
    """
    Fetch all tracks from a public Apple Music playlist.

    Args:
        playlist_id: e.g. "pl.u-EdveCXbK937"

    Returns:
        Tuple of (tracks, playlist_name).
        Tracks: list of dicts with keys: name, artist, album, duration_ms, isrc, apple_id, apple_url
        Tracks are in playlist order.
    """
    token = _scrape_apple_token()
    sess = requests.Session()
    sess.headers.update({
        **HEADERS_TEMPLATE,
        "Authorization": f"Bearer {token}",
    })

    # Fetch playlist metadata to get the name
    meta_resp = sess.get(f"{AM_BASE}/v1/catalog/us/playlists/{playlist_id}")
    meta_resp.raise_for_status()
    playlist_name = meta_resp.json()["data"][0]["attributes"].get("name", playlist_id)
    logger.info("Playlist name: %s", playlist_name)

    tracks = []
    url = f"{AM_BASE}/v1/catalog/us/playlists/{playlist_id}/tracks"
    params = {"limit": 100, "offset": 0}

    while True:
        resp = sess.get(url, params=params)
        if resp.status_code == 401:
            raise RuntimeError("Apple Music token rejected (401). Token may have expired.")
        if resp.status_code == 404:
            raise RuntimeError(f"Playlist {playlist_id} not found (404). Check the ID and storefront.")
        resp.raise_for_status()

        data = resp.json()
        for item in data.get("data", []):
            attrs = item.get("attributes", {})
            tracks.append({
                "name": attrs.get("name", ""),
                "artist": attrs.get("artistName", ""),
                "album": attrs.get("albumName", ""),
                "duration_ms": attrs.get("durationInMillis", 0),
                "isrc": attrs.get("isrc", ""),
                "apple_id": item.get("id", ""),
                "apple_url": attrs.get("url", ""),
            })

        # Handle pagination
        next_url = data.get("next")
        if next_url:
            url = AM_BASE + next_url
            params = {}  # params are embedded in the next URL
            time.sleep(0.3)
        else:
            break

    logger.info("Fetched %d tracks from Apple Music playlist %s (%s)", len(tracks), playlist_name, playlist_id)
    return tracks, playlist_name


def export_to_csv(tracks: list[dict], output_path: str | Path) -> Path:
    """
    Export Apple Music tracks to CSV.

    CSV columns: name, artist, album, duration_ms, isrc
    This format is used by compare.py to diff against a Spotify export.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "artist", "album", "duration_ms", "isrc"])
        writer.writeheader()
        for track in tracks:
            writer.writerow({
                "name": track["name"],
                "artist": track["artist"],
                "album": track["album"],
                "duration_ms": track["duration_ms"],
                "isrc": track["isrc"],
            })

    logger.info("Exported %d tracks to %s", len(tracks), output_path)
    return output_path


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if len(sys.argv) < 2:
        print("Usage: python apple_music.py <playlist_id> [output.csv]")
        print("  playlist_id: the 'pl.u-XXXXX' from your Apple Music playlist URL")
        sys.exit(1)

    playlist_id = sys.argv[1]
    tracks, playlist_name = fetch_apple_playlist(playlist_id)

    if len(sys.argv) > 2:
        output_file = sys.argv[2]
    else:
        output_file = f"exports/AM_{_slugify(playlist_name)}.csv"

    path = export_to_csv(tracks, output_file)
    print(f"\nExported {len(tracks)} tracks from '{playlist_name}' to {path}")
