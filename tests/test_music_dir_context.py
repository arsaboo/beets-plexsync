"""Item paths must stay absolute across the Plex search thread pool.

beets stores ``items.path`` relative to the library directory and expands it
lazily through a ``ContextVar``, caching the result on the item. Pool workers
inherit no context, so a path first read in a worker stays relative and the
tag write that follows looks for the file next to the working directory.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from beets import config, context
from beets.library import Item, Library

from beetsplug import plexsync
from beetsplug.plexsync import PlexSync
from beetsplug.utils.helpers import (
    materialize_paths,
    music_dir_context,
    music_dir_of,
)

REL_PATH = os.path.join(
    "Hindi Music",
    "M",
    "Meethe Mein Kya Hai (2026)",
    "Meethe Mein Kya Hai (2026) - Ichak Dana.opus",
)


@pytest.fixture
def library(tmp_path):
    music = tmp_path / "music"
    (music / os.path.dirname(REL_PATH)).mkdir(parents=True)
    (music / REL_PATH).write_bytes(b"")
    lib = Library(str(tmp_path / "library.blb"), str(music))
    lib.add(
        Item(
            path=os.path.join(str(music), REL_PATH),
            title="Ichak Dana",
            artist="A",
            album="Meethe Mein Kya Hai",
            format="OGG",
        )
    )
    return lib


def _fsb(path):
    return path if isinstance(path, bytes) else os.fsencode(str(path))


class HelperTest:
    def test_paths_resolve_relative_without_a_music_dir(self, library):
        """The hazard the helpers work around: a worker resolves nothing."""
        items = list(library.items())

        with ThreadPoolExecutor(max_workers=1) as pool:
            path = pool.submit(lambda: items[0].path).result()

        assert not os.path.isabs(os.fsdecode(path))

    def test_materialize_paths_pins_absolute_for_other_threads(self, library):
        items = list(library.items())
        with music_dir_context(music_dir_of(items[0])):
            materialize_paths(items)

        with ThreadPoolExecutor(max_workers=1) as pool:
            path = pool.submit(lambda: items[0].path).result()

        assert os.path.isabs(os.fsdecode(path))
        assert os.path.exists(os.fsdecode(path))

    def test_music_dir_of_prefers_the_items_library(self, library):
        item = list(library.items())[0]

        assert music_dir_of(item) == _fsb(library.directory)

    def test_music_dir_of_falls_back_to_bound_context(self, library):
        assert music_dir_of(SimpleNamespace()) == _fsb(library.directory)

    def test_music_dir_of_without_any_directory(self):
        previous = context.get_music_dir()
        context.set_music_dir(b"")
        try:
            assert music_dir_of(SimpleNamespace()) is None
        finally:
            context.set_music_dir(previous)

    def test_music_dir_context_restores_previous(self, library):
        before = context.get_music_dir()
        other = os.path.join(_fsb(library.directory), b"elsewhere")
        with music_dir_context(other):
            assert context.get_music_dir() == other
        assert context.get_music_dir() == before

    def test_music_dir_context_is_a_noop_without_a_directory(self):
        before = context.get_music_dir()
        with music_dir_context(None):
            assert context.get_music_dir() == before


def _search_stub(observed):
    """Stand-in for `_search_plex_item` that records what a worker sees."""

    def search(index, item, force, items_len):
        observed.append(
            {
                "music_dir": context.get_music_dir(),
                "path": item.path,
                "thread": threading_current(),
            }
        )
        return item, None

    return SimpleNamespace(
        _log=SimpleNamespace(
            info=lambda *a, **k: None,
            debug=lambda *a, **k: None,
            error=lambda *a, **k: None,
            warning=lambda *a, **k: None,
        ),
        create_progress_counter=lambda *a, **k: None,
        _PLEX_SEARCH_SKIP=object(),
        _search_plex_item=search,
        _apply_plex_result=lambda item, track: True,
    )


def threading_current():
    import threading

    return threading.current_thread().name


class FetchPlexInfoTest:
    def test_workers_and_writes_get_existing_absolute_paths(self, library, monkeypatch):
        observed, written = [], []
        monkeypatch.setattr(Item, "try_write", lambda self: written.append(self.path) or True)

        PlexSync._fetch_plex_info(_search_stub(observed), list(library.items()), True, True)

        assert len(observed) == 1
        worker_path = observed[0]["path"]
        assert observed[0]["thread"].startswith("ThreadPoolExecutor")
        assert not observed[0]["music_dir"], "workers should not need a bound dir"
        assert os.path.isabs(os.fsdecode(worker_path))
        assert os.path.exists(os.fsdecode(worker_path))
        assert written and all(os.path.exists(os.fsdecode(p)) for p in written)


class AuxLibraryTest:
    """`Library(db_path)` rebinds beets' music dir; the vector index may not leak it."""

    def test_orm_fallback_keeps_the_callers_music_dir(self, library, monkeypatch):
        captured = []
        before = _fsb(library.directory)

        class FakeIndex:
            def add_item(self, item_id, metadata):
                captured.append(metadata["path"])

        monkeypatch.setattr(plexsync, "read_item_metadata", lambda path: None)
        monkeypatch.setattr(plexsync, "BeetsVectorIndex", FakeIndex)
        db_path = str(library.path)
        original_db = config["library"].get()
        original_dir = config["directory"].get()
        config["library"].set(db_path)
        config["directory"].set(os.fsdecode(_fsb(library.directory)))

        plugin = SimpleNamespace(
            _log=SimpleNamespace(debug=lambda *a, **k: None),
            _vector_index=None,
            _vector_index_info=None,
            _extract_vector_metadata=lambda item: {
                "id": item.id,
                "title": item.title,
                "path": item.path,
            },
            _update_vector_index=lambda index, db_path=None: None,
        )

        try:
            PlexSync._ensure_vector_index(plugin, None)
        finally:
            config["library"].set(original_db)
            config["directory"].set(original_dir)

        assert captured, "ORM fallback should have read the item"
        assert context.get_music_dir() == before, "auxiliary library leaked its music dir"
        for path in captured:
            assert os.path.exists(os.fsdecode(path))
