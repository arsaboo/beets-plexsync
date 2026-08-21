"""Tests for plexsync queues (manual prompt queue limit)."""

import types

from beetsplug.plex.queues import ManualPromptItem, ManualPromptQueue


def _item(playlist_id, cache_key, title=None):
    return ManualPromptItem(
        song={"title": title or cache_key},
        cache_key=cache_key,
        playlist_id=playlist_id,
    )


class ManualPromptQueueLimitTest:
    def test_enqueue_stops_at_limit(self):
        log = types.SimpleNamespace(messages=[])
        log.info = lambda msg, *args: log.messages.append((msg, args))
        q = ManualPromptQueue(limit=2, log=log)

        q.enqueue(_item("Mix", "a"))
        q.enqueue(_item("Mix", "b"))
        q.enqueue(_item("Mix", "c"))
        q.enqueue(_item("Mix", "d"))

        drained = q.drain("Mix")
        assert [i.cache_key for i in drained] == ["a", "b"]
        assert len(log.messages) == 1
        assert log.messages[0][1] == ("Mix", 2)

    def test_limit_is_per_playlist(self):
        q = ManualPromptQueue(limit=1, log=types.SimpleNamespace(info=lambda *a, **k: None))
        q.enqueue(_item("A", "a1"))
        q.enqueue(_item("A", "a2"))
        q.enqueue(_item("B", "b1"))
        q.enqueue(_item("B", "b2"))
        assert [i.cache_key for i in q.drain("A")] == ["a1"]
        assert [i.cache_key for i in q.drain("B")] == ["b1"]

    def test_duplicate_cache_key_does_not_consume_slot(self):
        q = ManualPromptQueue(limit=1, log=types.SimpleNamespace(info=lambda *a, **k: None))
        q.enqueue(_item("Mix", "same"))
        q.enqueue(_item("Mix", "same"))
        q.enqueue(_item("Mix", "other"))
        assert [i.cache_key for i in q.drain("Mix")] == ["same"]

    def test_missing_playlist_or_key_is_ignored(self):
        q = ManualPromptQueue(limit=5)
        q.enqueue(_item("", "k"))
        q.enqueue(ManualPromptItem(song={}, cache_key="", playlist_id="Mix"))
        assert q.drain("Mix") == []
        assert q.drain("") == []
