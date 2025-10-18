import os
import tempfile
import types

from beetsplug.core.vector_index import BeetsVectorIndex
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

    model = types.SimpleNamespace(
        id=10,
        title="New Track",
        album="Fresh Album",
        artist="New Artist",
        plex_ratingkey=None,
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
