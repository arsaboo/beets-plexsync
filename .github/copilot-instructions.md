# GitHub Copilot Instructions for beets-plexsync

## Project Overview

This is a Python plugin for [beets](https://github.com/beetbox/beets), a music library manager. The `plexsync` plugin provides comprehensive tools to synchronize and manage music libraries between beets and Plex Media Server.

**Core Functionality:**
- Library synchronization (ratings, play counts, last played dates) between Plex and beets
- AI-generated playlists using LLMs (OpenAI, Ollama models)  
- Smart dynamic playlists (Daily Discovery, Forgotten Gems, Recent Hits)
- External playlist import from Spotify, Apple Music, YouTube, Tidal, JioSaavn, Gaana
- Playlist management and metadata search with LLM assistance

## Architecture & Key Files

```
beetsplug/
├── plexsync.py      # Main plugin class and core logic
├── llm.py           # LLM integration using agno framework
├── matching.py      # Custom track matching utilities  
├── caching.py       # SQLite-based caching system
├── helpers.py       # Utility functions
└── provider_*.py    # External service integrations
```

## Development Guidelines

### Critical Constraints ⚠️
- **NEVER modify cache keys** - They're stored in SQLite database and changes will invalidate cached data
- Preserve existing API interfaces and method signatures when possible
- Maintain compatibility with beets plugin architecture
- Follow beets plugin conventions for commands, configuration, and UI

### Code Patterns
- Use Python's `logging` module with `beets` logger namespace
- Implement Pydantic models for data validation and structured data
- Cache external API calls using the existing `Cache` class
- Use `agno` framework for LLM-related features
- Handle errors gracefully with appropriate logging

### Configuration
- Plugin configured via beets' `config.yaml` file
- Key sections: `plex`, `spotify`, `llm`, `plexsync`
- LLM search supports multiple providers: Ollama, SearxNG, Exa, Tavily, Brave Search

### Dependencies
- **Core**: `beets`, `plexapi`, `pydantic>=2.0.0`
- **LLM**: `agno>=1.2.16`, `openai`  
- **External Services**: `spotipy`, `jiosaavn-python`, etc.
- **Utilities**: `requests`, `beautifulsoup4`, `python-dateutil`

## Common Tasks

### Adding New Commands
```python
def cmd_yourcommand(self, lib, opts, args):
    """Command description for help text."""
    # Implementation
    pass

# Register in __init__
self.register_listener('cli_yourcommand', self.cmd_yourcommand)
```

### Working with Plex API
```python
# Use existing connection pattern
plex = self.get_plex()
library = plex.library.section(self.config['library_name'].get())
```

### Caching API Results
```python
from beetsplug.caching import Cache

cache = Cache(self.config['cache_file'].get())
result = cache.get(key)
if result is None:
    result = expensive_api_call()
    cache.set(key, result)
```

### LLM Integration
```python
from beetsplug.llm import get_search_toolkit, search_track_info

# For metadata search
track_info = search_track_info("song query")

# For recommendations  
toolkit = get_search_toolkit()
recommendations = toolkit.get_recommendations(prompt)
```

## Testing & Development

- Test commands via beets CLI: `beet plugincommand`
- No formal unit tests - test against real Plex server
- Use `self._log.debug()` for debugging output
- Follow existing error handling patterns

## Code Organization

- **Provider modules** (`provider_*.py`): Import logic for specific services
- **Core logic** (`plexsync.py`): Main plugin functionality 
- **Utilities** (`helpers.py`): Shared helper functions
- **Specialized features**: LLM (`llm.py`), matching (`matching.py`), caching (`caching.py`)

When suggesting code, prioritize compatibility with existing patterns and maintain the plugin's modular structure.