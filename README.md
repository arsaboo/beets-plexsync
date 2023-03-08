# beets-plexsync
A plugin for [beets](https://github.com/beetbox/beets) to sync with your Plex server.

## Installation

Install the plugin using `pip`:

```shell
pip install git+https://github.com/arsaboo/beets-plexsync.git
```

Then, [configure](#configuration) the plugin in your
[`config.yaml`](https://beets.readthedocs.io/en/latest/plugins/index.html) file.

## Configuration

Add `plexsync` to your list of enabled plugins.

```yaml
plugins: plexsync
```

Next, you can configure your Plex server and library like following (see instructions to obtain Plex token [here](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)).

```yaml
plex:
    host: '192.168.2.212'
    port: 32400
    token: PLEX_TOKEN
    library_name: 'Music'
```

If you want to import `spotify` playlists, you will also need to configure the `spotify` plugin. If you are already using the [Spotify](https://beets.readthedocs.io/en/stable/plugins/spotify.html) plugin, `plexsync`will reuse the same configuration.
```yaml
spotify:
    client_id: CLIENT_ID
    client_secret: CLIENT_SECRET
```

## Features

The following features are implemented in `plexsync`:

* `beet plexsync [-f]`: allows you to import all the data from your Plex library inside beets. Run the command `beet plexsync` and it will obtain `guid`, `ratingkey`, `userrating`, `skipcount`, `viewcount`, `lastviewedat`, `lastratedat`, and `plex_updated`. See details about these attributes [here](https://python-plexapi.readthedocs.io/en/latest/modules/audio.html). By default, `plexsync` will not overwrite information for tracks that are already rated. If you want to overwrite all the details again, use the `-f` flag, i.e., `beet plexsync -f` will force update the entire library with fresh information from Plex. This can be useful if you have made significant changes to your Plex library (e.g., updated ratings).

* `beet plexsyncrecent`: If you have a large library, `beets plexsync -f` can take a long time. To update only the recently updated tracks, use `beet plexsyncrecent` to update the information for tracks listened in the last 7 days.

* `plexplaylistadd` and `plexplaylistremove` to add or remove tracks from Plex playlists. These commands should be used in conjunction with beets [queries](https://beets.readthedocs.io/en/latest/reference/query.html?highlight=queries) to provide the desired items. Use the `-m` flag to provide the playlist name to be used.

   ** To add all country music tracks with `plex_userrating` greater than 5 in a playlist `Country`, you can use the command `beet plexplaylistadd -m Country genre:"Country" plex_userrating:5..`

   ** To remove all tracks that are rated less than 5 from the `Country` playlist, use the command `beet plexplaylistremove -m Country plex_userrating:..5`

* `beet plexplaylistimport`: allows you to import playlists from other online services. Apple Music, Gaana.com, and Spotify are currently supported (more coming soon). Use the `-m` flag to specify the playlist name to be created in Plex and supply the full playlist url with the `-u` flag.

  For example, to import the Global Top-100 Apple Music playlist, use the command `beet plexplaylistimport -m Top-100 -u https://music.apple.com/us/playlist/top-100-global/pl.d25f5d1181894928af76c85c967f8f31`. Similarly, to import the Hot-hits USA playlist from Spotify, use the command `beet plexplaylistimport -m HotHitsUSA -u https://open.spotify.com/playlist/37i9dQZF1DX0kbJZpiYdZl`

