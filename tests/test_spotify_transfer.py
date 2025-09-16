import importlib
import types
import unittest

from tests.test_playlist_import import ensure_stubs, DummyLogger


class SpotifyTransferTest(unittest.TestCase):
    def setUp(self):
        ensure_stubs({'plexsync': {}})
        if 'beetsplug.plex.spotify_transfer' in importlib.sys.modules:
            importlib.reload(importlib.sys.modules['beetsplug.plex.spotify_transfer'])
        else:
            importlib.import_module('beetsplug.plex.spotify_transfer')
        self.transfer = importlib.import_module('beetsplug.plex.spotify_transfer')

    def test_transfers_tracks_with_existing_ids(self):
        logger = DummyLogger()

        class Playlist:
            def __init__(self, items):
                self._items = items

            def items(self):
                return self._items

        class PlexItem:
            def __init__(self, rating_key, parent_title, title):
                self.ratingKey = rating_key
                self.parentTitle = parent_title
                self.title = title

        class Plugin:
            def __init__(self):
                self._log = logger
                self.plex = types.SimpleNamespace(playlist=lambda name: Playlist([
                    PlexItem(1, 'Album', 'Song'),
                ]))
                self.called_auth = False
                self.sp = types.SimpleNamespace(track=lambda track_id: {
                    'is_playable': True,
                    'available_markets': ['US'],
                })

            def authenticate_spotify(self):
                self.called_auth = True

            def add_tracks_to_spotify_playlist(self, playlist, tracks):
                self.sent = (playlist, tracks)

            def _search_spotify_track(self, beets_item):  # pragma: no cover
                return 'alt-track'

        plugin = Plugin()

        class LibraryItem:
            def __init__(self, rating_key, spotify_id, artist, album, title):
                self.plex_ratingkey = rating_key
                self.spotify_track_id = spotify_id
                self.artist = artist
                self.album = album
                self.title = title

        lib = types.SimpleNamespace(
            items=lambda *args, **kwargs: [
                LibraryItem(1, 'spotify:track:123', 'Artist', 'Album', 'Song')
            ]
        )
        self.transfer.plex_to_spotify(plugin, lib, 'Mix')

        self.assertTrue(plugin.called_auth)
        self.assertEqual(plugin.sent, ('Mix', ['spotify:track:123']))

    def test_falls_back_to_search_when_unplayable(self):
        logger = DummyLogger()

        class Plugin:
            def __init__(self):
                self._log = logger
                self.plex = types.SimpleNamespace(playlist=lambda name: types.SimpleNamespace(items=lambda: [types.SimpleNamespace(ratingKey=1, parentTitle='Alb', title='Song')]))
                self.sp = types.SimpleNamespace(track=lambda _id: {
                    'is_playable': False,
                    'available_markets': [],
                })

            def authenticate_spotify(self):
                pass

            def _search_spotify_track(self, beets_item):
                return 'fallback'

            def add_tracks_to_spotify_playlist(self, playlist, tracks):
                self.sent = tracks

        plugin = Plugin()

        class LibraryItem:
            def __init__(self, rating_key, spotify_id, artist, album, title):
                self.plex_ratingkey = rating_key
                self.spotify_track_id = spotify_id
                self.artist = artist
                self.album = album
                self.title = title

        lib = types.SimpleNamespace(
            items=lambda *args, **kwargs: [
                LibraryItem(1, 'orig', 'Art', 'Alb', 'Song')
            ]
        )
        self.transfer.plex_to_spotify(plugin, lib, 'Mix')

        self.assertEqual(plugin.sent, ['fallback'])


if __name__ == '__main__':
    unittest.main()

