import re
import json
import logging
import requests
from bs4 import BeautifulSoup

_log = logging.getLogger('beets.plexsync.youtube')

def import_yt_playlist(url, cache=None):
    """Import YouTube playlist with caching.

    Args:
        url: URL of the YouTube playlist
        cache: Cache object for storing results

    Returns:
        list: List of song dictionaries
    """
    # Generate cache key from URL
    playlist_id = url.split('list=')[-1].split('&')[0]  # Extract playlist ID from URL

    # Check cache
    if cache:
        cached_data = cache.get_playlist_cache(playlist_id, 'youtube')
        if cached_data:
            _log.info("Using cached YouTube playlist data")
            return cached_data

    try:
        from beetsplug.youtube import YouTubePlugin
    except ModuleNotFoundError:
        _log.error("YouTube plugin not installed")
        return None

    try:
        ytp = YouTubePlugin()
        song_list = ytp.import_youtube_playlist(url)

        # Cache successful results
        if cache and song_list:
            cache.set_playlist_cache(playlist_id, 'youtube', song_list)
            _log.info("Cached {} tracks from YouTube playlist", len(song_list))

        return song_list
    except Exception as e:
        _log.error("Unable to initialize YouTube plugin. Error: {}", e)
        return None


def import_yt_search(query, limit, cache=None):
    """Import YouTube search results.

    Args:
        query: Search query string
        limit: Maximum number of results to return
        cache: Cache object for storing results

    Returns:
        list: List of song dictionaries
    """
    try:
        from beetsplug.youtube import YouTubePlugin
    except ModuleNotFoundError:
        _log.error("YouTube plugin not installed")
        return []
    try:
        ytp = YouTubePlugin()
        return ytp.import_youtube_search(query, limit)
    except Exception as e:
        _log.error("Unable to initialize YouTube plugin. Error: {}", e)
        return []