from __future__ import annotations

"""Utilities for building and querying a lightweight cosine-similarity index
over beets library metadata."""

import math
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Counter as CounterType, Dict, Iterator, List, Mapping, Optional, Tuple

import numpy as np

from beetsplug.core.matching import clean_string

TOKEN_WEIGHTS = {"title": 3, "artist": 2, "album": 1}
MIN_SCORE_DEFAULT = 0.35
CHAR_NGRAM_SIZE = 3

_MIN_CAPACITY = 4096


def _normalize_token_text(value: str) -> str:
    """Normalize value for tokenization."""
    if not value:
        return ""
    cleaned = clean_string(value)
    if not cleaned:
        return ""
    normalized = unicodedata.normalize("NFKD", cleaned)
    ascii_only = "".join(
        ch for ch in normalized if not unicodedata.combining(ch)
    )
    return ascii_only


def _char_ngrams(text: str, size: int = CHAR_NGRAM_SIZE) -> Iterator[str]:
    text = text.replace(" ", "")
    if len(text) < size or size <= 0:
        return iter(())
    return (text[i : i + size] for i in range(len(text) - size + 1))


def _tokenize_metadata(metadata: Mapping[str, str]) -> CounterType[str]:
    counts: CounterType[str] = Counter()
    for field, weight in TOKEN_WEIGHTS.items():
        raw_value = metadata.get(field) or ""
        normalized = _normalize_token_text(raw_value)
        if not normalized:
            continue

        for token in normalized.split():
            if token:
                counts[token] += weight

        # Add lightweight char n-grams to tolerate minor misspellings.
        ngram_weight = max(1, weight - 1)
        for ngram in _char_ngrams(normalized):
            counts[f"ng:{ngram}"] += ngram_weight
    return counts


def _vector_norm(counts: Mapping[str, float]) -> float:
    return math.sqrt(sum(value * value for value in counts.values()))


@dataclass(frozen=True)
class VectorEntry:
    item_id: int
    counts: CounterType[str]
    norm: float
    metadata: Mapping[str, str]

    def overlap_tokens(self, other_counts: Mapping[str, float]) -> List[str]:
        return sorted(token for token in other_counts if token in self.counts)


def read_item_metadata(
    db_path: str, rating_key_field: str = "plex_ratingkey"
) -> Optional[List[Dict[str, Any]]]:
    """Read id/title/artist/album plus cached Plex rating key for every item.

    Returns None if a raw read is not possible; the caller then uses the ORM.
    """
    if not db_path:
        return None
    try:
        uri = Path(db_path).expanduser().resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except (sqlite3.Error, OSError, ValueError):
        return None

    sql = (
        "SELECT i.id, i.title, i.artist, i.album, a.value "
        "FROM items i "
        "LEFT JOIN item_attributes a ON a.entity_id = i.id AND a.key = ?"
    )
    try:
        rows = conn.execute(sql, (rating_key_field,)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    result: List[Dict[str, Any]] = []
    for item_id, title, artist, album, rating_key in rows:
        if item_id is None:
            continue
        metadata: Dict[str, Any] = {
            "id": item_id,
            "title": title or "",
            "artist": artist or "",
            "album": album or "",
        }
        # Flex values are strings; zero means unset.
        if rating_key and rating_key not in ("0", "0.0"):
            metadata[rating_key_field] = rating_key
        result.append(metadata)
    return result


class BeetsVectorIndex:
    """In-memory cosine-similarity index over beets metadata."""

    def __init__(self) -> None:
        self._entries: Dict[int, VectorEntry] = {}
        # Row -> item id; -1 marks a retired row.
        self._row_item: List[int] = []
        self._row_of: Dict[int, int] = {}

        self._vocab: Dict[str, int] = {}
        self._capacity = _MIN_CAPACITY
        self._size = 0
        self._rows = np.zeros(self._capacity, dtype=np.int32)
        self._cols = np.zeros(self._capacity, dtype=np.int32)
        self._vals = np.zeros(self._capacity, dtype=np.float64)

        self._norms: Optional[np.ndarray] = None
        self._row_item_np: Optional[np.ndarray] = None
        self._has_retired_rows = False

    def __len__(self) -> int:
        return len(self._entries)

    def add_item(self, item_id: int, metadata: Mapping[str, str]) -> bool:
        """Add a beets item to the index.

        Returns:
            bool: True if the item was indexed, False if skipped.
        """
        counts = _tokenize_metadata(metadata)
        if not counts:
            return False

        norm = _vector_norm(counts)
        if norm == 0.0:
            return False

        self._retire_row(item_id)

        self._entries[item_id] = VectorEntry(
            item_id=item_id,
            counts=counts,
            norm=norm,
            metadata=metadata,
        )

        row = len(self._row_item)
        self._row_item.append(item_id)
        self._row_of[item_id] = row
        self._append_row(row, counts)
        return True

    def remove_item(self, item_id: int) -> bool:
        """Remove an item from the index if present."""
        if self._entries.pop(item_id, None) is None:
            return False
        self._retire_row(item_id, keep_entry=False)
        return True

    def upsert_item(self, item_id: int, metadata: Mapping[str, str]) -> bool:
        """Add or replace an item in the index."""
        self.remove_item(item_id)
        return self.add_item(item_id, metadata)

    def _retire_row(self, item_id: int, keep_entry: bool = True) -> None:
        row = self._row_of.pop(item_id, None)
        if row is None:
            return
        self._row_item[row] = -1
        self._has_retired_rows = True
        self._row_item_np = None
        if not keep_entry:
            self._norms = None

    def _append_row(self, row: int, counts: CounterType[str]) -> None:
        needed = self._size + len(counts)
        if needed > self._capacity:
            capacity = max(_MIN_CAPACITY, self._capacity * 2, needed)
            rows = np.zeros(capacity, dtype=np.int32)
            cols = np.zeros(capacity, dtype=np.int32)
            vals = np.zeros(capacity, dtype=np.float64)
            rows[: self._size] = self._rows[: self._size]
            cols[: self._size] = self._cols[: self._size]
            vals[: self._size] = self._vals[: self._size]
            self._rows, self._cols, self._vals = rows, cols, vals
            self._capacity = capacity

        vocab = self._vocab
        start = self._size
        for token, weight in counts.items():
            col = vocab.get(token)
            if col is None:
                col = len(vocab)
                vocab[token] = col
            self._rows[start] = row
            self._cols[start] = col
            self._vals[start] = weight
            start += 1
        self._size = start
        self._norms = None

    def iter_entries(self) -> Iterator[VectorEntry]:
        return iter(self._entries.values())

    def build_query_vector(
        self, metadata: Mapping[str, str]
    ) -> Tuple[CounterType[str], float]:
        counts = _tokenize_metadata(metadata)
        return counts, _vector_norm(counts)

    def candidate_scores(
        self,
        query_counts: Mapping[str, float],
        query_norm: float,
        limit: int = 25,
        min_score: float = MIN_SCORE_DEFAULT,
    ) -> List[Tuple[VectorEntry, float]]:
        if not query_counts or query_norm == 0.0 or not self._entries:
            return []
        if self._size == 0:
            return []

        row_count = len(self._row_item)
        rows = self._rows[: self._size]
        cols = self._cols[: self._size]
        vals = self._vals[: self._size]

        query_cols: List[int] = []
        query_weights: List[float] = []
        vocab = self._vocab
        for token, weight in query_counts.items():
            col = vocab.get(token)
            if col is None or not weight:
                continue
            query_cols.append(col)
            query_weights.append(float(weight))
        if not query_cols:
            return []

        query_vector = np.zeros(len(vocab), dtype=np.float64)
        query_vector[np.asarray(query_cols, dtype=np.int64)] = np.asarray(
            query_weights, dtype=np.float64
        )

        scores = np.bincount(
            rows, weights=vals * query_vector[cols], minlength=row_count
        )

        if self._has_retired_rows:
            if self._row_item_np is None:
                self._row_item_np = np.asarray(self._row_item, dtype=np.int64)
            scores[self._row_item_np < 0] = 0.0

        norms = self._norms
        if norms is None:
            norms = np.sqrt(
                np.bincount(rows, weights=vals * vals, minlength=row_count)
            )
            self._norms = norms
        scores /= query_norm * norms + 1e-12
        scores[scores <= 0.0] = 0.0

        mask = scores > 0.0
        if min_score > 0.0:
            mask &= scores >= min_score
        candidates = np.flatnonzero(mask)
        if candidates.size == 0:
            return []

        if limit and candidates.size > limit:
            part = np.argpartition(-scores[candidates], limit)[:limit]
            candidates = candidates[part]
        order = candidates[np.argsort(-scores[candidates], kind="stable")]

        row_item = self._row_item
        return [
            (self._entries[row_item[int(row)]], float(scores[row]))
            for row in order
            if row_item[int(row)] >= 0
        ]
