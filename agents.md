# beets-plexsync - Project Context for Gemini

## Project Overview

This project is a plugin for [beets](https://github.com/beetbox/beets), a music library manager. The plugin, named `plexsync`, provides powerful tools to synchronize and manage your music library between beets and a Plex Media Server.

Key features include:
- **Library Sync**: Import track data (ratings, play counts, last played dates) from Plex into your beets library.
- **Smart Playlists**: Generate dynamic playlists in Plex based on your listening history, track ratings, genres, and other criteria. Includes "Daily Discovery", "Forgotten Gems", and "Recent Hits".
- **AI-Generated Playlists**: Create playlists in Plex based on natural language prompts using an LLM (like GPT, Ollama models).
- **External Playlist Import**: Import playlists from various sources like Spotify, Apple Music, YouTube, Tidal, JioSaavn, Gaana, local M3U8 files, and custom HTTP POST endpoints.
- **Playlist Management**: Add/remove tracks from Plex playlists using beets queries, clear playlists.
- **Additional Tools**: Copy Plex playlists to Spotify, convert playlists to collections, create album collages.

The plugin is written in Python and leverages several libraries including `plexapi`, `spotipy`, `openai`, `pydantic`, and others.

## Implementation Guidelines for Coding Assistants

- Ask clarifying questions for ambiguous changes
- Draft and confirm approach for non-trivial features
- List trade-offs when multiple approaches exist
- Follow existing patterns and module boundaries below

### Critical Constraints
- NEVER modify cache keys (stored in SQLite via core/cache.py)
- Keep public APIs and method signatures stable when possible
- Maintain compatibility with beets plugin architecture and CLI
- Preserve vector index behavior (core/vector_index.py) to avoid regressions

### Development Patterns
- Use logging with namespace beets.plexsync
- Prefer Pydantic v2 models for structured data
- Cache expensive operations (Plex calls, providers, LLM)
- Keep LLM tooling behind config flags and degrade gracefully

## Code Organization
- Entry point: beetsplug/plexsync.py
- AI: beetsplug/ai/llm.py (Agno-based; OpenAI-like or Ollama)
- Core: beetsplug/core/{cache.py, config.py, matching.py, vector_index.py}
- Plex: beetsplug/plex/{search.py, manual_search.py, playlist_import.py, smartplaylists.py, operations.py, spotify_transfer.py, collage.py}
- Providers: beetsplug/providers/{apple.py, spotify.py, youtube.py, tidal.py, jiosaavn.py, gaana.py, m3u8.py, post.py}
- Utils: beetsplug/utils/helpers.py

## Search Pipeline Overview (beetsplug/plex/search.py)
When PlexSync.search_plex_song(...) is called, the pipeline should proceed:
1. Cache check
   - Return cached ratingKey via plugin.music.fetchItem when present
2. Local beets candidates
   - Use core/vector_index.py to surface LocalCandidate entries
   - Try direct match via cached plex_ratingkey if present
     - Accept immediately if similarity >= 0.8
     - Otherwise queue for manual confirmation
   - Prepare variant queries from candidates and try Plex music.searchTracks
3. Single/multiple track search
   - If tracks found, score with core/matching.plex_track_distance
   - Accept when similarity threshold is met; else queue for review
4. Manual search UI (manual_search.py)
   - review_candidate_confirmations(…) queues and deduplicates options
   - handle_manual_search(…) supports actions:
     - a: Abort, s: Skip (store negative cache), e: Enter manual search
     - Numeric selection caches positive result against the original query only
   - _store_negative_cache(plugin, song, original_query)
     - Writes None to cache when there is a valid title in the chosen query
   - _cache_selection(plugin, song, track, original_query)
     - Caches ONLY the original query key (not the manual entry), matching tests
5. LLM search fallback (optional)
   - If enabled via plexsync.use_llm_search, use ai/llm.py
   - Provider priority in toolkit: SearxNG > Exa > Brave > Tavily
   - Brave Search is rate-limited to ~1 request/second

## Smart Playlists
- Built in PlexSync.plex_smartplaylists command supports:
  - System playlists: daily_discovery, forgotten_gems, recent_hits, fresh_favorites, 70s80s_flashback, highly_rated, most_played
  - Imported playlists from providers and M3U8 files
  - Flags:
    - --only: restrict to a comma-separated list of playlist IDs
    - --import-failed/--log-file: retry manual imports using generated logs

## Testing
- Run unit tests:
  ```bash
  python3 -m unittest discover -s ./tests -p "test_*.py" -v
  ```
- Compile modules quickly:
  ```bash
  python3 - << 'PY'
import os, py_compile
for root, _, files in os.walk('beetsplug'):
    for f in files:
        if f.endswith('.py'):
            py_compile.compile(os.path.join(root, f))
print('OK')
PY
  ```

## LLM Configuration Notes
- Auto-detect provider:
  - If llm.api_key is set: OpenAI-compatible via agno.models.openai.like.OpenAILike
  - Else: Ollama via agno.models.ollama.Ollama
- Search toolkit keys under llm.search:
  - searxng_host, exa_api_key, brave_api_key, tavily_api_key
- Brave Search requests are rate-limited in code