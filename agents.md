# Harmony - Project Context

## Project Overview

Harmony is a standalone Python application for playlist management and library tooling. It is not a beets plugin; beets integration is optional and used to enrich metadata and accelerate search when configured.

Key features include:
- Smart playlists based on ratings, recency, genres, and play counts.
- AI-generated playlists from natural language prompts (Ollama/OpenAI-compatible).
- External playlist import (Spotify, Apple Music, YouTube, Tidal, Qobuz, JioSaavn, Gaana, ListenBrainz, local M3U8, custom HTTP POST endpoints).
- Playlist management and transfer (Plex to Spotify today; extensible).
- Multi-stage search pipeline with cache, vector index, backend search, optional LLM cleanup, and manual confirmation.
- Optional AudioMuse enrichment for mood/energy filtering in smart playlists.

Harmony is written in Python and leverages plexapi, spotipy, pydantic, and agno.

## Implementation Guidelines for Coding Assistants

- Ask clarifying questions for ambiguous changes.
- Draft and confirm approach for non-trivial features.
- List trade-offs when multiple approaches exist.
- Follow existing patterns and module boundaries below.

### Critical Constraints
- Keep cache keys stable (SQLite via core/cache.py).
- Keep public APIs and method signatures stable when possible.
- Preserve vector index behavior (core/vector_index.py) to avoid regressions.
- Beets integration is optional; do not require beets to run core features.

### Development Patterns
- Use logging with namespace harmony.*
- Prefer Pydantic v2 models for structured data.
- Cache expensive operations (Plex calls, providers, LLM, vector index).
- Keep LLM tooling behind config flags and degrade gracefully.

## Code Organization
- Entry point: harmony/app.py and harmony/cli.py
- AI: harmony/ai/{llm.py, search.py}
- Core: harmony/core/{cache.py, matching.py, vector_index.py}
- Workflows (backend-agnostic): harmony/workflows/{search.py, manual_search.py, playlist_import.py}
- Backends: harmony/backends/{plex.py, beets.py, base.py}
- AudioMuse backend: harmony/backends/audiomuse.py
- Plex (compat wrappers + Plex-specific utilities): harmony/plex/{search.py, manual_search.py, playlist_import.py, smartplaylists.py, operations.py}
- Providers: harmony/providers/{apple.py, spotify.py, youtube.py, tidal.py, jiosaavn.py, gaana.py, m3u8.py, http_post.py, qobuz.py, listenbrainz.py}
- AudioMuse config lives under providers.audiomuse in harmony.yaml.
- Utils: harmony/utils/helpers.py

## Playlist Providers

### Qobuz Provider
- **Location**: harmony/providers/qobuz.py
- **URL Pattern**: `https://www.qobuz.com/{lang}/playlists/{category}/{id}`
- **Method**: Web scraping (no API credentials required)
- **Cache**: 168 hours (7 days)
- **Implementation**:
  - `extract_playlist_id(url)` - Extract numeric ID from Qobuz URLs
  - `import_qobuz_playlist(url, cache)` - Main import function with caching
  - Parses tracks from embedded JSON in script tags (similar to Apple Music)
  - Fallback to HTML parsing if JSON extraction fails
- **Usage**:
  ```bash
  harmony import-playlist "Qobuz Mix" --url https://www.qobuz.com/gb-en/playlists/bollywood/22893019
  ```

### ListenBrainz Provider
- **Location**: harmony/providers/listenbrainz.py
- **Playlist Types**: `weekly_jams`, `weekly_exploration`
- **Source**: Troi-bot generated recommendation playlists
- **Authentication**: User token + username (required)
- **Configuration** in harmony.yaml:
  ```yaml
  providers:
    listenbrainz:
      username: YOUR_USERNAME
      token: YOUR_TOKEN
  
  playlists:
    items:
      - id: weekly_jams
        name: "Weekly Jams"
        type: weekly_jams
        enabled: true
      - id: weekly_exploration
        name: "Weekly Exploration"
        type: weekly_exploration
        enabled: true
  ```
- **Implementation**:
  - `ListenBrainzClient` - API client with authentication
  - `parse_troi_playlists(username, token, playlist_type)` - Fetch and filter troi-bot playlists
  - `fetch_troi_playlist_tracks(playlist_id, token)` - Get tracks from a playlist
  - `get_weekly_jams(username, token, most_recent=True)` - Convenience function for Weekly Jams
  - `get_weekly_exploration(username, token, most_recent=True)` - Convenience function for Weekly Exploration
- **Usage**:
  ```bash
  harmony smart-playlists  # Processes ListenBrainz playlists configured in harmony.yaml
  ```
- **Integration**:
  - ListenBrainz playlists are handled in `Harmony._generate_listenbrainz_playlist()` (harmony/app.py)
  - Detected by playlist type in `generate_smart_playlist()` before standard smart playlist logic
  - Uses `harmony.workflows.playlist_import.add_songs_to_playlist()` to search and add tracks

## Search Pipeline Overview (harmony/workflows/search.py)
When search_backend_song(...) is called, the pipeline should proceed:
1. Cache check
   - Return cached backend_id via backend.get_track when present.
2. Local candidates
   - Prefer beets vector index when configured, then backend vector index.
   - Try direct match via cached backend_id/plex_ratingkey/provider_ids.
   - Prepare variant queries from candidates and try backend search.
3. Multi-strategy backend search
   - If tracks found, score with core/matching.plex_track_distance.
   - Accept when similarity threshold is met; else queue for review.
4. Manual search UI (workflows/manual_search.py)
   - review_candidate_confirmations(.) queues and deduplicates options.
   - handle_manual_search(.) supports actions:
     - a: Abort, s: Skip (store negative cache), e: Enter manual search.
     - Numeric selection caches positive result against the original query only.
   - _store_negative_cache(cache, song, original_query)
     - Writes None to cache when there is a valid title in the chosen query.
   - _cache_selection(cache, song, track, original_query)
     - Caches ONLY the original query key (not the manual entry).
5. LLM search fallback (optional)
   - If enabled via llm.use_llm_search, use ai/llm.py.
   - Provider priority in toolkit: SearxNG > Exa > Brave > Tavily.

## Smart Playlists
- Types: daily_discovery, forgotten_gems, recent_hits, fresh_favorites, 70s80s_flashback, highly_rated, most_played, energetic_workout, relaxed_evening.
- Filters: history_days, exclusion_days, discovery_ratio, include/exclude genres, include/exclude years, min_rating.
- Prefers beets metadata for genres/years when configured; falls back to Plex.
- AudioMuse mood filters live under filters.mood (min/max energy, danceable, happy, sad, relaxed, party, aggressive, tempo, mood_categories).

## Testing
- **Environment requirement**: All testing must be carried out in the `py311` conda environment, which has all dependencies installed.
  ```bash
  conda activate py311
  ```
- **Official unit tests**: Located in `tests/` directory (use for regression testing and CI).
  ```bash
  python3 -m unittest discover -s ./tests -p "test_*.py" -v
  ```
- **Debug and validation scripts**: For temporary testing, validation, or debugging purposes, place scripts under `test_scripts/` directory. These are not part of the formal test suite and are excluded from CI.
- Compile modules quickly:
  ```bash
  python3 - << 'PY'
import os, py_compile
for root, _, files in os.walk('harmony'):
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
