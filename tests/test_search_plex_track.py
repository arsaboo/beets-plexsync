import types
import unittest

from beetsplug.plexsync import PlexSync


class DummyLogger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class SearchPlexTrackTests(unittest.TestCase):
    """Unit tests for PlexSync.search_plex_track's delegation to search_plex_song.

    search_plex_track used to run its own brittle album+title query with no
    confidence threshold, which could confidently return the wrong track (or
    collide multiple distinct beets items onto the same Plex ratingKey) when
    no genuine match existed. It now delegates to the multi-strategy,
    threshold-gated search_plex_song pipeline, so these tests focus on
    verifying the delegation contract rather than re-testing the pipeline
    itself (already covered by tests/test_plex_search.py).
    """

    def _make_item(self, title="Song", album="Album", artist="Artist"):
        return types.SimpleNamespace(title=title, album=album, artist=artist)

    def test_delegates_with_non_interactive_fresh_search_kwargs(self):
        calls = []

        def fake_search_plex_song(song, **kwargs):
            calls.append((song, kwargs))
            return "the-track"

        plugin = types.SimpleNamespace(
            _log=DummyLogger(),
            search_plex_song=fake_search_plex_song,
        )
        item = self._make_item(title="Rang De Basanti", album="Rang De Basanti", artist="A.R. Rahman")

        result = PlexSync.search_plex_track(plugin, item)

        self.assertEqual(result, "the-track")
        self.assertEqual(len(calls), 1)
        song, kwargs = calls[0]
        self.assertEqual(
            song,
            {"title": "Rang De Basanti", "album": "Rang De Basanti", "artist": "A.R. Rahman"},
        )
        # Non-interactive: no manual prompts from a ThreadPoolExecutor worker.
        self.assertEqual(kwargs["manual_search"], False)
        # Skip the LLM web-search cleanup fallback: a full-library resync
        # should stay deterministic (see live-testing note in the docstring).
        self.assertEqual(kwargs["llm_attempted"], True)
        # Don't let unrelated beets items borrow each other's cached match.
        self.assertEqual(kwargs["use_local_candidates"], False)
        # Force resync should re-check live Plex state, not a stale cache
        # entry written by an unrelated feature (playlist import, etc.).
        self.assertEqual(kwargs["use_cache"], False)
        # No manual-prompt/LLM-enhancement queueing for a library sync.
        self.assertIsNone(kwargs["playlist_id"])

    def test_returns_none_without_raising_when_no_genuine_match(self):
        """No real Plex counterpart exists - must not collide onto a guess."""
        plugin = types.SimpleNamespace(
            _log=DummyLogger(),
            search_plex_song=lambda song, **kwargs: None,
        )
        item = self._make_item(title="Ghoomerdar Lehengo", album="Ghoomerdar Lehengo")

        result = PlexSync.search_plex_track(plugin, item)

        self.assertIsNone(result)

    def test_distinct_items_with_no_match_do_not_collide(self):
        """Two distinct beets items with no genuine Plex counterpart (e.g. the
        Ghoomar case: beets has 3 rows with no overlap in Plex's 3 differently
        -attributed "Ghoomar" tracks) must each resolve independently, never
        silently sharing a ratingKey."""
        plugin = types.SimpleNamespace(
            _log=DummyLogger(),
            search_plex_song=lambda song, **kwargs: None,
        )
        item_a = self._make_item(title="Ghoomar", album="Ghoomar", artist="Tsebster")
        item_b = self._make_item(title="Ghoomar", album="Ghoomar", artist="Deva Khan")

        result_a = PlexSync.search_plex_track(plugin, item_a)
        result_b = PlexSync.search_plex_track(plugin, item_b)

        self.assertIsNone(result_a)
        self.assertIsNone(result_b)


if __name__ == "__main__":
    unittest.main()
