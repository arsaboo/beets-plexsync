"""Gaana playlist importer for Harmony."""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("harmony.providers.gaana")


def import_gaana_playlist(
    url: str,
    cache: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Import Gaana playlist with caching.

    Args:
        url: URL of the Gaana playlist
        cache: Cache object for storing results

    Returns:
        List of song dictionaries
    """
    playlist_id = url.split("/")[-1]
    if not playlist_id:
        logger.error(f"Could not extract playlist ID from URL: {url}")
        return []

    if cache:
        cached_data = cache.get_playlist_cache(playlist_id, "gaana")
        if cached_data:
            logger.info(f"Using cached tracks for Gaana playlist {playlist_id}")
            return cached_data

    try:
        from beetsplug.gaana import GaanaPlugin
    except ModuleNotFoundError:
        logger.error(
            "Gaana provider requires the optional beets-gaana plugin. "
            "Install beets-gaana or add a native Gaana provider."
        )
        return []

    try:
        gaana = GaanaPlugin()
    except Exception as exc:
        logger.error(f"Unable to initialize Gaana plugin. Error: {exc}")
        return []

    song_list = gaana.import_gaana_playlist(url)

    if not song_list:
        logger.warning(f"No tracks found in Gaana playlist {playlist_id}")

    if song_list and cache:
        cache.set_playlist_cache(playlist_id, "gaana", song_list)
        logger.info(f"Cached {len(song_list)} tracks from Gaana playlist")

    return song_list or []
