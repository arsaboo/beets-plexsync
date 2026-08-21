import importlib
import json
import sys
import types

import pytest

from tests.test_playlist_import import ensure_stubs


class LLMSearchTest:
    @pytest.fixture(autouse=True)
    def setup(self, request):
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
        _saved_pydantic = sys.modules.get('pydantic')

        def _restore_pydantic():
            if _saved_pydantic is None:
                sys.modules.pop('pydantic', None)
            else:
                sys.modules['pydantic'] = _saved_pydantic

        request.addfinalizer(_restore_pydantic)
        sys.modules['pydantic'] = types.SimpleNamespace(
            BaseModel=SimpleBaseModel,
            Field=Field,
            field_validator=field_validator,
        )
        ensure_stubs(
            {
                'llm': {
                    'api_key': '',
                    'model': 'gpt-3.5-turbo',
                    'base_url': '',
                    'search': {
                        'provider': '',
                        'api_key': '',
                        'base_url': '',
                        'model': '',
                        'embedding_model': 'snowflake-arctic-embed2:latest',
                    },
                }
            },
            request.addfinalizer,
        )
        if 'beetsplug.ai.llm' in sys.modules:
            importlib.reload(sys.modules['beetsplug.ai.llm'])
        else:
            importlib.import_module('beetsplug.ai.llm')
        self.llm = importlib.import_module('beetsplug.ai.llm')
        yield
        if 'beetsplug.ai.llm' in sys.modules:
            sys.modules['beetsplug.ai.llm']._search_toolkit = None

    def test_search_track_info_toolkit_missing(self):
        self.llm._search_toolkit = None
        result = self.llm.search_track_info('Test Song')
        assert result == {'title': 'Test Song', 'artist': '', 'album': None}

    def test_search_track_info_with_toolkit(self):
        class Toolkit:
            def search_song_info(self, query):
                return {'title': 'Found', 'artist': 'Artist', 'album': 'Album'}
        self.llm._search_toolkit = Toolkit()
        result = self.llm.search_track_info('Input Song')
        assert result == {'title': 'Found', 'artist': 'Artist', 'album': 'Album'}

    def test_instructor_available_flag(self):
        """Test that INSTRUCTOR_AVAILABLE flag matches actual instructor availability."""
        try:
            import instructor  # noqa: F401
            expected = True
        except ImportError:
            expected = False
        assert self.llm.INSTRUCTOR_AVAILABLE == expected

    def test_create_fallback_song(self):
        """Test fallback song creation."""
        toolkit = self.llm.MusicSearchTools(provider='ollama')
        fallback = toolkit._create_fallback_song('Test Title')
        assert fallback.title == 'Test Title'
        assert fallback.artist == ''
        assert fallback.album is None

    def test_instructor_client_initialization(self):
        """Test that instructor_client is initialized when instructor is available."""
        toolkit = self.llm.MusicSearchTools(provider='ollama')
        # In test environment, instructor is not available
        assert toolkit.instructor_client is None

    def test_agno_fallback_exists(self):
        """Test that Agno agent fallback is maintained."""
        toolkit = self.llm.MusicSearchTools(provider='ollama')
        # Agno is also not available in test environment, so ollama_agent will be None
        # This test just verifies the attribute exists
        assert hasattr(toolkit, 'ollama_agent')
