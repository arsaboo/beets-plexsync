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
