import logging
import os
import sqlite3
import tempfile
import types

import pytest

from beets.library import Item

from beetsplug.core.vector_index import BeetsVectorIndex, read_item_metadata
from beetsplug.plexsync import PlexSync


def _extract_meta(item):
    return {
        "id": getattr(item, "id", None),
        "title": getattr(item, "title", "") or "",
        "album": getattr(item, "album", "") or "",
        "artist": getattr(item, "artist", "") or "",
        "plex_ratingkey": getattr(item, "plex_ratingkey", None),
    }


def _build_plugin(index, info):
    return types.SimpleNamespace(
        _vector_index=index,
        _vector_index_info=info,
        register_listener=lambda *args, **kwargs: None,
        _extract_vector_metadata=_extract_meta,
        _plexupdate=lambda *args, **kwargs: None,
    )


def test_vector_index_upsert_updates_tokens():
    index = BeetsVectorIndex()
    index.add_item(
        1,
        {"title": "First Song", "artist": "Artist", "album": "Album"},
    )

    # Query matches original tokens.
    query_counts, query_norm = index.build_query_vector({"title": "First"})
    assert index.candidate_scores(query_counts, query_norm)

    # Upsert with new metadata that shouldn't match the original query.
    index.upsert_item(
        1,
        {"title": "Second Song", "artist": "Artist", "album": "Album"},
    )

    query_counts, query_norm = index.build_query_vector({"title": "First"})
    assert not index.candidate_scores(query_counts, query_norm)

    query_counts, query_norm = index.build_query_vector({"title": "Second"})
    matches = index.candidate_scores(query_counts, query_norm)
    assert matches and matches[0][0].item_id == 1


def test_listen_for_db_change_upserts_into_index():
    index = BeetsVectorIndex()
    index.add_item(5, {"title": "Existing", "artist": "Artist", "album": "Album"})

    with tempfile.NamedTemporaryFile(delete=False) as handle:
        db_path = handle.name
    os.utime(db_path, None)

    info = {"db_path": db_path, "mtime": os.path.getmtime(db_path), "size": len(index)}
    plugin = _build_plugin(index, info)

    model = Item(
        id=10,
        title="New Track",
        album="Fresh Album",
        artist="New Artist",
    )
    lib = types.SimpleNamespace(path=db_path)

    PlexSync.listen_for_db_change(plugin, lib, model)

    assert len(plugin._vector_index) == 2
    assert plugin._vector_index_info["size"] == 2
    assert "mtime" in plugin._vector_index_info

    query_counts, query_norm = index.build_query_vector({"title": "New Track"})
    matches = index.candidate_scores(query_counts, query_norm)
    assert matches and matches[0][0].item_id == 10

    os.unlink(db_path)


def test_listen_for_db_change_defers_when_index_missing():
    plugin = _build_plugin(None, {"db_path": "/tmp/test.db"})
    model = types.SimpleNamespace(id=20)

    PlexSync.listen_for_db_change(plugin, None, model)

    assert plugin._vector_index is None
    assert plugin._vector_index_info == {}


CORPUS = [
    {"id": 1, "title": "Kesariya", "artist": "Arijit Singh", "album": "Brahmastra"},
    {"id": 2, "title": "Channa Mereya", "artist": "Arijit Singh", "album": " Ae Dil Hai Mushkil"},
    {"id": 3, "title": "Tum Hi Ho", "artist": "Arijit Singh", "album": "Aashiqui 2"},
    {"id": 4, "title": "Bahara", "artist": "Shreya Ghoshal", "album": "Vicky Donor"},
    {"id": 5, "title": "Apna Bana Le", "artist": "Arijit Singh", "album": "Bhediya"},
    {"id": 6, "title": "", "artist": "", "album": ""},
]

QUERIES = [
    {"title": "Kesariya", "artist": "Arijit Singh", "album": ""},
    {"title": "kesariya", "artist": "", "album": ""},
    {"title": "Kesaria", "artist": "", "album": ""},
    {"title": "Arijit", "artist": "", "album": ""},
    {"title": "", "artist": "", "album": "Aashiqui"},
    {"title": "nothing at all here", "artist": "nobody", "album": ""},
]


def _brute_force(index, query_counts, query_norm, min_score, limit):
    """Reference scoring: the definition the index is meant to implement."""
    scored = []
    for entry in index.iter_entries():
        dot = sum(weight * entry.counts.get(token, 0.0) for token, weight in query_counts.items())
        if dot <= 0.0:
            continue
        score = dot / (query_norm * entry.norm)
        if score < min_score:
            continue
        scored.append((entry.item_id, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


@pytest.mark.parametrize("min_score", [0.0, 0.2, 0.35, 0.9])
@pytest.mark.parametrize("limit", [1, 3, 10])
def test_candidate_scores_match_reference_scoring(min_score, limit):
    index = BeetsVectorIndex()
    for meta in CORPUS:
        assert index.add_item(meta["id"], meta) is (meta["id"] != 6)

    for query in QUERIES:
        counts, norm = index.build_query_vector(query)
        got = [(entry.item_id, score) for entry, score in index.candidate_scores(
            counts, norm, limit=limit, min_score=min_score
        )]
        want = _brute_force(index, counts, norm, min_score, limit)
        assert [item_id for item_id, _ in got] == [item_id for item_id, _ in want]
        for (__, got_score), (_, want_score) in zip(got, want):
            assert got_score == pytest.approx(want_score, abs=1e-9)


def test_removed_item_no_longer_matches():
    index = BeetsVectorIndex()
    for meta in CORPUS[:5]:
        index.add_item(meta["id"], meta)

    counts, norm = index.build_query_vector({"title": "Kesariya"})
    assert index.candidate_scores(counts, norm, min_score=0.0)

    assert index.remove_item(1) is True
    assert index.remove_item(1) is False
    ids = [entry.item_id for entry, _ in index.candidate_scores(counts, norm, min_score=0.0)]
    assert 1 not in ids
    assert len(index) == 4


def test_upsert_replaces_previous_weights():
    index = BeetsVectorIndex()
    index.add_item(1, {"title": "Kesariya", "artist": "Arijit Singh", "album": "Brahmastra"})

    counts, norm = index.build_query_vector({"title": "Kesariya"})
    before = index.candidate_scores(counts, norm, min_score=0.0)
    assert before and before[0][1] > 0.7

    index.upsert_item(1, {"title": "Apna Bana Le", "artist": "Arijit Singh", "album": "Bhediya"})

    # Only stray n-gram overlap remains, well below the acceptance floor.
    after = index.candidate_scores(counts, norm)
    assert not after
    assert index.candidate_scores(counts, norm, min_score=0.0)[0][1] < 0.2

    counts, norm = index.build_query_vector({"title": "Apna Bana Le"})
    matches = index.candidate_scores(counts, norm)
    assert matches and matches[0][0].item_id == 1 and matches[0][1] > 0.7


def test_arrays_grow_past_initial_capacity(monkeypatch):
    monkeypatch.setattr("beetsplug.core.vector_index._MIN_CAPACITY", 4)
    index = BeetsVectorIndex()
    for i in range(25):
        index.add_item(i, {"title": f"Track number {i}", "artist": "Artist", "album": "Album"})

    assert len(index) == 25
    counts, norm = index.build_query_vector({"title": "Track number 24"})
    matches = index.candidate_scores(counts, norm)
    assert matches and matches[0][0].item_id == 24


def _make_db(path, with_tables=True):
    conn = sqlite3.connect(path)
    if with_tables:
        conn.executescript(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, title TEXT, artist TEXT, album TEXT);"
            "CREATE TABLE item_attributes (id INTEGER PRIMARY KEY, entity_id INTEGER, key TEXT, value TEXT);"
        )
        conn.executemany(
            "INSERT INTO items (id, title, artist, album) VALUES (?, ?, ?, ?)",
            [(1, "Kesariya", "Arijit Singh", "Brahmastra"), (2, None, None, None)],
        )
        conn.executemany(
            "INSERT INTO item_attributes (entity_id, key, value) VALUES (?, ?, ?)",
            [(1, "plex_ratingkey", "54321"), (1, "ignored", "x"), (2, "plex_ratingkey", "0")],
        )
        conn.commit()
    conn.close()


def test_read_item_metadata_includes_cached_ratingkey(tmp_path):
    db = str(tmp_path / "lib.db")
    _make_db(db)

    rows = read_item_metadata(db)

    assert rows == [
        {"id": 1, "title": "Kesariya", "artist": "Arijit Singh", "album": "Brahmastra", "plex_ratingkey": "54321"},
        {"id": 2, "title": "", "artist": "", "album": ""},
    ]


def test_read_item_metadata_missing_file_returns_none(tmp_path):
    assert read_item_metadata(str(tmp_path / "nope.db")) is None


def test_read_item_metadata_unexpected_schema_returns_none(tmp_path):
    db = str(tmp_path / "other.db")
    _make_db(db, with_tables=False)
    assert read_item_metadata(db) is None


def test_ensure_vector_index_falls_back_to_orm():
    """A library path without the expected tables must not break indexing."""
    plugin = _build_plugin(None, {})
    plugin._log = logging.getLogger("beets")
    plugin._update_vector_index = lambda index, db_path=None: PlexSync._update_vector_index(
        plugin, index, db_path=db_path
    )

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        db_path = handle.name
    _make_db(db_path, with_tables=False)

    try:
        lib = types.SimpleNamespace(path=db_path, items=lambda: iter(()))
        index = PlexSync._ensure_vector_index(plugin, lib)
        assert index is not None
        assert len(index) == 0
        assert plugin._vector_index_info["db_path"] == db_path
    finally:
        os.unlink(db_path)
