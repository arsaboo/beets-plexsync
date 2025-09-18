# beets-plexsync Plugin Development Instructions

**ALWAYS follow these instructions first and only fallback to additional search and context gathering if the information here is incomplete or found to be in error.**

## Project Overview

beets-plexsync is a Python plugin for [beets](https://github.com/beetbox/beets), a music library manager. The plugin provides comprehensive integration with Plex Media Server including library synchronization, AI-generated playlists, smart playlist generation, and playlist import from external services (Spotify, YouTube, Apple Music, etc.).

## Working Effectively

### VS Code and Copilot Chat (MCP) Setup

- This repo ships a preconfigured MCP server for Copilot Chat:
  - .vscode/mcp.json defines server "context7"
  - .vscode/settings.json pins allowed models via chat.mcp.serverSampling for this project
- Use Copilot Chat inside VS Code; the MCP server attaches automatically when the workspace is opened. If it doesn’t:
  - Ensure both files exist (.vscode/mcp.json and .vscode/settings.json)
  - Reload window
- Network note: MCP tools may require external network access. Given this environment’s limitations, treat external tool calls as “best-effort” and prefer local validation steps below.

### Editor, Formatting, and Tests (VS Code defaults)

- Python formatting: ms-python.black-formatter is the default. Do not change provider; "python.formatting.provider" is intentionally set to "none".
- Tests: VS Code is configured for unittest discovery with:
  - Start dir: ./beetsplug
  - Pattern: test_*.py
- Keep tests compatible with unittest discovery or run the CLI snippets below.

### Prerequisites and Environment Setup

**CRITICAL**: This environment has significant network limitations that prevent pip installations from PyPI due to timeout issues. Use system packages wherever possible.

1. **Install beets and basic dependencies**:
   ```bash
   sudo apt update
   sudo apt install -y beets beets-doc
   sudo apt install -y python3-pydantic python3-requests python3-bs4 python3-dateutil python3-confuse
   ```
   - Installation time: 2-3 minutes. NEVER CANCEL.

2. **Verify beets installation**:
   ```bash
   beet --version  # Should show "beets version 1.6.0"
   ```

3. **Set up clean beets environment for testing**:
   ```bash
   mkdir -p /tmp/beets-test
   cd /tmp/beets-test
   beet -d /tmp/beets-test/library.db config
   ```

### Plugin Installation and Testing

**CRITICAL LIMITATION**: Direct pip installation fails due to network timeouts. The plugin requires these major dependencies that cannot be installed in this environment:
- `spotipy` (Spotify integration)
- `plexapi` (Plex server communication)
- `openai` (AI features)
- `agno>=1.2.16` (LLM framework)
- `jiosaavn-python`, `tavily-python`, `exa_py`, `brave-search` (external services)

**Installation approach**:
```bash
# DOES NOT WORK due to network issues - will fail after 5+ minutes
pip install git+https://github.com/arsaboo/beets-plexsync.git
```

**Validation approach**:
1. **Syntax validation** (works without dependencies):
   ```bash
   cd /home/runner/work/beets-plexsync/beets-plexsync
   python3 -m py_compile beetsplug/*.py  # Should complete silently
   ```

2. **Basic imports** (limited without full dependencies):
   ```bash
   PYTHONPATH=/home/runner/work/beets-plexsync/beets-plexsync python3 -c "from beetsplug.helpers import parse_title; print('Helper functions work')"
   ```

3. **Plugin structure validation**:
   ```bash
   cd /home/runner/work/beets-plexsync/beets-plexsync
   ls -la beetsplug/  # Should show 14 Python files including plexsync.py
   ```

## Build and Testing Process

### Code Validation

**NEVER CANCEL**: All validation steps complete in under 5 seconds total but are essential.

1. **Python syntax check** (<1 second):
   ```bash
   cd /home/runner/work/beets-plexsync/beets-plexsync
   python3 -m py_compile beetsplug/*.py
   ```

2. **Basic import test** (<1 second):
   ```bash
   PYTHONPATH=/home/runner/work/beets-plexsync/beets-plexsync python3 -c "
   import sys
   sys.path.insert(0, '/home/runner/work/beets-plexsync/beets-plexsync')
   from beetsplug.helpers import parse_title, clean_album_name
   print('Core helper functions import successfully')
   "
   ```

3. **Configuration validation**:
   ```bash
   cd /home/runner/work/beets-plexsync/beets-plexsync
   python3 -c "
   with open('setup.py', 'r') as f:
       content = f.read()
       print('setup.py loads correctly')
       print('Content length:', len(content), 'characters')
       if 'install_requires' in content:
           print('Has install_requires section')
   "
   ```

### Manual Validation Scenarios

Since this plugin requires external services (Plex server, Spotify, etc.), full functional testing requires:

1. **Configuration Testing**: Verify plugin can be loaded by beets (requires full dependency installation)
2. **Plex Server Integration**: Test library sync commands (requires running Plex server)
3. **External Service Integration**: Test playlist imports (requires API keys)

**Due to network limitations, focus validation on**:
- Python syntax correctness ✓
- Import structure validation ✓
- Configuration file parsing ✓
- Code style consistency ✓

### Additional Validation Commands

MCP configuration validation (<1 second):
```bash
cd /home/runner/work/beets-plexsync/beets-plexsync
python3 - << 'PY'
import json, os, sys
for p in ('.vscode/mcp.json', '.vscode/settings.json'):
    with open(p, 'r') as f:
        json.load(f)
print('VS Code MCP config loads successfully')
PY
```

VS Code unittest discovery parity (<1 second):
```bash
cd /home/runner/work/beets-plexsync/beets-plexsync
python3 -m unittest discover -s ./beetsplug -p "test_*.py" -v
```

**Provider module validation** (<1 second):
```bash
cd /home/runner/work/beets-plexsync/beets-plexsync
python3 -m py_compile beetsplug/provider_*.py
echo "All provider modules compile successfully"
```

**Core module validation** (<1 second):
```bash
cd /home/runner/work/beets-plexsync/beets-plexsync
python3 -m py_compile beetsplug/matching.py beetsplug/caching.py
echo "Core modules compile successfully"
```

**Helper functions test** (<1 second):
```bash
PYTHONPATH=/home/runner/work/beets-plexsync/beets-plexsync python3 -c "
from beetsplug.helpers import parse_title, clean_album_name
print('Helper functions import successfully')
"
```

**Plugin structure validation**:
```bash
cd /home/runner/work/beets-plexsync/beets-plexsync
echo 'Plugin files:' $(ls beetsplug/*.py | wc -l)
echo 'Provider files:' $(ls beetsplug/provider_*.py | wc -l)
echo 'Total LOC:' $(cat beetsplug/*.py | wc -l)
```

## Project Structure and Navigation

### Key Files and Directories

**Repository root**: `/home/runner/work/beets-plexsync/beets-plexsync/`

```
├── README.md           # Comprehensive documentation (17KB)
├── agents.md           # AI agent context documentation
├── setup.py            # Package configuration
├── beetsplug/          # Main plugin directory
│   ├── plexsync.py     # Core plugin (154KB) - main entry point
│   ├── llm.py          # LLM/AI integration (17KB)
│   ├── matching.py     # Music matching utilities (5KB)
│   ├── caching.py      # SQLite caching system (21KB)
│   ├── helpers.py      # Utility functions (1KB)
│   ├── provider_*.py   # External service integrations:
│   │   ├── provider_apple.py     # Apple Music
│   │   ├── provider_gaana.py     # Gaana.com
│   │   ├── provider_jiosaavn.py  # JioSaavn
│   │   ├── provider_m3u8.py      # M3U8 playlists
│   │   ├── provider_post.py      # Custom HTTP POST
│   │   ├── provider_tidal.py     # Tidal
│   │   └── provider_youtube.py   # YouTube
│   └── __init__.py     # Package initialization
└── collage.png         # Example album collage output (3.5MB)
```

### Core Components

1. **PlexSync class** (`plexsync.py`): Main plugin class inheriting from `BeetsPlugin`
2. **Smart playlist algorithms**: Daily Discovery, Forgotten Gems, Recent Hits
3. **AI integration**: OpenAI-compatible LLM for playlist generation
4. **External importers**: 7 different music service providers
5. **Caching system**: SQLite-based performance optimization

## Common Development Tasks

### Code Modification Guidelines

1. **Always run syntax validation** after any changes:
   ```bash
   python3 -m py_compile beetsplug/plexsync.py
   ```

2. **Plugin command structure** - all commands start with `beet`:
   - `beet plexsync` - Library sync
   - `beet plex_smartplaylists` - Generate smart playlists
   - `beet plexsonic -p "prompt"` - AI playlist generation
   - `beet plexplaylistimport -m "name" -u "url"` - Import playlists

3. **Configuration location**: Plugin reads from beets `config.yaml`
   - Plex server settings under `plex:` section
   - Plugin settings under `plexsync:` section
   - LLM settings under `llm:` section

### Debugging and Troubleshooting

1. **Import errors**: Usually indicate missing dependencies
   ```bash
   PYTHONPATH=/home/runner/work/beets-plexsync/beets-plexsync python3 -c "
   try:
       from beetsplug.plexsync import PlexSync
       print('Success')
   except ImportError as e:
       print('Missing dependency:', e)
   "
   ```

2. **Configuration issues**: Check beets config path
   ```bash
   beet config -p  # Shows config file location
   ```

3. **Network timeouts**: This environment has significant network limitations
   - PyPI installations fail with ReadTimeoutError
   - Use system packages when possible
   - Document network-dependent features as "requires external network access"

## Timing Expectations and Limitations

### Expected Command Times
- **Syntax validation**: <1 second - NEVER CANCEL (actually ~0.07s)
- **Basic imports**: <1 second - NEVER CANCEL (actually ~0.03s)
- **Full dependency installation**: 5+ minutes - WILL FAIL due to network timeouts
- **Plugin loading with beets**: 10 seconds (requires full dependencies)
- **All validation steps combined**: <5 seconds total

### Known Limitations in This Environment

**CRITICAL**: Network connectivity issues prevent:
1. **PyPI package installation** - pip commands timeout after 2-5 minutes
2. **External API testing** - Cannot reach Spotify, Plex, or other services
3. **Full functional validation** - Limited to syntax and import testing

**Working validation approaches**:
1. Python syntax compilation ✓
2. Import structure verification ✓
3. Configuration file parsing ✓
4. Code review and static analysis ✓

## Development Workflow

### Making Changes

1. **Before coding**: Always run initial validation
   ```bash
   cd /home/runner/work/beets-plexsync/beets-plexsync
   python3 -m py_compile beetsplug/*.py
   ```

2. **After changes**: Validate immediately
   ```bash
   python3 -m py_compile beetsplug/[modified_file].py
   ```

3. **Test imports**: Ensure module loading works
   ```bash
   PYTHONPATH=/home/runner/work/beets-plexsync/beets-plexsync python3 -c "from beetsplug.plexsync import [your_changes]"
   ```

### Plugin Architecture Understanding

- **BeetsPlugin inheritance**: Core plugin follows beets plugin architecture
- **Command registration**: Commands registered in plugin's `commands()` method
- **Database integration**: Uses beets' database for music metadata
- **External API caching**: Uses SQLite for performance optimization
- **Configuration management**: Uses beets' confuse library

## Quick Reference Commands

```bash
# Repository validation
cd /home/runner/work/beets-plexsync/beets-plexsync && python3 -m py_compile beetsplug/*.py

# MCP config check
cd /home/runner/work/beets-plexsync/beets-plexsync && python3 - << 'PY'
import json; json.load(open('.vscode/mcp.json')); json.load(open('.vscode/settings.json')); print('MCP OK')
PY

# Basic functionality test
PYTHONPATH=/home/runner/work/beets-plexsync/beets-plexsync python3 -c "from beetsplug.helpers import parse_title; print('OK')"

# Project structure overview
ls -la beetsplug/

# Documentation review
head -50 README.md

# Setup configuration review
cat setup.py
```

**Remember**: This environment cannot perform full installation or external service testing due to network limitations. Focus on code quality, structure, and syntax validation.