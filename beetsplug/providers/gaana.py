import logging

_log = logging.getLogger('beets.plexsync.gaana')

def import_gaana_playlist(url, cache=None):
    """Import Gaana playlist with caching.

    Args:
        url: URL of the Gaana playlist
        cache: Cache object for storing results

    Returns:
        list: List of song dictionaries
    """
    # Generate cache key from URL
    playlist_id = url.split('/')[-1]

    if not playlist_id:
        _log.error(f"Could not extract playlist ID from URL: {url}")
        return []

    # Check cache
    if cache:
        cached_data = cache.get_playlist_cache(playlist_id, 'gaana')
        if cached_data:
            _log.info(f"Using cached tracks for Gaana playlist {playlist_id}")
            return cached_data

    try:
        from beetsplug.gaana import GaanaPlugin
    except ModuleNotFoundError:
        _log.error(
            "Gaana plugin not installed. \
                        See https://github.com/arsaboo/beets-gaana"
        )
        return None

    try:
        gaana = GaanaPlugin()
    except Exception as e:
        _log.error(f"Unable to initialize Gaana plugin. Error: {e}")
        return None

    # Get songs from Gaana
    song_list = gaana.import_gaana_playlist(url)

    if not song_list:
        _log.warning(f"No tracks found in Gaana playlist {playlist_id}")

    # Cache successful results
    if song_list and cache:
        cache.set_playlist_cache(playlist_id, 'gaana', song_list)
        _log.info(f"Cached {len(song_list)} tracks from Gaana playlist")

    return song_list