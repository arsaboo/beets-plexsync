# beets-plexsync Copilot Instructions

This is a Python plugin for the [beets](https://github.com/beetbox/beets) music library manager that provides comprehensive Plex Media Server integration.

## Key Project Context

**Plugin Type**: beets plugin (extends BeetsPlugin)
**Main Language**: Python 3.7+
**Primary Dependencies**: beets, plexapi, pydantic, agno (LLM framework)

## Core Features
- Sync library data (ratings, play counts) between Plex and beets
- Generate AI-powered playlists using LLMs (GPT, Ollama)
- Create smart playlists (Daily Discovery, Forgotten Gems, Recent Hits)
- Import playlists from external services (Spotify, Apple Music, YouTube, etc.)

## Critical Development Rules

🚨 **NEVER modify cache keys** - They're stored in SQLite and changes break cached data
🚨 **Preserve API compatibility** - Don't break existing method signatures
🚨 **Follow beets conventions** - Use beets' plugin patterns, UI components, and configuration system

## Code Patterns to Follow

### Logging
```python
# Always use beets logger
self._log.debug("Debug message")
self._log.info("Info message") 
self._log.error("Error message")
```

### Configuration Access
```python
# Access plugin config
value = self.config['setting_name'].get()
library_name = self.config['library_name'].get()
```

### Plex Connection
```python
# Use existing connection method
plex = self.get_plex()
library = plex.library.section(self.config['library_name'].get())
```

### Caching Pattern
```python
from beetsplug.caching import Cache

cache = Cache(self.config['cache_file'].get())
key = f"cache_key:{query}"
result = cache.get(key)
if result is None:
    result = expensive_operation()
    cache.set(key, result)
```

### Pydantic Models
```python
# Use for data validation
from pydantic import BaseModel, Field

class SongInfo(BaseModel):
    title: str = Field(..., description="Song title")
    artist: str = Field(..., description="Artist name") 
```

## File Organization
- `plexsync.py`: Main plugin class and commands
- `llm.py`: LLM integration with agno framework
- `caching.py`: SQLite caching system
- `matching.py`: Track matching utilities
- `provider_*.py`: External service integrations
- `helpers.py`: Shared utilities

## Common Command Pattern
```python
def cmd_newcommand(self, lib, opts, args):
    """Command description for --help"""
    try:
        # Command implementation
        self._log.info("Command executed successfully")
    except Exception as e:
        self._log.error("Command failed: {}", e)
        return
```

## Testing Approach
- Test manually with `beet command` against real Plex server
- No formal unit tests - integration testing preferred
- Use debug logging extensively during development

Always maintain backward compatibility and follow existing code patterns when making suggestions.