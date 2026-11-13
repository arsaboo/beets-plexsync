"""`beet plexsyncrecent` must resolve only the tracks Plex reports.

Regression: the command used to build the whole-library lookup *and* a vector
index (about a minute on a 60k-track library) to update a few hundred tracks,
and never needed either.
"""

import types
from contextlib import contextmanager
from datetime import datetime

import pytest

from beetsplug.plex.playlist_lookup import PlaylistSelection
from beetsplug.plexsync import PlexSync


class FakeItem:
    def __init__(self, title):
        self.title = title
        self.stored = 0
        self.written = 0

    def store(self):
        self.stored += 1

    def try_write(self):
        self.written += 1


class FakeLib:
    def __init__(self):
        self.transactions = 0

    def transaction(self):
        self.transactions += 1

        class _Tx:
            def __enter__(self_inner):
                return None

            def __exit__(self_inner, *exc):
                return False

        return _Tx()

    def items(self):
        raise AssertionError("recently played sync must not scan the library")


def _log():
    return types.SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )


def _plugin(tracks, selection, resolved):
    def search(**kwargs):
        resolved.append(("search", kwargs))
        return list(tracks)

    def resolve(lib, plex_items, query_args=None):
        resolved.append(("resolve", list(plex_items)))
        return selection

    return types.SimpleNamespace(
        _log=_log(),
        music=types.SimpleNamespace(search=search),
        _resolve_playlist_items=resolve,
    )


def _track(key, viewed=datetime(2026, 11, 10, 8, 30), rated=None):
    return types.SimpleNamespace(
        ratingKey=key, userRating=5, skipCount=2, viewCount=9,
        lastViewedAt=viewed, lastRatedAt=rated,
    )


@pytest.fixture(autouse=True)
def no_file_io(monkeypatch):
    @contextmanager
    def ctx(directory):
        yield None

    monkeypatch.setattr("beetsplug.plexsync.music_dir_context", ctx)
    monkeypatch.setattr("beetsplug.plexsync.music_dir_of", lambda item: "/music")
    monkeypatch.setattr("beetsplug.plexsync.materialize_paths", lambda items: None)


def test_only_the_reported_tracks_are_resolved_and_updated():
    resolved = []
    lib = FakeLib()
    first, second = FakeItem("One"), FakeItem("Two")
    tracks = [_track(1), _track(2)]
    selection = PlaylistSelection(
        items=[first, second], pairs=[(tracks[0], first), (tracks[1], second)]
    )
    plugin = _plugin(tracks, selection, resolved)

    PlexSync._update_recently_played(plugin, lib, days=7)

    assert resolved == [("search", {"filters": {"track.lastViewedAt>>": "7d"},
                                    "libtype": "track"}),
                       ("resolve", tracks)]
    assert lib.transactions == 1
    for item in (first, second):
        assert item.stored == 1
        assert item.written == 1
        assert item.plex_userrating == 5
        assert item.plex_viewcount == 9
        assert item.plex_lastviewedat == tracks[0].lastViewedAt.timestamp()
        assert item.plex_lastratedat is None


def test_unmapped_tracks_are_skipped_without_failing_the_run():
    resolved = []
    lib = FakeLib()
    mapped = FakeItem("One")
    tracks = [_track(1), _track(2), _track(3)]
    selection = PlaylistSelection(
        items=[mapped], pairs=[(tracks[0], mapped)], missing=[tracks[1], tracks[2]]
    )

    PlexSync._update_recently_played(_plugin(tracks, selection, resolved), lib, days=7)

    assert mapped.stored == 1


def test_no_recent_tracks_touch_the_library_at_all():
    resolved = []
    lib = FakeLib()

    PlexSync._update_recently_played(_plugin([], PlaylistSelection(items=[]), resolved),
                                     lib, days=30)

    assert [kind for kind, _payload in resolved] == ["search"]
    assert lib.transactions == 0
