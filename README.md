# beets-plexsync
A plugin for [beets][beets] to sync with your Plex server.

## Key Features

### AI-Generated Playlists
- **AI-Generated Playlists**: Use `beet plexsonic -p "YOUR_PROMPT"` to create a playlist based on YOUR_PROMPT. Modify the playlist name using `-m` flag, change the number of tracks requested with `-n` flag, and clear the playlist before adding new songs with `-c` flag.

### Smart Playlists
Use `beet plex_smartplaylists` to generate or manage custom playlists in Plex. The plugin currently supports three types of playlists:

  1. **Daily Discovery**:
      - Uses tracks you've played in the last 15 days as a base to learn about listening habits (configurable via `history_days`)
      - Excludes tracks played in the last 30 days (configurable via `exclusion_days`)
      - Uses an intelligent scoring system that considers:
          - Track rating (primary factor)
          - Last played date
          - Play count
          - Recently added bonus
      - Introduces controlled randomization to ensure variety
      - Matches genres with your recent listening history
      - Uses Plex's [Sonic Analysis](https://support.plex.tv/articles/sonic-analysis-music/) to find sonically similar tracks
      - Limits the playlist size (configurable via `max_tracks`, default 20)
      - Controls discovery vs. familiar ratio (configurable via `discovery_ratio`, default 70%)

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

  3. **Imported Playlists**:
      - Import playlists from external services (Spotify, Apple Music, YouTube, etc.) and local M3U8 files
      - Configure multiple source URLs and file paths per playlist
      - For M3U8 files, use paths relative to beets config directory or absolute paths
      - Control playlist behavior with options:
        - `manual_search`: Enable/disable manual matching for unmatched tracks
        - `clear_playlist`: Clear existing playlist before adding new tracks
        - `max_tracks`: Limit the number of tracks in the playlist

You can use config filters to finetune any playlist. You can specify the `genre`, `year`, and `UserRating` to be included and excluded from any of the playlists. See the extended example below.

### Library Sync
- **Plex Library Sync**: `beet plexsync [-f]` imports all the data from your Plex library inside beets. Use the `-f` flag to force update the entire library with fresh information from Plex.
- **Recent Sync**: `beet plexsyncrecent` updates the information for tracks listened in the last 7 days.

### Playlist Manipulation
- **Playlist Manipulation**: `plexplaylistadd` and `plexplaylistremove` add or remove tracks from Plex playlists. Use the `-m` flag to provide the playlist name.
- **Playlist Clear**: `beet plexplaylistclear` clears a Plex playlist. Use the `-m` flag to specify the playlist name.

### Playlist Import
- **Playlist Import**: `beet plexplaylistimport` imports individual playlists from Spotify, Apple Music, Gaana.com, JioSaavn, Youtube, Tidal, and M3U8 files. Use the `-m` flag to specify the playlist name and:
  - For online services: use the `-u` flag to supply the full playlist url
  - For M3U8 files: use the `-u` flag with the file path (relative to beets config directory or absolute path)
  For each import session, a detailed log file is created in your beets config directory (named `<playlist_name>_import.log`) that records:
  - Tracks that couldn't be found in your Plex library
  - Low-rated tracks that were skipped
  - Import statistics and summary
  - The log file helps you identify which tracks need manual attention
- **Youtube Search Import**: `beet plexsearchimport` imports playlists based on Youtube search. Use the `-m` flag to specify the playlist name, the `-s` flag for the search query, and the `-l` flag to limit the number of search results.

### Additional Tools
- **Plex to Spotify**: `beet plex2spotify` copies a Plex playlist to Spotify. Use the `-m` flag to specify the playlist name.
- **Playlist to Collection**: `beet plexplaylist2collection` converts a Plex playlist to a collection. Use the `-m` flag to specify the playlist name.
- **Album Collage**: `beet plexcollage` creates a collage of most played albums. Use the `-i` flag to specify the number of days and `-g` flag to specify the grid size.

### Manual Import for Failed Tracks
The plugin creates detailed import logs for each playlist import session. You can manually process failed imports using:

- `beet plex_smartplaylists --import-failed`: Process all import logs and attempt manual matching for failed tracks
- `beet plex_smartplaylists --import-failed --log-file playlist_name_import.log`: Process a specific log file

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
```

Next, you can configure your Plex server and library like following (see instructions to obtain Plex token [here][plex_token]).

```yaml
plex:
  host: '192.168.2.212'
  port: 32400
  token: PLEX_TOKEN
  library_name: 'Music'
```

If you want to import `spotify` playlists, you will also need to configure the `spotify` plugin. If you are already using the [Spotify][Spotify] plugin, `plexsync`will reuse the same configuration.
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
        api_key: "ollama"  # optional for local models; will use base key if empty
        base_url: "http://192.168.2.162:3006/api/search"  # Override base_url for search
        model: "qwen2.5:latest"  # Override model for search
        embedding_model: "snowflake-arctic-embed2:latest"  # Embedding model
  ```

  You can get started with `beet plexsonic -p "YOUR_PROMPT"` to create the playlist based on YOUR_PROMPT. The default playlist name is `SonicSage` (wink wink), you can modify it using `-m` flag. By default, it requests 10 tracks from the AI model. Use the `-n` flag to change the number of tracks requested. Finally, if you prefer to clear the playlist before adding the new songs, you can add `-c` flag. So, to create a new classical music playlist, you can use something like `beet plexsonic -c -n 10 -p "classical music, romanticism era, like Schubert, Chopin, Liszt"`.

  Please note that not all tracks returned by the AI model may be available in your library or matched perfectly, affecting the size of the playlist created. The command will log the tracks that could not be found on your library. You can improve the matching by enabling `manual_search` (see Advanced Usage). This is working extremely well for me. I would love to hear your comments/feedback to improve this feature.

* `beet plexsync [-f]`: allows you to import all the data from your Plex library inside beets. Run the command `beet plexsync` and it will obtain `guid`, `ratingkey`, `userrating`, `skipcount`, `viewcount`, `lastviewedat`, `lastratedat`, and `plex_updated`. See details about these attributes [here][plaxapi]. By default, `plexsync` will not overwrite information for tracks that are already rated. If you want to overwrite all the details again, use the `-f` flag, i.e., `beet plexsync -f` will force update the entire library with fresh information from Plex. This can be useful if you have made significant changes to your Plex library (e.g., updated ratings).

* `beet plexsyncrecent`: If you have a large library, `beets plexsync -f` can take a long time. To update only the recently updated tracks, use `beet plexsyncrecent` to update the information for tracks listened in the last 7 days.

* `plexplaylistadd` and `plexplaylistremove` to add or remove tracks from Plex playlists. These commands should be used in conjunction with beets [queries][queries_] to provide the desired items. Use the `-m` flag to provide the playlist name to be used.

   * To add all country music tracks with `plex_userrating` greater than 5 in a playlist `Country`, you can use the command `beet plexplaylistadd -m Country genre:"Country" plex_userrating:5..`

   * To remove all tracks that are rated less than 5 from the `Country` playlist, use the command `beet plexplaylistremove -m Country plex_userrating:..5`

* `beet plexplaylistimport`: allows you to import playlists from other online services. Spotify, Apple Music, Gaana.com, JioSaavn, Youtube, Tidal, and M3U8 files are currently supported. Use the `-m` flag to specify the playlist name to be created in Plex and:
  - For online services: use the `-u` flag to supply the full playlist url
  - For M3U8 files: use the `-u` flag with the file path (relative to beets config directory or absolute path)

  For example, to import the Global Top-100 Apple Music playlist, use the command `beet plexplaylistimport -m Top-100 -u https://music.apple.com/us/playlist/top-100-global/pl.d25f5d1181894928af76c85c967f8f31`. Similarly, to import the Hot-hits USA playlist from Spotify, use the command `beet plexplaylistimport -m HotHitsUSA -u https://open.spotify.com/playlist/37i9dQZF1DX0kbJZpiYdZl`

  You can also use this function to import the weekly jams and weekly exploration playlists from ListenBrainz into Plex. You will need to install and configure the [Listenbrainz plugin][listenbrainz_plugin_]. To import the ListenBrainz playlists, use the command `beet plexplaylistimport --listenbrainz`.

* `beet plexsearchimport`: allows you to import playlists based on Youtube search (results are returned in descending order of the number of views). Use the `-m` flag to specify the playlist name to be created in Plex, supply the search query with the `-s` flag, and use the `-l` flag to limit the number of search results.

  For example, to import the top-20 songs by Taylor Swift, use the command `beet plexsearchimport -s "Taylor Swift" -l 20 -m "Taylor"`.

* `beet plexplaylistclear`: allows you to clear a Plex playlist. Use the `-m` flag to specify the playlist name to be cleared in Plex.

* `beet plex2spotify`: allows you to copy a Plex playlist to Spotify. Use the `-m` flag to specify the playlist name to be copied to Spotify.

* `beet plexplaylist2collection`: converts a Plex playlist to collection. Use the `-m` flag to specify the playlist name. A collection with the same name will be created.

* `beet plexcollage`: allows you to create a collage of most played albums. You can use the `-i` flag to specify the number of days to be used (default is 7 days) and `-g` flag to specify the grid size (default is 3). So, `beet plexcollage -g 5 -i 7` can be used to create a 5x5 collage of the most played albums over the last 7 days. You should get a collage.png file in the beet config folder. The output should look something like the following:

<p align="center">
  <img src="collage.png">
</p>

## Advanced
Plex matching may be less than perfect and it can miss tracks if the tags don't match perfectly. There are few tools you can use to improve searching:
* You can enable manual search to improve the matching by enabling `manual_search` in your config (default: `False`).
* You can enable a Perplexity-style LLM search. This is currently tested on [Perplexica](https://github.com/ItzCrazyKns/Perplexica). See `llm config above.

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
