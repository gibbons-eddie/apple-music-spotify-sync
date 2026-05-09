"""One-time OAuth bootstrap for SPOTIFY_REFRESH_TOKEN. See ARCHITECTURE.md § Setup checklist."""

import os
import base64
import urllib.parse
from pathlib import Path

import requests

REDIRECT_URI = "https://127.0.0.1:8888/callback"
SCOPE = "playlist-modify-public playlist-modify-private playlist-read-private"


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


def main():
    _load_env()
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("ERROR: Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env first.")
        return

    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
    })

    print("\n" + "=" * 70)
    print("STEP 1: Open this URL in your browser and log in with YOUR Spotify account")
    print("(not your friend's — the refresh_token must be tied to YOUR account):")
    print("=" * 70)
    print(f"\n{auth_url}\n")
    print("STEP 2: After authorizing, your browser will try to load")
    print(f"        {REDIRECT_URI}?code=...")
    print("        It will show a security warning and fail to load — that's fine.")
    print("        Copy the ENTIRE URL from the address bar (including ?code=...).")
    print()

    redirect_url = input("STEP 3: Paste the full redirect URL here: ").strip()

    if "code=" not in redirect_url:
        print("ERROR: No 'code=' in URL. Make sure you copied the full redirect URL.")
        return
    code = redirect_url.split("code=")[1].split("&")[0]

    auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    response = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        headers={"Authorization": f"Basic {auth_header}"},
    )
    tokens = response.json()

    if "error" in tokens:
        print(f"\nERROR: {tokens['error']} — {tokens.get('error_description', '')}")
        return

    refresh_token = tokens["refresh_token"]
    print("\n" + "=" * 70)
    print("SUCCESS — your refresh_token (add to .env as SPOTIFY_REFRESH_TOKEN):")
    print("=" * 70)
    print(f"\n{refresh_token}\n")


if __name__ == "__main__":
    main()
