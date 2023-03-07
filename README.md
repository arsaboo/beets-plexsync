## WIP - DO NOT INSTALL
This is still a work-in-progress and not ready for public consumption.

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
    baseurl: 'http://192.168.2.212:32400'
    token: PLEX_TOKEN
    library_name: 'Music'
```
