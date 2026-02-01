"""Background queues for plexsync playlist processing."""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from beetsplug.ai.llm import search_track_info


@dataclass
class LLMEnhancementItem:
    cache_key: str
    search_query: str
    song: Dict[str, str]
    playlist_id: Optional[str] = None


class LLMEnhancementQueue:
    """Background LLM metadata enhancement queue."""

    def __init__(self, cache, worker_count: int = 2, log=None):
        self._cache = cache
        self._log = log or logging.getLogger("beets.plexsync")
        self._queue: queue.Queue[LLMEnhancementItem] = queue.Queue()
        self._pending: Dict[str, int] = {}
        self._in_flight: Set[str] = set()
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._shutdown = threading.Event()
        self._workers: List[threading.Thread] = []

        for index in range(max(1, worker_count)):
            worker = threading.Thread(
                target=self._run,
                name=f"plexsync-llm-worker-{index}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)

    def enqueue(self, item: LLMEnhancementItem) -> bool:
        """Enqueue an item for LLM enhancement.

        Returns True if the item was actually queued, False if it was
        already in-flight or invalid.
        """
        if not item.cache_key or not item.search_query:
            return False
        playlist_id = item.playlist_id or "__global__"
        with self._lock:
            if item.cache_key in self._in_flight:
                return False
            self._in_flight.add(item.cache_key)
            self._pending[playlist_id] = self._pending.get(playlist_id, 0) + 1
        self._queue.put(item)
        return True

    def wait_for_playlist(self, playlist_id: str, timeout: float = 60.0) -> None:
        """Wait for all pending items for a playlist to complete."""
        if not playlist_id:
            return
        with self._condition:
            def _done():
                return self._pending.get(playlist_id, 0) <= 0

            if not self._condition.wait_for(_done, timeout=timeout):
                self._log.debug(
                    "Timed out waiting for LLM enhancements to finish for {} (pending={})",
                    playlist_id,
                    self._pending.get(playlist_id, 0),
                )

    def shutdown(self, timeout: float = 2.0) -> None:
        self._shutdown.set()
        for _ in self._workers:
            self._queue.put(None)  # type: ignore[arg-type]
        for worker in self._workers:
            worker.join(timeout=timeout)

    def _run(self) -> None:
        while not self._shutdown.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                self._queue.task_done()
                break
            try:
                cleaned_metadata = search_track_info(item.search_query)
                if cleaned_metadata:
                    self._cache.set(item.cache_key, None, cleaned_metadata)
            except Exception as exc:  # noqa: BLE001 - background failures are non-fatal
                self._log.debug(
                    "LLM enhancement failed for {}: {}",
                    item.search_query,
                    exc,
                )
            finally:
                playlist_id = item.playlist_id or "__global__"
                with self._condition:
                    self._in_flight.discard(item.cache_key)
                    remaining = self._pending.get(playlist_id, 0) - 1
                    if remaining <= 0:
                        self._pending.pop(playlist_id, None)
                    else:
                        self._pending[playlist_id] = remaining
                    self._condition.notify_all()
                self._queue.task_done()


@dataclass
class ManualPromptItem:
    song: Dict[str, str]
    cache_key: str
    candidates: List[Dict[str, object]] = field(default_factory=list)
    search_strategies_tried: List[str] = field(default_factory=list)
    playlist_id: Optional[str] = None


class ManualPromptQueue:
    """Queue manual prompts per playlist."""

    def __init__(self, limit: int = 15, log=None):
        self._limit = max(1, limit)
        self._queues: Dict[str, List[ManualPromptItem]] = {}
        self._seen: Dict[str, set[str]] = {}
        self._log = log or logging.getLogger("beets.plexsync")

    def enqueue(self, item: ManualPromptItem) -> bool:
        if not item.playlist_id:
            return False
        seen_keys = self._seen.setdefault(item.playlist_id, set())
        if item.cache_key in seen_keys:
            return False
        queue_items = self._queues.setdefault(item.playlist_id, [])
        queue_items.append(item)
        seen_keys.add(item.cache_key)
        return len(queue_items) >= self._limit

    def drain(self, playlist_id: str) -> List[ManualPromptItem]:
        if not playlist_id:
            return []
        self._seen.pop(playlist_id, None)
        items = self._queues.pop(playlist_id, [])
        return items


