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
        rating_key = -1 if value is None else value
        self.storage[key] = (rating_key, cleaned_metadata)

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

    def test_local_candidate_direct_match_short_circuits_search(self):
        track = types.SimpleNamespace(ratingKey=303, title='Vector Match', parentTitle='Album')

        class Music:
            def __init__(self):
                self.fetch_calls = []
                self.search_calls = []

            def fetchItem(self, key):
                self.fetch_calls.append(key)
                return track

            def searchTracks(self, **kwargs):
                self.search_calls.append(kwargs)
                return []

        class Candidate:
            def __init__(self, metadata, score):
                self.metadata = metadata
                self.score = score

            def song_dict(self):
                return {
                    'title': self.metadata.get('title', ''),
                    'album': self.metadata.get('album', ''),
                    'artist': self.metadata.get('artist', ''),
                }

            def overlap_tokens(self, counts):
                return []

        music = Music()
        plugin = types.SimpleNamespace()
        plugin._log = DummyLogger()
        plugin.cache = CacheStub()
        plugin.music = music
        plugin.search_llm = None
        plugin.manual_track_search = lambda song: None
        plugin._cache_result = lambda *args, **kwargs: None
        plugin._match_score_for_query = lambda song, track: 0.95

        candidate = Candidate(
            {'title': 'Vector Match', 'album': 'Album', 'artist': 'Artist', 'plex_ratingkey': 303},
            0.92,
        )
        plugin.get_local_beets_candidates = lambda song: [candidate]

        def stub_direct_match(cand, query):
            rating_key = cand.metadata.get('plex_ratingkey')
            if rating_key is None:
                return None
            return music.fetchItem(rating_key)

        plugin._try_candidate_direct_match = lambda cand, query, cache_key=None: stub_direct_match(cand, query)
        plugin._prepare_candidate_variants = lambda candidates, song: []

        song = {'title': 'Original', 'album': 'Album', 'artist': 'Artist'}
        result = self.search.search_plex_song(plugin, song, manual_search=False)

        self.assertIs(result, track)
        self.assertEqual(music.fetch_calls, [303])
        self.assertEqual(music.search_calls, [])

    def test_local_candidate_variant_fallback(self):
        variant_track = types.SimpleNamespace(
            ratingKey=808,
            title='Variant Song',
            parentTitle='Variant Album',
            artist=lambda: types.SimpleNamespace(title='Variant Artist'),
        )

        class Music:
            def __init__(self):
                self.search_calls = []

            def searchTracks(self, **kwargs):
                self.search_calls.append(kwargs)
                target = {'album.title': 'Variant Album', 'track.title': 'Variant Song'}
                filtered = {k: v for k, v in kwargs.items() if k != 'limit'}
                if filtered == target:
                    return [variant_track]
                return []

            def fetchItem(self, key):
                raise AssertionError('fetchItem should not be called without a rating key')

        class Candidate:
            def __init__(self, metadata, score):
                self.metadata = metadata
                self.score = score

            def song_dict(self):
                return {
                    'title': self.metadata.get('title', ''),
                    'album': self.metadata.get('album', ''),
                    'artist': self.metadata.get('artist', ''),
                }

            def overlap_tokens(self, counts):
                return []

        cache = CacheStub()
        recorded_cache = []
        music = Music()

        plugin = types.SimpleNamespace()
        plugin._log = DummyLogger()
        plugin.cache = cache
        plugin.music = music
        plugin.search_llm = None
        plugin.manual_track_search = lambda song: None
        def cache_result(key, result, cleaned=None):
            recorded_cache.append((key, result))
            cache.set(key, result, cleaned)
        plugin._cache_result = cache_result
        plugin.find_closest_match = lambda song, tracks: [(variant_track, 0.95)]
        plugin._match_score_for_query = lambda song, track: 0.92

        candidate = Candidate(
            {'title': 'Variant Song', 'album': 'Variant Album', 'artist': 'Variant Artist'},
            0.88,
        )
        plugin.get_local_beets_candidates = lambda song: [candidate]
        plugin._try_candidate_direct_match = lambda cand, query, cache_key=None: None

        def prepare_variants(candidates, original_song):
            return [(candidates[0].song_dict(), candidates[0].score)]

        plugin._prepare_candidate_variants = prepare_variants

        song = {'title': 'Original Song', 'album': 'Original Album', 'artist': 'Original Artist'}
        result = self.search.search_plex_song(plugin, song, manual_search=False)

        self.assertIs(result, variant_track)
        # Ensure the variant metadata search was attempted.
        self.assertGreaterEqual(len(music.search_calls), 1)
        self.assertTrue(
            any(
                {k: v for k, v in call.items() if k != 'limit'}
                == {'album.title': 'Variant Album', 'track.title': 'Variant Song'}
                for call in music.search_calls
            )
        )
        # Ensure search results are cached for the original query.
        cache_keys = list(cache.storage.keys())
        self.assertTrue(any('Original Song' in key for key in cache_keys))

    def test_single_track_low_similarity_rejected(self):
        track = types.SimpleNamespace(
            ratingKey=909,
            title='Mismatch Song',
            parentTitle='Mismatch Album',
            artist=lambda: types.SimpleNamespace(title='Mismatch Artist'),
        )

        class Music:
            def __init__(self):
                self.search_calls = []

            def searchTracks(self, **kwargs):
                self.search_calls.append(kwargs)
                return [track]

            def fetchItem(self, key):
                raise AssertionError('fetchItem should not be called without a rating key')

        cache = CacheStub()
        positive_results = []
        music = Music()

        plugin = types.SimpleNamespace()
        plugin._log = DummyLogger()
        plugin.cache = cache
        plugin.music = music
        plugin.search_llm = None
        plugin.manual_track_search = lambda song: None

        def cache_result(key, result, cleaned=None):
            if result is not None:
                positive_results.append(result)
            cache.set(key, result, cleaned)

        plugin._cache_result = cache_result
        plugin.find_closest_match = lambda song, tracks: []
        plugin.get_local_beets_candidates = lambda song: []
        plugin._try_candidate_direct_match = lambda cand, query, cache_key=None: None
        plugin._prepare_candidate_variants = lambda candidates, song: []
        plugin._match_score_for_query = lambda song, found: 0.55

        song = {'title': 'Original Song', 'album': 'Original Album', 'artist': 'Original Artist'}
        result = self.search.search_plex_song(plugin, song, manual_search=False)

        self.assertIsNone(result)
        self.assertFalse(positive_results)

    def test_user_confirmation_accepts_candidate(self):
        track = types.SimpleNamespace(
            ratingKey=111,
            title='Candidate Song',
            parentTitle='Candidate Album',
            artist=lambda: types.SimpleNamespace(title='Candidate Artist'),
        )

        class Music:
            def __init__(self):
                self.search_calls = []

            def searchTracks(self, **kwargs):
                self.search_calls.append(kwargs)
                return [track]

            def fetchItem(self, key):
                raise AssertionError('fetchItem should not be called without a rating key')

        cache = CacheStub()
        cached_results = []
        music = Music()

        plugin = types.SimpleNamespace()
        plugin._log = DummyLogger()
        plugin.cache = cache
        plugin.music = music
        plugin.search_llm = None
        plugin.manual_track_search_called = False

        def manual_track_search(_song):
            plugin.manual_track_search_called = True
            return None

        plugin.manual_track_search = manual_track_search
        plugin._cache_result = lambda key, result, cleaned=None: cached_results.append((key, result))
        plugin.find_closest_match = lambda song, tracks: []
        plugin.get_local_beets_candidates = lambda song: []
        plugin._try_candidate_direct_match = lambda cand, query, cache_key=None: None
        plugin._prepare_candidate_variants = lambda candidates, song: []
        plugin._candidate_confirmations = []

        def queue_candidate_confirmation(**kwargs):
            plugin._candidate_confirmations.append(kwargs)

        plugin._queue_candidate_confirmation = queue_candidate_confirmation
        plugin._match_score_for_query = lambda song, found: 0.75

        ui_module = self.search.ui
        original_input_yn = getattr(ui_module, 'input_yn', lambda prompt, default=True: default)
        responses = iter([True])
        ui_module.input_yn = lambda prompt, default=True: next(responses, default)
        self.addCleanup(lambda: setattr(ui_module, "input_yn", original_input_yn))

        song = {'title': 'Original Song', 'album': 'Original Album', 'artist': 'Original Artist'}

        result = self.search.search_plex_song(plugin, song, manual_search=True)

        self.assertIs(result, track)
        self.assertTrue(cached_results)
        self.assertFalse(plugin.manual_track_search_called)
        self.assertFalse(plugin._candidate_confirmations)

    def test_confirmation_survives_nested_call(self):
        variant_track = types.SimpleNamespace(
            ratingKey=222,
            title='Variant Track',
            parentTitle='Variant Album',
            artist=lambda: types.SimpleNamespace(title='Variant Artist'),
        )

        class Music:
            def __init__(self):
                self.search_calls = []

            def searchTracks(self, **kwargs):
                self.search_calls.append(kwargs)
                if kwargs.get('track.title') == 'Variant Track':
                    return [variant_track]
                return []

            def fetchItem(self, key):
                if key == variant_track.ratingKey:
                    return variant_track
                raise AssertionError(f'unexpected fetchItem call for {key}')

        class Candidate:
            def __init__(self, metadata, score):
                self.metadata = metadata
                self.score = score

            def song_dict(self):
                return {
                    'title': self.metadata.get('title', ''),
                    'album': self.metadata.get('album', ''),
                    'artist': self.metadata.get('artist', ''),
                }

            def overlap_tokens(self, counts):
                return []

        cache = CacheStub()
        cached_results = []
        music = Music()

        plugin = types.SimpleNamespace()
        plugin._log = DummyLogger()
        plugin.cache = cache
        plugin.music = music
        plugin.search_llm = None
        plugin.manual_track_search_called = False

        def manual_track_search(_song):
            plugin.manual_track_search_called = True
            return None

        plugin.manual_track_search = manual_track_search
        plugin._cache_result = lambda key, result, cleaned=None: cached_results.append((key, result))
        plugin.find_closest_match = lambda song, tracks: [(variant_track, 0.95)]

        candidate = Candidate(
            {'title': 'Original Candidate', 'album': 'Original Album', 'artist': 'Original Artist'},
            0.72,
        )
        plugin.get_local_beets_candidates = lambda song: [candidate]
        plugin._try_candidate_direct_match = lambda cand, query, cache_key=None: None

        def prepare_variants(_candidates, _song):
            return [(
                {'title': 'Variant Track', 'album': 'Variant Album', 'artist': 'Variant Artist'},
                0.82,
            )]

        plugin._prepare_candidate_variants = prepare_variants
        plugin._candidate_confirmations = []
        plugin._queue_candidate_confirmation = lambda **kwargs: plugin._candidate_confirmations.append(kwargs)

        def match_score(query, track):
            if query.get('title') == 'Variant Track':
                return 0.95
            return 0.55

        plugin._match_score_for_query = match_score

        ui_module = self.search.ui
        original_input_yn = getattr(ui_module, 'input_yn', lambda prompt, default=True: default)
        responses = iter([True])
        ui_module.input_yn = lambda prompt, default=True: next(responses, default)
        self.addCleanup(lambda: setattr(ui_module, "input_yn", original_input_yn))

        song = {'title': 'Original Song', 'album': 'Original Album', 'artist': 'Original Artist'}

        result = self.search.search_plex_song(plugin, song, manual_search=True)

        self.assertIs(result, variant_track)
        self.assertTrue(cached_results)
        self.assertFalse(plugin.manual_track_search_called)
        self.assertFalse(plugin._candidate_confirmations)
        self.assertGreaterEqual(len(music.search_calls), 1)

    def test_variant_rejected_when_similarity_low(self):
        variant_track = types.SimpleNamespace(
            ratingKey=512,
            title='Variant Song',
            parentTitle='Variant Album',
            artist=lambda: types.SimpleNamespace(title='Variant Artist'),
        )

        class Music:
            def __init__(self):
                self.search_calls = []

            def searchTracks(self, **kwargs):
                self.search_calls.append(kwargs)
                filtered = {k: v for k, v in kwargs.items() if k != 'limit'}
                target = {'album.title': 'Variant Album', 'track.title': 'Variant Song'}
                if filtered == target:
                    return [variant_track]
                return []

            def fetchItem(self, key):
                raise AssertionError('fetchItem should not be called without a rating key')

        class Candidate:
            def __init__(self, metadata, score):
                self.metadata = metadata
                self.score = score

            def song_dict(self):
                return {
                    'title': self.metadata.get('title', ''),
                    'album': self.metadata.get('album', ''),
                    'artist': self.metadata.get('artist', ''),
                }

            def overlap_tokens(self, counts):
                return []

        cache = CacheStub()
        cached_results = []
        music = Music()

        plugin = types.SimpleNamespace()
        plugin._log = DummyLogger()
        plugin.cache = cache
        plugin.music = music
        plugin.search_llm = None
        plugin.manual_track_search = lambda song: None
        def cache_result(key, result, cleaned=None):
            key_str = str(key)
            if result is not None and 'Original Song' in key_str:
                cached_results.append(result)
            cache.set(key, result, cleaned)
        plugin._cache_result = cache_result
        plugin.find_closest_match = lambda song, tracks: []
        plugin.get_local_beets_candidates = lambda song: [Candidate(
            {'title': 'Variant Song', 'album': 'Variant Album', 'artist': 'Variant Artist'},
            0.88,
        )]
        plugin._try_candidate_direct_match = lambda cand, query, cache_key=None: None
        plugin._prepare_candidate_variants = lambda candidates, song: [
            (candidates[0].song_dict(), candidates[0].score)
        ]

        def match_score(song, track):
            if song.get('title') == 'Original Song':
                return 0.5
            return 0.95

        plugin._match_score_for_query = match_score

        song = {'title': 'Original Song', 'album': 'Original Album', 'artist': 'Original Artist'}
        result = self.search.search_plex_song(plugin, song, manual_search=False)

        self.assertIsNone(result)
        # Ensure no positive cache entry was written.
        self.assertFalse(cached_results)


if __name__ == '__main__':
    unittest.main()
