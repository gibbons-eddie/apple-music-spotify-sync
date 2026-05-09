"""Public Spotify playlist → CSV via anonymous embed-page token. See ARCHITECTURE.md."""

import csv
import re
import time
import json
import logging
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

SP_EMBED = "https://open.spotify.com/embed/playlist"
SP_API = "https://api.spotify.com/v1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

PLAYLIST_ID_RE = re.compile(
    r"(?:open\.spotify\.com/playlist/)?([A-Za-z0-9]{22})"
)


def _extract_playlist_id(input_str: str) -> str:
    match = PLAYLIST_ID_RE.search(input_str)
    if not match:
        raise ValueError(f"Could not extract Spotify playlist ID from: {input_str}")
    return match.group(1)


def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "_", slug)
    return slug.strip("_")


def _scrape_spotify_token(playlist_id: str) -> tuple[str, dict]:
    url = f"{SP_EMBED}/{playlist_id}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    nd_match = NEXT_DATA_RE.search(resp.text)
    if not nd_match:
        raise RuntimeError(
            "Could not find __NEXT_DATA__ in Spotify embed page. "
            "Spotify may have changed their embed format."
        )

    page_data = json.loads(nd_match.group(1))

    token_match = re.search(r'"accessToken":"([^"]+)"', resp.text)
    if not token_match:
        raise RuntimeError(
            "Could not find accessToken in Spotify embed page."
        )
    token = token_match.group(1)

    entity = (
        page_data.get("props", {})
        .get("pageProps", {})
        .get("state", {})
        .get("data", {})
        .get("entity", {})
    )

    logger.info(
        "Scraped Spotify token (length=%d) and embed data for '%s'",
        len(token), entity.get("name", "unknown"),
    )
    return token, entity


def _api_request(sess: requests.Session, url: str,
                 params: dict | None = None) -> requests.Response:
    for attempt in range(4):
        resp = sess.get(url, params=params, timeout=15)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
            if wait > 60:
                raise RuntimeError(
                    f"Spotify rate limit too long ({wait}s). "
                    "Try again later or use --embed-only mode."
                )
            logger.warning("Rate limited, waiting %ds (attempt %d)", wait, attempt + 1)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp

    raise RuntimeError("Spotify API request failed after 4 retries")


def fetch_spotify_playlist(playlist_id_or_url: str,
                           embed_only: bool = False) -> tuple[list[dict], str]:
    playlist_id = _extract_playlist_id(playlist_id_or_url)

    token, entity = _scrape_spotify_token(playlist_id)
    playlist_name = entity.get("name", playlist_id)

    if embed_only:
        tracks = []
        for t in entity.get("trackList", []):
            track_uri = t.get("uri", "")
            track_id = track_uri.split(":")[-1] if track_uri else ""
            tracks.append({
                "name": t.get("title", ""),
                "artist": t.get("subtitle", ""),
                "album": "",
                "duration_ms": t.get("duration", 0),
                "isrc": "",
                "spotify_id": track_id,
                "spotify_url": f"https://open.spotify.com/track/{track_id}" if track_id else "",
            })
        logger.info(
            "Embed-only: got %d tracks from '%s' (no ISRC/album data)",
            len(tracks), playlist_name,
        )
        return tracks, playlist_name

    sess = requests.Session()
    sess.headers.update({
        **HEADERS,
        "Authorization": f"Bearer {token}",
    })

    tracks = []
    url = f"{SP_API}/playlists/{playlist_id}/tracks"
    params = {"limit": 100, "offset": 0}

    while True:
        resp = _api_request(sess, url, params=params)
        data = resp.json()

        for item in data.get("items", []):
            track = item.get("track")
            if not track or track.get("type") != "track":
                continue

            artists = ", ".join(a["name"] for a in track.get("artists", []))
            album = track.get("album", {}).get("name", "")
            isrc = track.get("external_ids", {}).get("isrc", "")
            track_id = track.get("id", "")

            tracks.append({
                "name": track.get("name", ""),
                "artist": artists,
                "album": album,
                "duration_ms": track.get("duration_ms", 0),
                "isrc": isrc,
                "spotify_id": track_id,
                "spotify_url": track.get("external_urls", {}).get("spotify", ""),
            })

        next_url = data.get("next")
        if next_url:
            url = next_url
            params = None
            time.sleep(1.0)
        else:
            break

    logger.info(
        "Fetched %d tracks from Spotify playlist '%s' (%s)",
        len(tracks), playlist_name, playlist_id,
    )
    return tracks, playlist_name


def export_to_csv(tracks: list[dict], output_path: str | Path) -> Path:
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
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Export a public Spotify playlist to CSV"
    )
    parser.add_argument(
        "playlist", help="Spotify playlist ID or URL",
    )
    parser.add_argument(
        "output", nargs="?", default=None,
        help="Output CSV path (default: exports/SP_{name}.csv)",
    )
    parser.add_argument(
        "--embed-only", action="store_true",
        help="Use embed data only (no API calls, but no ISRC/album data)",
    )
    args = parser.parse_args()

    tracks, playlist_name = fetch_spotify_playlist(
        args.playlist, embed_only=args.embed_only
    )

    if args.output:
        output_file = args.output
    else:
        output_file = f"exports/SP_{_slugify(playlist_name)}.csv"

    path = export_to_csv(tracks, output_file)
    print(f"\nExported {len(tracks)} tracks from '{playlist_name}' to {path}")
