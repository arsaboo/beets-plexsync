import types

from beetsplug.plexsync import PlexSync


class DummyLogger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
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


class FakeLib:
    """Minimal Library stand-in: transaction() is a no-op context manager."""

    def __init__(self):
        self.directory = b"/music"
        self.transaction_calls = 0

    def transaction(self):
        self.transaction_calls += 1
        return _NullCM()


class _NullCM:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeBeetsItem:
    """Minimal stand-in for a beets Item supporting the subset of the
    Model/flex-attribute protocol the sync path relies on: `in`, `del`,
    attribute get/set, plus store()/try_write() call tracking."""

    def __init__(self, db=None, **fields):
        self._fields = dict(fields)
        self._db = db if db is not None else FakeLib()
        self.store_calls = 0
        self.try_write_calls = 0
        self.call_order = []

    def __contains__(self, key):
        return key in self._fields

    def __delitem__(self, key):
        del self._fields[key]

    def __setattr__(self, name, value):
        if name in ("_fields", "_db", "store_calls", "try_write_calls", "call_order"):
            object.__setattr__(self, name, value)
        else:
            self._fields[name] = value

    def __getattr__(self, name):
        try:
            return self._fields[name]
        except KeyError:
            raise AttributeError(name)

    def store(self):
        self.call_order.append("store")
        self.store_calls += 1

    def try_write(self):
        self.call_order.append("write")
        self.try_write_calls += 1

    def __str__(self):
        return self._fields.get("title", "<item>")


def _sync_plugin(search_result=None):
    plugin = types.SimpleNamespace(
        _log=DummyLogger(),
        _PLEX_ITEM_FIELDS=PlexSync._PLEX_ITEM_FIELDS,
        _PLEX_SEARCH_SKIP=PlexSync._PLEX_SEARCH_SKIP,
        search_plex_track=lambda item: search_result,
        create_progress_counter=lambda *a, **k: None,
    )
    plugin._apply_plex_result = lambda item, track: PlexSync._apply_plex_result(
        plugin, item, track
    )
    plugin._search_plex_item = lambda index, item, force, items_len: (
        PlexSync._search_plex_item(plugin, index, item, force, items_len)
    )
    return plugin


class ApplyPlexResultTests:
    """In-memory field updates (no store/write)."""

    def test_clears_stale_plex_fields_when_no_longer_matched(self):
        item = FakeBeetsItem(
            title="Kurja",
            plex_ratingkey=595890,
            plex_guid="old-guid",
            plex_userrating=8.0,
        )
        plugin = _sync_plugin()
        mutated = PlexSync._apply_plex_result(plugin, item, None)

        assert mutated is True
        for field in ("plex_ratingkey", "plex_guid", "plex_userrating"):
            assert field not in item
        assert item.store_calls == 0

    def test_does_not_report_mutation_when_nothing_to_clear(self):
        item = FakeBeetsItem(title="Never Synced")
        plugin = _sync_plugin()
        mutated = PlexSync._apply_plex_result(plugin, item, None)

        assert mutated is False
        assert item.store_calls == 0

    def test_successful_match_sets_fields_without_storing(self):
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
        plugin = _sync_plugin()
        mutated = PlexSync._apply_plex_result(plugin, item, track)

        assert mutated is True
        assert item.plex_ratingkey == 12345
        assert item.plex_guid == "new-guid"
        assert item.store_calls == 0


class FetchPlexInfoBatchTests:
    """Main-thread write-then-store + one SQLite transaction."""

    def test_stores_mutated_items_in_one_transaction(self):
        lib = FakeLib()
        matched = FakeBeetsItem(db=lib, title="Found Song")
        stale = FakeBeetsItem(
            db=lib, title="Stale", plex_ratingkey=1, plex_userrating=5.0
        )
        never = FakeBeetsItem(db=lib, title="Never Synced")

        track = types.SimpleNamespace(
            guid="g",
            ratingKey=99,
            userRating=8.0,
            skipCount=0,
            viewCount=1,
            lastViewedAt=None,
            lastRatedAt=None,
        )

        def search(item):
            if item.title == "Found Song":
                return track
            return None

        plugin = _sync_plugin()
        plugin.search_plex_track = search
        plugin._search_plex_item = lambda index, item, force, items_len: (
            PlexSync._search_plex_item(plugin, index, item, force, items_len)
        )

        PlexSync._fetch_plex_info(
            plugin, [matched, stale, never], write=False, force=True
        )

        assert matched.plex_ratingkey == 99
        assert "plex_ratingkey" not in stale
        assert matched.store_calls == 1
        assert stale.store_calls == 1
        assert never.store_calls == 0
        assert lib.transaction_calls == 1
        assert matched.try_write_calls == 0

    def test_try_write_happens_before_store_when_write_true(self):
        lib = FakeLib()
        item = FakeBeetsItem(db=lib, title="Found Song")
        track = types.SimpleNamespace(
            guid="g",
            ratingKey=1,
            userRating=1.0,
            skipCount=0,
            viewCount=0,
            lastViewedAt=None,
            lastRatedAt=None,
        )
        plugin = _sync_plugin(search_result=track)
        plugin._search_plex_item = lambda index, it, force, items_len: (
            PlexSync._search_plex_item(plugin, index, it, force, items_len)
        )

        PlexSync._fetch_plex_info(plugin, [item], write=True, force=True)

        assert item.call_order == ["write", "store"]
        assert lib.transaction_calls == 1

    def test_skips_already_synced_without_force(self):
        lib = FakeLib()
        item = FakeBeetsItem(db=lib, title="Synced", plex_userrating=8.0)
        plugin = _sync_plugin(search_result=types.SimpleNamespace(ratingKey=1))
        plugin._search_plex_item = lambda index, it, force, items_len: (
            PlexSync._search_plex_item(plugin, index, it, force, items_len)
        )

        PlexSync._fetch_plex_info(plugin, [item], write=False, force=False)

        assert item.store_calls == 0
        assert lib.transaction_calls == 0

