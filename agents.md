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

### Before Coding
- **Ask clarifying questions** when requirements are ambiguous or incomplete
- **Draft and confirm approach** for complex features or significant changes
- **List pros and cons** when multiple implementation approaches exist (≥2 options)
- Understand the existing codebase structure and patterns before making changes

### Critical Constraints
- **NEVER modify cache keys** - Cache keys are stored in the database and changes will invalidate existing cached data
- Preserve existing API interfaces and method signatures when possible
- Maintain compatibility with beets plugin architecture

### Development Patterns
- Follow existing error handling patterns using Python's `logging` module and the `beets.plexsync` logger namespace
- Use Pydantic models for data validation and structured data
- Implement caching for external API calls to improve performance
- Follow beets plugin conventions and use beets' library/UI components
- Use the `agno` framework for LLM-related features

### Code Organization
- Keep the core plugin entry point in `beetsplug/plexsync.py`
- Shared infrastructure such as caching, matching, and config helpers lives under `beetsplug/core/`
- Plex-specific operations (playlist import, manual search UI, smart playlists, collage, Spotify transfer, search/operations shims) are in `beetsplug/plex/`
- Provider integrations are grouped in `beetsplug/providers/` (Apple, Spotify, YouTube, Tidal, JioSaavn, Gaana, M3U8, HTTP POST)
- LLM tooling resides in `beetsplug/ai/`, while lightweight presentation helpers live in `beetsplug/utils/`
- Continue using the shared `Cache` class for persistence without altering existing cache keys

## Key Technologies and Dependencies

- **Python**: The core language of the plugin.
- **Beets**: The music library manager it extends.
- **PlexAPI**: Python library for interacting with the Plex Media Server.
- **Spotipy**: Library for interacting with the Spotify Web API.
- **OpenAI**: Library for interacting with OpenAI-compatible LLMs.
- **Pydantic**: Used for data validation and settings management.
- **Agno**: A framework for building LLM agents, used for AI features and metadata search.
- **SQLite**: Used for local caching of API responses and search results.
- **LLM Search Providers**: Integrates with SearxNG, Exa, Tavily, and Brave Search for enhanced metadata search capabilities (when configured).

## Project Structure

- `setup.py`: Python package setup file.
- `README.md`: Main documentation.
- `beetsplug/`: Directory containing the plugin modules.
  - `plexsync.py`: The main plugin class and beets integrations.
  - `core/`: Shared infrastructure (`cache.py`, `config.py`, `matching.py`).
  - `plex/`: Plex-facing helpers (playlist import, manual search UI, smart playlists, collage, Spotify transfer, search/operations).
  - `providers/`: Source-specific playlist importers (Apple, Spotify, YouTube, Tidal, JioSaavn, Gaana, M3U8, HTTP POST).
  - `ai/`: LLM tooling (`llm.py`) for metadata search and playlist suggestions.
  - `utils/`: Lightweight presentation helpers.
- `collage.png`: Example output of the album collage feature.

## Configuration

The plugin is configured via beets' `config.yaml` file. Key sections include:
- `plex`: Plex server connection details (host, port, token, library name).
- `spotify`: Spotify API credentials (if importing from Spotify).
- `llm`: LLM API key and model settings, plus configuration for search providers (Ollama, SearxNG, Exa, Tavily, Brave Search).
- `plexsync`: Plugin-specific settings like `manual_search`, `use_llm_search`, and playlist definitions.

## Key Classes and Concepts

- `PlexSync`: The main plugin class inheriting from `beets.plugins.BeetsPlugin`.
- `Song`, `SongRecommendations`: Pydantic models for LLM-generated playlist data (`beetsplug/ai/llm.py`).
- `Cache`: SQLite-backed cache utilities in `beetsplug/core/cache.py`; cache keys must remain unchanged.
- `MusicSearchTools`: Agno-powered search helpers in `beetsplug/ai/llm.py`.
- Provider modules (`beetsplug/providers/*.py`): Import playlists from external sources (Spotify, Apple, YouTube, Tidal, JioSaavn, Gaana, M3U8, HTTP POST).
- `plex_track_distance`: Distance helpers in `beetsplug/core/matching.py` used for accurate item matching.
- Plex-facing helpers in `beetsplug/plex/` (manual search, playlist import, smart playlists, collage, Spotify transfer) provide reusable logic consumed by the main plugin.

## Development Conventions

- The code is written in Python, following standard Python conventions.
- Pydantic is used for data modeling and configuration validation.
- The plugin extensively uses beets' library and UI components.
- Logging is done using Python's `logging` module with the `beets` logger namespace.
- Caching is implemented to minimize redundant API calls and improve performance.
- LLM features are implemented using the `agno` framework for building agents. Manual search prompts are controlled via `plexsync.manual_search` in config (there is no CLI flag).

## Building, Running, and Testing

This is a Python package plugin for beets.

**Installation:**
```bash
pip install git+https://github.com/arsaboo/beets-plexsync.git
```

**Configuration:**
Add `plexsync` to your beets `plugins` list in `config.yaml` and configure the `plex`, `spotify`, and `llm` sections as needed.

**Usage:**
Commands are run via the beets CLI (`beet`). For example:
```bash
beet plexsync
beet plex_smartplaylists
beet plexsonic -p "mellow jazz from the 90s"
beet plexplaylistimport -m "My Playlist" -u "https://open.spotify.com/playlist/..."
```

Refer to `README.md` for a full list of commands and configuration options.

**Testing:**
Basic unit coverage exists in `tests/test_cache.py`, `tests/test_playlist_import.py`, and `tests/test_spotify_transfer.py`. For end-to-end validation, run the beets CLI commands against a test Plex server/library.