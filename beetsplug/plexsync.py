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


class PlexSync(BeetsPlugin):
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
        self.register_listener('database_change', self.listen_for_db_change)
        plex = PlexServer(config['plex']['host'], config['plex']['token'])
        music = plex.library.section(config['plex']['library_name'])
        self._log.info('Music section {}', music.key)

    def listen_for_db_change(self, lib, model):
        """Listens for beets db change and register the update for the end"""
        self.register_listener('cli_exit', self.plex.update())
