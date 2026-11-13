"""Playlist-scoped beets resolution against a real (small) library."""

import sqlite3

import pytest
from beets.dbcore import types
from beets.library import Item, Library

from beetsplug.plex.playlist_lookup import (
    IdListQuery,
    _rating_key_to_item_id,
    resolve_playlist_items,
)
from beetsplug.utils.helpers import plex_key


class SilentLog:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class PlexTrack:
    def __init__(self, rating_key):
        self.ratingKey = rating_key
        self.title = f"track {rating_key}"
        self.parentTitle = "album"


TRACKS = [
    # title, artist, album, plex rating key, cached plex user rating
    ("Alone", "Aarav", "Zebra", 100, "4.0"),
    ("Alone", "Aarav", "Zebra", 101, "0.0"),
    ("Two", "Bela", "Yak", 102, "5.0"),
    ("Three", "Chen", "Xylo", None, "4.0"),
    ("Four", "Diyar", "Waltz", 104, None),
]


@pytest.fixture
def library(tmp_path, monkeypatch):
    db_path = str(tmp_path / "library.blb")
    lib = Library(db_path, str(tmp_path))
    conn = sqlite3.connect(db_path)
    for title, artist, album, rating_key, rating in TRACKS:
        cursor = conn.execute(
            "INSERT INTO items (title, artist, album) VALUES (?, ?, ?)",
            (title, artist, album),
        )
        item_id = cursor.lastrowid
        if rating_key is not None:
            conn.execute(
                "INSERT INTO item_attributes (entity_id, key, value) VALUES (?, ?, ?)",
                (item_id, "plex_ratingkey", str(rating_key)),
            )
        if rating is not None:
            conn.execute(
                "INSERT INTO item_attributes (entity_id, key, value) VALUES (?, ?, ?)",
                (item_id, "plex_userrating", rating),
            )
    conn.commit()
    conn.close()
    # plexsync registers this in production; the query needs the same typing.
    monkeypatch.setitem(Item._types, "plex_userrating", types.FLOAT)
    try:
        yield lib
    finally:
        lib._close()


def ids(selection):
    return [item.id for item in selection.items]


def ground_truth(lib, query_args, key_map):
    """What the whole-library query would select, restricted to mapped ids."""
    matched = {item.id for item in lib.items(query_args)}
    return {v for v in key_map.values() if v in matched}


KEYS = [100, 101, 102, 999, 104]


def test_query_selection_matches_the_library_query(library):
    key_map = _rating_key_to_item_id(
        library.path, KEYS + [None], SilentLog()
    )
    for query_args in (
        ["plex_userrating:2.."],          # slow: flexible field
        ["artist:Aarav"],                # fast: standard column
        ["title:Alone", "plex_userrating:2.."],
        ["^artist:Bela"],               # exclusion
        ["album:Zebra", ",", "album:Yak"],
        'artist:"Aarav" plex_userrating:2..',
    ):
        selection = resolve_playlist_items(
            library, [PlexTrack(k) for k in KEYS], query_args, SilentLog()
        )
        assert set(ids(selection)) == ground_truth(library, query_args, key_map), query_args


def test_items_keep_plex_playlist_order(library):
    order = [102, 100, 101]
    selection = resolve_playlist_items(
        library, [PlexTrack(k) for k in order], None, SilentLog()
    )
    by_key = {102: "Two", 100: "Alone", 101: "Alone"}
    assert [item.title for item in selection.items] == [by_key[k] for k in order]


def test_rating_filter_drops_the_unrated_copy(library):
    selection = resolve_playlist_items(
        library,
        [PlexTrack(100), PlexTrack(101)],
        ["plex_userrating:2.."],
        SilentLog(),
    )
    assert [item.title for item in selection.items] == ["Alone"]
    assert selection.dropped_by_query == 1
    assert selection.missing == []


def test_unmapped_keys_are_reported(library):
    selection = resolve_playlist_items(
        library, [PlexTrack(100), PlexTrack(999)], None, SilentLog()
    )
    assert len(selection.items) == 1
    assert [item.ratingKey for item in selection.missing] == [999]
    assert selection.plex_total == 2


def test_item_without_a_rating_key_is_never_selected(library):
    # Item 4 has no plex_ratingkey, so no playlist entry can point at it.
    selection = resolve_playlist_items(
        library, [PlexTrack(k) for k in KEYS], ["plex_userrating:2.."], SilentLog()
    )
    assert all(item.title != "Three" for item in selection.items)


def test_falls_back_to_a_scan_when_the_database_is_unreadable(library, tmp_path):
    library.path = str(tmp_path / "does-not-exist.blb")
    selection = resolve_playlist_items(
        library, [PlexTrack(100), PlexTrack(999)], ["plex_userrating:2.."], SilentLog()
    )
    assert [item.title for item in selection.items] == ["Alone"]
    assert [item.ratingKey for item in selection.missing] == [999]


def test_id_list_query_restricts_in_sql(library):
    all_ids = [item.id for item in library.items()]
    subset = all_ids[:2]
    chosen = [item.id for item in library.items(IdListQuery(subset))]
    assert sorted(chosen) == sorted(subset)
    assert len(all_ids) > len(subset)


def test_empty_id_list_query_matches_nothing(library):
    assert list(library.items(IdListQuery([]))) == []
    clause, subvals = IdListQuery([]).clause()
    assert clause == "0"
    assert subvals == []


def _add_item(db_path, title, artist, album, rating_key=None, rating=None):
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        "INSERT INTO items (title, artist, album) VALUES (?, ?, ?)",
        (title, artist, album),
    )
    item_id = cursor.lastrowid
    if rating_key is not None:
        conn.execute(
            "INSERT INTO item_attributes (entity_id, key, value) VALUES (?, ?, ?)",
            (item_id, "plex_ratingkey", str(rating_key)),
        )
    if rating is not None:
        conn.execute(
            "INSERT INTO item_attributes (entity_id, key, value) VALUES (?, ?, ?)",
            (item_id, "plex_userrating", rating),
        )
    conn.commit()
    conn.close()
    return item_id


def test_duplicate_rating_key_resolves_like_a_last_write_wins_scan(library):
    # Two library entries claim the same Plex track. The scoped lookup has to
    # agree with a plain lib.items() scan, which walks ids ascending and lets
    # the last write win.
    duplicate_id = _add_item(
        library.path, "Alone", "Aarav", "Zebra", rating_key=100, rating="4.0"
    )

    scanned = {}
    for item in library.items():
        key = plex_key(getattr(item, "plex_ratingkey", None))
        if key:
            scanned[key] = item.id
    assert scanned[100] == duplicate_id

    selection = resolve_playlist_items(library, [PlexTrack(100)], None, SilentLog())
    assert [item.id for item in selection.items] == [duplicate_id]


def test_pairs_keep_the_plex_side_of_each_match(library):
    selection = resolve_playlist_items(
        library, [PlexTrack(100), PlexTrack(999), PlexTrack(102)], None, SilentLog()
    )

    assert [track.ratingKey for track, _item in selection.pairs] == [100, 102]
    assert [item.id for _track, item in selection.pairs] == [
        item.id for item in selection.items
    ]
    assert [item.ratingKey for item in selection.missing] == [999]


def test_pairs_drop_the_items_a_query_rejected(library):
    selection = resolve_playlist_items(
        library,
        [PlexTrack(100), PlexTrack(101)],
        ["plex_userrating:2.."],
        SilentLog(),
    )

    assert [(track.ratingKey, item.title) for track, item in selection.pairs] == [
        (100, "Alone")
    ]
