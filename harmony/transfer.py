"""Playlist transfer utilities for Harmony."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from harmony.models import Track
from harmony.providers import spotify as spotify_provider
from harmony.utils.helpers import format_track_info

logger = logging.getLogger("harmony.transfer")


class PlexPlaylistSource:
    """Playlist source backed by Plex."""

    def __init__(self, plex_backend) -> None:
        self._plex = plex_backend

    def get_tracks(self, playlist_name: str) -> List[Track]:
        return self._plex.get_playlist_tracks(playlist_name)


class SpotifyPlaylistDestination:
    """Playlist destination backed by Spotify."""

    def __init__(self, config: Dict[str, Any]) -> None:
        self._config = config
        self._client = spotify_provider.create_spotify_user_client(config)

    def ensure_playlist(self, playlist_name: str) -> Optional[str]:
        if not self._client:
            return None
        return spotify_provider.ensure_spotify_playlist(self._client, playlist_name)

    def resolve_track_id(self, track: Track) -> Optional[str]:
        if not self._client:
            return None
        return spotify_provider.search_spotify_track_id(
            self._client, track.title, track.artist
        )

    def add_tracks(self, playlist_id: str, track_ids: List[str]) -> int:
        if not self._client:
            return 0
        return spotify_provider.add_tracks_to_spotify_playlist(
            self._client, playlist_id, track_ids
        )


def transfer_playlist(
    harmony_app,
    source: str,
    destination: str,
    playlist_name: str,
    destination_playlist: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Transfer a playlist between services.

    Returns summary dict with counts and missing entries.
    """
    if source != "plex":
        raise ValueError(f"Unsupported source '{source}'")
    if destination != "spotify":
        raise ValueError(f"Unsupported destination '{destination}'")

    source_adapter = PlexPlaylistSource(harmony_app.plex)
    dest_config = harmony_app.config.providers.spotify.model_dump()
    destination_adapter = SpotifyPlaylistDestination(dest_config)

    dest_playlist_name = destination_playlist or playlist_name
    playlist_id = destination_adapter.ensure_playlist(dest_playlist_name)
    if not playlist_id:
        raise RuntimeError("Failed to create or locate destination playlist")

    tracks = source_adapter.get_tracks(playlist_name)
    if limit:
        tracks = tracks[:limit]

    resolved_ids: List[str] = []
    missing: List[str] = []
    for track in tracks:
        track_id = destination_adapter.resolve_track_id(track)
        if track_id:
            resolved_ids.append(track_id)
        else:
            missing.append(format_track_info(track.title, track.artist, track.album))

    added = destination_adapter.add_tracks(playlist_id, resolved_ids)
    return {
        "source_count": len(tracks),
        "matched": len(resolved_ids),
        "added": added,
        "missing": missing,
        "destination_playlist": dest_playlist_name,
    }
