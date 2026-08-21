import json
import sqlite3
import types

import pytest

from tests.test_playlist_import import DummyLogger, ensure_stubs


class CacheTests:
    @pytest.fixture(autouse=True)
    def setup(self, request, tmp_path):
        ensure_stubs({'plexsync': {}}, request.addfinalizer)
        import sys
        sys.modules.setdefault('plexapi.audio', types.SimpleNamespace(Track=object))
        sys.modules.setdefault('plexapi.video', types.SimpleNamespace(Video=object))
        sys.modules.setdefault('plexapi.server', types.SimpleNamespace(PlexServer=object))
        from beetsplug.core.cache import Cache

        self.db_path = str(tmp_path / 'cache.db')

        class PluginStub:
            def __init__(self):
                self._log = DummyLogger()

        self.cache = Cache(self.db_path, PluginStub())

    def test_set_and_get(self):
        key = json.dumps({'title': 'Song'})
        self.cache.set(key, 123)
        assert self.cache.get(key) == (123, None)

    def test_negative_cache_storage(self):
        key = json.dumps({'title': 'Skip'})
        self.cache.set(key, None)
        assert self.cache.get(key) == (-1, None)

    def test_clear(self):
        key = json.dumps({'title': 'Clear'})
        self.cache.set(key, 1)
        self.cache.clear()
        assert self.cache.get(key) is None

    def test_connect_enables_wal_and_closes_connection(self):
        # WAL mode lets readers proceed alongside a writer instead of every
        # write taking an exclusive lock on the whole file - the main fix
        # for the threaded plexsync -f slowdown.
        with self.cache._connect() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == 'wal'

        # A bare `with sqlite3.connect(...) as conn:` never closes the
        # connection (only commits/rolls back) - _connect() must actually
        # close it, or every cache call leaks a connection object.
        with self.cache._connect() as conn:
            pass
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_flexible_match_uses_index_with_case_sensitive_like(self):
        # get()'s flexible title|artist|% match relies on case_sensitive_like
        # so the prefix LIKE can use the `query` primary key index instead of
        # a full table scan. Cache keys are always lowercased via
        # normalize_text(), so this must not change which rows match.
        self.cache.set('song|artist|album one', 111)
        result = self.cache.get({'title': 'Song', 'artist': 'Artist', 'album': 'Album Two'})
        assert result == (111, None)

    def test_smart_playlist_style_dict_query_roundtrip(self):
        # Sanity check that dict-style queries (as used by search_plex_song)
        # still round-trip correctly through the new connection handling.
        song = {'title': 'Roundtrip', 'artist': 'Artist', 'album': 'Album'}
        self.cache.set(song, 456)
        assert self.cache.get(song) == (456, None)
