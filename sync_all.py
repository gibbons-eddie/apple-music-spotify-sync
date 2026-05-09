"""Comparison driver: fetch both sides + diff per playlist pair. See ARCHITECTURE.md."""

import json
import logging
import argparse
from pathlib import Path

from apple_music import fetch_apple_playlist, export_to_csv as am_export, _slugify
from spotify_export import fetch_spotify_playlist, export_to_csv as sp_export
from compare import _read_csv, compare_playlists, write_report, _is_dj_mix

CONFIG_FILE = Path("playlists.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Compare all configured playlist pairs")
    parser.add_argument(
        "--cached", action="store_true",
        help="Use existing CSVs instead of fetching live",
    )
    parser.add_argument(
        "--embed-only", action="store_true",
        help="Spotify: use embed data only (no ISRC/album, but avoids API rate limits)",
    )
    args = parser.parse_args()

    pairs = json.loads(CONFIG_FILE.read_text())
    logger.info("Loaded %d playlist pairs from %s", len(pairs), CONFIG_FILE)

    for pair in pairs:
        name = pair["name"]
        apple_id = pair["apple_id"]
        spotify_id = pair.get("spotify_id")
        spotify_csv = Path(pair["spotify_csv"])
        slug = _slugify(name)
        apple_csv = Path(f"exports/AM_{slug}.csv")

        print(f"\n{'=' * 60}")
        print(f"  {name}")
        print(f"{'=' * 60}")

        if args.cached and apple_csv.exists():
            logger.info("Using cached Apple Music CSV: %s", apple_csv)
            apple_tracks = _read_csv(apple_csv)
            playlist_name = name
        else:
            logger.info("Fetching Apple Music playlist %s...", apple_id)
            apple_tracks, playlist_name = fetch_apple_playlist(apple_id)
            am_export(apple_tracks, apple_csv)

        if args.cached and spotify_csv.exists():
            logger.info("Using cached Spotify CSV: %s", spotify_csv)
            spotify_tracks = _read_csv(spotify_csv)
        elif spotify_id:
            logger.info("Fetching Spotify playlist %s...", spotify_id)
            spotify_tracks, _ = fetch_spotify_playlist(
                spotify_id, embed_only=args.embed_only
            )
            sp_export(spotify_tracks, spotify_csv)
        elif spotify_csv.exists():
            logger.info("No spotify_id configured, using CSV: %s", spotify_csv)
            spotify_tracks = _read_csv(spotify_csv)
        else:
            logger.warning("No spotify_id and no CSV found: %s — skipping", spotify_csv)
            continue

        logger.info("Loaded %d Spotify tracks", len(spotify_tracks))

        results = compare_playlists(apple_tracks, spotify_tracks)
        report_path = Path(f"reports/{slug}_diff.txt")
        write_report(results, report_path, playlist_name=playlist_name)

        add_regular = [t for t in results["missing_from_spotify"] if not _is_dj_mix(t)]
        add_dj_mix = [t for t in results["missing_from_spotify"] if _is_dj_mix(t)]
        print(f"\n  Matched:              {len(results['matched'])}")
        print(f"  Add to Spotify:       {len(add_regular)}")
        print(f"  Remove from Spotify:  {len(results['missing_from_apple'])}")
        if add_dj_mix:
            print(f"  DJ Mixes (skip):      {len(add_dj_mix)}")
        print(f"  Report: {report_path}")

    print(f"\n{'=' * 60}")
    print(f"  All comparisons complete.")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
