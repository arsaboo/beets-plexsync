"""Tests for smart playlist helpers.

Focus: the Daily Discovery dedup fix - the old inline loop keyed on
``ratingKey`` only, which beets Items (exposing ``plex_ratingkey``)
don't have, so the entire library discovery pool was silently dropped.
"""

import types

from beetsplug.plex import smartplaylists


def _plex_track(key, title="Plex Track"):
    return types.SimpleNamespace(ratingKey=key, title=title)


def _beets_item(key, title="Beets Item", rating=0):
    return types.SimpleNamespace(
        plex_ratingkey=key, title=title, plex_userrating=rating
    )


class DedupeByRatingKeyTest:
    def test_keeps_library_beets_items(self):
        # Regression: beets Items must survive dedup (previously dropped
        # because they lack a .ratingKey attribute).
        lookup = {1: _beets_item(1), 2: _beets_item(2)}
        tracks = [_plex_track(1), _beets_item(2)]
        result = smartplaylists._dedupe_by_rating_key(tracks, lookup)
        assert [t.plex_ratingkey for t in result] == [1, 2]

    def test_normalizes_plex_tracks_to_beets_items(self):
        item = _beets_item(1)
        lookup = {1: item}
        result = smartplaylists._dedupe_by_rating_key([_plex_track(1)], lookup)
        assert result == [item]
        assert all(not hasattr(t, "ratingKey") for t in result)

    def test_drops_cross_type_duplicates(self):
        # Same rating key via a Plex Track and a beets Item -> one entry.
        item1, item2 = _beets_item(1), _beets_item(2)
        lookup = {1: item1, 2: item2}
        tracks = [_plex_track(1), _beets_item(2), _beets_item(1)]
        result = smartplaylists._dedupe_by_rating_key(tracks, lookup)
        assert [t.plex_ratingkey for t in result] == [1, 2]
        assert result[0] is item1  # first occurrence (the Plex Track) wins

    def test_drops_unresolvable_plex_tracks(self):
        # A Plex Track with no beets counterpart is dropped, not kept.
        lookup = {1: _beets_item(1)}
        result = smartplaylists._dedupe_by_rating_key([_plex_track(99), _beets_item(1)], lookup)
        assert [t.plex_ratingkey for t in result] == [1]

    def test_empty_and_keyless(self):
        assert smartplaylists._dedupe_by_rating_key([], {}) == []
        keyless = types.SimpleNamespace(title="No Key")
        assert smartplaylists._dedupe_by_rating_key([keyless], {}) == []


class TrackScoringTest:
    """Scoring must tolerate missing/None flex-field values (beets stores
    unset flexible attrs as None, and popularity can be None for tracks that
    the beets `spotify` plugin hasn't synced). A single None used to crash
    the whole weighted selection with TypeError."""

    @staticmethod
    def _track(name, popularity=None, rating=5, viewcount="10"):
        return types.SimpleNamespace(
            title=name,
            album="X",
            artist="Y",
            plex_userrating=rating,
            plex_viewcount=viewcount,
            plex_lastviewedat=None,
            year=2018,
            spotify_track_popularity=popularity,
        )

    def test_scores_track_with_none_popularity(self):
        # Regression: popularity=None (unset flex field) must not crash.
        track = self._track("None pop", popularity=None)
        score = smartplaylists.calculate_track_score(None, track)
        assert 0 <= score <= 100

    def test_scores_track_with_string_popularity(self):
        # beets stores flex attrs as strings -> must parse, not crash.
        track = self._track("Str pop", popularity="42")
        assert smartplaylists.calculate_track_score(None, track) > 0

    def test_scores_track_without_popularity_attr(self):
        track = self._track("No pop")
        del track.spotify_track_popularity
        score = smartplaylists.calculate_track_score(None, track)
        assert 0 <= score <= 100

    def test_select_tracks_with_mixed_none_popularity(self):
        # A None popularity among other tracks must not abort selection.
        pool = [
            self._track("A", popularity="42"),
            self._track("B", popularity=None),
            self._track("C", popularity="17"),
        ]
        selected = smartplaylists.select_tracks_weighted(None, pool, 2, playlist_type="daily_discovery")
        assert len(selected) == 2

    def test_scores_track_with_none_rating_and_viewcount(self):
        # Un-synced-ish item with None rating/viewcount must not crash.
        track = self._track("None rating", popularity="10", rating=None, viewcount=None)
        score = smartplaylists.calculate_track_score(None, track)
        assert 0 <= score <= 100


class SongIdentityTest:
    """Compilation/clone copies of the same song (distinct rating keys, same
    title+artist) must collapse to a single best representative."""

    @staticmethod
    def _item(key, title, artist="Artist", rating=5, plays=0, last=None):
        return types.SimpleNamespace(
            plex_ratingkey=key, title=title, artist=artist,
            plex_userrating=rating, plex_viewcount=plays, plex_lastviewedat=last,
        )

    def test_collapses_same_song_different_keys(self):
        items = [
            self._item(1, "Hit Song", "Artist", rating=5),
            self._item(2, "Hit Song", "Artist", rating=8),  # best copy
            self._item(3, "Hit Song", "Artist", rating=3),
        ]
        result = smartplaylists._dedupe_by_song_identity(items)
        assert len(result) == 1
        assert result[0].plex_ratingkey == 2  # best rating wins

    def test_keeps_distinct_songs(self):
        items = [
            self._item(1, "Song A", "Artist"),
            self._item(2, "Song B", "Artist"),
        ]
        assert [t.plex_ratingkey for t in smartplaylists._dedupe_by_song_identity(items)] == [1, 2]

    def test_prefers_most_played_on_tie(self):
        items = [
            self._item(1, "Hit", "A", rating=5, plays=2),
            self._item(2, "Hit", "A", rating=5, plays=9),  # more played wins tie
        ]
        result = smartplaylists._dedupe_by_song_identity(items)
        assert result[0].plex_ratingkey == 2

    def test_keeps_keyless_items(self):
        items = [
            self._item(1, "Hit", "A"),
            types.SimpleNamespace(title="", artist="A", plex_ratingkey=None),  # no title
            types.SimpleNamespace(title="NoKey", artist="B", plex_ratingkey=None),
        ]
        result = smartplaylists._dedupe_by_song_identity(items)
        assert len(result) == 3  # nothing keyable is dropped


class MinPopularityTest:
    """`filters.min_popularity` floor drops tracks without verified popularity."""

    @staticmethod
    def _item(key, title, pop):
        return types.SimpleNamespace(
            plex_ratingkey=key, title=title, artist="A",
            spotify_track_popularity=pop,
        )

    def test_drops_below_floor_and_unverified(self):
        items = [
            self._item(1, "A", "50"),
            self._item(2, "B", "10"),
            self._item(3, "C", None),   # unverified -> dropped
            self._item(4, "D", 0),      # unverified -> dropped
            self._item(5, "E", "42"),
        ]
        ps = types.SimpleNamespace(_log=types.SimpleNamespace(debug=lambda *a, **k: None))
        result = smartplaylists._apply_min_popularity(ps, items, 40, "test")
        assert [t.plex_ratingkey for t in result] == [1, 5]

    def test_noop_without_floor(self):
        items = [self._item(1, "A", None)]
        ps = types.SimpleNamespace(_log=types.SimpleNamespace(debug=lambda *a, **k: None))
        assert smartplaylists._apply_min_popularity(ps, items, None, "test") == items
