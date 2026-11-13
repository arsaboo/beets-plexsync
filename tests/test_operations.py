import types

import pytest
from plexapi import exceptions

from beetsplug.plex.operations import (
    plex_add_playlist_item,
    plex_clear_playlist,
    plex_remove_playlist_item,
    plex_replace_playlist_items,
)
from tests.test_playlist_import import DummyLogger as Logger


class Item:
    def __init__(self, key, title=None):
        self.ratingKey = str(key)
        self.title = title or f"Track {key}"


class FakePlaylist:
    def __init__(self, items=(), events=None):
        self._items = list(items)
        self.events = events if events is not None else []
        self.fail_add = False
        self.fail_remove = False
        self.fail_move = False

    def items(self):
        return list(self._items)

    def addItems(self, items):
        self.events.append(("add", [item.ratingKey for item in items]))
        if self.fail_add:
            raise RuntimeError("add failed")
        self._items.extend(items)

    def removeItems(self, items):
        self.events.append(("remove", [item.ratingKey for item in items]))
        if self.fail_remove:
            raise RuntimeError("remove failed")
        for item in items:
            for index, current in enumerate(self._items):
                if current.ratingKey == item.ratingKey:
                    del self._items[index]
                    break

    def moveItem(self, item, after=None):
        self.events.append(
            ("move", item.ratingKey, getattr(after, "ratingKey", None))
        )
        if self.fail_move:
            raise RuntimeError("move failed")
        current = next(i for i in self._items if i.ratingKey == item.ratingKey)
        self._items.remove(current)
        if after is None:
            self._items.insert(0, current)
        else:
            after_index = next(
                i for i, value in enumerate(self._items)
                if value.ratingKey == after.ratingKey
            )
            self._items.insert(after_index + 1, current)


class FakePlex:
    def __init__(self, playlist=None, catalog=()):
        self._playlist = playlist
        self.catalog = {str(item.ratingKey): item for item in catalog}

    def playlist(self, name):
        if self._playlist is None:
            raise exceptions.NotFound(name)
        return self._playlist

    def fetchItems(self, key):
        keys = key.rsplit("/", 1)[-1].split(",")
        return [self.catalog[value] for value in keys if value in self.catalog]

    def fetchItem(self, key):
        try:
            return self.catalog[str(key)]
        except KeyError as exc:
            raise exceptions.NotFound(str(key)) from exc

    def createPlaylist(self, name, items):
        self._playlist = FakePlaylist(items)
        return self._playlist


class Track:
    """Server-side playlist entry: unique playlistItemID plus rating key."""

    def __init__(self, playlist_item_id, rating_key):
        self.playlistItemID = playlist_item_id
        self.ratingKey = str(rating_key)
        self.title = f"Track {rating_key}"


class ServerPlaylist:
    """What the Plex server holds, plus an event log.

    ``cascade`` mimics Plex pruning a playlist's other entries for a file when
    that file leaves the library.
    """

    def __init__(self, keys, drop=True, cascade=False):
        self.entries = [Track(i + 1, key) for i, key in enumerate(keys)]
        self.events = []
        self.drop = drop
        self.cascade = cascade

    def remove_by_playlist_item_id(self, playlist_item_id):
        self.events.append(("remove", playlist_item_id))
        if not self.drop:
            return
        target = next(
            (e for e in self.entries if e.playlistItemID == playlist_item_id), None
        )
        if target is None:
            return  # a stale playlistItemID is ignored, which is how copies survived
        self.entries = [
            e
            for e in self.entries
            if e.playlistItemID != playlist_item_id
            and not (self.cascade and e.ratingKey == target.ratingKey)
        ]


class PlaylistView:
    """plexapi.Playlist stand-in.

    ``items()`` is cached on the object and removals resolve a rating key
    against that cache, so a single view cannot delete two copies of the same
    track - the behaviour that made bulk removal of duplicates unreliable.
    """

    def __init__(self, server):
        self.server = server
        self._cache = None

    def items(self):
        if self._cache is None:
            self._cache = list(self.server.entries)
        return list(self._cache)

    def removeItems(self, items):
        for item in items:
            entry = next(
                (e for e in self._cache if e.ratingKey == item.ratingKey), None
            )
            if entry is None:
                raise exceptions.NotFound(item.ratingKey)
            self.server.remove_by_playlist_item_id(entry.playlistItemID)


class DedupPlex:
    """Fresh view per playlist() call, like ``PlexServer.playlist()``."""

    def __init__(self, keys, drop=True, cascade=False):
        self.server = ServerPlaylist(keys, drop=drop, cascade=cascade)

    def playlist(self, name):
        return PlaylistView(self.server)


def test_one_view_cannot_delete_two_copies_of_the_same_track():
    # Documents the plexapi limitation the removal helpers work around.
    plex = DedupPlex([1, 2, 1])
    view = plex.playlist("Mix")

    view.removeItems([entry for entry in view.items() if entry.ratingKey == "1"])

    assert [e.ratingKey for e in plex.server.entries] == ["2", "1"]


def test_remove_deletes_every_copy_of_a_track():
    plex = DedupPlex([1, 2, 1, 3, 1])
    log = Logger()

    plex_remove_playlist_item(plex, [Track(0, 1)], "Mix", log)

    assert [e.ratingKey for e in plex.server.entries] == ["2", "3"]
    # One DELETE per occurrence, each after a refresh that re-resolves the key.
    assert [event[0] for event in plex.server.events] == ["remove"] * 3
    assert not [m for level, m in log.messages if level == "error"]


def test_remove_reports_tracks_that_stayed_behind():
    plex = DedupPlex([1, 1, 2], drop=False)
    log = Logger()

    plex_remove_playlist_item(plex, [Track(0, 1)], "Mix", log)

    assert any(
        level == "error" and "still in" in message for level, message in log.messages
    )


def test_clear_deletes_every_copy():
    plex = DedupPlex([1, 1, 2, 3])

    plex_clear_playlist(plex, "Mix")

    assert plex.server.entries == []


def test_removal_tolerates_entries_plex_pruned_by_itself():
    # Plex drops playlist entries on its own when a file leaves the library, so
    # a copy can vanish between batches. That is the wanted end state, and it
    # must not raise or report a failure.
    plex = DedupPlex([1, 1, 2], cascade=True)
    log = Logger()

    plex_remove_playlist_item(plex, [Track(0, 1)], "Mix", log)

    assert [e.ratingKey for e in plex.server.entries] == ["2"]
    assert [event[0] for event in plex.server.events] == ["remove"]
    assert not [m for level, m in log.messages if level == "error"]


def test_clear_refuses_to_claim_success_when_tracks_remain():
    plex = DedupPlex([1, 2], drop=False)

    with pytest.raises(RuntimeError, match="still holds 2"):
        plex_clear_playlist(plex, "Mix")


def test_remove_without_usable_keys_is_a_noop():
    plex = DedupPlex([1, 2])
    log = Logger()

    plex_remove_playlist_item(plex, [object()], "Mix", log)

    assert plex.server.events == []


def _keys(playlist):
    return [item.ratingKey for item in playlist.items()]


def test_replace_rejects_empty_target_without_touching_playlist():
    old = Item(1)
    playlist = FakePlaylist([old])
    plex = FakePlex(playlist, [old])

    assert not plex_replace_playlist_items(plex, [], "Mix", Logger())
    assert _keys(playlist) == ["1"]
    assert playlist.events == []


def test_replace_add_failure_leaves_existing_membership_intact():
    old, new = Item(1), Item(2)
    playlist = FakePlaylist([old])
    playlist.fail_add = True
    plex = FakePlex(playlist, [old, new])

    with pytest.raises(RuntimeError, match="add failed"):
        plex_replace_playlist_items(plex, [new], "Mix", Logger())

    assert _keys(playlist) == ["1"]
    assert playlist.events == [("add", ["2"])]


def test_replace_remove_failure_leaves_safe_superset():
    old, new = Item(1), Item(2)
    playlist = FakePlaylist([old])
    playlist.fail_remove = True
    plex = FakePlex(playlist, [old, new])

    with pytest.raises(RuntimeError, match="remove failed"):
        plex_replace_playlist_items(plex, [new], "Mix", Logger())

    assert _keys(playlist) == ["1", "2"]
    assert [event[0] for event in playlist.events] == ["add", "remove"]


def test_replace_reorders_only_after_exact_membership_is_verified():
    one, two, three = Item(1), Item(2), Item(3)
    playlist = FakePlaylist([one, three, one])
    plex = FakePlex(playlist, [one, two, three])

    assert plex_replace_playlist_items(plex, [two, one], "Mix", Logger())

    assert _keys(playlist) == ["2", "1"]
    assert [event[0] for event in playlist.events][:2] == ["add", "remove"]


def test_exact_target_is_reported_unchanged():
    one, two = Item(1), Item(2)
    playlist = FakePlaylist([one, two])
    plex = FakePlex(playlist, [one, two])

    assert not plex_replace_playlist_items(plex, [one, two], "Mix", Logger())
    assert playlist.events == []


def test_reorder_failure_does_not_damage_membership():
    one, two = Item(1), Item(2)
    logger = Logger()
    playlist = FakePlaylist([one, two])
    playlist.fail_move = True
    plex = FakePlex(playlist, [one, two])

    assert plex_replace_playlist_items(plex, [two, one], "Mix", logger)
    assert set(_keys(playlist)) == {"1", "2"}
    assert any(level == "error" and "reordering failed" in message
               for level, message in logger.messages)


def test_append_deduplicates_by_rating_key_not_object_hash():
    existing = Item(1, "Old title")
    fetched_same = Item(1, "New title")
    new = Item(2)
    playlist = FakePlaylist([existing])
    plex = FakePlex(playlist, [fetched_same, new])

    assert plex_add_playlist_item(plex, [fetched_same, new], "Mix", Logger())
    assert _keys(playlist) == ["1", "2"]
