"""Syncs Plex library and updates Plex library when  beets library is changed.

Plex Home users enter the Plex Token to enable updating.
Put something like the following in your config.yaml to configure:
    plex:
        host: localhost
        token: token
"""

import os

import beets.ui
from beets import config
from beets.plugins import BeetsPlugin
from plexapi import exceptions
from plexapi.server import PlexServer


class PlexSync(BeetsPlugin):
    data_source = 'Plex'

    def __init__(self):
        super().__init__()

        # Adding defaults.
        config['plex'].add({
            'baseurl': 'localhost',
            'port': 32400,
            'token': '',
            'library_name': 'Music',
            'secure': False,
            'ignore_cert_errors': False})

        config['plex']['token'].redact = True
        try:
            plex = PlexServer(config['plex']['baseurl'].get(),
                          config['plex']['token'].get())
        except exceptions.Unauthorized:
            raise beets.ui.UserError('Plex token request failed')
        try:
            self.music = plex.library.section(config['plex']['library_name']
                                              .get())
        except exceptions.NotFound:
            raise beets.ui.UserError(f"{config['plex']['library_name']} library not found")
        self.register_listener('database_change', self.listen_for_db_change)

    def listen_for_db_change(self, lib, model):
        """Listens for beets db change and register the update for the end"""
        self.register_listener('cli_exit', self._plexupdate)

    def commands(self):
        plexupdate_cmd = beets.ui.Subcommand(
            'plexupdate', help=f'Update {self.data_source} library'
        )

        def func(lib, opts, args):
            self._plexupdate()

        plexupdate_cmd.func = func

        # plexsync command
        sync_cmd = beets.ui.Subcommand('plexsync',
                                       help="fetch track attributes from Plex")
        sync_cmd.parser.add_option(
            '-f', '--force', dest='force_refetch',
            action='store_true', default=False,
            help='re-download data when already present'
        )

        def func_sync(lib, opts, args):
            items = lib.items(beets.ui.decargs(args))
            self._fetch_plex_info(items, beets.ui.should_write(),
                                  opts.force_refetch)

        sync_cmd.func = func_sync

        return [plexupdate_cmd, sync_cmd]

    def _plexupdate(self):
        """Update Plex music library."""

        try:
            self.music.update()
            self._log.info('Update started.')
        except exceptions.PlexApiException:
            self._log.warning("{} Update failed",
                              self.config['plex']['library_name'])

    def _fetch_plex_info(self, items, write, force):
        """Obtain track information from Plex."""

        self._log.info('Total {} tracks', len(items))

        for index, item in enumerate(items, start=1):
            self._log.info('Processing {}/{} tracks - {} ',
                           index, len(items), item)
            # If we're not forcing re-downloading for all tracks, check
            # whether the popularity data is already present
            if not force:
                if 'plex_userrating' in item:
                    self._log.debug('Plex rating already present for: {}',
                                    item)
                    continue
            plex_track = self.search_plex_track(item)
            if plex_track is None:
                self._log.info('No track found for: {}', item)
                continue
            item.plex_key = plex_track.key
            item.plex_guid = plex_track.guid
            self._log.info('Rating: {}', plex_track.userRating)
            item.plex_userrating = plex_track.userRating
            item.store()
            if write:
                item.try_write()

    def search_plex_track(self, item):
        """Fetch the Plex track key."""
        tracks = self.music.searchTracks(
            **{'album.title': item.album, 'track.title': item.title})
        if len(tracks) == 1:
            if self.compare_file_name(tracks[0], item):
                return tracks[0]
            #return tracks[0]
        elif len(tracks) > 1:
            for track in tracks:
                if self.compare_file_name(track, item):
                    return track
        else:
            return None
        self._log.info('tracks: {}', len(tracks))
        if len(tracks) == 0:
            return None
        elif len(tracks) == 1:
            return tracks[0]

    def get_plex_filename(self, track):
        """Fetch Plex filename."""
        return os.path.basename(track.media[0].parts[0].file)

    def compare_file_name(self, track, item):
        """Compare file names."""
        if self.get_plex_filename(track) == os.path.basename(item.path):
            return True
        else:
            return False
