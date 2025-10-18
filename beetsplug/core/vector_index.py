from __future__ import annotations

"""Utilities for building and querying a lightweight cosine-similarity index
over beets library metadata."""

import math
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Counter as CounterType
from typing import Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Tuple

from beetsplug.core.matching import clean_string

TOKEN_WEIGHTS = {"title": 3, "artist": 2, "album": 1}
MIN_SCORE_DEFAULT = 0.35
CHAR_NGRAM_SIZE = 3


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


class BeetsVectorIndex:
    """In-memory cosine-similarity index over beets metadata."""

    def __init__(self) -> None:
        self._entries: Dict[int, VectorEntry] = {}
        self._token_index: MutableMapping[str, set[int]] = defaultdict(set)

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

        entry = VectorEntry(
            item_id=item_id,
            counts=counts,
            norm=norm,
            metadata=metadata,
        )
        self._entries[item_id] = entry

        for token in counts:
            self._token_index[token].add(item_id)
        return True

    def remove_item(self, item_id: int) -> bool:
        """Remove an item from the index if present."""
        entry = self._entries.pop(item_id, None)
        if entry is None:
            return False

        for token in entry.counts:
            bucket = self._token_index.get(token)
            if not bucket:
                continue
            bucket.discard(item_id)
            if not bucket:
                self._token_index.pop(token, None)
        return True

    def upsert_item(self, item_id: int, metadata: Mapping[str, str]) -> bool:
        """Add or replace an item in the index."""
        self.remove_item(item_id)
        return self.add_item(item_id, metadata)

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
        if not query_counts or query_norm == 0.0:
            return []

        candidate_ids: set[int] = set()
        for token in query_counts:
            candidate_ids.update(self._token_index.get(token, ()))

        scored: List[Tuple[VectorEntry, float]] = []
        for item_id in candidate_ids:
            entry = self._entries.get(item_id)
            if entry is None or entry.norm == 0.0:
                continue

            dot = 0.0
            for token, weight in query_counts.items():
                if not weight:
                    continue
                dot += weight * entry.counts.get(token, 0.0)

            if dot <= 0.0:
                continue

            score = dot / (query_norm * entry.norm)
            if score < min_score:
                continue
            scored.append((entry, score))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]
