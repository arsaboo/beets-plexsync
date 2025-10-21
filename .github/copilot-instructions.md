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
  - Start dir: ./tests
  - Pattern: test_*.py
- Keep tests compatible with unittest discovery or run the CLI snippets below.

## Prerequisites and Environment Setup

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

NEVER CANCEL: All validation steps complete in under 5 seconds total but are essential.

1. Python syntax check (<1 second):
   ```bash
   cd /home/runner/work/beets-plexsync/beets-plexsync
   python3 - << 'PY'
import os, py_compile
for root, _, files in os.walk('beetsplug'):
    for f in files:
        if f.endswith('.py'):
            py_compile.compile(os.path.join(root, f))
print('All modules compiled')
PY
   ```

2. Basic import test (<1 second):
   ```bash
   PYTHONPATH=/home/runner/work/beets-plexsync/beets-plexsync python3 -c "
import sys
sys.path.insert(0, '/home/runner/work/beets-plexsync/beets-plexsync')
from beetsplug.utils.helpers import parse_title, clean_album_name
print('Core helper functions import successfully')
"
   ```

3. Configuration validation:
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
python3 -m unittest discover -s ./tests -p "test_*.py" -v
```

Provider module validation (<1 second):
```bash
cd /home/runner/work/beets-plexsync/beets-plexsync
python3 -m py_compile beetsplug/providers/*.py
echo "All provider modules compile successfully"
```

Core module validation (<1 second):
```bash
cd /home/runner/work/beets-plexsync/beets-plexsync
python3 -m py_compile beetsplug/core/matching.py beetsplug/core/cache.py beetsplug/core/config.py
echo "Core modules compile successfully"
```

Helper functions test (<1 second):
```bash
PYTHONPATH=/home/runner/work/beets-plexsync/beets-plexsync python3 -c "
from beetsplug.utils.helpers import parse_title, clean_album_name
print('Helper functions import successfully')
"
```

Plugin structure validation:
```bash
cd /home/runner/work/beets-plexsync/beets-plexsync
echo 'Plugin files:' $(find beetsplug -name "*.py" | wc -l)
echo 'Provider files:' $(ls beetsplug/providers/*.py | wc -l)
```