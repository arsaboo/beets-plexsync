"""Plex Music Service Backend for Harmony."""

import logging
from typing import Any, Dict, List, Optional

from plexapi.server import PlexServer
from plexapi.exceptions import NotFound
from plexapi import exceptions

from harmony.models import Track
from harmony.backends.base import MusicBackend

logger = logging.getLogger("harmony")


class PlexBackend(MusicBackend):
    """Music backend for Plex Media Server."""

    def __init__(self, config: Dict[str, Any]):
        """Initialize Plex backend.

        Args:
            config: Dict with 'host', 'port', 'token', 'library_name', 'verify_ssl'
        """
        super().__init__(config)
        self.provider_name = "plex"
        self.server: Optional[PlexServer] = None
        self.music_library = None
        self.host = config.get("host", "localhost")
        self.port = config.get("port", 32400)
        self.token = config.get("token", "")
        self.library_name = config.get("library_name", "Music")
        self.verify_ssl = config.get("verify_ssl", True)

    @property
    def music(self):
        """Alias for music_library for compatibility with search code."""
        return self.music_library

    def connect(self) -> None:
        """Connect to Plex server."""
        try:
            base_url = f"http://{self.host}:{self.port}"
            # PlexServer doesn't have a verify parameter, SSL verification is handled elsewhere
            self.server = PlexServer(base_url, self.token)
            self.music_library = self.server.library.section(self.library_name)
            self.connected = True
            logger.info(f"Connected to Plex server at {base_url}")
        except Exception as e:
            logger.error(f"Failed to connect to Plex server: {e}")
            self.connected = False
            raise

    def disconnect(self) -> None:
        """Disconnect from Plex server."""
        self.server = None
        self.music_library = None
        self.connected = False
        logger.info("Disconnected from Plex server")

    def _plex_track_to_track(self, plex_track: Any) -> Track:
        """Convert Plex track object to Track model."""
        try:
            artist_name = ""
            if hasattr(plex_track, "originalTitle") and plex_track.originalTitle:
                artist_name = plex_track.originalTitle
            elif hasattr(plex_track, "artist") and callable(plex_track.artist):
                try:
                    artist_name = plex_track.artist().title
                except Exception:
                    pass

            return Track(
                title=getattr(plex_track, "title", ""),
                artist=artist_name,
                album=getattr(plex_track, "parentTitle", ""),
                year=getattr(plex_track, "year", None),
                backend_id=str(getattr(plex_track, "ratingKey", "")),
                plex_ratingkey=getattr(plex_track, "ratingKey", None),
                plex_guid=getattr(plex_track, "guid", None),
                plex_userrating=getattr(plex_track, "userRating", None),
                plex_viewcount=getattr(plex_track, "viewCount", 0),
                plex_lastviewedat=getattr(plex_track, "lastViewedAt", None),
                duration=getattr(plex_track, "duration", None),
                source="plex",
                metadata={
                    "plex_obj": plex_track,
                },
            )
        except Exception as e:
            logger.error(f"Failed to convert Plex track: {e}")
            raise

    def search_tracks(
        self,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        limit: int = 50,
    ) -> List[dict]:
        """Search for tracks in Plex library.

        Args:
            title: Track title
            artist: Artist name
            album: Album name
            limit: Maximum number of results

        Returns:
            List of track dicts with title, artist, album, plex_ratingkey
        """
        if not self.connected or not self.music_library:
            logger.error("Not connected to Plex server")
            return []

        try:
            search_query = {}

            if album:
                search_query["album.title"] = album
            if title:
                search_query["track.title"] = title
            if artist:
                search_query["artist.title"] = artist

            if search_query:
                plex_tracks = self.music_library.searchTracks(**search_query, limit=limit)
                logger.debug(
                    f"Plex search for {search_query} returned {len(plex_tracks)} tracks"
                )
                # Return dicts for compatibility with search.py pipeline
                return [
                    {
                        "title": getattr(t, "title", ""),
                        "artist": self._get_artist_name(t),
                        "album": getattr(t, "parentTitle", ""),
                        "backend_id": getattr(t, "ratingKey", None),
                        "plex_ratingkey": getattr(t, "ratingKey", None),
                    }
                    for t in plex_tracks
                ]
            else:
                return []

        except Exception as e:
            logger.error(f"Plex track search failed: {e}")
            return []

    def _get_artist_name(self, plex_track: Any) -> str:
        """Extract artist name from Plex track."""
        try:
            if hasattr(plex_track, "originalTitle") and plex_track.originalTitle:
                return plex_track.originalTitle
            if hasattr(plex_track, "grandparentTitle") and plex_track.grandparentTitle:
                return plex_track.grandparentTitle
            if hasattr(plex_track, "artist") and callable(plex_track.artist):
                try:
                    return plex_track.artist().title
                except Exception:
                    pass
        except Exception:
            pass
        return ""

    def get_all_tracks(self) -> List[Track]:
        """Get all tracks from Plex library."""
        if not self.connected or not self.music_library:
            logger.error("Not connected to Plex server")
            return []

        try:
            tracks = []
            try:
                tracks = self.music_library.search(libtype="track", maxresults=None)
            except TypeError:
                try:
                    tracks = self.music_library.searchTracks(maxresults=None)
                except TypeError:
                    tracks = self.music_library.searchTracks()

            if not tracks:
                tracks = self.music_library.all()

            logger.info(f"Retrieved {len(tracks)} tracks from Plex library")
            return [self._plex_track_to_track(t) for t in tracks]
        except Exception as e:
            logger.error(f"Failed to get all tracks from Plex: {e}")
            return []

    def get_track_count(self) -> Optional[int]:
        """Return the total number of tracks in the Plex music library."""
        if not self.connected or not self.music_library:
            logger.error("Not connected to Plex server")
            return None

        try:
            total = getattr(self.music_library, "totalSize", None)
            if total is None:
                total = getattr(self.music_library, "total_size", None)
            return int(total) if total is not None else None
        except (TypeError, ValueError) as e:
            logger.debug(f"Failed to read Plex track count: {e}")
            return None

    def get_track(self, track_id: str) -> Optional[Track]:
        """Get a track by rating key."""
        if not self.connected or not self.server:
            logger.error("Not connected to Plex server")
            return None

        try:
            plex_track = self.server.fetchItem(int(track_id))
            return self._plex_track_to_track(plex_track)
        except (NotFound, ValueError, Exception) as e:
            logger.debug(f"Track {track_id} not found: {e}")
            return None

    def get_track_metadata(self, track_id: str) -> Optional[Dict[str, Any]]:
        """Get full metadata for a track."""
        track = self.get_track(track_id)
        if track:
            return track.model_dump()
        return None

    def get_plex_track(self, track_id: str) -> Optional[Any]:
        """Get the raw Plex track object by rating key."""
        if not self.connected or not self.server:
            logger.error("Not connected to Plex server")
            return None

        try:
            return self.server.fetchItem(int(track_id))
        except (NotFound, ValueError, Exception) as e:
            logger.debug(f"Plex track {track_id} not found: {e}")
            return None

    def get_playlist_tracks(self, playlist_name: str) -> List[Track]:
        """Get all tracks from a Plex playlist."""
        if not self.connected or not self.server:
            logger.error("Not connected to Plex server")
            return []

        try:
            playlist = self.server.playlist(playlist_name)
            items = playlist.items()
            return [self._plex_track_to_track(item) for item in items]
        except Exception as e:
            logger.error(f"Failed to get playlist '{playlist_name}': {e}")
            return []

    def _resolve_plex_items(self, tracks: List[Any]) -> set:
        """Resolve Track/dict items into Plex items."""
        plex_set = set()
        for item in tracks:
            try:
                if isinstance(item, dict):
                    rating_key = item.get("plex_ratingkey") or item.get("backend_id")
                else:
                    rating_key = getattr(item, "plex_ratingkey", None) or getattr(
                        item, "backend_id", None
                    )

                if rating_key is None:
                    logger.warning(f"{item} does not have a backend_id or plex_ratingkey")
                    continue

                plex_set.add(self.server.fetchItem(int(rating_key)))
            except (exceptions.NotFound, AttributeError, ValueError) as e:
                logger.warning(f"{item} not found in Plex library. Error: {e}")
                continue
        return plex_set

    def add_tracks_to_playlist(self, playlist_name: str, tracks: List[Any]) -> int:
        """Add tracks to a Plex playlist (no duplicates)."""
        if not self.connected or not self.server:
            logger.error("Not connected to Plex server")
            return 0
        if not tracks:
            logger.warning(f"No tracks to add to playlist {playlist_name}")
            return 0

        try:
            playlist = self.server.playlist(playlist_name)
            playlist_set = set(playlist.items())
        except exceptions.NotFound:
            playlist = None
            playlist_set = set()

        plex_set = self._resolve_plex_items(tracks)
        to_add = plex_set - playlist_set
        logger.info(f"Adding {len(to_add)} tracks to {playlist_name} playlist")

        if playlist is None:
            logger.info(f"{playlist_name} playlist will be created")
            if to_add:
                self.server.createPlaylist(playlist_name, items=list(to_add))
        else:
            try:
                if to_add:
                    playlist.addItems(items=list(to_add))
            except exceptions.BadRequest as e:
                logger.error(f"Error adding items to {playlist_name} playlist. Error: {e}")

        try:
            if playlist is None:
                playlist = self.server.playlist(playlist_name)
            items = playlist.items()
            sorted_items = sorted(
                items,
                key=lambda x: (
                    getattr(x, "lastViewedAt").timestamp()
                    if getattr(x, "lastViewedAt", None) is not None
                    else 0
                ),
                reverse=True,
            )
            playlist.removeItems(items)
            for item in sorted_items:
                playlist.addItems(item)
        except Exception as e:
            logger.debug(f"Could not sort playlist {playlist_name}: {e}")

        return len(to_add)

    def clear_playlist(self, playlist_name: str) -> None:
        """Clear all items from a Plex playlist."""
        if not self.connected or not self.server:
            logger.error("Not connected to Plex server")
            return

        playlist = self.server.playlist(playlist_name)
        tracks = playlist.items()
        if tracks:
            playlist.removeItems(items=tracks)
