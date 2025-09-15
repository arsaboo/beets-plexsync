import importlib
import sys
import types
import unittest

from tests.test_playlist_import import ensure_stubs, DummyLogger


class ManualSearchTest(unittest.TestCase):
    def setUp(self):
        # set up beets/confuse stubs and load manual_search
        self.config, _ = ensure_stubs({'plexsync': {'manual_search': False}})
        sys.modules['beetsplug.matching'] = types.SimpleNamespace(get_fuzzy_score=lambda a, b: 1.0 if a and b and a.lower() == b.lower() else 0.5)
        if 'beetsplug.manual_search' in sys.modules:
            importlib.reload(sys.modules['beetsplug.manual_search'])
        else:
            importlib.import_module('beetsplug.manual_search')
        self.manual = importlib.import_module('beetsplug.manual_search')

    def test_handle_manual_search_caches_selection(self):
        class Plugin:
            def __init__(self):
                self.cache = types.SimpleNamespace(_key=None)
                self._log = DummyLogger()
                self.cache_calls = []

            def _cache_result(self, cache_key, result, cleaned_metadata=None):
                self.cache_calls.append((cache_key, result))

            def manual_track_search(self, original):
                assert False, "Should not recurse"

            def cache_key(self, song):
                return f"cache-{song['title']}"

        plugin = Plugin()
        plugin.cache._make_cache_key = lambda song: f"cache-{song['title']}"

        track = types.SimpleNamespace(title='Song', parentTitle='Album', artist=lambda: types.SimpleNamespace(title='Artist'))
        # ensure helper stores negative cache
        self.manual._store_negative_cache(plugin, {'title': 'Song'}, None)
        self.assertIn(('cache-Song', None), plugin.cache_calls)


if __name__ == '__main__':
    unittest.main()
