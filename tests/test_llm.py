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
                        'reasoning_effort': '',
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

    def test_normalize_reasoning_effort(self):
        norm = self.llm.normalize_reasoning_effort
        assert norm('') is None
        assert norm(None) is None
        assert norm('  Low ') == 'low'
        assert norm('NONE') == 'none'
        assert norm('unlimited') is None

    def test_reasoning_kwargs(self):
        assert self.llm.reasoning_kwargs(None) == {}
        assert self.llm.reasoning_kwargs('low') == {'reasoning_effort': 'low'}
        assert self.llm.reasoning_kwargs('none') == {
            'reasoning_effort': 'none',
            'extra_body': {'thinking': {'type': 'disabled'}},
        }

    def test_reasoning_rejected_detection(self):
        assert self.llm.reasoning_rejected(Exception("Unsupported parameter: 'reasoning_effort'"))
        assert self.llm.reasoning_rejected(Exception("thinking is not supported"))
        assert not self.llm.reasoning_rejected(Exception('connection reset by peer'))

    def test_unknown_reasoning_effort_is_ignored(self):
        toolkit = self.llm.MusicSearchTools(provider='openai', reasoning_effort='unlimited')
        assert toolkit.reasoning_effort is None
        assert toolkit._reasoning_kwargs() == {}

    def test_reasoning_kwargs_for_openai_provider(self):
        toolkit = self.llm.MusicSearchTools(
            provider='openai', api_key='k', base_url='http://x/v1', reasoning_effort='minimal'
        )
        assert toolkit._reasoning_kwargs() == {'reasoning_effort': 'minimal'}

    def test_ollama_provider_ignores_reasoning_effort(self):
        toolkit = self.llm.MusicSearchTools(provider='ollama', reasoning_effort='none')
        assert toolkit.reasoning_effort == 'none'
        assert toolkit._reasoning_kwargs() == {}

    def test_disable_reasoning_stops_sending_controls(self):
        toolkit = self.llm.MusicSearchTools(
            provider='openai', api_key='k', base_url='http://x/v1', reasoning_effort='none'
        )
        toolkit._disable_reasoning(Exception("Unsupported parameter: 'reasoning_effort'"))
        assert toolkit._reasoning_rejected is True
        assert toolkit._reasoning_kwargs() == {}
