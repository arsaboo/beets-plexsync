"""Plex identity matching: resolve the stored rating key, and only treat Plex
statistics as belonging to a beets item when the matched copy is the same file.

A metadata search can return a different copy of the same song (compilation,
re-release). That copy carries its own rating and play history, so applying it
blindly stamps one file's statistics onto another - or erases a verified sync's
history with nothing.
"""

import types

from beetsplug.plexsync import PlexSync
from beetsplug.utils.helpers import (
    media_identity,
    same_media_file,
)

LIB = b"/data/music"
REL = b"Hindi Music/A/Album (2020)/Album (2020) - Song.flac"
ABS = "/data/music/Hindi Music/A/Album (2020)/Album (2020) - Song.flac"
MOVED = "/data/music/Hindi Music/Z/Album (2020)/Album (2020) - Song.flac"
COMPILATION = "/data/music/Comp/Best Of (2021)/Best Of - Song.flac"
SAME_NAME_OTHER_ALBUM = "/data/music/Comp/Best Of (2021)/Best Of (2021) - Song.flac"


class FakeItem:
    """Minimal beets Item stand-in (flex-field `in`, attribute access, path)."""

    def __init__(self, **fields):
        self.__dict__["_db"] = types.SimpleNamespace(directory=LIB)
        self.__dict__.update(fields)

    def __contains__(self, name):
        return name in self.__dict__ and self.__dict__[name] is not None

    def __getitem__(self, name):
        return self.__dict__[name]

    def __str__(self):
        return str(self.__dict__.get("title", "<item>"))


def item_with(path=REL, **extra):
    return FakeItem(title="Song", artist="Artist", album="Album (2020)", path=path, **extra)


def plex_track(key=101, file=ABS, title="Song", artist="Artist",
               album="Album (2020)", **stats):
    fields = {
        "guid": f"plex://track/{key}",
        "ratingKey": key,
        "title": title,
        "artist": artist,
        "parentTitle": album,
        "userRating": 8.0,
        "skipCount": 1,
        "viewCount": 4,
        "lastViewedAt": 1700000000.0,
        "lastRatedAt": 1699999000.0,
    }
    fields.update(stats)
    track = types.SimpleNamespace(**fields)
    if file is not None:
        track.media = [types.SimpleNamespace(parts=[types.SimpleNamespace(file=file)])]
    return track


def _plugin(**overrides):
    base = {"_log": types.SimpleNamespace(
        debug=lambda *a, **k: None, warning=lambda *a, **k: None
    )}
    base.update(overrides)
    return types.SimpleNamespace(**base)


class SameMediaFileTests:
    def test_identical_absolute_paths(self):
        assert same_media_file(ABS, ABS, LIB) is True

    def test_relative_beets_path_matches_absolute_plex_path(self):
        assert same_media_file(REL, ABS, LIB) is True

    def test_different_mount_roots_same_tail(self):
        assert same_media_file("/mnt/music/a/b.flac", "/data/music/a/b.flac", "/mnt/music")

    def test_different_file_is_false(self):
        assert same_media_file(ABS, ABS.replace("Song.flac", "Other.flac"), LIB) is False

    def test_missing_path_is_false(self):
        assert same_media_file(None, ABS, LIB) is False
        assert same_media_file(ABS, None, LIB) is False


class MediaIdentityTests:
    def test_identical_file(self):
        assert media_identity(item_with(), plex_track(), LIB) == "same"

    def test_renamed_location_is_moved(self):
        assert media_identity(item_with(path=MOVED), plex_track(file=MOVED), LIB) == "same"
        moved_in_beets = item_with(path=b"Hindi Music/Z/Album (2020)/Album (2020) - Song.flac")
        assert media_identity(moved_in_beets, plex_track(), LIB) == "moved"

    def test_compilation_copy_is_different(self):
        assert media_identity(item_with(), plex_track(file=COMPILATION), LIB) == "different"

    def test_same_filename_other_album_is_different(self):
        assert media_identity(
            item_with(), plex_track(file=SAME_NAME_OTHER_ALBUM), LIB
        ) == "different"

    def test_missing_paths_are_unknown(self):
        assert media_identity(item_with(), plex_track(file=None), LIB) == "unknown"
        assert media_identity(FakeItem(title="Song"), plex_track(), LIB) == "unknown"


class ResolveByStoredKeyTests:
    def test_stored_key_resolves_without_metadata_search(self):
        plugin = _plugin(music=types.SimpleNamespace(fetchItem=lambda key: plex_track()))
        assert PlexSync._resolve_plex_track_by_key(plugin, item_with(plex_ratingkey="101")) is not None

    def test_moved_file_still_resolves_by_key(self):
        plugin = _plugin(music=types.SimpleNamespace(fetchItem=lambda key: plex_track()))
        moved = item_with(path=b"Hindi Music/Z/Album (2020)/Album (2020) - Song.flac",
                         plex_ratingkey="101")
        assert PlexSync._resolve_plex_track_by_key(plugin, moved) is not None

    def test_key_pointing_at_another_copy_is_rejected(self):
        plugin = _plugin(
            music=types.SimpleNamespace(fetchItem=lambda key: plex_track(file=COMPILATION))
        )
        assert PlexSync._resolve_plex_track_by_key(plugin, item_with(plex_ratingkey="101")) is None

    def test_no_stored_key(self):
        plugin = _plugin(music=types.SimpleNamespace(fetchItem=lambda key: plex_track()))
        assert PlexSync._resolve_plex_track_by_key(plugin, item_with()) is None

    def test_unresolvable_key_returns_none(self):
        def boom(key):
            raise RuntimeError("gone")

        plugin = _plugin(music=types.SimpleNamespace(fetchItem=boom))
        assert PlexSync._resolve_plex_track_by_key(plugin, item_with(plex_ratingkey="101")) is None


class ApplyResultTests:
    def test_verified_match_writes_statistics(self):
        item = item_with()
        assert PlexSync._apply_plex_result(_plugin(), item, plex_track()) is True
        assert item.plex_viewcount == 4
        assert item.plex_lastviewedat == 1700000000.0

    def test_verified_match_treats_absent_history_as_authoritative(self):
        item = item_with(plex_lastviewedat=1600000000.0)
        PlexSync._apply_plex_result(_plugin(), item, plex_track(lastViewedAt=None))
        assert item.plex_lastviewedat is None

    def test_unknown_identity_keeps_existing_history(self):
        item = item_with(plex_lastviewedat=1600000000.0, plex_viewcount=9,
                        plex_ratingkey="101")
        item.__dict__["path"] = None
        assert PlexSync._apply_plex_result(
            _plugin(), item, plex_track(file=None, lastViewedAt=None, viewCount=0)
        ) is True
        assert item.plex_lastviewedat == 1600000000.0
        assert item.plex_viewcount == 9
        assert item.plex_ratingkey == 101

    def test_unknown_identity_records_new_values(self):
        item = item_with()
        item.__dict__["path"] = None
        assert PlexSync._apply_plex_result(
            _plugin(), item, plex_track(file=None, viewCount=2)
        ) is True
        assert item.plex_viewcount == 2

    def test_other_copy_is_rejected_and_item_left_untouched(self):
        item = item_with(plex_ratingkey="7", plex_guid="stored-guid",
                        plex_userrating=6.0, plex_viewcount=3)
        other = plex_track(key=999, file=COMPILATION, userRating=1.0, viewCount=0,
                          lastViewedAt=None)
        assert PlexSync._apply_plex_result(_plugin(), item, other) is False
        assert item.plex_ratingkey == "7"
        assert item.plex_guid == "stored-guid"
        assert item.plex_userrating == 6.0
        assert item.plex_viewcount == 3


SIBLING_01 = "/data/music/Hindi Music/S/OST (2017)/OST (2017) - Jogi [01].mp3"
SIBLING_06 = "/data/music/Hindi Music/S/OST (2017)/OST (2017) - Jogi [06].mp3"
REL_SIBLING_06 = b"Hindi Music/S/OST (2017)/OST (2017) - Jogi [06].mp3"


class ResolveByPathTests:
    """Two tracks sharing a title on one album cannot be told apart by metadata."""

    def _plugin(self, tracks, key_track=None):
        album = types.SimpleNamespace(title="OST (2017)", tracks=lambda: tracks)
        return types.SimpleNamespace(
            _log=types.SimpleNamespace(
                debug=lambda *a, **k: None, warning=lambda *a, **k: None,
                info=lambda *a, **k: None, error=lambda *a, **k: None,
            ),
            music=types.SimpleNamespace(
                searchAlbums=lambda title: [album],
                fetchItem=lambda key: key_track,
            ),
        )

    def test_walks_the_album_to_the_exact_file(self):
        plugin = self._plugin([
            plex_track(key=1, file=SIBLING_01, title="Jogi"),
            plex_track(key=6, file=SIBLING_06, title="Jogi"),
        ])
        item = item_with(path=REL_SIBLING_06, plex_ratingkey="1")
        found = PlexSync._resolve_plex_track_by_path(plugin, item)
        assert found is not None and found.ratingKey == 6

    def test_no_matching_track_returns_none(self):
        plugin = self._plugin([plex_track(key=1, file=SIBLING_01)])
        item = item_with(path=REL_SIBLING_06)
        assert PlexSync._resolve_plex_track_by_path(plugin, item) is None

    def test_missing_path_or_album_returns_none(self):
        plugin = self._plugin([])
        assert PlexSync._resolve_plex_track_by_path(
            plugin, item_with(path=None)) is None
        assert PlexSync._resolve_plex_track_by_path(
            plugin, FakeItem(title="x", path=REL)) is None


    def test_track_title_search_can_locate_the_file_without_the_album(self):
        album_title_miss = types.SimpleNamespace(
            title="Unrelated Album",
            tracks=lambda: [plex_track(key=1, file="/data/music/X/Y (2001) - Z.flac")],
        )
        plugin = types.SimpleNamespace(
            _log=types.SimpleNamespace(debug=lambda *a, **k: None),
            music=types.SimpleNamespace(
                searchTracks=lambda title, limit: [
                    plex_track(key=1, file=SIBLING_01, title="Jogi"),
                    plex_track(key=6, file=SIBLING_06, title="Jogi"),
                ],
                searchAlbums=lambda title: [album_title_miss],
            ),
        )
        item = item_with(path=REL_SIBLING_06)
        found = PlexSync._resolve_plex_track_by_path(plugin, item)
        assert found is not None and found.ratingKey == 6

class SearchItemRepairTests:
    def _sync_plugin(self, search_candidate, album_tracks, key_track=None):
        plugin = types.SimpleNamespace(
            _log=types.SimpleNamespace(
                debug=lambda *a, **k: None, warning=lambda *a, **k: None,
                info=lambda *a, **k: None, error=lambda *a, **k: None,
            ),
            _PLEX_SEARCH_SKIP=PlexSync._PLEX_SEARCH_SKIP,
            search_plex_track=lambda item: search_candidate,
            music=types.SimpleNamespace(
                searchAlbums=lambda title: [
                    types.SimpleNamespace(title="OST (2017)", tracks=lambda: album_tracks)
                ],
                fetchItem=lambda key: key_track,
            ),
            _media_verdict=PlexSync._media_verdict,
        )
        plugin._resolve_plex_track_by_key = lambda item: (
            PlexSync._resolve_plex_track_by_key(plugin, item)
        )
        plugin._resolve_plex_track_by_path = lambda item: (
            PlexSync._resolve_plex_track_by_path(plugin, item)
        )
        plugin._apply_plex_result = lambda item, track: PlexSync._apply_plex_result(
            plugin, item, track
        )
        return plugin

    def test_contradicted_candidate_is_replaced_by_the_path_match(self):
        wrong = plex_track(key=1, file=SIBLING_01, title="Jogi", userRating=9.0)
        right = plex_track(key=6, file=SIBLING_06, title="Jogi", userRating=3.0)
        plugin = self._sync_plugin(wrong, [wrong, right], key_track=wrong)
        item = item_with(path=REL_SIBLING_06, plex_ratingkey="1")

        _, track = PlexSync._search_plex_item(plugin, 1, item, True, 1)
        assert PlexSync._apply_plex_result(plugin, item, track) is True
        assert item.plex_ratingkey == 6
        assert item.plex_userrating == 3.0

    def test_unrepairable_candidate_is_not_applied(self):
        wrong = plex_track(key=1, file=SIBLING_01, title="Jogi", userRating=9.0)
        plugin = self._sync_plugin(wrong, [wrong], key_track=wrong)
        item = item_with(path=REL_SIBLING_06, plex_ratingkey="1", plex_userrating=4.0)

        _, track = PlexSync._search_plex_item(plugin, 1, item, True, 1)
        assert PlexSync._apply_plex_result(plugin, item, track) is False
        assert item.plex_userrating == 4.0
        assert item.plex_ratingkey == "1"
