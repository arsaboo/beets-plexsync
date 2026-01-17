"""ListenBrainz provider for Harmony."""

import logging
import re
from typing import List, Dict, Any, Optional
import requests
from datetime import datetime

logger = logging.getLogger("harmony.providers.listenbrainz")


class ListenBrainzClient:
    """ListenBrainz API client."""
    
    BASE_URL = "https://api.listenbrainz.org/1/"
    
    def __init__(self, token: Optional[str] = None):
        """Initialize ListenBrainz client.
        
        Args:
            token: ListenBrainz user token for authentication
        """
        self.token = token
        self.auth_header = {"Authorization": f"Token {token}"} if token else {}
    
    def _make_request(self, endpoint: str) -> Optional[Dict[str, Any]]:
        """Make authenticated API request.
        
        Args:
            endpoint: API endpoint path (e.g., "user/username/playlists")
        
        Returns:
            JSON response as dictionary, or None on error
        """
        try:
            url = f"{self.BASE_URL}{endpoint}"
            response = requests.get(url, headers=self.auth_header, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"ListenBrainz API error: {e}")
            return None
    
    def get_createdfor_playlists(self, username: str) -> List[Dict[str, Any]]:
        """Get playlists created for user (troi-bot recommendations).
        
        Args:
            username: ListenBrainz username
        
        Returns:
            List of playlist objects
        """
        resp = self._make_request(f"user/{username}/playlists/createdfor")
        if not resp:
            return []
        return resp.get("playlists", [])
    
    def get_playlist(self, identifier: str) -> Optional[Dict[str, Any]]:
        """Get specific playlist by identifier.
        
        Args:
            identifier: Playlist identifier (UUID)
        
        Returns:
            Playlist object with tracks, or None on error
        """
        return self._make_request(f"playlist/{identifier}")


def parse_troi_playlists(username: str, token: str, playlist_type: str = "jams") -> List[Dict[str, Any]]:
    """Parse troi-bot playlists from ListenBrainz.
    
    Args:
        username: ListenBrainz username
        token: ListenBrainz user token
        playlist_type: "jams" or "exploration"
    
    Returns:
        List of playlist metadata dictionaries sorted by date (most recent first)
    """
    client = ListenBrainzClient(token)
    
    # Fetch createdfor playlists
    playlists = client.get_createdfor_playlists(username)
    if not playlists:
        logger.warning(f"No ListenBrainz playlists found for user: {username}")
        return []
    
    # Filter by playlist type
    type_keyword = "Jams" if playlist_type == "jams" else "Exploration"
    filtered = []
    
    for item in playlists:
        playlist = item.get("playlist", {})
        if playlist.get("creator") != "listenbrainz":
            continue
        
        title = playlist.get("title", "")
        if type_keyword not in title:
            continue
        
        # Extract date from title: "week of YYYY-MM-DD"
        date_match = re.search(r"week of (\d{4}-\d{2}-\d{2})", title)
        if not date_match:
            continue
        
        date_str = date_match.group(1)
        try:
            date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            logger.debug(f"Failed to parse date from playlist title: {title}")
            continue
        
        identifier = playlist.get("identifier", "")
        if not isinstance(identifier, str):
            continue
        
        # Extract UUID from identifier (format: https://listenbrainz.org/playlist/{uuid})
        playlist_id = identifier.split("/")[-1] if "/" in identifier else identifier
        
        filtered.append({
            "type": playlist_type,
            "date": date,
            "identifier": playlist_id,
            "title": title,
            "name": f"Weekly {type_keyword}"
        })
    
    # Sort by date descending (most recent first)
    filtered.sort(key=lambda x: x["date"], reverse=True)
    
    logger.info(f"Found {len(filtered)} ListenBrainz {type_keyword} playlists")
    return filtered


def fetch_troi_playlist_tracks(identifier: str, token: str) -> List[Dict[str, Any]]:
    """Fetch tracks from a troi-bot playlist.
    
    Args:
        identifier: Playlist identifier (UUID)
        token: ListenBrainz user token
    
    Returns:
        List of track dictionaries with title, artist, album
    """
    client = ListenBrainzClient(token)
    playlist_resp = client.get_playlist(identifier)
    
    if not playlist_resp:
        logger.error(f"Failed to fetch ListenBrainz playlist: {identifier}")
        return []
    
    playlist_data = playlist_resp.get("playlist", {})
    tracks_data = playlist_data.get("track", [])
    
    tracks = []
    for track in tracks_data:
        # Extract basic metadata only (no MusicBrainz lookup)
        creator = track.get("creator", "Unknown Artist")
        title = track.get("title", "Unknown Track")
        
        # ListenBrainz doesn't provide album info in playlist tracks
        # Extension metadata might have album, but we'll keep it simple for now
        
        tracks.append({
            "title": title.strip(),
            "artist": creator.strip(),
            "album": None  # Not provided by ListenBrainz
        })
    
    logger.info(f"Fetched {len(tracks)} tracks from ListenBrainz playlist")
    return tracks


def get_weekly_jams(username: str, token: str, most_recent: bool = True) -> List[Dict[str, Any]]:
    """Get Weekly Jams playlist tracks.
    
    Args:
        username: ListenBrainz username
        token: ListenBrainz user token
        most_recent: True for latest, False for previous week
    
    Returns:
        List of track dictionaries
    """
    playlists = parse_troi_playlists(username, token, "jams")
    if not playlists:
        logger.warning("No Weekly Jams playlists found")
        return []
    
    # Select most recent or second most recent
    if not most_recent and len(playlists) < 2:
        logger.warning("Not enough Weekly Jams playlists to select previous week")
        return []
    
    selected = playlists[0] if most_recent else playlists[1]
    logger.info(f"Fetching Weekly Jams: {selected['title']}")
    
    return fetch_troi_playlist_tracks(selected["identifier"], token)


def get_weekly_exploration(username: str, token: str, most_recent: bool = True) -> List[Dict[str, Any]]:
    """Get Weekly Exploration playlist tracks.
    
    Args:
        username: ListenBrainz username
        token: ListenBrainz user token
        most_recent: True for latest, False for previous week
    
    Returns:
        List of track dictionaries
    """
    playlists = parse_troi_playlists(username, token, "exploration")
    if not playlists:
        logger.warning("No Weekly Exploration playlists found")
        return []
    
    # Select most recent or second most recent
    if not most_recent and len(playlists) < 2:
        logger.warning("Not enough Weekly Exploration playlists to select previous week")
        return []
    
    selected = playlists[0] if most_recent else playlists[1]
    logger.info(f"Fetching Weekly Exploration: {selected['title']}")
    
    return fetch_troi_playlist_tracks(selected["identifier"], token)
