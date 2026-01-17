"""Native Spotify provider for Harmony (no beetsplug dependency)."""

import logging
import os
import re
from typing import List, Dict, Any, Optional

logger = logging.getLogger("harmony.providers.spotify")

DEFAULT_SPOTIFY_SCOPES = "playlist-modify-private playlist-modify-public playlist-read-private"


def extract_playlist_id(url: str) -> Optional[str]:
    """Extract Spotify playlist ID from URL.

    Supports:
    - https://open.spotify.com/playlist/PLAYLIST_ID
    - https://open.spotify.com/playlist/PLAYLIST_ID?si=...
    """
    try:
        # Match pattern: /playlist/PLAYLIST_ID (followed by ? or end)
        match = re.search(r"/playlist/([a-zA-Z0-9]+)", url)
        if match:
            return match.group(1)
    except Exception as e:
        logger.debug(f"Error extracting playlist ID: {e}")
    return None


def import_spotify_playlist(
    url: str,
    cache: Optional[Any] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Import tracks from a Spotify playlist URL.

    This is a lightweight implementation that requires optional spotipy library.
    Falls back to web scraping if spotipy unavailable.

    Args:
        url: Spotify playlist URL (https://open.spotify.com/playlist/PLAYLIST_ID)
        cache: Optional cache object for storing results
        client_id: Spotify API client ID (optional)
        client_secret: Spotify API client secret (optional)

    Returns:
        List of track dictionaries with title, artist, album
    """
    playlist_id = extract_playlist_id(url)
    if not playlist_id:
        logger.error(f"Could not extract playlist ID from: {url}")
        return []

    # Check cache first
    if cache:
        try:
            cached_data = cache.get_playlist_cache(playlist_id, "spotify_tracks")
            if cached_data:
                logger.info(f"Using cached Spotify playlist data for {playlist_id}")
                return cached_data
        except Exception as e:
            logger.debug(f"Cache lookup failed: {e}")

    song_list: List[Dict[str, Any]] = []

    # Try to use spotipy if available
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials
        import os

        # Get client credentials from parameters or environment
        cred_client_id = client_id or os.getenv("SPOTIPY_CLIENT_ID")
        cred_client_secret = client_secret or os.getenv("SPOTIPY_CLIENT_SECRET")

        if not cred_client_id or not cred_client_secret:
            logger.debug("No Spotify credentials provided - skipping API import")
            raise ImportError("Spotify credentials required")

        # Use client credentials flow (read-only, no user auth needed)
        auth = SpotifyClientCredentials(client_id=cred_client_id, client_secret=cred_client_secret)
        sp = spotipy.Spotify(auth_manager=auth)

        # Fetch playlist tracks
        results = sp.playlist_items(playlist_id, additional_types=["track"])

        while results:
            for item in results.get("items", []):
                track = item.get("track")
                if not track:
                    continue

                try:
                    title = track.get("name", "Unknown")
                    artist = track["artists"][0]["name"] if track.get("artists") else "Unknown"
                    album = track.get("album", {}).get("name", None)

                    song_list.append({
                        "title": title.strip(),
                        "artist": artist.strip(),
                        "album": album.strip() if album else None,
                    })
                except (KeyError, IndexError, AttributeError) as e:
                    logger.debug(f"Error processing Spotify track: {e}")

            # Fetch next page if available
            if results.get("next"):
                results = sp.next(results)
            else:
                break

        logger.info(f"Imported {len(song_list)} tracks from Spotify via API: {playlist_id}")

        # Cache results
        if song_list and cache:
            try:
                cache.set_playlist_cache(playlist_id, "spotify_tracks", song_list)
            except Exception as e:
                logger.debug(f"Failed to cache Spotify data: {e}")

        return song_list

    except ImportError:
        logger.warning("spotipy not available - falling back to web scraping")
    except Exception as e:
        logger.warning(f"Spotify API import failed: {e} - falling back to web scraping")

    # Fallback: Web scraping (requires beautifulsoup4 and requests)
    try:
        import requests
        from bs4 import BeautifulSoup
        import json

        playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        response = requests.get(playlist_url, headers=headers, timeout=10)
        if response.status_code != 200:
            logger.error(f"Failed to fetch Spotify playlist page: {response.status_code}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")

        # Look for JSON data in script tag
        for script in soup.find_all("script"):
            if not script.string:
                continue

            script_str = str(script.string)

            # Look for Spotify's initial state
            if "initialState" in script_str or "playlists" in script_str:
                try:
                    # Extract JSON from script tag
                    start = script_str.find("{")
                    if start == -1:
                        continue

                    # Simple JSON extraction
                    json_data = json.loads(script_str[start:])

                    # Try to find tracks in the JSON structure
                    if isinstance(json_data, dict):
                        # Navigate nested structure to find tracks
                        tracks = _extract_tracks_from_json(json_data)
                        if tracks:
                            song_list.extend(tracks)
                            break
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    logger.debug(f"Error parsing Spotify JSON: {e}")

        logger.info(f"Imported {len(song_list)} tracks from Spotify via web scraping: {playlist_id}")

        # Cache results
        if song_list and cache:
            try:
                cache.set_playlist_cache(playlist_id, "spotify_tracks", song_list)
            except Exception as e:
                logger.debug(f"Failed to cache Spotify data: {e}")

        return song_list

    except ImportError:
        logger.error("Web scraping requires 'beautifulsoup4' and 'requests' packages")
        return []
    except Exception as e:
        logger.error(f"Spotify web scraping failed: {e}")
        return []


def create_spotify_user_client(config: Dict[str, Any]) -> Optional[Any]:
    """Create an authenticated Spotify client for playlist writes."""
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth
    except ImportError:
        logger.error("spotipy not installed - Spotify transfer requires spotipy")
        return None

    client_id = config.get("client_id") or os.getenv("SPOTIPY_CLIENT_ID")
    client_secret = config.get("client_secret") or os.getenv("SPOTIPY_CLIENT_SECRET")
    redirect_uri = config.get("redirect_uri") or os.getenv("SPOTIPY_REDIRECT_URI")
    scope = config.get("scopes") or DEFAULT_SPOTIFY_SCOPES

    if not client_id or not client_secret or not redirect_uri:
        logger.error(
            "Spotify OAuth not configured. Set client_id, client_secret, and redirect_uri."
        )
        return None

    auth = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=scope,
        open_browser=True,
        cache_path=config.get("cache_path"),
    )
    return spotipy.Spotify(auth_manager=auth)


def ensure_spotify_playlist(sp, playlist_name: str) -> Optional[str]:
    """Find or create a Spotify playlist and return its ID."""
    try:
        user = sp.current_user()
        user_id = user.get("id")
        if not user_id:
            logger.error("Spotify user ID not available")
            return None

        playlists = sp.current_user_playlists(limit=50)
        while playlists:
            for playlist in playlists.get("items", []):
                if playlist.get("name") == playlist_name:
                    return playlist.get("id")
            if playlists.get("next"):
                playlists = sp.next(playlists)
            else:
                break

        created = sp.user_playlist_create(user_id, playlist_name, public=False)
        return created.get("id")
    except Exception as exc:
        logger.error(f"Failed to ensure Spotify playlist '{playlist_name}': {exc}")
        return None


def search_spotify_track_id(sp, title: str, artist: str) -> Optional[str]:
    """Resolve a Spotify track ID using title and artist."""
    query_parts = []
    if title:
        query_parts.append(f"track:{title}")
    if artist:
        query_parts.append(f"artist:{artist}")
    if not query_parts:
        return None

    query = " ".join(query_parts)
    try:
        results = sp.search(q=query, type="track", limit=5)
        items = results.get("tracks", {}).get("items", [])
        for item in items:
            if not item:
                continue
            if item.get("is_playable") is False:
                continue
            if item.get("restrictions", {}).get("reason") == "unavailable":
                continue
            if item.get("available_markets") == []:
                continue
            return item.get("id")
    except Exception as exc:
        logger.debug(f"Spotify track search failed for '{query}': {exc}")
    return None


def add_tracks_to_spotify_playlist(sp, playlist_id: str, track_ids: List[str]) -> int:
    """Add tracks to a Spotify playlist in batches."""
    if not playlist_id or not track_ids:
        return 0

    added = 0
    try:
        for i in range(0, len(track_ids), 100):
            batch = track_ids[i : i + 100]
            sp.playlist_add_items(playlist_id, batch)
            added += len(batch)
    except Exception as exc:
        logger.error(f"Failed adding tracks to Spotify playlist: {exc}")
    return added


def _extract_tracks_from_json(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Recursively search JSON for track information.

    This is a best-effort helper for web scraping fallback.
    """
    tracks = []

    if isinstance(data, dict):
        # Look for tracks array
        if "tracks" in data and isinstance(data["tracks"], list):
            for item in data["tracks"]:
                if isinstance(item, dict):
                    track = item.get("track") or item
                    if "name" in track and "artists" in track:
                        try:
                            title = track.get("name", "").strip()
                            artist = track["artists"][0].get("name", "Unknown") if track["artists"] else "Unknown"
                            album = track.get("album", {}).get("name")

                            if title:
                                tracks.append({
                                    "title": title,
                                    "artist": artist.strip(),
                                    "album": album.strip() if album else None,
                                })
                        except (KeyError, IndexError, AttributeError):
                            pass

        # Recurse into nested structures
        for value in data.values():
            if isinstance(value, (dict, list)):
                tracks.extend(_extract_tracks_from_json(value))

    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                tracks.extend(_extract_tracks_from_json(item))

    return tracks
