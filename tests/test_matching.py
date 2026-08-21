"""Tests for plex_track_distance scoring."""

import types

from beets.autotag.distance import Distance
from beets.library import Item

from beetsplug.core.matching import plex_track_distance


def _track(title, album, artist):
    return types.SimpleNamespace(
        title=title,
        parentTitle=album,
        originalTitle="",
        artist=lambda: types.SimpleNamespace(title=artist),
    )


class PlexTrackDistanceTest:
    def test_does_not_mutate_beets_distance_weights(self):
        before = dict(Distance._weights)
        item = Item(title="Song", artist="Artist", album="Album")
        plex_track_distance(item, _track("Song", "Album", "Artist"))
        assert dict(Distance._weights) == before
        assert "title" not in Distance._weights

    def test_identical_metadata_is_a_high_score(self):
        item = Item(title="Song", artist="Artist", album="Album")
        score, _ = plex_track_distance(item, _track("Song", "Album", "Artist"))
        assert score >= 0.95

    def test_completely_different_metadata_is_a_low_score(self):
        item = Item(title="Alpha", artist="One", album="First")
        score, _ = plex_track_distance(item, _track("Omega", "Last", "Two"))
        assert score < 0.5
