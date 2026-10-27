"""Apple Music catalog search and library read/write for the Spotify → Apple import.

Uses the same amp-api host and scraped web-player token as apple_music.py.
Library calls also send the Music User Token copied from a signed-in
music.apple.com browser session (the `media-user-token` cookie), read from the
APPLE_MUSIC_USER_TOKEN environment variable. That token grants full access to
the library, so it is never logged.

Apple's public API can create a library playlist and append songs to it, but
cannot remove or reorder songs; see spotify_to_apple.py for how that's handled.
"""

import os
import time
import logging

import requests

from apple_music import AM_BASE, HEADERS_TEMPLATE, _scrape_apple_token

logger = logging.getLogger(__name__)

STOREFRONT = "us"
USER_TOKEN_ENV = "APPLE_MUSIC_USER_TOKEN"
USER_TOKEN_HELP = (
    "Refresh it: sign in at music.apple.com, open developer tools → Application "
    "(Storage in Safari) → Cookies → https://music.apple.com, copy the value of "
    f"`media-user-token`, and update the {USER_TOKEN_ENV} secret (and .env locally)."
)
# Apple accepts up to 100 items per playlist-tracks request.
BATCH_SIZE = 100


class AppleAuthError(RuntimeError):
    """The scraped developer token or the Music User Token was rejected."""


def _load_env():
    # Reuse spotify_match's .env loader so local runs read APPLE_MUSIC_USER_TOKEN too.
    from spotify_match import _load_env as load
    load()


class AppleClient:
    def __init__(self, user_token: str | None = None):
        _load_env()
        raw = user_token if user_token is not None else os.environ.get(USER_TOKEN_ENV, "")
        # Values copied from the browser's Local Storage view can carry quotes.
        self.user_token = raw.strip().strip('"').strip("'").strip()
        self.sess = requests.Session()
        self.sess.headers.update({
            **HEADERS_TEMPLATE,
            "Authorization": f"Bearer {_scrape_apple_token()}",
        })

    @property
    def has_library_access(self) -> bool:
        return bool(self.user_token)

    def _request(self, method: str, path: str, *, library: bool = False, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else AM_BASE + path
        headers = kwargs.pop("headers", {})
        if library:
            if not self.user_token:
                raise AppleAuthError(f"{USER_TOKEN_ENV} is not set. {USER_TOKEN_HELP}")
            # The official Apple Music API reads Music-User-Token; the web player's
            # amp-api host, which this client uses, reads media-user-token.
            headers["Music-User-Token"] = self.user_token
            headers["Media-User-Token"] = self.user_token
        for attempt in range(4):
            resp = self.sess.request(method, url, headers=headers, timeout=20, **kwargs)
            if resp.status_code == 429:
                wait = min(int(resp.headers.get("Retry-After", 2 ** (attempt + 1))), 30)
                logger.warning("Apple rate limit, waiting %ds", wait)
                time.sleep(wait)
                continue
            if resp.status_code in (401, 403):
                if library:
                    cause = "the Music User Token was rejected"
                    if resp.status_code == 403 and method != "GET":
                        cause += " (or the playlist isn't editable)"
                    hint = USER_TOKEN_HELP
                else:
                    cause = "the scraped web-player token was rejected"
                    hint = "Apple may have changed the web player."
                raise AppleAuthError(f"Apple returned {resp.status_code} for {method} {path}: {cause}. {hint}")
            resp.raise_for_status()
            return resp
        raise RuntimeError(f"Apple rate limit persisted for {method} {path}")

    def _paged(self, path: str, *, library: bool = False, params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        url, query = path, {"limit": 100, **(params or {})}
        while url:
            data = self._request("GET", url, library=library, params=query).json()
            items.extend(data.get("data", []))
            url, query = data.get("next"), None
        return items

    # --- catalog (no user token needed) ---

    def songs_by_isrc(self, isrc: str) -> list[dict]:
        data = self._request(
            "GET", f"/v1/catalog/{STOREFRONT}/songs", params={"filter[isrc]": isrc},
        ).json()
        return data.get("data", [])

    def search_songs(self, term: str, limit: int = 10) -> list[dict]:
        data = self._request(
            "GET", f"/v1/catalog/{STOREFRONT}/search",
            params={"term": term, "types": "songs", "limit": limit},
        ).json()
        return data.get("results", {}).get("songs", {}).get("data", [])

    # --- library (user token needed) ---

    def library_playlists(self) -> list[dict]:
        return self._paged("/v1/me/library/playlists", library=True)

    def library_playlist(self, playlist_id: str) -> dict | None:
        try:
            data = self._request("GET", f"/v1/me/library/playlists/{playlist_id}", library=True).json()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise
        rows = data.get("data", [])
        return rows[0] if rows else None

    def library_playlist_tracks(self, playlist_id: str) -> list[dict]:
        try:
            return self._paged(f"/v1/me/library/playlists/{playlist_id}/tracks", library=True)
        except requests.HTTPError as e:
            # An empty playlist answers 404 on its tracks.
            if e.response is not None and e.response.status_code == 404:
                return []
            raise

    def create_library_playlist(self, name: str, description: str, song_ids: list[str]) -> str:
        first, rest = song_ids[:BATCH_SIZE], song_ids[BATCH_SIZE:]
        body = {
            "attributes": {"name": name, "description": description},
            "relationships": {"tracks": {"data": [{"id": s, "type": "songs"} for s in first]}},
        }
        data = self._request("POST", "/v1/me/library/playlists", library=True, json=body).json()
        playlist_id = data["data"][0]["id"]
        if rest:
            self.append_tracks(playlist_id, rest)
        return playlist_id

    def append_tracks(self, playlist_id: str, song_ids: list[str]):
        for i in range(0, len(song_ids), BATCH_SIZE):
            body = {"data": [{"id": s, "type": "songs"} for s in song_ids[i:i + BATCH_SIZE]]}
            self._request(
                "POST", f"/v1/me/library/playlists/{playlist_id}/tracks", library=True, json=body,
            )


def song_to_plain(song: dict) -> dict:
    """Apple catalog song → the plain shape spotify_match's scorers compare."""
    a = song.get("attributes", {})
    return {
        "name": a.get("name", ""),
        "artist": a.get("artistName", ""),
        "album": a.get("albumName", ""),
        "duration_ms": a.get("durationInMillis", 0) or 0,
        "isrc": a.get("isrc", ""),
        # Song attributes carry no album type or album artist; compilation
        # detection falls back to the album-name patterns.
        "album_type": "",
        "album_artists": [],
        "release_date": a.get("releaseDate", ""),
    }


def library_track_catalog_id(track: dict) -> str:
    """Catalog song ID behind a library playlist entry ('' for uploads)."""
    return str(track.get("attributes", {}).get("playParams", {}).get("catalogId", "") or "")


def library_track_plain(track: dict) -> dict:
    a = track.get("attributes", {})
    return {"name": a.get("name", ""), "artist": a.get("artistName", "")}
