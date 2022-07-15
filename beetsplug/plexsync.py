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
from beets.ui import Subcommand
from plexapi.server import PlexServer


def get_protocol(secure):
    if secure:
        return 'https'
    else:
        return 'http'


class PlexSync(BeetsPlugin):
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

    def commands(self):
        plexupdate_cmd = ui.Subcommand(
            'plexupdate', help=f'Update {self.data_source} library'
        )

        def func(lib, args):
            self._plexupdate(music)

        plexupdate_cmd.func = func
        return [plexupdate_cmd]

    def _plexupdate(self, music):
        """Update Plex music library."""

        self._log.info('Music section {}', music.key)
        music.update()
