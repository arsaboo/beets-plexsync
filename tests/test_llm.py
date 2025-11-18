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

    def test_instructor_available_flag(self):
        """Test that INSTRUCTOR_AVAILABLE flag is properly set."""
        # The flag should be False in test environment (no instructor installed)
        self.assertFalse(self.llm.INSTRUCTOR_AVAILABLE)

    def test_create_fallback_song(self):
        """Test fallback song creation."""
        toolkit = self.llm.MusicSearchTools(provider='ollama')
        fallback = toolkit._create_fallback_song('Test Title')
        self.assertEqual(fallback.title, 'Test Title')
        self.assertEqual(fallback.artist, '')
        self.assertIsNone(fallback.album)

    def test_instructor_client_initialization(self):
        """Test that instructor_client is initialized when instructor is available."""
        toolkit = self.llm.MusicSearchTools(provider='ollama')
        # In test environment, instructor is not available
        self.assertIsNone(toolkit.instructor_client)

    def test_agno_fallback_exists(self):
        """Test that Agno agent fallback is maintained."""
        toolkit = self.llm.MusicSearchTools(provider='ollama')
        # Agno is also not available in test environment, so ollama_agent will be None
        # This test just verifies the attribute exists
        self.assertTrue(hasattr(toolkit, 'ollama_agent'))


if __name__ == '__main__':
    unittest.main()
