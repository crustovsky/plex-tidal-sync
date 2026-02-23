#!/usr/bin/env python3
"""
Sync Plex music library albums and artists to Tidal favorites.

On first run, you will be asked to authenticate with both Tidal and Plex via
browser links. All credentials are saved to:
  ~/.config/plex-tidal-sync/config.json

Optional environment variable overrides:
  PLEX_URL            Plex server URL (default: http://localhost:32400)
  PLEX_TOKEN          Plex token (skips PIN OAuth if set)
  PLEX_MUSIC_SECTION  Name of the Plex music library section (default: Music)
  MUSIC_ROOT          Root path of the music library (skips interactive prompt if set)

Usage:
  uv run python sync.py [--dry-run] [--no-albums] [--no-artists] [--debug]
"""

import argparse
import datetime
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import tidalapi
from plexapi.exceptions import Unauthorized
from plexapi.myplex import MyPlexPinLogin
from plexapi.server import PlexServer

# --- Static config (non-credential) ---
PLEX_MUSIC_SECTION = os.environ.get("PLEX_MUSIC_SECTION", "Music")
CONFIG_FILE = Path(__file__).parent / "config.json"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


# --- Config file (stores both Plex credentials and Tidal tokens) ---

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def save_config(config: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


# --- String matching ---

def normalize(s: str) -> str:
    """Lowercase and strip punctuation for fuzzy comparison."""
    return re.sub(r"[^\w\s]", "", s.lower()).strip()


def names_match(a: str, b: str) -> bool:
    """True if two names are similar (case-insensitive, partial match acceptable)."""
    a_n, b_n = normalize(a), normalize(b)
    return a_n in b_n or b_n in a_n


# --- Plex auth ---

def _plex_pin_login(config: dict) -> tuple[str, dict]:
    """Run Plex OAuth flow, save and return the token."""
    log.info("Starting Plex OAuth login...")
    pinlogin = MyPlexPinLogin(oauth=True)
    pinlogin.run(timeout=300)
    print(f"Visit {pinlogin.oauthUrl()} to log in to Plex")
    pinlogin.waitForLogin()
    token = pinlogin.token
    config.setdefault("plex", {})
    config["plex"]["token"] = token
    save_config(config)
    log.info("Plex token saved.")
    return token, config


def get_plex_connection(config: dict) -> tuple[PlexServer, dict]:
    """
    Return an authenticated PlexServer.
    - Token: restored from config or obtained via PIN OAuth (like Tidal).
    - URL: restored from config or prompted once; defaults to localhost.
    - Re-authenticates automatically if the token is rejected.
    """
    token = os.environ.get("PLEX_TOKEN") or config.get("plex", {}).get("token", "")
    url = os.environ.get("PLEX_URL") or config.get("plex", {}).get("url", "")

    if not token:
        token, config = _plex_pin_login(config)

    if not url:
        url = input("Plex server URL [http://localhost:32400]: ").strip() or "http://localhost:32400"

    while True:
        try:
            plex = PlexServer(url, token)
            config.setdefault("plex", {})
            config["plex"]["url"] = url
            save_config(config)
            log.info(f"Connected to Plex at {url}.")
            return plex, config
        except Unauthorized:
            log.error("Plex token rejected — re-authenticating...")
            config.get("plex", {}).pop("token", None)
            token, config = _plex_pin_login(config)
        except Exception as e:
            log.error(f"Plex connection failed: {e}")
            url = input(f"Plex server URL [{url}]: ").strip() or url


# --- Tidal auth ---

def get_tidal_session(config: dict) -> tuple[tidalapi.Session, dict]:
    """
    Return an authenticated Tidal session, restoring from config or running OAuth.
    Re-runs OAuth if the saved session is invalid.
    """
    session = tidalapi.Session()
    tidal_cfg = config.get("tidal", {})

    if tidal_cfg:
        try:
            expiry: Optional[datetime.datetime] = None
            if tidal_cfg.get("expiry_time"):
                expiry = datetime.datetime.fromisoformat(tidal_cfg["expiry_time"])
            ok = session.load_oauth_session(
                token_type=tidal_cfg["token_type"],
                access_token=tidal_cfg["access_token"],
                refresh_token=tidal_cfg.get("refresh_token"),
                expiry_time=expiry,
            )
            if ok:
                log.info("Restored existing Tidal session.")
                return session, config
        except Exception as e:
            log.warning(f"Could not restore Tidal session: {e}")

    log.info("No valid Tidal session — starting OAuth login...")
    session.login_oauth_simple()
    config["tidal"] = {
        "token_type": session.token_type,
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "expiry_time": session.expiry_time.isoformat() if session.expiry_time else None,
    }
    save_config(config)
    log.info(f"Tidal session saved to {CONFIG_FILE}")
    return session, config


# --- Music root ---

def get_music_root(config: dict) -> tuple[Path, dict]:
    """Get the music library root path, prompting if missing or unreachable."""
    root_str = os.environ.get("MUSIC_ROOT") or config.get("music_root", "")

    while True:
        if not root_str:
            root_str = input("Music library path: ").strip()

        path = Path(root_str).expanduser()
        if path.is_dir():
            config["music_root"] = str(path)
            save_config(config)
            return path, config

        log.error(f"Path not found or not accessible: {path}")
        root_str = ""


# --- Tidal favorites helpers ---

def _paginate(fetch_fn, limit: int = 50) -> set[int]:
    """Collect all IDs from a paginated favorites fetch function."""
    ids: set[int] = set()
    offset = 0
    while True:
        batch = fetch_fn(limit=limit, offset=offset)
        if not batch:
            break
        for item in batch:
            ids.add(item.id)
        if len(batch) < limit:
            break
        offset += limit
    return ids


# --- Filesystem helpers ---

def fs_artist_album(track_path: str, music_root: Path) -> Optional[tuple[str, str]]:
    """
    Parse artist and album from a track's filesystem path.
    Expects structure: music_root/Artist/Album/[disc/]track
    Returns the first two path components after music_root.
    """
    try:
        rel = Path(track_path).relative_to(music_root)
        parts = rel.parts
        if len(parts) >= 2:
            return parts[0], parts[1]
    except ValueError:
        pass
    return None


# --- Album sync ---

def get_plex_albums(plex: PlexServer, music_root: Path) -> list[dict]:
    """Return all albums from Plex with metadata and parsed filesystem info."""
    section = plex.library.section(PLEX_MUSIC_SECTION)
    result = []
    for album in section.all(libtype="album"):
        plex_artist = album.parentTitle
        plex_album = album.title
        fs_artist = fs_album = None
        try:
            tracks = album.tracks()
            if tracks:
                file_path = tracks[0].media[0].parts[0].file
                parsed = fs_artist_album(file_path, music_root)
                if parsed:
                    fs_artist, fs_album = parsed
        except Exception:
            pass
        result.append({
            "plex_artist": plex_artist,
            "plex_album": plex_album,
            "fs_artist": fs_artist,
            "fs_album": fs_album,
        })
    return result


def find_tidal_album(
    session: tidalapi.Session, artist: str, album: str
) -> Optional[tidalapi.Album]:
    """Search Tidal and return the best matching Album, or None."""
    query = f"{artist} {album}"
    try:
        results = session.search(query, models=[tidalapi.Album], limit=5)
        for tidal_album in results.get("albums", []):
            tidal_artist = tidal_album.artist.name if tidal_album.artist else ""
            if names_match(album, tidal_album.name) and names_match(artist, tidal_artist):
                return tidal_album
    except Exception as e:
        log.debug(f"Tidal search error for '{query}': {e}")
    return None


def sync_albums(
    tidal: tidalapi.Session,
    favorites: tidalapi.Favorites,
    plex: PlexServer,
    music_root: Path,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Sync Plex albums to Tidal favorites. Returns (added, already_have, not_found)."""
    existing_ids = _paginate(favorites.albums)
    log.info(f"  {len(existing_ids)} albums already in Tidal favorites.")

    albums = get_plex_albums(plex, music_root)
    log.info(f"  {len(albums)} albums found in Plex.")

    added = already_have = not_found = 0

    for entry in albums:
        plex_artist = entry["plex_artist"]
        plex_album = entry["plex_album"]
        fs_artist = entry["fs_artist"]
        fs_album = entry["fs_album"]

        # Pick the best names to search with, based on what we have:
        #   HIGH:   both sources agree    → use Plex names (may be cleaner)
        #   MEDIUM: sources disagree      → trust FS (more reliable per CLAUDE.md)
        #   MEDIUM: FS parse failed       → fall back to Plex names, log warning
        if fs_artist and fs_album:
            if names_match(plex_artist, fs_artist) and names_match(plex_album, fs_album):
                search_artist, search_album = plex_artist, plex_album
                confidence = "high"
            else:
                search_artist, search_album = fs_artist, fs_album
                confidence = "medium"
                log.warning(
                    f"MEDIUM CONFIDENCE: Plex='{plex_artist}/{plex_album}' "
                    f"disagrees with FS='{fs_artist}/{fs_album}' — using FS names"
                )
        else:
            search_artist, search_album = plex_artist, plex_album
            confidence = "medium"
            log.warning(
                f"MEDIUM CONFIDENCE: Could not parse FS path for "
                f"'{plex_artist}/{plex_album}' — using Plex names only"
            )

        match = find_tidal_album(tidal, search_artist, search_album)
        if match is None:
            log.info(f"NOT FOUND:  {search_artist} — {search_album}"
                     + (" [medium confidence]" if confidence == "medium" else ""))
            not_found += 1
            continue

        if match.id in existing_ids:
            log.debug(f"Already in favorites: {search_artist} — {search_album}")
            already_have += 1
            continue

        tidal_label = f"{match.artist.name if match.artist else '?'} — {match.name}"
        local_label = f"{search_artist} — {search_album}"
        display = local_label
        if normalize(local_label) != normalize(tidal_label):
            display += f"  [Tidal: {tidal_label}]"
        if confidence == "medium":
            display += "  [medium confidence]"

        if dry_run:
            log.info(f"[DRY RUN] Would add album: {display}")
        else:
            if favorites.add_album(str(match.id)):
                log.info(f"ADDED:      {display}")
                existing_ids.add(match.id)
            else:
                log.error(f"Failed to add album: {display}")
                continue

        added += 1
        time.sleep(0.2)

    return added, already_have, not_found


# --- Artist sync ---

def find_tidal_artist(
    session: tidalapi.Session, name: str
) -> Optional[tidalapi.Artist]:
    """Search Tidal and return the best matching Artist, or None."""
    try:
        results = session.search(name, models=[tidalapi.Artist], limit=5)
        for artist in results.get("artists", []):
            if names_match(name, artist.name):
                return artist
    except Exception as e:
        log.debug(f"Tidal search error for artist '{name}': {e}")
    return None


def sync_artists(
    tidal: tidalapi.Session,
    favorites: tidalapi.Favorites,
    plex: PlexServer,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Sync Plex artists to Tidal favorites. Returns (added, already_have, not_found)."""
    existing_ids = _paginate(favorites.artists)
    log.info(f"  {len(existing_ids)} artists already in Tidal favorites.")

    section = plex.library.section(PLEX_MUSIC_SECTION)
    plex_artists = section.all(libtype="artist")
    log.info(f"  {len(plex_artists)} artists found in Plex.")

    added = already_have = not_found = 0

    for plex_artist in plex_artists:
        name = plex_artist.title

        match = find_tidal_artist(tidal, name)
        if match is None:
            log.info(f"NOT FOUND:  {name}")
            not_found += 1
            continue

        if match.id in existing_ids:
            log.debug(f"Already in favorites: {name}")
            already_have += 1
            continue

        display = name
        if normalize(name) != normalize(match.name):
            display += f"  [Tidal: {match.name}]"

        if dry_run:
            log.info(f"[DRY RUN] Would add artist: {display}")
        else:
            if favorites.add_artist(str(match.id)):
                log.info(f"ADDED:      {display}")
                existing_ids.add(match.id)
            else:
                log.error(f"Failed to add artist: {display}")
                continue

        added += 1
        time.sleep(0.2)

    return added, already_have, not_found


# --- Main ---

def sync(dry_run: bool, do_albums: bool, do_artists: bool) -> None:
    config = load_config()

    tidal, config = get_tidal_session(config)
    plex, config = get_plex_connection(config)
    music_root, config = get_music_root(config)
    favorites = tidalapi.Favorites(tidal, tidal.user.id)

    if do_albums:
        log.info("\n--- Albums ---")
        al_added, al_have, al_missing = sync_albums(tidal, favorites, plex, music_root, dry_run)
        print(
            f"Albums  — added: {al_added}  |  already in favorites: {al_have}"
            f"  |  not found: {al_missing}"
        )

    if do_artists:
        log.info("\n--- Artists ---")
        ar_added, ar_have, ar_missing = sync_artists(tidal, favorites, plex, dry_run)
        print(
            f"Artists — added: {ar_added}  |  already in favorites: {ar_have}"
            f"  |  not found: {ar_missing}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync Plex music library albums and artists to Tidal favorites."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be added without modifying Tidal.",
    )
    parser.add_argument(
        "--no-albums", action="store_true", help="Skip album sync."
    )
    parser.add_argument(
        "--no-artists", action="store_true", help="Skip artist sync."
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging.")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    sync(
        dry_run=args.dry_run,
        do_albums=not args.no_albums,
        do_artists=not args.no_artists,
    )


if __name__ == "__main__":
    main()
