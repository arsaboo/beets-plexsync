import importlib
import sys
import types
import unittest


class DummyConfigNode:
    def __init__(self, data):
        self._data = data

    def __getitem__(self, key):
        if isinstance(self._data, dict) and key in self._data:
            return DummyConfigNode(self._data[key])
        raise NotFoundError(key)

    def add(self, value):
        if isinstance(self._data, dict) and isinstance(value, dict):
            self._data.update(value)
        return self

    def get(self, cast=None):
        value = self._data
        if isinstance(value, DummyConfigNode):
            value = value._data
        if cast is None or value is None:
            return value
        if cast is bool:
            return bool(value)
        return cast(value)


class DummyConfig(DummyConfigNode):
    def __init__(self):
        super().__init__({})

    def set_data(self, data):
        self._data = data


class NotFoundError(Exception):
    pass


class ConfigValueError(Exception):
    pass


class CacheStub:
    def get_playlist_cache(self, *args, **kwargs):
        return None

    def set_playlist_cache(self, *args, **kwargs):
        return None



def ensure_stubs(data):
    config = DummyConfig()
    config.set_data(data)

    beets = types.ModuleType('beets')
    ui_module = types.ModuleType('beets.ui')

    class UserError(Exception):
        pass

    def colorize(_name, text):
        return text

    ui_module.UserError = UserError
    ui_module.colorize = colorize
    ui_module.input_ = lambda prompt='': ''
    ui_module.input_yn = lambda prompt='', default=True: default
    ui_module.input_options = lambda *args, **kwargs: 0
    ui_module.print_ = print

    beets.ui = ui_module
    beets.config = config

    # Minimal beets.library stub used by matching helpers during tests.
    library_module = types.ModuleType('beets.library')

    class LibraryItem:
        """Lightweight stand-in for beets.library.Item."""

        def __init__(self, **fields):
            for key, value in fields.items():
                setattr(self, key, value)

    library_module.Item = LibraryItem
    beets.library = library_module

    # Provide beets.autotag.distance with the API expected by matching.py.
    autotag_module = types.ModuleType('beets.autotag')
    distance_module = types.ModuleType('beets.autotag.distance')

    class Distance:
        def __init__(self):
            self._weights = {}
            self._components = {}

        def add_ratio(self, key, value, weight):
            self._components[key] = abs(value)

        def add_string(self, key, left, right):
            self._components[key] = 0.0 if (left or "") == (right or "") else 1.0

        @property
        def distance(self):
            if not self._components:
                return 0.0
            return sum(self._components.values()) / len(self._components)

    def string_dist(left, right):
        left = left or ""
        right = right or ""
        if left == right:
            return 0.0
        if left in right or right in left:
            return 0.25
        return 0.75

    autotag_module.distance = distance_module
    distance_module.Distance = Distance
    distance_module.string_dist = string_dist
    beets.autotag = autotag_module

    plexapi_module = types.ModuleType('plexapi')
    plexapi_audio_module = types.ModuleType('plexapi.audio')

    class TrackStub:
        """Minimal plexapi.audio.Track replacement for tests."""

        def __init__(self, title="", parentTitle="", originalTitle="", artist_title=""):
            self.title = title
            self.parentTitle = parentTitle
            self.originalTitle = originalTitle
            self._artist_title = artist_title

        def artist(self):
            return types.SimpleNamespace(title=self._artist_title or self.originalTitle or "")

    plexapi_audio_module.Track = TrackStub
    plexapi_module.audio = plexapi_audio_module

    confuse = types.ModuleType('confuse')
    confuse.NotFoundError = NotFoundError
    confuse.ConfigValueError = ConfigValueError

    sys.modules['beets'] = beets
    sys.modules['beets.ui'] = ui_module
    sys.modules['beets.library'] = library_module
    sys.modules['beets.autotag'] = autotag_module
    sys.modules['beets.autotag.distance'] = distance_module
    sys.modules['plexapi'] = plexapi_module
    sys.modules['plexapi.audio'] = plexapi_audio_module
    sys.modules['confuse'] = confuse

    return config, UserError


class DummyLogger:
    def __init__(self):
        self.messages = []

    def _record(self, level, msg, *args):
        self.messages.append((level, msg.format(*args)))

    def warning(self, msg, *args):
        self._record('warning', msg, *args)

    def info(self, msg, *args):
        self._record('info', msg, *args)

    def error(self, msg, *args):
        self._record('error', msg, *args)

    def debug(self, msg, *args):
        self._record('debug', msg, *args)


class PluginStub:
    def __init__(self, logger):
        self._log = logger
        self.added = None
        self.last_manual = None
        self.cache = CacheStub()

    def search_plex_song(self, song, manual_search=False):
        self.last_manual = manual_search
        return f"match-{song['title']}"

    def create_progress_counter(self, *args, **kwargs):
        return None

    def _plex_add_playlist_item(self, tracks, playlist):
        self.added = (tracks, playlist)

    def get_playlist_id(self, url):
        return 'list-id'

    def import_spotify_playlist(self, playlist_id):
        return []

    def import_apple_playlist(self, url):
        return []

    def import_jiosaavn_playlist(self, url):
        return []


class PlaylistImportTest(unittest.TestCase):
    def setUp(self):
        self.config, self.UserError = ensure_stubs({'plexsync': {'manual_search': False}})
        if 'beetsplug.plex.playlist_import' in sys.modules:
            importlib.reload(sys.modules['beetsplug.plex.playlist_import'])
        else:
            importlib.import_module('beetsplug.plex.playlist_import')
        self.module = importlib.import_module('beetsplug.plex.playlist_import')
        self.search_calls = []

        def _stub_search(query, limit, cache):
            self.search_calls.append((query, limit))
            return [{'title': 'Q'}]

        self.module.import_yt_search = _stub_search
        self.module.import_yt_playlist = lambda url, cache: []
        self.module.import_gaana_playlist = lambda url, cache: []
        self.module.import_tidal_playlist = lambda url, cache: []

    def test_add_songs_to_plex_adds_matches(self):
        logger = DummyLogger()
        plugin = PluginStub(logger)
        songs = [{'title': 'One'}, {'title': 'Two'}]

        self.module.add_songs_to_plex(plugin, 'Mix', songs)

        self.assertEqual(plugin.added, (['match-One', 'match-Two'], 'Mix'))
        self.assertFalse(plugin.last_manual)

    def test_add_songs_to_plex_warns_when_empty(self):
        logger = DummyLogger()

        class EmptyPlugin(PluginStub):
            def search_plex_song(self, song, manual_search=False):
                self.last_manual = manual_search
                return None

        plugin = EmptyPlugin(logger)
        self.module.add_songs_to_plex(plugin, 'Empty', [{'title': 'Zero'}])

        self.assertIsNone(plugin.added)
        self.assertTrue(any(level == 'warning' for level, _ in logger.messages))

    def test_import_playlist_spotify_flow(self):
        logger = DummyLogger()

        class SpotifyPlugin(PluginStub):
            def __init__(self, logger):
                super().__init__(logger)
                self.imported_id = None

            def import_spotify_playlist(self, playlist_id):
                self.imported_id = playlist_id
                return [{'title': 'Track'}]

        plugin = SpotifyPlugin(logger)
        self.module.import_playlist(plugin, 'MyMix', 'https://open.spotify.com/playlist/demo')

        self.assertEqual(plugin.imported_id, 'list-id')
        self.assertEqual(plugin.added, (['match-Track'], 'MyMix'))
        self.assertFalse(plugin.last_manual)

    def test_import_playlist_requires_url(self):
        logger = DummyLogger()
        plugin = PluginStub(logger)
        from beets import ui

        with self.assertRaises(self.UserError):
            self.module.import_playlist(plugin, 'Test', None)

    def test_import_search(self):
        logger = DummyLogger()
        plugin = PluginStub(logger)

        self.module.import_search(plugin, 'SearchMix', 'query', limit=5)

        self.assertEqual(plugin.added, (['match-Q'], 'SearchMix'))
        self.assertEqual(self.search_calls[-1], ('query', 5))


if __name__ == '__main__':
    unittest.main()
