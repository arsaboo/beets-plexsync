import importlib
import json
import sys
import types
import unittest

from tests.test_playlist_import import DummyLogger, ensure_stubs


class CacheStub:
    def __init__(self):
        self.storage = {}

    def _make_cache_key(self, query):
        if isinstance(query, dict):
            return str(sorted(query.items()))
        return str(query)

    def get(self, query):
        return self.storage.get(self._make_cache_key(query))

    def set(self, query, value, cleaned_metadata=None):
        key = self._make_cache_key(query)
        if cleaned_metadata is None or isinstance(value, tuple):
            self.storage[key] = value
        else:
            self.storage[key] = (value, cleaned_metadata)

    def debug_cache_keys(self, song):  # pragma: no cover
        pass


class PlexSearchTests(unittest.TestCase):
    def setUp(self):
        if sys.version_info < (3, 9):
            self.skipTest('Plex search tests require Python 3.9+')
        class SimpleBaseModel:
            def __init__(self, **data):
                for key, value in data.items():
                    setattr(self, key, value)
            def model_dump(self):
                return self.__dict__.copy()
            @classmethod
            def model_validate_json(cls, data):
                return cls(**json.loads(data))
        def Field(default=None, **kwargs):
            return default
        def field_validator(*args, **kwargs):
            def decorator(func):
                return func
            return decorator
        sys.modules['pydantic'] = types.SimpleNamespace(
            BaseModel=SimpleBaseModel,
            Field=Field,
            field_validator=field_validator,
        )
        ensure_stubs({'plexsync': {}, 'llm': {'search': {}}})
        if 'beetsplug.plex.search' in sys.modules:
            importlib.reload(sys.modules['beetsplug.plex.search'])
        else:
            importlib.import_module('beetsplug.plex.search')
        self.search = importlib.import_module('beetsplug.plex.search')

    def test_returns_cached_track(self):
        track = types.SimpleNamespace(ratingKey=42, title='Cached')

        class Music:
            def fetchItem(self, key):
                return track
            def searchTracks(self, **kwargs):
                return []

        plugin = types.SimpleNamespace()
        plugin._log = DummyLogger()
        cache = CacheStub()
        cache.storage[cache._make_cache_key({'title': 'Song', 'artist': 'Artist'})] = (track.ratingKey, None)
        plugin.cache = cache
        plugin.music = Music()
        plugin.search_llm = None
        plugin.manual_track_search = lambda song: None
        plugin._cache_result = lambda *args, **kwargs: None

        result = self.search.search_plex_song(plugin, {'title': 'Song', 'artist': 'Artist'}, manual_search=False)
        self.assertIs(result, track)

    def test_single_track_search_caches_result(self):
        track = types.SimpleNamespace(ratingKey=7, title='Match', parentTitle='Album')

        class Music:
            def searchTracks(self, **kwargs):
                return [track]
            def fetchItem(self, key):
                raise AssertionError('fetchItem should not be called')

        recorded = []

        plugin = types.SimpleNamespace()
        plugin._log = DummyLogger()
        plugin.cache = CacheStub()
        plugin.music = Music()
        plugin.search_llm = None
        plugin.manual_track_search = lambda song: None
        plugin._cache_result = lambda key, result: recorded.append((key, result))

        song = {'title': 'Song', 'album': 'Album', 'artist': 'Artist'}
        result = self.search.search_plex_song(plugin, song, manual_search=False)
        self.assertIs(result, track)
        self.assertTrue(recorded)


if __name__ == '__main__':
    unittest.main()
