import types

from beetsplug.plexsync import PlexSync


class DummyLogger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class SearchPlexTrackTests:
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

        assert result == "the-track"
        assert len(calls) == 1
        song, kwargs = calls[0]
        assert song == {
            "title": "Rang De Basanti", "album": "Rang De Basanti", "artist": "A.R. Rahman",
        }
        # Non-interactive: no manual prompts from a ThreadPoolExecutor worker.
        assert kwargs["manual_search"] is False
        # Skip the LLM web-search cleanup fallback: a full-library resync
        # should stay deterministic (see live-testing note in the docstring).
        assert kwargs["llm_attempted"] is True
        # Don't let unrelated beets items borrow each other's cached match.
        assert kwargs["use_local_candidates"] is False
        # Force resync should re-check live Plex state, not a stale cache
        # entry written by an unrelated feature (playlist import, etc.).
        assert kwargs["use_cache"] is False
        # No manual-prompt/LLM-enhancement queueing for a library sync.
        assert kwargs["playlist_id"] is None

    def test_returns_none_without_raising_when_no_genuine_match(self):
        """No real Plex counterpart exists - must not collide onto a guess."""
        plugin = types.SimpleNamespace(
            _log=DummyLogger(),
            search_plex_song=lambda song, **kwargs: None,
        )
        item = self._make_item(title="Ghoomerdar Lehengo", album="Ghoomerdar Lehengo")

        result = PlexSync.search_plex_track(plugin, item)

        assert result is None

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

        assert result_a is None
        assert result_b is None


class FakeBeetsItem:
    """Minimal stand-in for a beets Item supporting the subset of the
    Model/flex-attribute protocol _process_item relies on: `in`, `del`,
    attribute get/set, plus store()/try_write() call tracking."""

    def __init__(self, **fields):
        self._fields = dict(fields)
        self._db = types.SimpleNamespace(directory=b"/music")
        self.store_calls = 0
        self.try_write_calls = 0

    def __contains__(self, key):
        return key in self._fields

    def __delitem__(self, key):
        del self._fields[key]

    def __setattr__(self, name, value):
        if name in ("_fields", "_db", "store_calls", "try_write_calls"):
            object.__setattr__(self, name, value)
        else:
            self._fields[name] = value

    def __getattr__(self, name):
        # Only called when normal attribute lookup fails (i.e. not one of
        # the real instance attributes set via object.__setattr__ above).
        try:
            return self._fields[name]
        except KeyError:
            raise AttributeError(name)

    def store(self):
        self.store_calls += 1

    def try_write(self):
        self.try_write_calls += 1

    def __str__(self):
        return self._fields.get("title", "<item>")


class ProcessItemStaleFieldClearingTests:
    """Regression tests for _process_item leaving a stale plex_ratingkey in
    place when search_plex_track now correctly returns None.

    Before this fix: a beets item whose old (buggy) match collided with
    another item, or whose Plex track was since removed/retagged, kept its
    bogus plex_ratingkey forever - search_plex_track returning None just
    caused an early return, never overwriting the stale value. So `-f`
    could never actually fix these items even though the matcher itself was
    already correct.
    """

    PLEX_FIELDS = (
        "plex_guid",
        "plex_ratingkey",
        "plex_userrating",
        "plex_skipcount",
        "plex_viewcount",
        "plex_lastviewedat",
        "plex_lastratedat",
        "plex_updated",
    )

    def _make_plugin(self, search_result=None):
        return types.SimpleNamespace(
            _log=DummyLogger(),
            search_plex_track=lambda item: search_result,
        )

    def test_clears_stale_plex_fields_when_no_longer_matched(self):
        item = FakeBeetsItem(
            title="Kurja",
            plex_ratingkey=595890,
            plex_guid="old-guid",
            plex_userrating=8.0,
        )
        plugin = self._make_plugin(search_result=None)

        PlexSync._process_item(plugin, 1, item, write=False, force=True, items_len=1)

        for field in ("plex_ratingkey", "plex_guid", "plex_userrating"):
            assert field not in item
        assert item.store_calls == 1

    def test_does_not_store_when_nothing_to_clear(self):
        """An item that never had Plex fields shouldn't trigger a needless
        store() just because the search returned None."""
        item = FakeBeetsItem(title="Never Synced")
        plugin = self._make_plugin(search_result=None)

        PlexSync._process_item(plugin, 1, item, write=False, force=True, items_len=1)

        assert item.store_calls == 0

    def test_successful_match_still_sets_all_fields_normally(self):
        track = types.SimpleNamespace(
            guid="new-guid",
            ratingKey=12345,
            userRating=9.0,
            skipCount=0,
            viewCount=3,
            lastViewedAt=None,
            lastRatedAt=None,
        )
        item = FakeBeetsItem(title="Found Song")
        plugin = self._make_plugin(search_result=track)

        PlexSync._process_item(plugin, 1, item, write=False, force=True, items_len=1)

        assert item.plex_ratingkey == 12345
        assert item.plex_guid == "new-guid"
        assert item.store_calls == 1

