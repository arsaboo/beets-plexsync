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

    # Check cache
    if cache:
        cached_data = cache.get_playlist_cache(playlist_id, 'gaana')
        if (cached_data):
            _log.info("Using cached Gaana playlist data")
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
        _log.error("Unable to initialize Gaana plugin. Error: %s", e)
        return None

    # Get songs from Gaana
    song_list = gaana.import_gaana_playlist(url)

    # Cache successful results
    if song_list and cache:
        cache.set_playlist_cache(playlist_id, 'gaana', song_list)
        _log.info("Cached {} tracks from Gaana playlist", len(song_list))

    return song_list