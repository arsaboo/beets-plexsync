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
