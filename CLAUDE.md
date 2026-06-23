# CLAUDE.md

## Environment

- **Conda env**: Always use `py311` — run all commands with `conda run -n py311 ...`
- **Python**: 3.11
- **Platform**: Windows 11 (use Unix shell syntax in bash — forward slashes, /dev/null, etc.)
- **Tests**: `conda run -n py311 python -m pytest tests/ -v`
- **Single test**: `conda run -n py311 python -m pytest tests/test_spotify_transfer.py -v`
- **Compile check**: `conda run -n py311 python -c "import py_compile, os; [py_compile.compile(os.path.join(r,f)) for r,_,fs in os.walk('beetsplug') for f in fs if f.endswith('.py')]; print('OK')"`

## Project Overview

beets-plexsync is a [beets](https://github.com/beetbox/beets) plugin (`PlexSync` class extending `BeetsPlugin`) that syncs music libraries between beets and Plex Media Server. Key features:

- **Library Sync**: Import ratings, play counts, last played dates from Plex into beets
- **Smart Playlists**: Generate dynamic playlists (Daily Discovery, Forgotten Gems, Recent Hits, etc.)
- **AI Playlists**: Create playlists from natural language prompts via LLM (GPT/Ollama)
- **External Import**: Import playlists from Spotify, Apple Music, YouTube, Tidal, JioSaavn, Gaana, M3U8, HTTP POST
- **Spotify Transfer**: Copy Plex playlists to Spotify (`plex2spotify`)
- **Playlist Management**: Add/remove tracks, clear playlists, convert to collections, create album collages

## Critical Constraints

- **NEVER** modify cache keys (stored in SQLite via `core/cache.py`)
- Keep public APIs and method signatures stable
- Maintain beets plugin architecture compatibility
- Preserve vector index behavior (`core/vector_index.py`)
- Spotify API calls should be minimized — use batch endpoints (`sp.tracks()` for 50 at once) and caching
- spotipy client is configured with retries/backoff for rate limits
- Cache expensive operations (Plex calls, provider fetches, LLM queries)
- Keep LLM tooling behind config flags; degrade gracefully

## Code Organization

```
beetsplug/
├── plexsync.py              # Main plugin entry point (PlexSync class)
├── ai/
│   └── llm.py               # Agno-based LLM integration (OpenAI-like or Ollama)
├── core/
│   ├── cache.py              # SQLite cache (track lookups, playlist data, Spotify data)
│   ├── config.py             # Config helpers (get_config_value, get_plexsync_config)
│   ├── matching.py           # String similarity, fuzzy matching, plex_track_distance
│   └── vector_index.py       # In-memory cosine-similarity index over beets metadata
├── plex/
│   ├── search.py             # Multi-strategy Plex track search pipeline
│   ├── manual_search.py      # Interactive manual search UI
│   ├── playlist_import.py    # Import playlists into Plex from various sources
│   ├── smartplaylists.py     # Smart playlist generation (scoring, filtering, selection)
│   ├── operations.py         # Plex CRUD (add/remove/clear/sort playlists, playlist→collection)
│   ├── spotify_transfer.py   # Plex→Spotify transfer with batch availability checks
│   ├── queues.py             # LLMEnhancementQueue and ManualPromptQueue
│   └── collage.py            # Album art collage generator
├── providers/
│   ├── spotify.py            # Spotify API (spotipy) + web scraping fallback
│   ├── apple.py              # Apple Music HTML scraping
│   ├── youtube.py            # Wrapper around external YouTubePlugin
│   ├── tidal.py              # Wrapper around external TidalPlugin
│   ├── jiosaavn.py           # JioSaavn async API
│   ├── gaana.py              # Wrapper around external GaanaPlugin
│   ├── m3u8.py               # M3U8 file parser
│   └── http_post.py          # Generic HTTP POST playlist importer
└── utils/
    ├── helpers.py            # parse_title, clean_album_name, highlight_matches
    └── prompt_logging.py     # Thread-safe log buffering during interactive prompts
```

## Beets Subcommands (13 total)

| Command | Description | Key Options |
|---------|-------------|-------------|
| `plexupdate` | Update Plex library | |
| `plexsync` | Fetch track attributes from Plex | `-f`/`--force` |
| `plexplaylistadd` | Add tracks to Plex playlist | `-m`/`--playlist` (default: "Beets") |
| `plexplaylistremove` | Remove tracks from Plex playlist | `-m`/`--playlist` |
| `plexsyncrecent` | Sync recently played tracks | `--days` (default: 7) |
| `plexplaylistimport` | Import playlist into Plex | `-m`/`--playlist`, `-u`/`--url`, `-l`/`--listenbrainz` |
| `plexplaylistclear` | Clear a Plex playlist | `-m`/`--playlist` |
| `plexcollage` | Create album collage from history | `-i`/`--interval`, `-g`/`--grid` |
| `plexsonic` | Create LLM-based playlists | `-n`/`--number`, `-p`/`--prompt`, `-m`/`--playlist`, `-c`/`--clear` |
| `plexsearchimport` | Import from YouTube search | `-m`/`--playlist`, `-s`/`--search`, `-l`/`--limit` |
| `plexplaylist2collection` | Convert playlist to collection | `-m`/`--playlist` |
| `plex2spotify` | Transfer Plex playlist to Spotify | `-m`/`--playlist` (default: "beets") |
| `plex_smartplaylists` | Generate smart playlists | `-i`/`--import-failed`, `-l`/`--log-file`, `-o`/`--only` |

## Key Instance Variables (PlexSync)

| Variable | Type | Description |
|----------|------|-------------|
| `self.plex` | `PlexServer` | Plex server connection |
| `self.music` | Library section | Plex music library section |
| `self.sp` | `spotipy.Spotify` | Authenticated Spotify client |
| `self.cache` | `Cache` | SQLite cache instance |
| `self.llm_client` | OpenAI client | LLM for plexsonic |
| `self.search_llm` | LLM client | LLM for search enhancement |
| `self._vector_index` | `BeetsVectorIndex` | In-memory cosine-similarity index |
| `self._llm_enhancement_queue` | `LLMEnhancementQueue` | Background LLM processing queue |
| `self._manual_prompt_queue` | `ManualPromptQueue` | Deferred manual prompt queue |
| `self._progress_manager` | Enlighten manager | Progress bar manager |

## Beets Flexible Fields

| Field | Type | Description |
|-------|------|-------------|
| `plex_guid` | STRING | Plex GUID identifier |
| `plex_ratingkey` | INTEGER | Plex rating key |
| `plex_userrating` | FLOAT | User rating from Plex |
| `plex_skipcount` | INTEGER | Skip count |
| `plex_viewcount` | INTEGER | Play count |
| `plex_lastviewedat` | DateType | Last played date |
| `plex_lastratedat` | DateType | Last rated date |
| `plex_updated` | DateType | Last sync date |

## Config Options

**Plex** (`config["plex"]`): `host`, `port`, `token`, `library_name`, `secure`, `ignore_cert_errors`

**PlexSync** (`config["plexsync"]`): `tokenfile`, `manual_search`, `max_tracks`, `exclusion_days`, `history_days`, `discovery_ratio`, `use_llm_search`, `llm.background_enhancement`, `search.manual_prompt_queue_enabled`, `search.manual_prompt_queue_limit`

**LLM** (`config["llm"]`): `api_key`, `model`, `base_url`, `search.provider`, `search.api_key`, `search.base_url`, `search.model`, `search.embedding_model`

**Spotify** (`config["spotify"]`): `client_id`, `client_secret`

## Search Pipeline (plex/search.py)

1. **Cache check** → return cached ratingKey via `plugin.music.fetchItem`
2. **Local beets candidates** → `core/vector_index.py` cosine similarity → direct match if similarity >= 0.8, else queue confirmation
3. **Plex search** → `music.searchTracks` with variant queries → score with `core/matching.plex_track_distance`
4. **Manual search UI** → `manual_search.py` (abort/skip/enter/select) → caches result
5. **LLM fallback** (optional) → `ai/llm.py` search toolkit (SearxNG > Exa > Brave > Tavily)

## Spotify API Architecture

- **Auth**: OAuth via spotipy with token caching (`providers/spotify.py:authenticate`)
- **Client**: `spotipy.Spotify` with `retries=3`, `backoff_factor=0.5` for rate limit handling
- **Playlist import**: API first (`playlist_items` + pagination), web scraping fallback, 3-tier cache (api/web/tracks)
- **Track search**: 5 strategies with in-memory result cache (`_spotify_search_result_cache`)
- **Availability**: Batch check via `sp.tracks()` (50/request) instead of per-track `sp.track()`
- **Playlist sync**: Diff-based — only adds missing tracks, removes obsolete, 100-track chunks

## Smart Playlists

System types: `daily_discovery`, `forgotten_gems`, `recent_hits`, `fresh_favorites`, `70s80s_flashback`, `highly_rated`, `most_played`

Imported playlists configured via `plexsync.playlists` with sources from any supported provider.

Flags: `--only` (comma-separated IDs), `--import-failed`/`--log-file` (retry from logs)

## LLM Configuration

- `beet plexsonic` uses top-level `llm.*` settings
- `llm.search.*` used only for LLM search cleanup when `plexsync.use_llm_search` is enabled
- Auto-detect: if `llm.api_key` set → OpenAI-compatible (agno), else → Ollama
- Search toolkit priority: SearxNG > Exa > Brave > Tavily
- Brave Search is rate-limited (~1 req/sec)

## Dependencies (key)

`beets>=2.4.0`, `plexapi>=4.13.4`, `spotipy`, `openai`, `agno>=1.2.16`, `instructor>=1.0`, `pydantic>=2.0.0`, `numpy`, `scipy`, `beautifulsoup4`, `requests`, `python-dateutil`, `pillow`

## Development Patterns

- Logging namespace: `beets.plexsync`
- Prefer Pydantic v2 models for structured data
- Follow existing module boundaries (providers, plex, core, ai)
- Ask clarifying questions for ambiguous changes
- Draft and confirm approach for non-trivial features
