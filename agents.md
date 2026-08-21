# beets-plexsync - Project Context

This file is kept in sync with `gemini.md`. Codex/Cursor-style agents read `agents.md`; Gemini CLI reads `gemini.md`; Claude Code reads `CLAUDE.md` (more detailed command/config map).

## Environment

- **Conda env**: `py311` — run commands with `conda run -n py311 ...`
- **Python**: 3.11 locally; plugin requires `>=3.10`
- **Beets**: `>=2.13.0` (tested against 2.13.1)
- **Platform**: Windows 11 (Unix shell syntax in bash: forward slashes, `/dev/null`)
- **Remote live machine** (optional): `arsaboo@192.168.2.188`, plugin under `~/.local`, `beet` at `~/.local/bin/beet`

## Project Overview

This project is a plugin for [beets](https://github.com/beetbox/beets). The plugin, named `plexsync`, syncs and manages a music library between beets and a Plex Media Server.

Key features:
- **Library Sync**: Import ratings, play counts, last played dates from Plex into beets (`beet plexsync`, `plexsyncrecent`)
- **Smart Playlists**: Daily Discovery, Forgotten Gems, Recent Hits, Fresh Favorites, 70s80s Flashback, Highly Rated, Most Played
- **AI Playlists**: Natural-language playlists via LLM (`beet plexsonic`)
- **External Import**: Spotify, Apple Music, YouTube, Tidal, JioSaavn, Gaana, M3U8, HTTP POST
- **Playlist Management**: Add/remove/clear, playlist→collection, album collages
- **Spotify Transfer**: Copy Plex playlists to Spotify (`plex2spotify`)

## Implementation Guidelines

- Ask clarifying questions for ambiguous changes
- Draft and confirm approach for non-trivial features
- List trade-offs when multiple approaches exist
- Follow existing patterns and module boundaries below

### Critical Constraints
- **NEVER modify cache keys** (`Cache._make_cache_key` pipe format `title|artist|album` in `core/cache.py`). Changing keys invalidates the existing SQLite cache.
- Keep public APIs and method signatures stable when possible
- Maintain compatibility with beets plugin architecture and CLI
- Preserve vector index behavior (`core/vector_index.py`)
- Minimize Spotify API calls (batch `sp.tracks()`, cache)
- Keep LLM tooling behind config flags; degrade gracefully

### Development Patterns
- Logging: `from beets import logging` so loggers are `BeetsLogger` (`{}`-style formatting)
- Prefer Pydantic v2 models for structured data
- Cache expensive operations (Plex, providers, LLM)
- Provider HTTP: `beetsplug._utils.requests.TimeoutAndRetrySession` (timeout, 429/5xx retry)
- `plex_track_distance` uses a local weighted sum — do not mutate `beets.autotag.distance.Distance._weights`
- `beet plexsync`: search in threads; `try_write` then one `lib.transaction()` for all `store()`

## Code Organization
- Entry point: `beetsplug/plexsync.py`
- AI: `beetsplug/ai/llm.py` (Agno; OpenAI-like or Ollama)
- Core: `beetsplug/core/{cache.py, config.py, matching.py, vector_index.py}`
- Plex: `beetsplug/plex/{search.py, manual_search.py, playlist_import.py, smartplaylists.py, operations.py, spotify_transfer.py, queues.py, collage.py}`
- Providers: `beetsplug/providers/{apple.py, spotify.py, youtube.py, tidal.py, jiosaavn.py, gaana.py, m3u8.py, http_post.py}`
- Utils: `beetsplug/utils/{helpers.py, prompt_logging.py}`

## Search Pipeline (`beetsplug/plex/search.py`)
When `PlexSync.search_plex_song(...)` is called:
1. Cache check — return cached ratingKey via `plugin.music.fetchItem`
2. Local beets candidates (`core/vector_index.py`)
   - Direct match via cached `plex_ratingkey` if similarity >= 0.8
   - Else queue for confirmation; try variant queries on `music.searchTracks`
3. Score hits with `core/matching.plex_track_distance`; accept on threshold
4. Manual search UI (`manual_search.py`): a abort, s skip (negative cache), e enter, numeric select (cache original query only)
5. Optional LLM fallback if `plexsync.use_llm_search` (SearxNG > Exa > Brave > Tavily; Brave ~1 req/s)

## Smart Playlists
`beet plex_smartplaylists`:
- System: `daily_discovery`, `forgotten_gems`, `recent_hits`, `fresh_favorites`, `70s80s_flashback` (1970–1989), `highly_rated`, `most_played`
- Daily Discovery dedupes sonic + library pools by `ratingKey` / `plex_ratingkey`
- Flags: `--only` (comma-separated IDs), `--import-failed`/`--log-file`

## Testing
```bash
conda run -n py311 python -m pytest -v
conda run -n py311 python -m pytest tests/test_cache.py -v
```
Install the test extra if needed: `pip install -e .[test]`

```bash
conda run -n py311 python -c "import os, py_compile; [py_compile.compile(os.path.join(r,f)) for r,_,fs in os.walk('beetsplug') for f in fs if f.endswith('.py')]; print('OK')"
```

## LLM Configuration
- `beet plexsonic` uses top-level `llm.*` (`api_key`, `model`, `base_url`)
- `llm.search.*` only when `plexsync.use_llm_search` is enabled
- If `llm.api_key` is set: OpenAI-compatible via agno; else Ollama
- Search toolkit: `searxng_host`, `exa_api_key`, `brave_api_key`, `tavily_api_key`
