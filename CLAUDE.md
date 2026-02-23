# plex-tidal-sync

Sync a local Plex music library to Tidal favorites by matching albums and artists.

## Goal

Read all albums and artists from a Plex music library and add matching ones to Tidal favorites. Do not upload anything — purely catalog matching.

## Stack

- Python, managed with `uv`
- `plexapi` — read Plex library
- `tidalapi` — search and add to Tidal favorites

## Usage

```bash
uv run python sync.py [--dry-run] [--no-albums] [--no-artists] [--debug]
```

Dependencies: `uv sync --no-install-project`

## Music library structure

```
/path/to/music/
  Artist Name/
    Album Name/
      track files
```

## First-run onboarding

On first run the script prompts interactively for all three pieces of config and saves them to `config.json`:

1. **Tidal** — OAuth via browser link (`tidalapi` `login_oauth_simple`)
2. **Plex** — OAuth via browser link (`MyPlexPinLogin(oauth=True)` → `oauthUrl()`)
3. **Music library path** — prompted and validated with `is_dir()`; re-asked if unreachable

Subsequent runs restore everything from `config.json` silently. If a session expires or a token is rejected, the relevant auth step is re-run automatically.

## Config file

Stored at `./config.json` (project-local, next to `sync.py`). Committed to `.gitignore`.
Contains all credentials and settings:

```json
{
  "plex":       { "url": "...", "token": "..." },
  "tidal":      { "token_type": "...", "access_token": "...", "refresh_token": "...", "expiry_time": "..." },
  "music_root": "/path/to/music"
}
```

Optional environment variable overrides (skip prompts):
- `PLEX_URL`, `PLEX_TOKEN`, `MUSIC_ROOT`, `PLEX_MUSIC_SECTION`

## Matching strategy

### Albums
Two independent sources for artist/album identity:

1. **Filesystem path** — parse first two components after music root: `Artist/Album/` (more reliable, not subject to metadata issues)
2. **Plex metadata** — `album.parentTitle` (artist) and `album.title`

Confidence levels (never skip outright — always attempt a Tidal search):
- **HIGH**: both sources agree → use Plex names (may be cleaner)
- **MEDIUM**: sources disagree → trust FS names, log warning
- **MEDIUM**: FS parse failed → use Plex names only, log warning

Medium confidence matches are added but tagged `[medium confidence]` in the log.

On the Tidal result, check both artist name and album title match (case-insensitive, partial match acceptable via `names_match`).

Exact vs partial match (after normalization):
- **Exact match**: added automatically
- **Partial match**: user is prompted `Add '...'? [y/N]` before adding; default is N

### Artists
Match by artist name only (simpler — no two-source check needed). Same exact/partial confirmation applies.

## Behaviour

- `--dry-run`: print what would be added without touching Tidal
- `--no-albums` / `--no-artists`: skip either sync type
- Log clearly: ADDED / SKIPPED / NOT FOUND / MEDIUM CONFIDENCE warnings
- Partial Tidal matches require interactive confirmation (`[y/N]`, default N); dry-run shows `[partial match — will confirm]` tag instead
- 0.2s delay between Tidal API calls (rate limiting)
- Tidal favorites paginated at 50 per call

## plexapi API notes

- `MyPlexPinLogin(oauth=True)` — must pass `oauth=True` for browser URL flow
- Call `.run(timeout=300)` before `.oauthUrl()` — `.run()` registers the PIN first
- `Unauthorized` exception on bad token (import from `plexapi.exceptions`)

## tidalapi API notes

- `session.login_oauth_simple()` — prints URL, blocks until authenticated
- `session.load_oauth_session(token_type, access_token, refresh_token, expiry_time)` — restore session
- `session.search(query, models=[tidalapi.Album], limit=N)` — returns dict with `'albums'` key
- `session.search(query, models=[tidalapi.Artist], limit=N)` — returns dict with `'artists'` key
- `tidalapi.Favorites(session, session.user.id)` — create favorites object
- `favorites.albums(limit=50, offset=0)` / `favorites.artists(limit=50, offset=0)` — paginated
- `favorites.add_album(str(album_id))` / `favorites.add_artist(str(artist_id))` — takes string ID

## Code style

- Simple and readable, no over-engineering
- Single script (`sync.py`), no package structure
- Type hints and comments where they add clarity
