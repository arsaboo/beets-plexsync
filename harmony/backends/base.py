"""Abstract base class for music service backends."""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any

from harmony.models import Track


class MusicBackend(ABC):
    """Abstract base class for music service backends."""

    def __init__(self, config: Dict[str, Any]):
        """Initialize backend with configuration."""
        self.config = config
        self.connected = False
        self.provider_name = ""

    @abstractmethod
    def connect(self) -> None:
        """Connect to the music service."""
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Disconnect from the music service."""
        pass

    @abstractmethod
    def search_tracks(
        self,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        limit: int = 50,
    ) -> List[Track]:
        """Search for tracks matching the given criteria.

        Args:
            title: Track title
            artist: Artist name
            album: Album name
            limit: Maximum number of results

        Returns:
            List of Track objects
        """
        pass

    @abstractmethod
    def get_all_tracks(self) -> List[Track]:
        """Get all tracks from the backend."""
        pass

    @abstractmethod
    def get_track(self, track_id: str) -> Optional[Track]:
        """Get a track by ID."""
        pass

    @abstractmethod
    def get_track_metadata(self, track_id: str) -> Optional[Dict[str, Any]]:
        """Get full metadata for a track."""
        pass

    def add_tracks_to_playlist(self, playlist_name: str, tracks: List[Any]) -> int:
        """Add tracks to a playlist."""
        raise NotImplementedError("Playlist operations not supported by this backend")

    def clear_playlist(self, playlist_name: str) -> None:
        """Clear all items from a playlist."""
        raise NotImplementedError("Playlist operations not supported by this backend")

    def get_playlist_tracks(self, playlist_name: str) -> List[Track]:
        """Get all tracks from a playlist."""
        raise NotImplementedError("Playlist operations not supported by this backend")

    def is_connected(self) -> bool:
        """Check if backend is connected."""
        return self.connected
