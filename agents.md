# beets-plexsync - Project Context

Canonical agent brief. `CLAUDE.md` and `gemini.md` point here so they stay in sync.

## Environment

- **Conda env**: Always use `py311` — `conda run -n py311 ...`. This env is **local-only** (Windows); it does **not** exist on the remote machine.
- **Python**: 3.11 locally; plugin requires `>=3.10`
- **Beets**: `>=2.13.0` (tested against 2.13.1)
- **Platform**: Windows 11 (Unix shell syntax in bash: forward slashes, `/dev/null`)
- **Tests**: `conda run -n py311 python -m pytest -v`
- **Single test**: `conda run -n py311 python -m pytest tests/test_cache.py -v`
- **Compile check**: `conda run -n py311 python -c "import os, py_compile; [py_compile.compile(os.path.join(r,f)) for r,_,fs in os.walk('beetsplug') for f in fs if f.endswith('.py')]; print('OK')"`
- **Test extra**: `pip install -e .[test]` (pytest)
- **Remote** (optional live checks): `arsaboo@192.168.2.188`. **No conda on the remote** — run Python directly: `ssh arsaboo@192.168.2.188` then use the system `python3` (Python 3.10.12; beets installed under `~/.local/lib/python3.10/site-packages`) or the `beet` CLI at `/home/arsaboo/.local/bin/beet`. The library DB is `~/.config/beets/musiclibrary.blb`. Plugin code lives under `~/.local/lib/python3.10/site-packages/beetsplug` (the `plexsync` plugin is loaded from there). Beets version on remote is 2.13.1. `~/.local/lib` paths can be queried directly, e.g.: `python3 -c "from beets.library import Library; lib=Library('/home/arsaboo/.config/beets/musiclibrary.blb'); ..."`.

## Project Overview

beets-plexsync is a [beets](https://github.com/beetbox/beets) plugin (`PlexSync` extending `BeetsPlugin`) that syncs a music library between beets and Plex.

- **Library Sync**: ratings, play counts, last played (`beet plexsync`, `plexsyncrecent`)
- **Smart Playlists**: Daily Discovery, Forgotten Gems, Recent Hits, Fresh Favorites, 70s80s Flashback, Highly Rated, Most Played
- **AI Playlists**: natural-language playlists (`beet plexsonic`)
- **External Import**: Spotify, Apple Music, YouTube, Tidal, JioSaavn, Gaana, M3U8, HTTP POST
- **Spotify Transfer**: Plex → Spotify (`plex2spotify`)
- **Playlist Management**: add/remove/clear, playlist→collection, album collages

## Implementation Guidelines

- Ask clarifying questions for ambiguous changes
- Draft and confirm approach for non-trivial features
- List trade-offs when multiple approaches exist
- Follow existing module boundaries (providers, plex, core, ai)

### Critical Constraints

- **NEVER modify cache keys** (`Cache._make_cache_key` pipe format `title|artist|album` in `core/cache.py`). Changing keys invalidates the existing SQLite cache.
- Keep public APIs and method signatures stable when possible
- Maintain beets plugin architecture and CLI compatibility
- Preserve vector index behavior (`core/vector_index.py`)
- Minimize Spotify API calls — batch `sp.tracks()` (50 at once) and cache
- spotipy is configured with retries/backoff for rate limits
- Provider HTTP: `beetsplug._utils.requests.TimeoutAndRetrySession` (timeout, 429/5xx retry)
- Do not mutate `beets.autotag.distance.Distance._weights` (`plex_track_distance` uses a local weighted sum)
- `beet plexsync`: search in threads; `try_write` then one `lib.transaction()` for all `store()`
- Cache expensive operations (Plex, providers, LLM)
- Keep LLM tooling behind config flags; degrade gracefully

### Development Patterns

- Logging: `from beets import logging` so loggers are `BeetsLogger` (`{}`-style)
- Prefer Pydantic v2 models
- Cache expensive operations

## Code Organization

```
beetsplug/
├── plexsync.py              # Main plugin entry point (PlexSync)
├── ai/llm.py                # Agno LLM (OpenAI-like or Ollama)
├── core/
│   ├── cache.py             # SQLite cache (track lookups, playlists, Spotify)
│   ├── config.py            # get_config_value, get_plexsync_config
│   ├── matching.py          # fuzzy matching, plex_track_distance (local weights)
│   └── vector_index.py      # In-memory cosine-similarity index
├── plex/
│   ├── search.py            # Multi-strategy Plex track search
│   ├── manual_search.py     # Interactive manual search UI
│   ├── playlist_import.py   # Import playlists into Plex
│   ├── smartplaylists.py    # Smart playlist generation
│   ├── operations.py        # Plex CRUD, playlist→collection
│   ├── spotify_transfer.py  # Plex→Spotify transfer
│   ├── queues.py            # LLMEnhancementQueue, ManualPromptQueue
│   └── collage.py           # Album art collage
├── providers/
│   ├── spotify.py           # spotipy + web scrape fallback
│   ├── apple.py             # Apple Music HTML scrape
│   ├── youtube.py / tidal.py / gaana.py  # wrappers around other beets plugins
│   ├── jiosaavn.py          # JioSaavn async API
│   ├── m3u8.py              # M3U8 parser
│   └── http_post.py         # HTTP POST importer (TimeoutAndRetrySession)
└── utils/
    ├── helpers.py           # parse_title, clean_album_name, highlight_matches
    └── prompt_logging.py    # Log buffering during interactive prompts
```

## Beets Subcommands (13)

| Command | Description | Key Options |
|---------|-------------|-------------|
| `plexupdate` | Update Plex library | |
| `plexsync` | Fetch track attributes from Plex | `-f`/`--force` |
| `plexplaylistadd` | Add tracks to Plex playlist | `-m`/`--playlist` (default: Beets) |
| `plexplaylistremove` | Remove tracks from Plex playlist | `-m`/`--playlist` |
| `plexsyncrecent` | Sync recently played tracks | `--days` (default: 7) |
| `plexplaylistimport` | Import playlist into Plex | `-m`, `-u`/`--url`, `-l`/`--listenbrainz` |
| `plexplaylistclear` | Clear a Plex playlist | `-m`/`--playlist` |
| `plexcollage` | Album collage from history | `-i`/`--interval`, `-g`/`--grid` |
| `plexsonic` | LLM playlists | `-n`, `-p`/`--prompt`, `-m`, `-c`/`--clear` |
| `plexsearchimport` | Import from YouTube search | `-m`, `-s`/`--search`, `-l`/`--limit` |
| `plexplaylist2collection` | Playlist → collection | `-m`/`--playlist` |
| `plex2spotify` | Plex playlist → Spotify | `-m`/`--playlist` (default: beets) |
| `plex_smartplaylists` | Generate smart playlists | `-i`/`--import-failed`, `-l`/`--log-file`, `-o`/`--only` |

## Key Instance Variables (PlexSync)

| Variable | Type | Description |
|----------|------|-------------|
| `self.plex` | `PlexServer` | Plex server connection |
| `self.music` | Library section | Plex music library |
| `self.sp` | `spotipy.Spotify` | Authenticated Spotify client |
| `self.cache` | `Cache` | SQLite cache |
| `self.llm_client` | OpenAI-like client | LLM for plexsonic |
| `self.search_llm` | LLM client | Search enhancement |
| `self._vector_index` | `BeetsVectorIndex` | In-memory cosine index |
| `self._llm_enhancement_queue` | `LLMEnhancementQueue` | Background LLM queue |
| `self._manual_prompt_queue` | `ManualPromptQueue` | Deferred manual prompts |
| `self._progress_manager` | Enlighten manager | Progress bars |

## Beets Flexible Fields

| Field | Type | Description |
|-------|------|-------------|
| `plex_guid` | STRING | Plex GUID |
| `plex_ratingkey` | INTEGER | Plex rating key |
| `plex_userrating` | FLOAT | User rating |
| `plex_skipcount` | INTEGER | Skip count |
| `plex_viewcount` | INTEGER | Play count |
| `plex_lastviewedat` | DateType | Last played |
| `plex_lastratedat` | DateType | Last rated |
| `plex_updated` | DateType | Last sync |

### Data model gotchas (verified live on arsmusic)

- **Genres live in `item.genres` — a multi-value beets field that returns a `list`**
  (e.g. `['Bollywood', 'Soundtrack']`). It is stored in the `items.genres` column
  (~63k rows) and is what `smartplaylists._genres_of(item)`
  (`_normalized_strings(getattr(item, 'genres', None))`) consumes.
- **`item.genre` (singular) is NOT a recognized beets field** — reading it raises
  `AttributeError: no such field 'genre'`. Do not use it. The orphaned
  `items.genre` column (3,624 rows, mostly `Rajasthani`; note the skew) and the
  item_attributes `genre` key (61 rows) are stale/unused and misleading.
  Always read genres via `item.genres` (list).
- **`year` is a standard `items` column** (`SELECT id, year FROM items`), NOT in
  item_attributes.
- **Flex fields are strings** in `item_attributes` (`entity_id, key, value`, joined
  on `items.id`); must parse, e.g. `float(x or 0)`. Map beets items ↔ Plex via
  `plex_ratingkey` (in item_attributes).
- **`plex_lastviewedat` is stored as a `'YYYY-MM-DD HH:MM:SS'` datetime string for
  played tracks and `'0.0'` when never played** (Plex `lastViewedAt=None`) — NOT a
  unix epoch or NULL. Beets parses it to a `datetime` on `item.plex_lastviewedat`;
  in raw SQL treat `'0.0'`/empty as never played. Use `smartplaylists._last_viewed_ts()`
  (handles both datetime and float).
- **Plex playlists can be STALE after logic changes** — validate by regenerating
  (`beet plex_smartplaylists`, config `clear_playlist` controls clearing first),
  not by reading an existing Plex playlist.
- `genres`/`genre` values may be `\n`-joined in raw SQL (`LIKE '%;%'` finds 0 rows);
  beets already parses them into a list via `item.genres`, so match against the
  parsed list, never raw string equality on one joined string.

## Config Options

- **Plex** (`config["plex"]`): `host`, `port`, `token`, `library_name`, `secure`, `ignore_cert_errors`
- **PlexSync** (`config["plexsync"]`): `tokenfile`, `manual_search`, `max_tracks`, `exclusion_days`, `history_days`, `discovery_ratio`, `use_llm_search`, `llm.background_enhancement`, `search.manual_prompt_queue_enabled`, `search.manual_prompt_queue_limit`
- **LLM** (`config["llm"]`): `api_key`, `model`, `base_url`, `search.provider`, `search.api_key`, `search.base_url`, `search.model`, `search.embedding_model`
- **Spotify** (`config["spotify"]`): `client_id`, `client_secret`

## Search Pipeline (`plex/search.py`)

1. Cache check → cached ratingKey via `plugin.music.fetchItem`
2. Local beets candidates (`core/vector_index.py`) → accept if similarity >= 0.8, else queue confirmation; variant `music.searchTracks`
3. Score with `core/matching.plex_track_distance`; accept on threshold
4. Manual UI (`manual_search.py`): a abort, s skip (negative cache), e enter, numeric select (cache original query only)
5. Optional LLM fallback if `plexsync.use_llm_search` (SearxNG > Exa > Brave > Tavily; Brave ~1 req/s)

## Spotify API

- OAuth via spotipy with token cache (`providers/spotify.py`)
- Client: `retries=3`, `backoff_factor=0.5`
- Playlist import: API first (`playlist_items` + pagination), web scrape fallback, cache (api/web/tracks)
- Track search: in-memory `_spotify_search_result_cache`
- Availability: batch `sp.tracks()` (50/request)
- Playlist sync: diff-based add/remove, 100-track chunks
- Playlist IDs: `extract_release_id` (URLs with `?si=`, `spotify:playlist:` URIs, bare IDs); reject album/track/artist URLs

## Smart Playlists

System types: `daily_discovery`, `forgotten_gems`, `recent_hits`, `fresh_favorites`, `70s80s_flashback` (1970–1989), `highly_rated`, `most_played`

Daily Discovery dedupes sonic + library pools by `ratingKey` / `plex_ratingkey`.

Imported playlists via `plexsync.playlists`. Flags: `--only`, `--import-failed`/`--log-file`.

## LLM Configuration

- `beet plexsonic` uses top-level `llm.*` (`api_key`, `model`, `base_url`)
- `llm.search.*` only when `plexsync.use_llm_search` is enabled
- If `llm.api_key` is set: OpenAI-compatible via agno; else Ollama
- Search toolkit: `searxng_host`, `exa_api_key`, `brave_api_key`, `tavily_api_key`

## Dependencies (key)

`beets>=2.13.0`, Python `>=3.10`, `plexapi>=4.13.4`, `spotipy`, `openai`, `agno>=1.2.16`, `instructor>=1.0`, `pydantic>=2.0.0`, `numpy`, `scipy`, `beautifulsoup4`, `requests`, `python-dateutil`, `pillow`
