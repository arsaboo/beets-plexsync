"""Tests for smart playlist helpers.

Focus: the Daily Discovery dedup fix - the old inline loop keyed on
``ratingKey`` only, which beets Items (exposing ``plex_ratingkey``)
don't have, so the entire library discovery pool was silently dropped.
"""

import types
from datetime import datetime

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

    def test_weighted_selection_with_zero_count_is_empty(self):
        assert smartplaylists.select_tracks_weighted(
            None, [self._track("A", popularity="10")], 0,
        ) == []


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


class PlexRatingKeyDedupeTest:
    """Duplicate beets entries pointing at the same Plex track must collapse to
    the best representative (the regular path dedupes via the lookup dict, but
    special playlists iterate raw beets items)."""

    @staticmethod
    def _item(key, title, album="Album", rating=5, plays=0, last=None):
        return types.SimpleNamespace(
            plex_ratingkey=key, title=title, artist="Artist", album=album,
            plex_userrating=rating, plex_viewcount=plays, plex_lastviewedat=last,
        )

    def test_collapses_same_rating_key_keeps_best(self):
        items = [
            self._item(7, "Jogi", album="Vol. 2", rating=5, plays=3),
            self._item(7, "Jogi", album="Vol. 3", rating=8, plays=9),  # best
            self._item(7, "Jogi", album="Vol:2", rating=3),
        ]
        result = smartplaylists._dedupe_by_plex_ratingkey(items)
        assert len(result) == 1
        assert result[0].album == "Vol. 3"  # best representative wins

    def test_keeps_distinct_rating_keys(self):
        items = [self._item(1, "A"), self._item(2, "B")]
        assert [t.plex_ratingkey for t in smartplaylists._dedupe_by_plex_ratingkey(items)] == [1, 2]

    def test_passes_through_keyless_items(self):
        items = [
            self._item(1, "A"),
            types.SimpleNamespace(title="NoKey", artist="B", plex_ratingkey=None),
        ]
        result = smartplaylists._dedupe_by_plex_ratingkey(items)
        assert len(result) == 2


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

    def test_zero_floor_keeps_real_zero_but_drops_missing(self):
        items = [
            self._item(1, "verified zero", "0"),
            self._item(2, "missing", None),
        ]
        ps = types.SimpleNamespace(_log=types.SimpleNamespace(debug=lambda *a, **k: None))
        result = smartplaylists._apply_min_popularity(ps, items, 0, "test")
        assert [t.plex_ratingkey for t in result] == [1]

    def test_noop_without_floor(self):
        items = [self._item(1, "A", None)]
        ps = types.SimpleNamespace(_log=types.SimpleNamespace(debug=lambda *a, **k: None))
        assert smartplaylists._apply_min_popularity(ps, items, None, "test") == items


class SelectByPopularityTest:
    """The unrated selection should be popularity-first: pick the most popular
    eligible tracks, with only a little bounded randomness at near-ties."""

    @staticmethod
    def _ps():
        return types.SimpleNamespace(_log=types.SimpleNamespace(
            warning=lambda *a, **k: None,
            info=lambda *a, **k: None,
            debug=lambda *a, **k: None,
        ))

    @staticmethod
    def _item(key, title, pop):
        return types.SimpleNamespace(
            plex_ratingkey=key, title=title, artist="A",
            spotify_track_popularity=pop,
        )

    def test_picks_most_popular(self):
        # Gaps of 20 (>> 2*jitter) => deterministic top-3 by popularity.
        items = [
            self._item(1, "A", "10"),
            self._item(2, "B", "90"),
            self._item(3, "C", "50"),
            self._item(4, "D", "30"),
            self._item(5, "E", "70"),
        ]
        ps = self._ps()
        sel = smartplaylists.select_by_popularity(ps, items, 3)
        pops = sorted(float(t.spotify_track_popularity) for t in sel)
        assert pops == [50.0, 70.0, 90.0]

    def test_gap_beyond_jitter_is_deterministic(self):
        # A(90) vs B(85): gap 5 > 2*jitter(4), so A can never be displaced.
        items = [
            self._item(1, "A", "90"),
            self._item(2, "B", "85"),
            self._item(3, "C", "10"),
        ]
        ps = self._ps()
        for _ in range(200):
            assert smartplaylists.select_by_popularity(ps, items, 1)[0].plex_ratingkey == 1

    def test_near_ties_can_rotate(self):
        # A(90) vs B(89): gap 1 < jitter span, so both can win; C(10) never.
        items = [
            self._item(1, "A", "90"),
            self._item(2, "B", "89"),
            self._item(3, "C", "10"),
        ]
        ps = self._ps()
        winners = set()
        for _ in range(200):
            winners.add(smartplaylists.select_by_popularity(ps, items, 1)[0].plex_ratingkey)
        assert winners <= {1, 2}
        assert winners == {1, 2}  # both appear over enough runs

    def test_zero_popularity_treated_as_scored(self):
        # A real pop=0 is scored (unlike missing); so the unverified C is only
        # ever chosen as a fill when there are too few scored candidates.
        items = [
            self._item(1, "A", "10"),
            self._item(2, "B", "0"),     # real zero -> scored
            self._item(3, "C", None),    # missing -> unscored
        ]
        ps = self._ps()
        for _ in range(50):
            sel = smartplaylists.select_by_popularity(ps, items, 2)
            assert all(t.plex_ratingkey != 3 for t in sel)
        assert {t.plex_ratingkey for t in smartplaylists.select_by_popularity(ps, items, 3)} == {1, 2, 3}

    def test_all_missing_returns_unverified(self):
        items = [self._item(1, "A", None), self._item(2, "B", None)]
        ps = self._ps()
        sel = smartplaylists.select_by_popularity(ps, items, 1)
        assert len(sel) == 1 and sel[0].plex_ratingkey in (1, 2)

    def test_non_finite_and_out_of_range_popularity_are_unverified(self):
        items = [
            self._item(1, "valid", "10"),
            self._item(2, "infinite", "inf"),
            self._item(3, "too high", "101"),
            self._item(4, "negative", "-1"),
        ]
        ps = self._ps()
        assert smartplaylists.select_by_popularity(ps, items, 1) == [items[0]]

    def test_empty_and_zero_count(self):
        ps = self._ps()
        assert smartplaylists.select_by_popularity(ps, [], 5) == []
        assert smartplaylists.select_by_popularity(ps, [self._item(1, "A", "10")], 0) == []


class FilterBeetsItemsTest:
    """Null semantics for the beets-centric candidate filter (the Forgotten
    Gems 0-unrated regression: unrated + never-played must survive)."""

    NOW = 1_000_000_000.0

    def _item(self, title="T", key=1, genres=None, year=None, rating=None,
              played_days_ago=None, viewcount=None):
        lastviewed = None
        if played_days_ago is not None:
            lastviewed = datetime.fromtimestamp(self.NOW - played_days_ago * 86400)
        return types.SimpleNamespace(
            plex_ratingkey=key, title=title, genres=genres, year=year,
            plex_userrating=rating, plex_lastviewedat=lastviewed,
            plex_viewcount=viewcount,
        )

    def _keys(self, items):
        return {it.plex_ratingkey for it in items}

    def test_min_rating_keeps_unrated(self):
        f = {"min_rating": 5}
        items = [self._item("unr_none", 1, rating=None),
                 self._item("unr_zero", 2, rating="0"),
                 self._item("low", 3, rating="4"),
                 self._item("ok", 4, rating="6")]
        res = smartplaylists._filter_beets_items(items, f, None, now_ts=self.NOW)
        assert self._keys(res) == {1, 2, 4}  # 3 (rating 4) dropped

    def test_exclusion_keeps_never_played(self):
        items = [self._item("never", 1, played_days_ago=None),
                 self._item("recent", 2, played_days_ago=10),
                 self._item("old", 3, played_days_ago=100)]
        res = smartplaylists._filter_beets_items(items, {}, 90, now_ts=self.NOW)
        assert self._keys(res) == {1, 3}  # 2 (played 10d ago) dropped

    def test_include_genres_rejects_no_genre(self):
        f = {"include": {"genres": ["Punjabi"]}}
        items = [self._item("none", 1, genres=None),
                 self._item("match", 2, genres=["Punjabi", "Filmi"]),
                 self._item("other", 3, genres=["Rock"])]
        res = smartplaylists._filter_beets_items(items, f, None, now_ts=self.NOW)
        assert self._keys(res) == {2}

    def test_exclude_genres_keeps_no_genre(self):
        f = {"exclude": {"genres": ["Religious"]}}
        items = [self._item("none", 1, genres=None),
                 self._item("bad", 2, genres=["Religious"]),
                 self._item("ok", 3, genres=["Rock"])]
        res = smartplaylists._filter_beets_items(items, f, None, now_ts=self.NOW)
        assert self._keys(res) == {1, 3}

    def test_include_year_rejects_unknown(self):
        f = {"include": {"years": {"between": [1970, 1989]}}}
        items = [self._item("nonyear", 1, year=None),
                 self._item("in", 2, year=1985),
                 self._item("out", 3, year=1965)]
        res = smartplaylists._filter_beets_items(items, f, None, now_ts=self.NOW)
        assert self._keys(res) == {2}

    def test_max_plays_optional(self):
        items = [self._item("a", 1, viewcount="2"),
                 self._item("b", 2, viewcount="50")]
        all_kept = smartplaylists._filter_beets_items(items, {}, None, max_plays=None, now_ts=self.NOW)
        assert len(all_kept) == 2
        res = smartplaylists._filter_beets_items(items, {}, None, max_plays=10, now_ts=self.NOW)
        assert self._keys(res) == {1}

    def test_preferred_genres_fallback(self):
        item = self._item("t", 1, genres=["Sufi"])
        # No include.genres -> preferred applies (kept).
        assert len(smartplaylists._filter_beets_items([item], {}, None, preferred_genres=["Sufi"], now_ts=self.NOW)) == 1
        # include.genres present -> preferred ignored (no "Punjabi" -> dropped).
        assert len(smartplaylists._filter_beets_items([item], {"include": {"genres": ["Punjabi"]}}, None, preferred_genres=["Sufi"], now_ts=self.NOW)) == 0

    def test_explicit_empty_genres_disables_preferred_genres(self):
        items = [
            self._item("sufi", 1, genres=["Sufi"]),
            self._item("rock", 2, genres=["Rock"]),
        ]
        filters = {"include": {"genres": []}}
        res = smartplaylists._filter_beets_items(
            items, filters, None, preferred_genres=["Sufi"], now_ts=self.NOW,
        )
        assert self._keys(res) == {1, 2}

    def test_invalid_synced_values_do_not_crash_or_wrongly_exclude(self):
        item = self._item(
            "invalid", 1, genres="Punjabi", rating="nan", viewcount="bad",
        )
        item.plex_lastviewedat = "nan"
        res = smartplaylists._filter_beets_items(
            [item], {"min_rating": "bad"}, 90,
            max_plays="bad", now_ts=self.NOW,
        )
        assert res == [item]

    def test_lookup_candidates_require_valid_keys(self):
        synced = self._item("synced", 1)
        null_key = self._item("null", None)
        mismatched = self._item("mismatched", None)
        lookup = {None: null_key, 1: synced, 2: mismatched}
        assert smartplaylists._beets_candidates_from_lookup(lookup) == [synced]

    def test_legacy_advanced_filter_builder_remains_available(self):
        result = smartplaylists.build_advanced_filters(
            {"include": {"genres": ["Rock"]}}, 30,
        )
        assert {"or": [{"genre": "rock"}]} in result["and"]

    def test_shared_lookup_builder_skips_unsynced_items(self):
        from beetsplug.plexsync import PlexSync

        synced = self._item("synced", 7)
        unsynced = self._item("unsynced", None)
        fake_plugin = types.SimpleNamespace(
            _log=types.SimpleNamespace(debug=lambda *a, **k: None),
            _extract_vector_metadata=lambda item: {"id": None},
        )
        fake_lib = types.SimpleNamespace(items=lambda: [unsynced, synced])
        lookup = PlexSync._build_plex_lookup_and_vector_index(fake_plugin, fake_lib)
        assert lookup == {7: synced}

    def test_forgotten_gems_keeps_unrated_never_played(self):
        # Regression: the exact Forgotten Gems config must keep an unrated,
        # never-played, included-genre track (the original 0-unrated bug).
        f = {
            "include": {"genres": ["Filmi", "Punjabi"]},
            "exclude": {"genres": ["Religious"]},
            "min_rating": 5,
        }
        items = [
            self._item("good_discovery", 1, genres=["Punjabi"], rating=None, played_days_ago=None),
            self._item("recent", 2, genres=["Punjabi"], rating="6", played_days_ago=5),
            self._item("low_rated", 3, genres=["Filmi"], rating="4", played_days_ago=None),
            self._item("excluded_genre", 4, genres=["Religious"], rating=None, played_days_ago=None),
            self._item("wrong_genre", 5, genres=["Rock"], rating=None, played_days_ago=None),
        ]
        res = smartplaylists._filter_beets_items(items, f, 90, now_ts=self.NOW)
        assert self._keys(res) == {1}


class BeetsCentricGenerationTest:
    def test_proportions_always_fill_requested_size(self):
        unrated, rated = smartplaylists.calculate_playlist_proportions(None, 7, 30)
        assert (unrated, rated) == (2, 5)
        assert unrated + rated == 7

    @staticmethod
    def _item(key, rating, popularity):
        return types.SimpleNamespace(
            plex_ratingkey=key,
            title=f"Track {key}",
            artist=f"Artist {key}",
            album="Album",
            genres=["Punjabi"],
            year=2000,
            plex_userrating=rating,
            plex_lastviewedat=None,
            plex_viewcount="0",
            spotify_track_popularity=str(popularity),
        )

    def test_forgotten_gems_generates_95_rated_5_unrated_without_plex_search(
            self, monkeypatch):
        monkeypatch.setattr(smartplaylists, "get_plexsync_config", lambda *a, **k: {})
        added = {}
        log = types.SimpleNamespace(
            debug=lambda *a, **k: None,
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            error=lambda *a, **k: None,
        )
        ps = types.SimpleNamespace(
            _log=log,
            _plex_clear_playlist=lambda name: None,
            _plex_add_playlist_item=lambda tracks, name: added.update(
                tracks=list(tracks), name=name,
            ),
        )
        rated = [self._item(i, "6", 50 + (i % 40)) for i in range(1, 96)]
        unrated = [self._item(i, None, i - 5) for i in range(96, 106)]
        lookup = {item.plex_ratingkey: item for item in rated + unrated}
        playlist = {
            "name": "Forgotten Gems",
            "max_tracks": 100,
            "discovery_ratio": 5,
            "exclusion_days": 90,
            "filters": {
                "include": {"genres": ["Punjabi"]},
                "min_rating": 5,
            },
        }

        # ps intentionally has no .music attribute: any old Plex candidate
        # search call would make this test fail immediately.
        smartplaylists.generate_unified_playlist(
            ps, None, playlist, lookup, ["Rock"], [], "forgotten_gems",
        )

        assert added["name"] == "Forgotten Gems"
        assert len(added["tracks"]) == 100
        assert sum(smartplaylists._rating_of(t) > 0 for t in added["tracks"]) == 95
        assert sum(smartplaylists._rating_of(t) == 0 for t in added["tracks"]) == 5
