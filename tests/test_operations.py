import types

import pytest
from plexapi import exceptions

from beetsplug.plex.operations import (
    plex_add_playlist_item,
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
