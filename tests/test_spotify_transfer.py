import importlib
import types

import pytest

from tests.test_playlist_import import ensure_stubs, DummyLogger


class SpotifyTransferTest:
    @pytest.fixture(autouse=True)
    def setup(self, request):
        ensure_stubs({'plexsync': {}}, request.addfinalizer)
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

        class LibraryItem:
            def __init__(self, rating_key, spotify_id, artist, album, title):
                self.plex_ratingkey = rating_key
                self.spotify_track_id = spotify_id
                self.artist = artist
                self.album = album
                self.title = title

        beets_item = LibraryItem(1, 'spotify:track:123', 'Artist', 'Album', 'Song')

        class Plugin:
            def __init__(self):
                self._log = logger
                self.plex = types.SimpleNamespace(playlist=lambda name: Playlist([
                    PlexItem(1, 'Album', 'Song'),
                ]))
                self.called_auth = False
                self.sp = types.SimpleNamespace(
                    tracks=lambda ids: {'tracks': [
                        {'id': tid, 'is_playable': True, 'available_markets': ['US']}
                        for tid in ids
                    ]},
                )

            def authenticate_spotify(self):
                self.called_auth = True

            def _build_plex_lookup_and_vector_index(self, lib):
                return {1: beets_item}

            def add_tracks_to_spotify_playlist(
                    self, playlist, tracks, *, allow_removals=True):
                self.sent = (playlist, tracks, allow_removals)

            def _search_spotify_track(
                    self, beets_item, *, raise_on_error=False):  # pragma: no cover
                return 'alt-track'

            def create_progress_counter(self, *a, **kw):
                return None

        plugin = Plugin()

        lib = types.SimpleNamespace(
            items=lambda *args, **kwargs: [beets_item]
        )
        self.transfer.plex_to_spotify(plugin, lib, 'Mix')

        assert plugin.called_auth
        assert plugin.sent == ('Mix', ['spotify:track:123'], True)

    def test_falls_back_to_search_when_unplayable(self):
        logger = DummyLogger()

        class LibraryItem:
            def __init__(self, rating_key, spotify_id, artist, album, title):
                self.plex_ratingkey = rating_key
                self.spotify_track_id = spotify_id
                self.artist = artist
                self.album = album
                self.title = title

        beets_item = LibraryItem(1, 'orig', 'Art', 'Alb', 'Song')

        class Plugin:
            def __init__(self):
                self._log = logger
                self.plex = types.SimpleNamespace(playlist=lambda name: types.SimpleNamespace(items=lambda: [types.SimpleNamespace(ratingKey=1, parentTitle='Alb', title='Song')]))
                self.sp = types.SimpleNamespace(
                    tracks=lambda ids: {'tracks': [
                        {'id': tid, 'is_playable': False, 'available_markets': []}
                        for tid in ids
                    ]},
                )

            def authenticate_spotify(self):
                pass

            def _build_plex_lookup_and_vector_index(self, lib):
                return {1: beets_item}

            def _search_spotify_track(self, beets_item, *, raise_on_error=False):
                return 'fallback'

            def add_tracks_to_spotify_playlist(
                    self, playlist, tracks, *, allow_removals=True):
                self.sent = (tracks, allow_removals)

            def create_progress_counter(self, *a, **kw):
                return None

        plugin = Plugin()

        lib = types.SimpleNamespace(
            items=lambda *args, **kwargs: [beets_item]
        )
        self.transfer.plex_to_spotify(plugin, lib, 'Mix')

        assert plugin.sent == (['fallback'], True)

    def test_empty_resolution_never_updates_destination(self):
        logger = DummyLogger()

        class Plugin:
            def __init__(self):
                self._log = logger
                self.plex = types.SimpleNamespace(
                    playlist=lambda name: types.SimpleNamespace(
                        items=lambda: [types.SimpleNamespace(
                            ratingKey=1, parentTitle='Album', title='Song'
                        )]
                    )
                )
                self.sp = types.SimpleNamespace()
                self.called = False

            def authenticate_spotify(self):
                pass

            def _build_plex_lookup_and_vector_index(self, lib):
                return {}

            def add_tracks_to_spotify_playlist(self, *args, **kwargs):
                self.called = True

            def create_progress_counter(self, *args, **kwargs):
                return None

        plugin = Plugin()
        result = self.transfer.plex_to_spotify(
            plugin, types.SimpleNamespace(items=lambda *a, **k: []), 'Mix'
        )

        assert result is False
        assert not plugin.called

    def test_partial_resolution_uses_add_only_mode(self):
        logger = DummyLogger()
        beets_item = types.SimpleNamespace(
            plex_ratingkey=1,
            spotify_track_id='known',
            artist='Artist',
            album='Album',
            title='Song',
        )
        plex_items = [
            types.SimpleNamespace(ratingKey=1, parentTitle='Album', title='Song'),
            types.SimpleNamespace(ratingKey=2, parentTitle='Album', title='Missing'),
        ]

        class Plugin:
            def __init__(self):
                self._log = logger
                self.plex = types.SimpleNamespace(
                    playlist=lambda name: types.SimpleNamespace(items=lambda: plex_items)
                )
                self.sp = types.SimpleNamespace(
                    tracks=lambda ids: {'tracks': [
                        {'id': 'known', 'is_playable': True, 'available_markets': ['US']}
                    ]}
                )

            def authenticate_spotify(self):
                pass

            def _build_plex_lookup_and_vector_index(self, lib):
                return {1: beets_item}

            def add_tracks_to_spotify_playlist(
                    self, playlist, tracks, *, allow_removals=True):
                self.sent = (tracks, allow_removals)
                return True

            def create_progress_counter(self, *args, **kwargs):
                return None

        plugin = Plugin()
        assert self.transfer.plex_to_spotify(
            plugin, types.SimpleNamespace(items=lambda *a, **k: []), 'Mix'
        )
        assert plugin.sent == (['known'], False)

    def test_availability_failure_retains_known_id_and_disables_removals(self):
        logger = DummyLogger()
        beets_item = types.SimpleNamespace(
            plex_ratingkey=1,
            spotify_track_id='known',
            artist='Artist',
            album='Album',
            title='Song',
        )

        class Plugin:
            def __init__(self):
                self._log = logger
                self.plex = types.SimpleNamespace(
                    playlist=lambda name: types.SimpleNamespace(
                        items=lambda: [types.SimpleNamespace(
                            ratingKey=1, parentTitle='Album', title='Song'
                        )]
                    )
                )
                self.sp = types.SimpleNamespace(
                    tracks=lambda ids: (_ for _ in ()).throw(RuntimeError('offline'))
                )

            def authenticate_spotify(self):
                pass

            def _build_plex_lookup_and_vector_index(self, lib):
                return {1: beets_item}

            def add_tracks_to_spotify_playlist(
                    self, playlist, tracks, *, allow_removals=True):
                self.sent = (tracks, allow_removals)
                return True

            def create_progress_counter(self, *args, **kwargs):
                return None

        plugin = Plugin()
        assert self.transfer.plex_to_spotify(
            plugin, types.SimpleNamespace(items=lambda *a, **k: []), 'Mix'
        )
        assert plugin.sent == (['known'], False)


