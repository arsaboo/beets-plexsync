"""Utilities for building and querying a lightweight cosine-similarity index over music metadata."""

from __future__ import annotations

import json
import logging
import math
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Counter as CounterType, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Tuple

from harmony.core.matching import clean_string

logger = logging.getLogger("harmony")

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
    """Entry in the vector index."""

    item_id: int
    counts: CounterType[str]
    norm: float
    metadata: Mapping[str, str]

    def overlap_tokens(self, other_counts: Mapping[str, float]) -> List[str]:
        return sorted(token for token in other_counts if token in self.counts)


class VectorIndex:
    """In-memory cosine-similarity index over music metadata."""

    def __init__(self) -> None:
        self._entries: Dict[int, VectorEntry] = {}
        self._token_index: MutableMapping[str, set[int]] = defaultdict(set)

    def __len__(self) -> int:
        return len(self._entries)

    def add_item(self, item_id: int, metadata: Mapping[str, str]) -> bool:
        """Add an item to the index.

        Args:
            item_id: Unique identifier for the item
            metadata: Dict with 'title', 'artist', 'album' keys

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
        """Build query vector from metadata."""
        counts = _tokenize_metadata(metadata)
        return counts, _vector_norm(counts)

    def candidate_scores(
        self,
        query_counts: Mapping[str, float],
        query_norm: float,
        limit: int = 25,
        min_score: float = MIN_SCORE_DEFAULT,
    ) -> List[Tuple[VectorEntry, float]]:
        """Find candidate items with cosine similarity scores.

        Args:
            query_counts: Token counts from query vector
            query_norm: Norm of query vector
            limit: Maximum number of results to return
            min_score: Minimum similarity score (0-1)

        Returns:
            List of (VectorEntry, similarity_score) tuples, sorted by score descending
        """
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

    def save_to_file(self, filepath: str | Path) -> bool:
        """Save the vector index to a JSON file for faster loading.

        Args:
            filepath: Path to save the index to

        Returns:
            bool: True if saved successfully, False otherwise
        """
        try:
            filepath = Path(filepath)
            filepath.parent.mkdir(parents=True, exist_ok=True)

            # Convert to JSON-serializable format
            data = {
                "entries": {},
                "token_index": {},
            }

            for item_id, entry in self._entries.items():
                data["entries"][str(item_id)] = {
                    "item_id": entry.item_id,
                    "counts": dict(entry.counts),
                    "norm": entry.norm,
                    "metadata": dict(entry.metadata),
                }

            for token, ids in self._token_index.items():
                data["token_index"][token] = [str(id) for id in ids]

            with open(filepath, "w") as f:
                json.dump(data, f)

            logger.debug(f"Vector index saved to {filepath}")
            return True

        except Exception as exc:
            logger.warning(f"Failed to save vector index: {exc}")
            return False

    def load_from_file(self, filepath: str | Path) -> bool:
        """Load the vector index from a saved JSON file.

        Args:
            filepath: Path to load the index from

        Returns:
            bool: True if loaded successfully, False otherwise
        """
        try:
            filepath = Path(filepath)
            if not filepath.exists():
                return False

            with open(filepath, "r") as f:
                data = json.load(f)

            # Clear current index
            self._entries.clear()
            self._token_index.clear()

            # Restore entries
            for item_id_str, entry_data in data.get("entries", {}).items():
                item_id = int(item_id_str)
                counts = Counter(entry_data["counts"])
                metadata = entry_data["metadata"]

                entry = VectorEntry(
                    item_id=item_id,
                    counts=counts,
                    norm=entry_data["norm"],
                    metadata=metadata,
                )
                self._entries[item_id] = entry

            # Restore token index
            for token, ids in data.get("token_index", {}).items():
                self._token_index[token] = set(int(id) for id in ids)

            logger.debug(f"Vector index loaded from {filepath} ({len(self._entries)} items)")
            return True

        except Exception as exc:
            logger.warning(f"Failed to load vector index: {exc}")
            return False
