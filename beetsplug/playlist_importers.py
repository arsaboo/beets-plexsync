"""
Consolidated playlist import utilities for various music services.
This module acts as a central point for initiating playlist imports,
calling provider-specific functions where necessary.
"""
import logging
import os # Added for path operations for m3u8
from beetsplug.caching import Cache # Assuming Cache is a class that can be instantiated or used statically
# Import provider-specific functions
from beetsplug.provider_apple import import_apple_playlist as apple_importer
from beetsplug.provider_jiosaavn import import_jiosaavn_playlist as jiosaavn_importer
from beetsplug.provider_gaana import import_gaana_playlist as gaana_importer
from beetsplug.provider_youtube import import_yt_playlist as yt_playlist_importer
from beetsplug.provider_youtube import import_yt_search as yt_search_importer
from beetsplug.provider_tidal import import_tidal_playlist as tidal_importer
from beetsplug.provider_m3u8 import import_m3u8_playlist as m3u8_importer
from beetsplug.provider_post import import_post_playlist as post_importer
# Assuming spotify_utils handles its own import logic including authentication if needed by its functions
from beetsplug.spotify_utils import import_spotify_playlist_with_fallback, get_spotify_playlist_id_from_url

_log = logging.getLogger('beets.plexsync.playlist_importers')

# Headers might be needed by some importers, passed from PlexSync
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 0.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
}

def import_playlist_from_url(playlist_url: str, cache_instance: Cache, headers: dict = None, spotify_sp_instance=None):
    """
    Imports a playlist from a given URL, determining the provider.

    Args:
        playlist_url: The URL of the playlist.
        cache_instance: An instance of the Cache class.
        headers: HTTP headers to use for scraping (if applicable).
        spotify_sp_instance: Authenticated Spotipy instance, required for Spotify API.

    Returns:
        A list of song dicts, or an empty list if import fails or URL is unsupported.
    """
    if not headers:
        headers = DEFAULT_HEADERS

    songs = []
    if not playlist_url or not isinstance(playlist_url, str):
        _log.error("Invalid playlist URL provided.")
        return songs

    _log.info("Attempting to import playlist from URL: {}", playlist_url)

    url_lower = playlist_url.lower() # For case-insensitive matching

    if "apple" in url_lower:
        _log.debug("Detected Apple Music playlist.")
        songs = apple_importer(playlist_url, cache_instance, headers)
    elif "jiosaavn" in url_lower:
        _log.debug("Detected JioSaavn playlist.")
        songs = jiosaavn_importer(playlist_url, cache_instance)
    elif "gaana.com" in url_lower:
        _log.debug("Detected Gaana playlist.")
        songs = gaana_importer(playlist_url, cache_instance, headers)
    elif "spotify" in url_lower:
        _log.debug("Detected Spotify playlist.")
        playlist_id = get_spotify_playlist_id_from_url(playlist_url)
        if playlist_id:
            if not spotify_sp_instance:
                _log.warning("Spotify instance not provided; Spotify API import might fail or be limited.")
            songs = import_spotify_playlist_with_fallback(spotify_sp_instance, playlist_id, cache_instance, headers)
        else:
            _log.error("Could not extract Spotify playlist ID from URL: {}", playlist_url)
    elif "youtube" in url_lower:
        _log.debug("Detected YouTube playlist.")
        songs = yt_playlist_importer(playlist_url, cache_instance)
    elif "tidal" in url_lower:
        _log.debug("Detected Tidal playlist.")
        songs = tidal_importer(playlist_url, cache_instance, headers)
    else:
        _log.warning("Playlist URL not recognized or supported by common importers: {}", playlist_url)

    if songs:
        _log.info("Successfully imported {} songs from {}", len(songs), playlist_url)
    else:
        _log.warning("No songs imported from {}. The URL might be unsupported, private, or the playlist empty.", playlist_url)

    return songs

def import_from_m3u8_file(filepath: str, cache_instance: Cache, config_dir_path: str = None):
    """
    Imports a playlist from an M3U8 file.
    If filepath is relative, it's considered relative to config_dir_path.
    """
    _log.info("Attempting to import playlist from M3U8 file: {}", filepath)

    actual_filepath = filepath
    if config_dir_path and not os.path.isabs(filepath):
        actual_filepath = os.path.join(config_dir_path, filepath)
        _log.debug("Relative M3U8 path provided. Resolved to: {}", actual_filepath)

    if not os.path.exists(actual_filepath):
        _log.error("M3U8 file not found at path: {}", actual_filepath)
        return []

    songs = m3u8_importer(actual_filepath, cache_instance)
    if songs:
        _log.info("Successfully imported {} songs from M3U8 file: {}", len(songs), actual_filepath)
    else:
        _log.warning("No songs imported from M3U8 file: {}", actual_filepath)
    return songs

def import_from_post_endpoint(source_config: dict, cache_instance: Cache):
    """Imports a playlist from a POST request endpoint."""
    endpoint_url = source_config.get('url', 'Unknown POST endpoint')
    _log.info("Attempting to import playlist from POST endpoint: {}", endpoint_url)
    songs = post_importer(source_config, cache_instance)
    if songs:
        _log.info("Successfully imported {} songs from POST endpoint: {}", len(songs), endpoint_url)
    else:
        _log.warning("No songs imported from POST endpoint: {}", endpoint_url)
    return songs

def import_from_youtube_search(query: str, limit: int, cache_instance: Cache):
    """Imports tracks based on a YouTube search query."""
    _log.info("Attempting to import tracks from YouTube search: '{}' (limit: {})", query, limit)
    songs = yt_search_importer(query, limit, cache_instance)
    if songs:
        _log.info("Successfully imported {} tracks from YouTube search: '{}'", len(songs), query)
    else:
        _log.warning("No songs imported from YouTube search: '{}'", query)
    return songs
