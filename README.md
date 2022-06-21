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

This plugin requires you to add and configure the [`plexupdate`](https://beets.readthedocs.io/en/latest/plugins/plexupdate.html) plugin. Next, add `plexsync` to your list of enabled plugins and you should be able to get started.

```yaml
plugins: plexsync
```
