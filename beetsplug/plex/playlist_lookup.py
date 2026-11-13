"""Resolve the beets items behind a Plex playlist without scanning the library.

beets cannot express flexible-field predicates in SQL: ``plex_userrating:2..``
parses to a slow query, so ``lib.items(query)`` fetches every row of ``items``
plus every flexible attribute, materialises all of them, and only then filters.
A playlist only needs the items its own rating keys map to, so the keys are
resolved with a read-only lookup and the parsed query is applied to that small
set. Matching still uses beets' own query objects, so query semantics are
unchanged; only the number of rows touched changes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from beets.dbcore.query import AndQuery, Query
from beets.library import Item, parse_query_parts, parse_query_string

from beetsplug.utils.helpers import plex_key

# SQLite builds older than 3.32 cap bound parameters at 999.
_SQL_CHUNK = 400


class IdListQuery(Query):
    """Restrict a query to an explicit set of row ids, in SQL."""

    def __init__(self, ids: Sequence[int]) -> None:
        self.ids = [int(i) for i in ids]
        self._id_set = frozenset(self.ids)

    @property
    def field_names(self):
        return {"id"}

    def clause(self):
        if not self.ids:
            return "0", []
        return f"id IN ({','.join(['?'] * len(self.ids))})", self.ids

    def match(self, obj):
        return obj.id in self._id_set


@dataclass
class PlaylistSelection:
    """Beets items for a Plex playlist, in Plex playlist order.

    ``pairs`` holds ``(plex item, beets item)`` in the same order, so callers
    that need the Plex-side attributes of each match (recently played stats, for
    example) do not have to redo the mapping. ``items`` is its second column.
    """

    items: List[Any]
    missing: List[Any] = field(default_factory=list)
    pairs: List[Tuple[Any, Any]] = field(default_factory=list)
    dropped_by_query: int = 0
    plex_total: int = 0


def _chunks(seq: Sequence, size: int) -> Iterator[Sequence]:
    for start in range(0, len(seq), size):
        yield seq[start:start + size]


def _rating_key_to_item_id(db_path, keys: Sequence, log) -> Optional[Dict[Any, int]]:
    """Map Plex rating keys to beets item ids, or None if no raw read is possible."""
    text_keys = sorted({str(key) for key in keys if key is not None})
    if not text_keys:
        return {}
    try:
        uri = Path(db_path).expanduser().resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except (sqlite3.Error, OSError, ValueError) as exc:
        log.debug("Read-only library read failed for {}: {}", db_path, exc)
        return None
    key_map: Dict[Any, int] = {}
    try:
        for chunk in _chunks(text_keys, _SQL_CHUNK):
            # Ordered by id so that a rating key carried by several library
            # entries resolves to the highest one, which is what a plain
            # ``lib.items()`` scan (sorted by id, last write wins) yields.
            rows = conn.execute(
                "SELECT i.id, a.value FROM items i "
                "JOIN item_attributes a ON a.entity_id = i.id AND a.key = ? "
                f"WHERE a.value IN ({','.join(['?'] * len(chunk))}) "
                "ORDER BY i.id",
                ("plex_ratingkey", *chunk),
            ).fetchall()
            for item_id, value in rows:
                key = plex_key(value)
                if key is not None and item_id is not None:
                    key_map[key] = int(item_id)
    except sqlite3.Error as exc:
        log.debug("Plex rating key lookup failed: {}", exc)
        return None
    finally:
        conn.close()
    return key_map


def _scan_rating_keys(lib, log) -> Dict[Any, int]:
    """Fallback for libraries that cannot be read directly."""
    log.debug("Resolving Plex rating keys with a full library scan")
    key_map: Dict[Any, int] = {}
    for item in lib.items():
        key = plex_key(getattr(item, "plex_ratingkey", None))
        if key is not None and item.id is not None:
            key_map[key] = item.id
    return key_map


def _items_by_id(lib, ids: Sequence[int]) -> Dict[int, Any]:
    by_id: Dict[int, Any] = {}
    ordered = sorted(set(ids))
    for chunk in _chunks(ordered, _SQL_CHUNK):
        for item in lib.items(IdListQuery(chunk)):
            by_id[item.id] = item
    return by_id


def _matching_ids(lib, parsed: Query, ids: Sequence[int], log) -> set:
    """Ids satisfying the parsed query, restricted to ``ids``.

    A query that compiles to SQL is ANDed with the id restriction and left to
    SQLite. A slow query (``clause() is None``, typical for flexible ``plex_*``
    fields) is applied with ``Query.match`` to the candidates instead, which is
    the same predicate beets applies after fetching the whole library.
    """
    ids = sorted(set(ids))
    if not ids:
        return set()
    where, _subvals = parsed.clause()
    if where is None:
        return {
            item.id
            for item in _items_by_id(lib, ids).values()
            if parsed.match(item)
        }
    matched = set()
    for chunk in _chunks(ids, _SQL_CHUNK):
        matched.update(
            item.id for item in lib.items(AndQuery([parsed, IdListQuery(chunk)]))
        )
    return matched


def resolve_playlist_items(lib, plex_items: Sequence[Any], query_args, log) -> PlaylistSelection:
    """Return the beets items for ``plex_items``, in Plex playlist order.

    ``query_args`` is a beets query (list of terms or a string) applied to the
    mapped items; terms that sort are ignored because the output order is the
    Plex playlist's.
    """
    keyed = [(plex_key(getattr(item, "ratingKey", None)), item) for item in plex_items]
    keys = [key for key, _item in keyed if key is not None]

    db_path = getattr(lib, "path", None)
    key_map = _rating_key_to_item_id(db_path, keys, log) if db_path else None
    if key_map is None:
        key_map = _scan_rating_keys(lib, log)

    # Aligned with ``keyed``: the beets item id behind each Plex item, or None
    # when that item has no mapping. Keeping the alignment lets the result carry
    # the Plex side of each match without a second mapping pass.
    aligned_ids = [key_map.get(key) if key is not None else None for key, _ in keyed]
    candidate_ids = [item_id for item_id in aligned_ids if item_id is not None]

    if query_args:
        parse = parse_query_string if isinstance(query_args, str) else parse_query_parts
        parsed, _sort = parse(query_args, Item)
        keep = _matching_ids(lib, parsed, candidate_ids, log)
    else:
        keep = set(candidate_ids)

    wanted_ids = [item_id for item_id in candidate_ids if item_id in keep]
    items_by_id = _items_by_id(lib, wanted_ids)

    pairs = []
    missing = []
    for (key, plex_item), item_id in zip(keyed, aligned_ids):
        if item_id is None:
            missing.append(plex_item)
        elif item_id in keep:
            item = items_by_id.get(item_id)
            if item is not None:
                pairs.append((plex_item, item))
    items = [item for _plex_item, item in pairs]

    log.debug(
        "Playlist resolution: {} Plex items, {} mapped to beets, {} dropped by query",
        len(plex_items),
        len(candidate_ids),
        len(candidate_ids) - len(wanted_ids),
    )
    return PlaylistSelection(
        items=items,
        missing=missing,
        pairs=pairs,
        dropped_by_query=len(candidate_ids) - len(wanted_ids),
        plex_total=len(plex_items),
    )
