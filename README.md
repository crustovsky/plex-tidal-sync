# plex-tidal-sync

Sync a local Plex music library to Tidal favorites by matching albums and artists.

It reads every album and artist from a Plex music library, searches Tidal for each
one, and adds matches to your Tidal favorites. Nothing is uploaded; this is purely
catalog matching, so your Tidal favorites end up mirroring what you own locally.

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- A Plex server with a music library
- A Tidal account

## Install

```bash
uv sync --no-install-project
```

## Usage

```bash
uv run python sync.py [--dry-run] [--no-albums] [--no-artists] [--debug]
```

| Flag | Effect |
| --- | --- |
| `--dry-run` | Print what would be added without touching Tidal |
| `--no-albums` | Skip album sync |
| `--no-artists` | Skip artist sync |
| `--debug` | Verbose logging |

Start with `--dry-run`. It shows exactly which albums would be added, which are
already favorited, and which could not be found.

## First run

On first run the script prompts for everything it needs and saves it:

1. **Tidal**: OAuth via a browser link
2. **Plex**: OAuth via a browser link, then the server URL (defaults to
   `http://localhost:32400`)
3. **Music library path**: the root of your music files on disk

Subsequent runs restore everything silently. If a session expires or a token is
rejected, that auth step re-runs automatically.

## Configuration

Credentials and settings live in `config.json` next to `sync.py`. It is gitignored,
since it holds live tokens. Keep it out of version control and off shared machines.

```json
{
  "plex":       { "url": "...", "token": "..." },
  "tidal":      { "token_type": "...", "access_token": "...", "refresh_token": "...", "expiry_time": "..." },
  "music_root": "/path/to/music"
}
```

Environment variables override the prompts:

| Variable | Purpose |
| --- | --- |
| `PLEX_URL` | Plex server URL |
| `PLEX_TOKEN` | Plex token (skips the OAuth flow) |
| `PLEX_MUSIC_SECTION` | Name of the Plex music section (default: `Music`) |
| `MUSIC_ROOT` | Music library root (skips the prompt) |

## Expected library layout

The filesystem is used as a second opinion on artist/album names, so it expects:

```
/path/to/music/
  Artist Name/
    Album Name/
      track files
```

Libraries that don't follow this still work; they just fall back to Plex metadata
alone and get flagged as medium confidence.

## How matching works

### Albums

Two independent sources are used for artist/album identity:

1. **Filesystem path**: the first two components after the music root
   (`Artist/Album/`). Generally more reliable, since it isn't subject to Plex
   metadata scraping.
2. **Plex metadata**: `album.parentTitle` and `album.title`.

Nothing is ever skipped outright; every album gets a Tidal search. The two sources
only decide which names to search with and how much to trust the result:

| Situation | Confidence | Search uses |
| --- | --- | --- |
| Both sources agree | high | Plex names (often cleaner) |
| Sources disagree | medium | Filesystem names, with a warning |
| Filesystem parse failed | medium | Plex names, with a warning |

Medium-confidence matches are still added, but tagged `[medium confidence]` in the
log so they're easy to audit afterwards.

Tidal results are checked against both the artist name and the album title
(case-insensitive, partial matches allowed):

- **Exact match** (after normalization): added automatically
- **Partial match**: prompts `Add '...'? [y/N]` first, defaulting to no

### Artists

Matched on artist name alone, with the same exact/partial confirmation rules.

## Output

Each item is logged as `ADDED`, `SKIPPED`, or `NOT FOUND`, with medium-confidence
warnings called out inline, followed by a per-category summary. Tidal calls are
spaced 0.2s apart to stay well inside rate limits, and favorites are paginated 50
at a time.
