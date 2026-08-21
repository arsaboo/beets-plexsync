import importlib
import sys
import types

import pytest

from tests.test_playlist_import import ensure_stubs, DummyLogger


class ManualSearchTest:
    @pytest.fixture(autouse=True)
    def setup(self, request):
        # set up beets/confuse stubs and load manual_search
        self.config, _ = ensure_stubs(
            {'plexsync': {'manual_search': False}}, request.addfinalizer
        )
        _saved_matching = sys.modules.get('beetsplug.core.matching')

        def _restore_matching():
            if _saved_matching is None:
                sys.modules.pop('beetsplug.core.matching', None)
            else:
                sys.modules['beetsplug.core.matching'] = _saved_matching

        request.addfinalizer(_restore_matching)
        sys.modules['beetsplug.core.matching'] = types.SimpleNamespace(
            get_fuzzy_score=lambda a, b: 1.0 if a and b and a.lower() == b.lower() else 0.5
        )
        module_name = 'beetsplug.plex.manual_search'
        if module_name in sys.modules:
            importlib.reload(sys.modules[module_name])
        else:
            importlib.import_module(module_name)
        self.manual = importlib.import_module(module_name)

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

        # ensure helper stores negative cache
        self.manual._store_negative_cache(plugin, {'title': 'Song'}, None)
        assert ('cache-Song', None) in plugin.cache_calls

    def test_cache_selection_skips_manual_query_cache(self):
        class Plugin:
            def __init__(self):
                self.cache = types.SimpleNamespace(_key=None)
                self._log = DummyLogger()
                self.cache_calls = []

            def _cache_result(self, cache_key, result, cleaned_metadata=None):
                self.cache_calls.append((cache_key, result))

        plugin = Plugin()
        plugin.cache._make_cache_key = lambda song: f"cache-{song['title']}"

        manual_query = {'title': '', 'album': 'Manual Album', 'artist': ''}
        original_query = {'title': 'Original Title', 'album': 'Original Album', 'artist': 'Original Artist'}
        track = types.SimpleNamespace(ratingKey=123)

        self.manual._cache_selection(plugin, manual_query, track, original_query)

        assert ('cache-Original Title', track) in plugin.cache_calls
        assert ('cache-', track) not in plugin.cache_calls

    def test_cache_selection_without_original_query_does_not_cache(self):
        class Plugin:
            def __init__(self):
                self.cache = types.SimpleNamespace(_key=None)
                self._log = DummyLogger()
                self.cache_calls = []

            def _cache_result(self, cache_key, result, cleaned_metadata=None):
                self.cache_calls.append((cache_key, result))

        plugin = Plugin()
        plugin.cache._make_cache_key = lambda song: f"cache-{song['title']}"

        manual_query = {'title': '', 'album': 'Manual Album', 'artist': ''}
        track = types.SimpleNamespace(ratingKey=456)

        self.manual._cache_selection(plugin, manual_query, track)

        assert plugin.cache_calls == []
