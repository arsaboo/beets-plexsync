"""AudioMuse backend for acoustic feature enrichment."""

import logging
from typing import Any, Dict, List, Optional

import requests

from harmony.backends.base import MusicBackend
from harmony.core.cache import Cache
from harmony.models import Track

logger = logging.getLogger("harmony.backends.audiomuse")


class AudioMuseBackend(MusicBackend):
    """Backend for AudioMuse acoustic analysis integration."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.provider_name = "audiomuse"
        self.base_url = config.get("base_url", "http://localhost:8001")
        self.enabled = config.get("enabled", False)
        self.timeout = config.get("timeout", 30)
        self.acoustic_enrichment = config.get("acoustic_enrichment", True)
        self.cache_ttl_days = config.get("cache_ttl_days", 7)
        self.cache: Optional[Cache] = None

    def connect(self) -> None:
        """Connect to AudioMuse server."""
        if not self.enabled:
            self.connected = False
            return

        try:
            response = requests.get(self.base_url, timeout=self.timeout)
            if response.status_code == 200:
                self.connected = True
                logger.info(f"Connected to AudioMuse at {self.base_url}")
            else:
                self.connected = False
                logger.warning(
                    f"AudioMuse returned status {response.status_code} from {self.base_url}"
                )
        except Exception as exc:
            self.connected = False
            logger.warning(f"AudioMuse not available: {exc}")

    def disconnect(self) -> None:
        """Disconnect from AudioMuse."""
        self.connected = False

    def _normalize_text(self, text: Optional[str]) -> str:
        if not text:
            return ""
        if self.cache:
            return self.cache.normalize_text(text)
        return " ".join(text.lower().split())

    def _make_lookup_key(self, title: str, artist: str) -> str:
        normalized_title = self._normalize_text(title)
        normalized_artist = self._normalize_text(artist)
        return f"audiomuse:lookup:{normalized_title}|{normalized_artist}"

    def _get_cached_item_id(self, title: str, artist: str) -> Optional[str]:
        if not self.cache:
            return None
        cache_key = self._make_lookup_key(title, artist)
        cached = self.cache.get(cache_key)
        if not cached:
            return None
        cached_value, cached_metadata = cached
        if cached_value == -1:
            return None
        if cached_metadata and isinstance(cached_metadata, dict):
            cached_item_id = cached_metadata.get("item_id")
            if cached_item_id:
                return str(cached_item_id)
        if cached_value is None:
            return None
        return str(cached_value)

    def _set_cached_item_id(self, title: str, artist: str, item_id: Optional[str]) -> None:
        if not self.cache:
            return
        cache_key = self._make_lookup_key(title, artist)
        metadata = {"item_id": item_id} if item_id else None
        self.cache.set(cache_key, item_id, cleaned_metadata=metadata, ttl_days=self.cache_ttl_days)

    def search_tracks_by_metadata(self, title: str, artist: str) -> Optional[str]:
        """Search AudioMuse for a track and return its item_id."""
        if not title or not artist:
            return None

        cached_item_id = self._get_cached_item_id(title, artist)
        if cached_item_id:
            return cached_item_id

        if not self.connected:
            return None

        try:
            response = requests.get(
                f"{self.base_url}/external/search",
                params={"title": title, "artist": artist},
                timeout=self.timeout,
            )
            response.raise_for_status()
            results = response.json()
            item_id = None
            if isinstance(results, list) and results:
                item_id = results[0].get("item_id")
            if item_id:
                self._set_cached_item_id(title, artist, str(item_id))
                return str(item_id)

            self._set_cached_item_id(title, artist, None)
            return None
        except Exception as exc:
            logger.debug(f"AudioMuse search failed: {exc}")
            return None

    def _get_cached_features(self, item_id: str) -> Optional[Dict[str, Any]]:
        if not self.cache or not item_id:
            return None
        cache_key = f"audiomuse:score:{item_id}"
        cached = self.cache.get(cache_key)
        if not cached:
            return None
        _, cached_metadata = cached
        if cached_metadata and isinstance(cached_metadata, dict):
            return cached_metadata
        return None

    def _set_cached_features(self, item_id: str, features: Dict[str, Any]) -> None:
        if not self.cache or not item_id:
            return
        cache_key = f"audiomuse:score:{item_id}"
        self.cache.set(cache_key, item_id, cleaned_metadata=features, ttl_days=self.cache_ttl_days)

    @staticmethod
    def _parse_feature_string(raw_value: Optional[str]) -> Dict[str, float]:
        parsed: Dict[str, float] = {}
        if not raw_value:
            return parsed
        for item in raw_value.split(","):
            parts = item.split(":", 1)
            if len(parts) != 2:
                continue
            key = parts[0].strip()
            try:
                value = float(parts[1])
            except ValueError:
                continue
            if key:
                parsed[key] = value
        return parsed

    def get_track_features(self, item_id: str) -> Optional[Dict[str, Any]]:
        """Fetch acoustic features from AudioMuse."""
        if not item_id:
            return None

        cached = self._get_cached_features(item_id)
        if cached:
            return cached

        if not self.connected or not self.acoustic_enrichment:
            return None

        try:
            response = requests.get(
                f"{self.base_url}/external/get_score",
                params={"id": item_id},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json() or {}

            features = {
                "item_id": data.get("item_id", item_id),
                "title": data.get("title"),
                "artist": data.get("author"),
                "energy": data.get("energy"),
                "tempo": data.get("tempo"),
                "key": data.get("key"),
                "scale": data.get("scale"),
                "mood_categories": self._parse_feature_string(data.get("mood_vector")),
                "mood_features": self._parse_feature_string(data.get("other_features")),
            }

            self._set_cached_features(item_id, features)
            return features
        except Exception as exc:
            logger.debug(f"Failed to fetch AudioMuse features: {exc}")
            return None

    def search_tracks(
        self,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        limit: int = 50,
    ) -> List[Track]:
        """AudioMuse backend does not provide direct track search."""
        return []

    def get_all_tracks(self) -> List[Track]:
        """AudioMuse backend does not provide track listings."""
        return []

    def get_track(self, track_id: str) -> Optional[Track]:
        """AudioMuse backend does not provide track objects."""
        return None

    def get_track_metadata(self, track_id: str) -> Optional[Dict[str, Any]]:
        """Return cached AudioMuse metadata when available."""
        return self.get_track_features(track_id)
