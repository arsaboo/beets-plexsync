# Harmony - Standalone Music Library Sync & Playlist Management

**Harmony** is a standalone Python application (not a beets plugin) for managing your music library with **Plex Media Server**. It provides intelligent playlist generation, multi-source imports, and advanced search—without requiring beets.

> **Note**: This is evolved from [beets-plexsync](https://github.com/arsaboo/beets-plexsync) but is now a standalone application with optional beets integration, not a plugin.

## ✨ Key Features

- **🎵 Multi-Source Playlist Import**: Spotify, YouTube, Apple Music, Tidal, Gaana, JioSaavn, M3U8
- **🎯 Smart Playlists**: 7 automatic playlist types (Daily Discovery, Forgotten Gems, Recent Hits, etc.)
- **🤖 AI Playlist Generation**: Create playlists from natural language using LLMs (Ollama, OpenAI)
- **⚡ Fast Search**: 6-strategy pipeline with vector caching (150x faster after first run)
- **💾 Persistent Caching**: SQLite + JSON vector index for instant results
- **🔌 Flexible Backends**: Works with Plex (required) + optional beets + AudioMuse enrichment
- **🚀 Batch Processing**: Generate multiple playlists efficiently with single cache build

## 🎛️ Beets Integration (Optional)

Harmony works standalone with Plex, but integrating **beets** provides significant performance and metadata improvements:

**Benefits**:
- **Faster startup**: Skips Plex library scan when beets is configured (uses beets as source of truth)
- **Better metadata**: Leverages beets' superior genre, year, and tag information
- **Faster cache validation**: SQLite COUNT query instead of Plex API call
- **Rich provider IDs**: Spotify/MusicBrainz IDs from beets for better matching

**Setup**:
```yaml
beets:
  library_db: "\\\\server\\path\\musiclibrary.blb"  # Or /path/to/musiclibrary.blb on Unix
```

**Performance Comparison** (60k tracks):
| Operation | Without Beets | With Beets | Speedup |
|-----------|---------------|------------|---------|
| First startup (vector index build) | ~90 sec | ~85 sec | 1.06x |
| Cache validation | Plex API call | SQLite COUNT | 10x+ |
| Subsequent startups | ~5 sec | ~1 sec | 5x |
| Smart playlist generation | Same speed | Same speed | 1x |

💡 **Recommendation**: If you use beets, configure it in Harmony for optimal performance!

## 🎧 AudioMuse Integration (Optional)

Harmony can optionally enrich tracks with **AudioMuse-AI** acoustic analysis, adding mood/energy filters to smart playlists.

**Benefits**:
- **Mood-aware filtering**: Filter playlists by energy, danceability, happy/sad, relaxed, party, etc.
- **Acoustic categories**: Use mood categories (rock, pop, oldies, etc.) from AudioMuse analysis
- **Cached enrichment**: Only fetches data when needed and caches results

**Setup**:
```yaml
providers:
  audiomuse:
    base_url: "http://localhost:8001"
    enabled: true
    timeout: 30
    acoustic_enrichment: true
    cache_ttl_days: 7
```

## 🚀 Quick Start

### 1. Installation

```bash
# Clone repo
git clone https://github.com/yourusername/harmony.git
cd harmony

# Install
pip install -e .
```

### 2. Configuration

```bash
# Copy example config
cp harmony.yaml.example harmony.yaml

# Edit with your Plex credentials
nano harmony.yaml
```

**Minimal `harmony.yaml`**:
```yaml
plex:
  host: "192.168.1.100"
  port: 32400
  token: "YOUR_PLEX_TOKEN"        # Get from Plex > Settings > Remote Access
  library_name: "Music"

# Optional: Beets integration for faster search & better metadata
beets:
  library_db: "/path/to/musiclibrary.blb"

# Optional: AudioMuse enrichment for mood-based filtering
providers:
  audiomuse:
    base_url: "http://localhost:8001"
    enabled: true

# Optional: Configure smart playlists for batch generation
playlists:
  smart:
    - name: "Daily Discovery"
      type: daily_discovery
      num_tracks: 50
      enabled: true
```

### 3. Test Connection

```bash
python -c "from harmony import Harmony; h = Harmony(); h.initialize(); print('✓ Connected!')"
```

### 4. Generate Playlists

**CLI (Recommended)**:
```bash
# Generate a single smart playlist
harmony smart-playlist "My Daily Mix" --type daily_discovery --count 50

# Generate multiple playlists efficiently (builds cache once!)
harmony smart-playlists --all

# Or generate specific playlists
harmony smart-playlists "Daily Discovery" "Forgotten Gems"
```

**Python API**:
```python
from harmony import Harmony

h = Harmony("harmony.yaml")
h.initialize()

# Generate a smart playlist
result = h.generate_smart_playlist(
    "My Daily Discovery",
    playlist_type="daily_discovery",
    num_tracks=50
)
print(f"Created {result['selected_count']} track playlist")

h.shutdown()
```

## 📚 Usage Examples

### CLI Commands

```bash
# Import a playlist from URL
harmony import-playlist "My Mix" "https://open.spotify.com/playlist/ID"

# Generate a single smart playlist
harmony smart-playlist "Daily Mix" --type daily_discovery --count 50

# Generate multiple smart playlists efficiently (recommended!)
harmony smart-playlists --all

# Generate specific playlists only
harmony smart-playlists "Daily Discovery" "Forgotten Gems" "Recent Hits"

# Create AI playlist (requires LLM configured)
harmony ai-playlist "Workout Mix" --mood energetic --genre rock --count 30

# Refresh vector index cache
harmony refresh-cache
```

### Python API

#### Search for Tracks

```python
# Search by title + artist
track = h.search_plex_song({
    "title": "Bohemian Rhapsody",
    "artist": "Queen"
})
print(f"Found: {track['title']} ({track['plex_ratingkey']})")
```

### Import from Spotify

```python
# Import Spotify playlist
count = h.import_playlist_from_url(
    "My Spotify Hits",
    "https://open.spotify.com/playlist/PLAYLIST_ID"
)
print(f"Imported {count} tracks")
```

### Create AI Playlists

```python
# Enable LLM
h.init_llm(provider="ollama", model="qwen3:latest")

# Generate from prompt
count = h.generate_ai_playlist(
    "Workout Mix",
    mood="energetic",
    genre="rock"
)
print(f"AI playlist: {count} matched tracks")
```

## 🎯 Playlist Types

| Type | Purpose | Key Factors |
|------|---------|------------|
| **daily_discovery** | Mix of familiar + new | Rating, popularity, recency |
| **forgotten_gems** | Highly-rated but unplayed | High rating, days since played |
| **recent_hits** | New popular releases | Release year, popularity |
| **fresh_favorites** | New releases you rated | Rating, release year |
| **70s80s_flashback** | Era-specific nostalgia | Era filter, rating |
| **highly_rated** | Your top-rated tracks | User rating only |
| **most_played** | Frequently played | Play count |
| **energetic_workout** | High energy, fast tempo | Rating, popularity, recency |
| **relaxed_evening** | Calm, low energy | Rating, recency |

### Mood Filters (AudioMuse)

When AudioMuse is enabled, smart playlists can add a `filters.mood` section:

```yaml
filters:
  mood:
    min_energy: 0.6
    min_tempo: 120
    min_danceable: 0.5
    max_sad: 0.3
    mood_categories: [rock, pop]
```

## ⚙️ Architecture

### Components

```
harmony/
├── backends/              # Plex, Beets
├── core/                  # Cache, Vector Index, Matching
├── plex/                  # Search, Smart Playlists, Imports
├── providers/             # Spotify, YouTube, etc. (wraps beetsplug)
└── ai/                    # LLM integration
```

### Search Pipeline

Harmony implements a sophisticated **5-stage search pipeline** for maximum accuracy:

```
1. Cache      → 2. Vector Index → 3. Plex API    → 4. LLM Enhancement → 5. Manual Search
   ↓              ↓                 ↓                ↓                    ↓
 O(1)         Cosine Sim.       6 Strategies      Metadata Cleanup    User Confirmation
 Return       (score ≥ 0.8)     (score ≥ 0.7)     Recursive Search    Interactive UI
```

**Stages**:
1. **Cache Lookup**: Instant O(1) retrieval with negative caching and cleaned metadata
2. **Vector Index**: Token-based cosine similarity with direct ratingKey matching (score ≥ 0.8)
3. **Plex API Search**: 6 progressive strategies (Album+Title → Artist+Title → Fuzzy → etc.)
4. **LLM Enhancement**: Optional AI-powered metadata cleanup and retry (if enabled)
5. **Manual Search**: Interactive confirmation queue for ambiguous matches (0.0-0.8 similarity)

**Performance**:
- First run: 36 seconds (builds index)
- Subsequent runs: 0.24 seconds (loads cache) = **150x speedup**
- With LLM: +2-5 seconds per cleanup attempt

**Similarity Thresholds**:
- `≥ 0.8`: Auto-accept (direct matches, variants, single results)
- `0.7-0.8`: Auto-accept multiple tracks, queue single tracks
- `0.35-0.7`: Queue for manual confirmation (if enabled)
- `< 0.35`: Ignore candidate

📖 **[Read Complete Documentation](SEARCH_WORKFLOW.md)** | **[Quick Reference](QUICK_REFERENCE_SEARCH.md)**

## 🔧 Configuration

### Smart Playlists

**Config-based batch generation (recommended for efficiency)**:

```yaml
playlists:
  defaults:
    history_days: 15           # Look back for listening history
    exclusion_days: 30         # Exclude recently played from discovery
    manual_search: false       # Auto-match, no prompts
    clear_playlist: true       # Clear before regenerating

  smart:
    - name: "Daily Discovery"
      type: daily_discovery
      num_tracks: 50
      enabled: true
    
    - name: "Forgotten Gems"
      type: forgotten_gems
      num_tracks: 40
      enabled: true
    
    - name: "Recent Hits"
      type: recent_hits
      num_tracks: 30
      enabled: true
    
    - name: "Fresh Favorites"
      type: fresh_favorites
      num_tracks: 30
      enabled: false  # Skip this one

    - name: "70s80s Flashback"
      type: 70s80s_flashback
      num_tracks: 40
      enabled: true
```

**Then generate efficiently**:
```bash
# Builds cache once, generates all enabled playlists
harmony smart-playlists --all

# Or generate specific ones
harmony smart-playlists "Daily Discovery" "Forgotten Gems"
```

**⚡ Performance Benefit**: 
- Individual commands: 5 playlists × 1.5 min = **7.5 minutes**
- Batch command: 1 cache build + 5 generations = **~2 minutes** (5x faster!)

### LLM (AI Playlists & Search Enhancement)

```yaml
llm:
  enabled: true

  # Ollama (free, self-hosted)
  provider: "ollama"
  model: "qwen3:latest"
  ollama_host: "http://localhost:11434"

  # Or OpenAI
  # provider: "openai"
  # model: "gpt-4"
  # api_key: "${OPENAI_API_KEY}"

  # Enable LLM-enhanced search (Stage 4)
  # When enabled, failed searches will use LLM to clean metadata and retry
  use_llm_search: false

  # Search tools (required for use_llm_search)
  search:
    searxng_host: "http://localhost:8888"  # At least one required
    # OR
    brave_api_key: "YOUR_KEY"
    # OR
    exa_api_key: "YOUR_KEY"
    # OR
    tavily_api_key: "YOUR_KEY"
```

### Search Options

```yaml
search:
  # Enable manual confirmation for ambiguous matches (similarity 0.0-0.8)
  # When enabled, you'll review queued candidates and manually search if needed
  use_manual_confirmation: false

  # Similarity threshold for auto-accepting matches (0.0-1.0)
  # Lower = more lenient, Higher = more strict
  similarity_threshold: 0.7

  # Use LLM to clean metadata (requires llm.use_llm_search: true)
  use_llm_cleanup: false
```

### Imported Playlists

```yaml
playlists:
  imported:
    - name: "Spotify Mix"
      sources:
        - "https://open.spotify.com/playlist/ID"
      max_tracks: 100
      manual_search: false
```

## 🆚 Harmony vs beets-plexsync

| Feature | Harmony | beets-plexsync |
|---------|---------|----------------|
| **Installation** | Standalone app | beets plugin |
| **Plex Required** | ✅ Yes | ✅ Yes |
| **Beets Required** | ❌ No (optional) | ✅ Yes |
| **Playlists** | 7 smart types | Same 7 types |
| **AI Playlists** | ✅ Yes | ✅ Yes |
| **Imports** | Spotify, YT, Apple, etc. | Same |
| **Speed** | 150x faster (cached) | Slower (rebuilds) |
| **Architecture** | Modular, extensible | Plugin-based |

## 🛠️ Development

### Test Connection

```bash
python -c "
from harmony import Harmony
h = Harmony('harmony.yaml')
h.initialize()
print(f'✓ {len(h.vector_index)} tracks indexed')
h.shutdown()
"
```

### Run Tests

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

### Debug Search

```python
from harmony import Harmony

h = Harmony()
h.initialize()

# Detailed search output
result = h.search_plex_song({
    "title": "Test",
    "artist": "Artist"
}, use_local_candidates=True)

print(f"Cache hit: {result is not None}")
print(f"Index size: {len(h.vector_index)}")
```

## 📝 Examples

See [examples/](./examples/) folder for:
- `daily_sync.py` - Auto-generate smart playlists
- `import_spotify.py` - Weekly Spotify import
- `ai_mood_playlists.py` - LLM-generated mood mixes
- `batch_import.py` - Import multiple sources

## 🐛 Troubleshooting

### Slow First Run
Expected! Vector index building takes ~36s for 2,700+ tracks. Subsequent runs are instant.

**Tip**: If you have beets configured, the first run will skip the Plex library scan and use beets data instead.

### "Cache invalid or missing - building fresh vector index"
This happens when:
- First run (no cache exists)
- Library size changed by >2%
- Cache files were deleted

**Solution**: This is normal! The cache will be rebuilt (takes ~1-2 min for large libraries) and subsequent runs will be fast.

### Plex Connection Failed
```bash
# Test your token
curl -H "X-Plex-Token: YOUR_TOKEN" "http://192.168.1.100:32400/library/sections"

# Update harmony.yaml
plex:
  explicit_url: "http://192.168.1.100:32400"
```

### LLM Not Working
```bash
# Install Ollama: https://ollama.ai
ollama pull qwen3:latest
ollama serve  # Runs on port 11434
```

## 💡 Best Practices

### 1. Use Batch Playlist Generation
Instead of running individual `smart-playlist` commands, define all playlists in config and use:
```bash
harmony smart-playlists --all
```
This builds the cache **once** and reuses it for all playlists (5x faster for multiple playlists).

### 2. Configure Beets for Performance
If you use beets, configure it in Harmony:
```yaml
beets:
  library_db: "/path/to/musiclibrary.blb"
```
Benefits:
- Skips Plex library scan on startup
- Faster cache validation (SQLite vs Plex API)
- Better metadata for smart playlists

### 3. Enable Persistent Cache
Harmony automatically caches results in:
- `harmony_vector_index.json` - Vector index (fast search)
- `harmony_vector_index.meta.json` - Cache metadata
- SQLite DB - Search results

**Don't delete these files** unless you want to force a rebuild.

### 4. Optimal Smart Playlist Workflow
```bash
# Step 1: Define all playlists in harmony.yaml once
# Step 2: Run batch generation (daily/weekly via cron)
harmony smart-playlists --all

# Or refresh specific ones
harmony smart-playlists "Daily Discovery" "Forgotten Gems"
```

### 5. Schedule Regular Updates
Add to crontab (Unix) or Task Scheduler (Windows):
```bash
# Daily at 6 AM
0 6 * * * cd /path/to/harmony && harmony smart-playlists --all
```

## 📖 Documentation

- **[Configuration Guide](./docs/CONFIGURATION.md)** - Detailed config options
- **[API Reference](./docs/API.md)** - Python API documentation
- **[Architecture](./docs/ARCHITECTURE.md)** - Technical details
- **[FAQ](./docs/FAQ.md)** - Common questions

## 🤝 Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Add tests for new functionality
4. Submit a pull request

## 📄 License

MIT License - See [LICENSE](./LICENSE)

## 🙏 Credits

- Original [beets-plexsync](https://github.com/arsaboo/beets-plexsync) by [arsaboo](https://github.com/arsaboo)
- [plexapi](https://github.com/pkkid/python-plexapi) for Plex integration
- [Agno](https://github.com/phidatahq/agno) for LLM framework
- Search providers: [Brave](https://search.brave.com/api/), [Exa](https://exa.ai), [Tavily](https://tavily.com), [SearxNG](https://searxng.github.io/searxng/)

---

**Harmony** - Your music, perfectly organized 🎵

For updates and discussions, visit [GitHub Issues](https://github.com/yourusername/harmony/issues)
