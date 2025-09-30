# beets-plexsync
A plugin for [beets][beets] to sync with your Plex server.

## Key Features

### AI-Generated Playlists
- **AI-Generated Playlists**: Use `beet plexsonic -p "YOUR_PROMPT"` to create a playlist based on YOUR_PROMPT. Modify the playlist name using `-m` flag, change the number of tracks requested with `-n` flag, and clear the playlist before adding new songs with `-c` flag.

### Smart Playlists
Use `beet plex_smartplaylists [-o ONLY]` to generate or manage custom playlists in Plex. The plugin currently supports four types of playlists:

You can use the `-o` or `--only` option to specify a comma-separated list of playlist IDs to update. This is useful for updating only certain playlists (e.g., just the AI playlists) on a schedule:

```sh
beet plex_smartplaylists -o daily_discovery,forgotten_gems
```

The command will only generate the specified playlists, skipping others in your configuration.

  1. **Daily Discovery**:
      - Uses tracks you've played in the last 15 days as a base to learn about listening habits (configurable via `history_days`)
      - Excludes tracks played in the last 30 days (configurable via `exclusion_days`)
      - Uses an intelligent scoring system that considers:
          - Track popularity relative to your library
          - Rating for rated tracks
          - Recency of addition to library
          - Release year (favors newer releases)
      - Introduces controlled randomization to ensure variety
      - Matches genres with your recent listening history using both sonic analysis and library-wide genre preferences
      - Uses Plex's [Sonic Analysis](https://support.plex.tv/articles/sonic-analysis-music/) to find sonically similar tracks
      - Also discovers tracks from your entire library that match your preferred genres
      - Limits the playlist size (configurable via `max_tracks`, default 20)
      - Controls discovery vs. familiar ratio (configurable via `discovery_ratio`, default 30% - more familiar tracks)

  2. **Forgotten Gems**:
      - Creates a playlist of tracks that deserve more attention
      - Uses your highly-rated tracks to establish a quality baseline
      - Prioritizes unrated tracks with popularity comparable to your favorites
      - Only includes tracks matching your genre preferences
      - Automatically adjusts selection criteria based on your library's characteristics
      - Limits the playlist size (configurable via `max_tracks`, default 20)
      - Controls maximum play count (configurable via `max_plays`, default 2)
      - Minimum rating for rated tracks to be included (configurable via `min_rating`, default 4)
      - Percentage of playlist to fill with unrated but popular tracks (configurable via `discovery_ratio`, default 30%)
      - Excludes tracks played recently (configurable via `exclusion_days`)

  3. **Recent Hits**:
      - Curates a playlist of recent, high-energy tracks
      - Applies a default release-year guard covering roughly the last 3 years whenever no year filter is provided; override with `filters.include.years` or the playlist-level `max_age_years`/`min_year` options
      - Updated scoring leans harder on release recency and last-play data, with popularity and ratings acting as the tie-breakers
      - Uses weighted randomness for track selection while respecting your genre preferences
      - Automatically adjusts selection criteria and limits size (configurable via `max_tracks`, default 20)
      - Requires a minimum rating (`min_rating`, default 4) and lets you control the discovery ratio (default 20%)
      - Set `exclusion_days` if you want to keep very recent listens out (default 30 days)

  4. **Fresh Favorites**:
      - Creates a playlist of high-quality tracks that deserve more plays
      - Enforces a default release window spanning roughly the last 7 years unless you supply custom year filters or specify `max_age_years`/`min_year`
      - Updated scoring strongly favors release recency and recent spins while still rewarding strong ratings and popularity
      - Skips tracks without a trusted release year when the recency guard is active to keep the mix on-theme
      - Defaults: `max_tracks: 100`, `discovery_ratio: 25`, `min_rating: 6`, `exclusion_days: 21`

  5. **Imported Playlists**:
      - Import playlists from external services (Spotify, Apple Music, YouTube, etc.) and local M3U8 files
      - Configure multiple source URLs and file paths per playlist
      - For M3U8 files, use paths relative to beets config directory or absolute paths
      - Support for custom HTTP POST requests to fetch playlists
      - Control playlist behavior with options:
        - `manual_search`: Enable/disable manual matching for unmatched tracks
        - `clear_playlist`: Clear existing playlist before adding new tracks
        - `max_tracks`: Limit the number of tracks in the playlist

You can use config filters to finetune any playlist. You can specify the `genre`, `year`, and `UserRating` to be included and excluded from any of the playlists. See the extended example below.

### Library Sync
- **Plex Library Sync**: `beet plexsync [-f]` imports all the data from your Plex library inside beets. Use the `-f` flag to force update the entire library with fresh information from Plex.
- **Recent Sync**: `beet plexsyncrecent [--days N]` updates the information for tracks listened in the last N days (default: 7). For example, `beet plexsyncrecent [--days 14]` will update tracks played in the last 14 days.

### Playlist Manipulation
- **Playlist Manipulation**: `beet plexplaylistadd [-m PLAYLIST] [QUERY]` and `beet plexplaylistremove [-m PLAYLIST] [QUERY]` add or remove tracks from Plex playlists. Use the `-m` flag to provide the playlist name. You can use any [beets query][queries_] as an optional filter.
- **Playlist Clear**: `beet plexplaylistclear [-m PLAYLIST]` clears a Plex playlist. Use the `-m` flag to specify the playlist name.

### Playlist Import
- **Playlist Import**: `beet plexplaylistimport [-m PLAYLIST] [-u URL] [-l]` imports individual playlists from Spotify, Apple Music, Gaana.com, JioSaavn, Youtube, Tidal, M3U8 files, custom APIs, and ListenBrainz. Use the `-m` flag to specify the playlist name and:
  - For online services: use the `-u` flag to supply the full playlist url
  - For M3U8 files: use the `-u` flag with the file path (relative to beets config directory or absolute path)
  - For custom APIs: configure POST requests in config.yaml (see Configuration section)
  - For ListenBrainz: use the `-l` or `--listenbrainz` flag to import "Weekly Jams" and "Weekly Exploration" playlists

  You can define multiple sources per playlist in your config including custom POST endpoints:
  ```yaml
  - name: "Mixed Sources Playlist"
    type: "imported"
    sources:
      - "https://open.spotify.com/playlist/37i9dQZF1DX0kbJZpiYdZl"  # Spotify
      - "playlists/local.m3u8"                                      # Local M3U8
      - type: "post"                                                # Custom API
        server_url: "http://localhost:8000/api/playlist"
        headers:
          Authorization: "Bearer your-token"
        payload:
          playlist_url: "https://example.com/playlist/123"
  ```

  For each import session, a detailed log file is created in your beets config directory (named `<playlist_name>_import.log`) that records:
  - Tracks that couldn't be found in your Plex library
  - Low-rated tracks that were skipped
  - Import statistics and summary
  - The log file helps you identify which tracks need manual attention
- **Youtube Search Import**: `beet plexsearchimport [-m PLAYLIST] [-s SEARCH] [-l LIMIT]` imports playlists based on Youtube search. Use the `-m` flag to specify the playlist name, the `-s` flag for the search query, and the `-l` flag to limit the number of search results.

### Additional Tools
- **Plex to Spotify**: `beet plex2spotify [-m PLAYLIST] [QUERY]` copies a Plex playlist to Spotify. Use the `-m` flag to specify the playlist name.

  You can use [beets queries][queries_] with this command to filter which tracks are sent to Spotify. For example, to add only tracks with a `plex_userrating` greater than 2 to the "Sufiyana" playlist, use:

  ```sh
  beet plex2spotify -m "Sufiyana" plex_userrating:2..
  ```

  Additional filtering examples:
  - Only transfer highly-rated tracks: `beet plex2spotify -m "My Playlist" plex_userrating:8..`
  - Transfer tracks by specific artist: `beet plex2spotify -m "Rock Hits" artist:"The Beatles"`
  - Transfer tracks from a specific year range: `beet plex2spotify -m "2000s Hits" year:2000..2009`
  - Combine multiple filters: `beet plex2spotify -m "Recent Favorites" plex_userrating:7.. year:2020..`
- **Playlist to Collection**: `beet plexplaylist2collection [-m PLAYLIST]` converts a Plex playlist to a collection. Use the `-m` flag to specify the playlist name.
- **Album Collage**: `beet plexcollage [-i INTERVAL] [-g GRID]` creates a collage of most played albums. Use the `-i` flag to specify the number of days and `-g` flag to specify the grid size.

### Manual Import for Failed Tracks
The plugin creates detailed import logs for each playlist import session. You can manually process failed imports using:

- `beet plex_smartplaylists [--import-failed] [--log-file LOGFILE]`: Process all import logs and attempt manual matching for failed tracks, or process a specific log file.

This is especially useful when:
- You've added new music to your library and want to retry matching previously failed tracks
- You want to manually match specific tracks from a particular playlist's import log
- You need to clean up import logs by removing successfully matched tracks

## Introduction

This plugin allows you to sync your Plex library with beets, create playlists based on AI-generated prompts, import playlists from other online services, and more.

## Installation

Install the plugin using `pip`:

```shell
pip install git+https://github.com/arsaboo/beets-plexsync.git
```

Then, [configure](#configuration) the plugin in your [`config.yaml`][config] file.

To upgrade, use the command:
```shell
pip install --upgrade --force-reinstall --no-deps git+https://github.com/arsaboo/beets-plexsync.git
```

## Configuration

Add `plexsync` to your list of enabled plugins.

```yaml
plugins: plexsync

# If you want to use the ListenBrainz import feature, you'll need to configure
# the ListenBrainz plugin. See https://github.com/arsaboo/beets-listenbrainz for setup.
listenbrainz:
  user_token: YOUR_USER_TOKEN
  username: YOUR_USERNAME
```

Next, you can configure your Plex server and library like following (see instructions to obtain Plex token [here][plex_token]).

```yaml
plex:
  host: '192.168.2.212'
  port: 32400
  token: PLEX_TOKEN
  library_name: 'Music'
```

If you want to import `spotify` playlists, you will also need to configure the `spotify` plugin. If you are already using the [Spotify][Spotify] plugin, `plexsync` will reuse the same configuration.
```yaml
spotify:
  client_id: CLIENT_ID
  client_secret: CLIENT_SECRET
```

* The `beet plexsonic` command allows you to create AI-based playlists using an OpenAI-compatible language model. To use this feature, you will need to configure the AI model with an API key. Once you have obtained an API key, you can configure `beets` to use it by adding the following to your `config.yaml` file:

  ```yaml
  llm:
      api_key: API_KEY
      model: "gpt-3.5-turbo"
      base_url: "https://api.openai.com/v1"  # Optional, for other providers
      search:
        provider: "ollama"                                # Search provider (ollama is default)
        model: "qwen2.5:latest"                           # Model to use for search processing
        ollama_host: "http://localhost:11434"             # Ollama host address
        searxng_host: "http://your-searxng-instance.com"  # Optional SearxNG instance.
        exa_api_key: "your-exa-api-key"                   # Optional Exa search API key
        tavily_api_key: "your-tavily-api-key"             # Optional Tavily API key
        brave_api_key: "your-brave-api-key"               # Optional Brave Search API key
  ```

  Note: To enable LLM search, you must also set `use_llm_search: yes` in your `plexsync` configuration (see Advanced Usage section).

  When multiple search providers are configured, they're used in the following priority order:
  1. SearxNG (tried first if configured)
  2. Exa (used if SearxNG fails or isn't configured)
  3. Brave Search (used if both SearxNG and Exa fail or aren't configured)
  4. Tavily (used if all above fail or aren't configured)

  You can get started with `beet plexsonic -p "YOUR_PROMPT"` to create the playlist based on YOUR_PROMPT. The default playlist name is `SonicSage` (wink wink), you can modify it using `-m` flag. By default, it requests 10 tracks from the AI model. Use the `-n` flag to change the number of tracks requested. Finally, if you prefer to clear the playlist before adding the new songs, you can add `-c` flag. So, to create a new classical music playlist, you can use something like `beet plexsonic -c -n 10 -p "classical music, romanticism era, like Schubert, Chopin, Liszt"`.

  Please note that not all tracks returned by the AI model may be available in your library or matched perfectly, affecting the size of the playlist created. The command will log the tracks that could not be found on your library. You can improve the matching by enabling `manual_search` (see Advanced Usage). This is working extremely well for me. I would love to hear your comments/feedback to improve this feature.

* To configure imported playlists, you can use various source types including custom POST requests:

  ```yaml
  plexsync:
    playlists:
      items:
        - name: "Custom Playlist"
          type: "imported"
          sources:
            # Standard URL sources
            - "https://open.spotify.com/playlist/37i9dQZF1DX0kbJZpiYdZl"
            - "playlists/local_hits.m3u8"
            # POST request source
            - type: "post"
              server_url: "http://localhost:8000/api/playlist"
              headers:
                Authorization: "Bearer your-token"
                Content-Type: "application/json"
              payload:
                playlist_url: "https://example.com/playlist/123"
  ```

  The POST request expects a JSON response with this format:
  ```json
  {
      "song_list": [
          {
              "title": "Song Title",
              "artist": "Artist Name",
              "album": "Album Name",  # Optional
              "year": "2024"         # Optional
          }
      ]
  }
  ```

## Advanced
Plex matching may be less than perfect and it can miss tracks if the tags don't match perfectly. There are few tools you can use to improve searching:
* You can enable manual search to improve the matching by enabling `manual_search` in your config (default: `False`).
* You can enable LLM-powered search using Ollama with optional integration for SearxNG, Exa, or Tavily (used in that order if all of them are configured). This provides intelligent search capabilities that can better match tracks with incomplete or variant metadata. See the `llm` configuration section above.

```yaml
plexsync:
  manual_search: yes
  use_llm_search: yes  # Enable LLM searching; see llm config
  playlists:
    defaults:
      max_tracks: 20
    items:
      - id: daily_discovery
        name: "Daily Discovery"
        max_tracks: 20      # Maximum number of tracks for Daily Discovery playlist
        exclusion_days: 30  # Number of days to exclude recently played tracks. Tracks played in the last 30 days will not be included in the playlist.
        history_days: 15    # Number of days to use to learn listening habits
        discovery_ratio: 70 # Percentage of unrated tracks (0-100)
                            # Higher values = more discovery
                            # Example: 30 = 30% unrated + 70% rated tracks
                            #          70 = 70% unrated + 30% rated tracks

      - id: forgotten_gems
        name: "Forgotten Gems"
        max_tracks: 50      # Maximum number of tracks for playlist
        max_plays: 2        # Maximum number of plays for tracks to be included
        min_rating: 4       # Minimum rating for rated tracks
        discovery_ratio: 30 # Percentage of unrated tracks (0-100); Higher values = more discovery
        exclusion_days: 30  # Number of days to exclude recently played tracks
        filters:
          include:
            genres:
              - Filmi
              - Indi Pop
              - Punjabi
              - Sufi
              - Ghazals
            years:
              after: 1970
          exclude:
            genres:
              - Religious
              - Bollywood Unwind
              - Bollywood Instrumental
            years:
              before: 1960
          min_rating: 5

      - id: recent_hits
        name: "Recent Hits"
        max_tracks: 20
        discovery_ratio: 20
        exclusion_days: 0   # Number of days to exclude recently played tracks (default: 0 = include all)
        filters:
          include:
            genres:
              - Pop
              - Rock
            years:
              after: 2022
          min_rating: 4

      - id: bollywood_hits
        name: "Bollywood Hits"
        type: imported
        sources: # full playlist urls or M3U8 file paths
          - https://music.youtube.com/playlist?list=RDCLAK5uy_kjNBBWqyQ_Cy14B0P4xrcKgd39CRjXXKk
          - "playlists/local_hits.m3u8"  # Relative to beets config dir
          - "/absolute/path/to/playlist.m3u8"
        max_tracks: 100     # Optional limit
        manual_search: no
        clear_playlist: no
```

[collage]: collage.png
[queries_]: https://beets.readthedocs.io/en/latest/reference/query.html?highlight=queries
[plaxapi]: https://python-plexapi.readthedocs.io/en/latest/modules/audio.html
[plex_token]: https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/
[config]: https://beets.readthedocs.io/en/latest/plugins/index.html
[beets]: https://github.com/beetbox/beets
[Spotify]: https://beets.readthedocs.io/en/stable/plugins/spotify.html
[listenbrainz_plugin_]: https://github.com/arsaboo/beets-listenbrainz
