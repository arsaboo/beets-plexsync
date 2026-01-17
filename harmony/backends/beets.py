"""Beets Music Service Backend for Harmony (optional)."""

import logging
from typing import Any, Dict, List, Optional

from harmony.models import Track
from harmony.backends.base import MusicBackend

logger = logging.getLogger("harmony")


class BeetsBackend(MusicBackend):
    """Music backend for beets library (optional - graceful fallback if not installed)."""

    def __init__(self, config: Dict[str, Any]):
        """Initialize beets backend.

        Args:
            config: Dict with 'library_db' path to musiclibrary.blb
        """
        super().__init__(config)
        self.provider_name = "beets"
        self.library = None
        self.library_db = config.get("library_db", "")

        # Try importing beets
        try:
            from beets.library import Library

            self._library_class = Library
            self._available = True
        except ImportError:
            self._available = False
            logger.debug("beets not installed, BeetsBackend will not be available")

    def connect(self) -> None:
        """Connect to beets library."""
        if not self._available:
            logger.debug("beets not installed, skipping BeetsBackend connection")
            self.connected = False
            return

        if not self.library_db:
            logger.warning("No beets library_db path configured")
            self.connected = False
            return

        try:
            self.library = self._library_class(self.library_db)
            self.connected = True
            logger.info(f"Connected to beets library at {self.library_db}")
        except Exception as e:
            logger.debug(f"Failed to connect to beets library: {e}")
            self.connected = False

    def disconnect(self) -> None:
        """Disconnect from beets library."""
        if self.library:
            try:
                self.library._close()
            except Exception:
                pass
        self.library = None
        self.connected = False

    def _extract_provider_ids(self, item: Any) -> Dict[str, str]:
        """Extract provider IDs from a beets item."""
        provider_ids: Dict[str, str] = {}

        plex_ratingkey = getattr(item, "plex_ratingkey", None)
        if plex_ratingkey:
            provider_ids["plex"] = str(plex_ratingkey)

        spotify_track_id = getattr(item, "spotify_track_id", None) or getattr(item, "spotify_id", None)
        if spotify_track_id:
            provider_ids["spotify"] = str(spotify_track_id)

        apple_music_id = getattr(item, "apple_music_id", None)
        if apple_music_id:
            provider_ids["apple"] = str(apple_music_id)

        tidal_id = getattr(item, "tidal_id", None)
        if tidal_id:
            provider_ids["tidal"] = str(tidal_id)

        youtube_id = getattr(item, "youtube_id", None)
        if youtube_id:
            provider_ids["youtube"] = str(youtube_id)

        return provider_ids

    def _beets_item_to_track(self, item: Any) -> Track:
        """Convert beets Item to Track model."""
        provider_ids = self._extract_provider_ids(item)
        plex_ratingkey = getattr(item, "plex_ratingkey", None)
        return Track(
            title=getattr(item, "title", ""),
            artist=getattr(item, "artist", ""),
            album=getattr(item, "album", ""),
            year=getattr(item, "year", None),
            backend_id=str(getattr(item, "id", "")),
            plex_ratingkey=plex_ratingkey,
            beets_id=getattr(item, "id", None),
            genre=getattr(item, "genre", None),
            path=getattr(item, "path", None),
            source="beets",
            metadata={
                "beets_obj": item,
                "provider_ids": provider_ids,
            },
        )

    def search_tracks(
        self,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        limit: int = 50,
    ) -> List[Track]:
        """Search for tracks in beets library.

        Args:
            title: Track title
            artist: Artist name
            album: Album name
            limit: Maximum number of results

        Returns:
            List of Track objects
        """
        if not self.connected or not self.library:
            return []

        try:
            # Build query string
            query_parts = []
            if title:
                query_parts.append(f"title:{title}")
            if artist:
                query_parts.append(f"artist:{artist}")
            if album:
                query_parts.append(f"album:{album}")

            if not query_parts:
                return []

            query_str = " ".join(query_parts)
            items = self.library.items(query_str)

            # Convert to list and limit results
            tracks = [self._beets_item_to_track(item) for item in items][:limit]
            logger.debug(f"beets search for '{query_str}' returned {len(tracks)} tracks")
            return tracks

        except Exception as e:
            logger.debug(f"beets track search failed: {e}")
            return []

    def get_all_tracks(self) -> List[Track]:
        """Get all tracks from beets library."""
        if not self.connected or not self.library:
            return []

        try:
            items = self.library.items()
            tracks = [self._beets_item_to_track(item) for item in items]
            logger.debug(f"Retrieved {len(tracks)} tracks from beets library")
            return tracks
        except Exception as e:
            logger.debug(f"Failed to get all tracks from beets: {e}")
            return []

    def get_track(self, track_id: str) -> Optional[Track]:
        """Get a track by ID."""
        if not self.connected or not self.library:
            return None

        try:
            item = self.library.get_item(int(track_id))
            if item:
                return self._beets_item_to_track(item)
            return None
        except Exception as e:
            logger.debug(f"beets track {track_id} not found: {e}")
            return None

    def get_track_metadata(self, track_id: str) -> Optional[Dict[str, Any]]:
        """Get full metadata for a track."""
        track = self.get_track(track_id)
        if track:
            return track.model_dump()
        return None

    def get_item_by_metadata(
        self, title: str, artist: str, album: str
    ) -> Optional[Any]:
        """Get beets Item by metadata for enrichment."""
        if not self.connected or not self.library:
            return None

        try:
            query_parts = []
            if title:
                query_parts.append(f"title:{title}")
            if artist:
                query_parts.append(f"artist:{artist}")
            if album:
                query_parts.append(f"album:{album}")

            if not query_parts:
                return None

            query_str = " ".join(query_parts)
            items = list(self.library.items(query_str))
            if items:
                return items[0]
            return None
        except Exception as e:
            logger.debug(f"beets metadata lookup failed: {e}")
            return None
