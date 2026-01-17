"""Tidal playlist importer for Harmony."""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("harmony.providers.tidal")


def import_tidal_playlist(
    url: str,
    cache: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Import Tidal playlist with caching.

    Args:
        url: URL of the Tidal playlist
        cache: Cache object for storing results

    Returns:
        List of song dictionaries
    """
    playlist_id = url.split("/")[-1]
    if not playlist_id:
        logger.error(f"Could not extract playlist ID from URL: {url}")
        return []

    if cache:
        cached_data = cache.get_playlist_cache(playlist_id, "tidal")
        if cached_data:
            logger.info("Using cached Tidal playlist data")
            return cached_data

    try:
        from beetsplug.tidal import TidalPlugin
    except ModuleNotFoundError:
        logger.error(
            "Tidal provider requires the optional beets-tidal plugin. "
            "Install beets-tidal or add a native Tidal provider."
        )
        return []

    try:
        tidal = TidalPlugin()
        song_list = tidal.import_tidal_playlist(url)

        if cache and song_list:
            cache.set_playlist_cache(playlist_id, "tidal", song_list)
            logger.info(f"Cached {len(song_list)} tracks from Tidal playlist")

        return song_list or []
    except Exception as exc:
        logger.error(f"Unable to initialize Tidal plugin. Error: {exc}")
        return []
