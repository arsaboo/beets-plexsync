"""Updates an Plex library whenever the beets library is changed.

Plex Home users enter the Plex Token to enable updating.
Put something like the following in your config.yaml to configure:
    plex:
        host: localhost
        port: 32400
        token: token
"""

from urllib.parse import urlencode, urljoin
from xml.etree import ElementTree

import requests
from beets import config
from beets.plugins import BeetsPlugin
from plexapi.server import PlexServer


def append_token(url, token):
    """Appends the Plex Home token to the api call if required.
    """
    if token:
        url += '?' + urlencode({'X-Plex-Token': token})
    return url


def get_protocol(secure):
    if secure:
        return 'https'
    else:
        return 'http'


class PlexSync(MetadataSourcePlugin, BeetsPlugin):
    data_source = 'Plex'

    def __init__(self):
        super().__init__()

        # Adding defaults.
        config['plex'].add({
            'host': 'localhost',
            'port': 32400,
            'token': '',
            'library_name': 'Music',
            'secure': False,
            'ignore_cert_errors': False})

        config['plex']['token'].redact = True
        plex = PlexServer(config['plex']['host'], config['plex']['token'])
        music = plex.library.section(config['plex']['library_name'])
        self.register_listener('database_change', self.listen_for_db_change)

    def listen_for_db_change(self):
        """Listens for beets db change and register the update for the end"""
        self.register_listener('cli_exit', self.music.update())

    def commands(self):
        # autotagger import command
        def queries(lib, opts, args):
            success = self._parse_opts(opts)
            if success:
                results = self._match_library_tracks(lib, ui.decargs(args))
                self._output_match_results(results)

        plexupdate_cmd = ui.Subcommand(
            'plexupdate', help=f'Update {self.data_source} library'
        )

        def func(lib, args):
            self._plexupdate(music)

        plexupdate_cmd.func = func
        return [plexupdate_cmd]

    def _plexupdate(music_lib):
        """Update Plex music library."""

        self._log.info('Music section {}', music_lib.key)
        music_lib.update()
