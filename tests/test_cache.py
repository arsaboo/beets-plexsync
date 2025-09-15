import os
import json
import sqlite3
import tempfile
import types
import unittest

from tests.test_playlist_import import DummyLogger, ensure_stubs


class CacheTests(unittest.TestCase):
    def setUp(self):
        ensure_stubs({'plexsync': {}})
        import sys
        sys.modules.setdefault('plexapi.audio', types.SimpleNamespace(Track=object))
        sys.modules.setdefault('plexapi.video', types.SimpleNamespace(Video=object))
        sys.modules.setdefault('plexapi.server', types.SimpleNamespace(PlexServer=object))
        from beetsplug.caching import Cache

        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, 'cache.db')

        class PluginStub:
            def __init__(self):
                self._log = DummyLogger()

        self.cache = Cache(self.db_path, PluginStub())

    def tearDown(self):
        self.tempdir.cleanup()

    def test_set_and_get(self):
        key = json.dumps({'title': 'Song'})
        self.cache.set(key, 123)
        self.assertEqual(self.cache.get(key), (123, None))

    def test_negative_cache_storage(self):
        key = json.dumps({'title': 'Skip'})
        self.cache.set(key, None)
        self.assertEqual(self.cache.get(key), (-1, None))

    def test_clear(self):
        key = json.dumps({'title': 'Clear'})
        self.cache.set(key, 1)
        self.cache.clear()
        self.assertIsNone(self.cache.get(key))


if __name__ == '__main__':
    unittest.main()
