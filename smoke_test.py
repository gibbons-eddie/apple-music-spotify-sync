"""Pre-sync health check for both upstreams. Runs before anything modifies state.

Probes:
  1. Apple Music — scrape the web token and fetch the Essentials playlist's
     metadata (the same amp-api call fetch_apple_playlist makes first).
  2. Spotify — refresh the access token, run one trivial search, and read the
     Essentials playlist. A 401/403 here means the Developer Dashboard
     registration (User Management) or the refresh token is broken.

Exits non-zero with a message naming the failing probe, so an upstream break
shows up as a clear CI failure instead of a traceback mid-sync.
"""

import argparse
import json
import sys
from pathlib import Path

import requests
import spotipy

from apple_music import AM_BASE, HEADERS_TEMPLATE, _scrape_apple_token
from spotify_match import build_client

PLAYLISTS_FILE = Path(__file__).parent / "playlists.json"
PLAYLIST_NAME = "Eddie Gibbons Essentials"


def _playlist() -> dict:
    playlists = json.loads(PLAYLISTS_FILE.read_text())
    return next(p for p in playlists if p["name"] == PLAYLIST_NAME)


def probe_apple(apple_id: str) -> str:
    token = _scrape_apple_token()
    resp = requests.get(
        f"{AM_BASE}/v1/catalog/us/playlists/{apple_id}",
        headers={**HEADERS_TEMPLATE, "Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    name = resp.json()["data"][0]["attributes"].get("name", apple_id)
    return f"token scraped, playlist metadata OK ({name})"


def probe_spotify(spotify_id: str) -> str:
    sp = build_client()
    sp.search(q="test", type="track", limit=1)
    info = sp.playlist(spotify_id, fields="name")
    return f"token refreshed, search OK, playlist read OK ({info.get('name', spotify_id)})"


def probe_apple_library() -> str:
    from apple_client import AppleClient

    client = AppleClient()
    if not client.has_library_access:
        raise RuntimeError("APPLE_MUSIC_USER_TOKEN is not set.")
    client._request("GET", "/v1/me/library/playlists", library=True, params={"limit": 1})
    return "Music User Token accepted, library readable"


def _explain(exc: Exception) -> str:
    if isinstance(exc, spotipy.SpotifyException) and exc.http_status in (401, 403):
        return (
            f"HTTP {exc.http_status}: {exc.msg}\n"
            "    Check https://developer.spotify.com/dashboard → User Management "
            "(the account must be listed while the app is in Development Mode) "
            "and the SPOTIFY_REFRESH_TOKEN secret."
        )
    from apple_client import USER_TOKEN_HELP, AppleAuthError
    if isinstance(exc, AppleAuthError):
        return str(exc)
    if isinstance(exc, RuntimeError) and "APPLE_MUSIC_USER_TOKEN" in str(exc):
        return f"{exc} {USER_TOKEN_HELP}"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code} from {exc.response.url}"
    return f"{type(exc).__name__}: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-run health check")
    parser.add_argument(
        "--import", dest="import_mode", action="store_true",
        help="Checks for the Spotify → Apple import: Apple catalog, plus the Apple library "
             "when --library is given (no Spotify API credentials needed)",
    )
    parser.add_argument("--library", action="store_true", help="With --import: require Apple library access")
    args = parser.parse_args()

    entry = _playlist()
    if args.import_mode:
        probes = [("Apple Music catalog", lambda: probe_apple(entry["apple_id"]))]
        if args.library:
            probes.append(("Apple Music library", probe_apple_library))
    else:
        probes = [
            ("Apple Music", lambda: probe_apple(entry["apple_id"])),
            ("Spotify", lambda: probe_spotify(entry["spotify_id"])),
        ]

    failed = []
    for label, probe in probes:
        try:
            print(f"[ OK ] {label}: {probe()}")
        except Exception as e:
            print(f"[FAIL] {label}: {_explain(e)}", file=sys.stderr)
            failed.append(label)

    if failed:
        print(
            f"\nSmoke test failed ({', '.join(failed)}). Aborting before any sync "
            "state is modified.",
            file=sys.stderr,
        )
        return 1
    print("\nSmoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
