"""Data models for Harmony."""

from typing import Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field


class Track(BaseModel):
    """Normalized music track model across all backends."""

    # Core metadata
    title: str
    artist: str
    album: str = ""
    year: Optional[int] = None

    # Identifiers
    backend_id: str = ""  # Backend-specific unique identifier
    plex_ratingkey: Optional[int] = None
    plex_guid: Optional[str] = None
    beets_id: Optional[int] = None

    # Plex sync fields
    plex_userrating: Optional[float] = None
    plex_viewcount: Optional[int] = None
    plex_lastviewedat: Optional[datetime] = None
    plex_lastratedat: Optional[datetime] = None
    plex_skipcount: Optional[int] = None

    # Additional metadata
    genre: Optional[str] = None
    duration: Optional[int] = None  # seconds
    path: Optional[str] = None

    # Internal metadata
    source: str = "plex"  # 'plex', 'beets', etc.
    metadata: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        """Pydantic config."""

        arbitrary_types_allowed = True

    def __str__(self) -> str:
        """String representation of track."""
        return f"{self.artist} - {self.title}"

    def to_search_dict(self) -> Dict[str, str]:
        """Convert track to search dictionary format."""
        return {
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
        }
