import importlib
import json
import sys
import types
import unittest

from tests.test_playlist_import import ensure_stubs


class LLMSearchTest(unittest.TestCase):
    def setUp(self):
        if sys.version_info < (3, 9):
            self.skipTest('LLM module requires Python 3.9+')
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
        ensure_stubs({'llm': {'search': {}}})
        if 'beetsplug.ai.llm' in sys.modules:
            importlib.reload(sys.modules['beetsplug.ai.llm'])
        else:
            importlib.import_module('beetsplug.ai.llm')
        self.llm = importlib.import_module('beetsplug.ai.llm')

    def tearDown(self):
        if 'beetsplug.ai.llm' in sys.modules:
            sys.modules['beetsplug.ai.llm']._search_toolkit = None

    def test_search_track_info_toolkit_missing(self):
        self.llm._search_toolkit = None
        result = self.llm.search_track_info('Test Song')
        self.assertEqual(result, {'title': 'Test Song', 'artist': '', 'album': None})

    def test_search_track_info_with_toolkit(self):
        class Toolkit:
            def search_song_info(self, query):
                return {'title': 'Found', 'artist': 'Artist', 'album': 'Album'}
        self.llm._search_toolkit = Toolkit()
        result = self.llm.search_track_info('Input Song')
        self.assertEqual(result, {'title': 'Found', 'artist': 'Artist', 'album': 'Album'})


if __name__ == '__main__':
    unittest.main()
