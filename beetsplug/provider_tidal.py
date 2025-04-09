import logging

_log = logging.getLogger('beets.plexsync.tidal')

def import_tidal_playlist(url, cache=None):
    """Import Tidal playlist with caching.

    Args:
        url: URL of the Tidal playlist
        cache: Cache object for storing results

    Returns:
        list: List of song dictionaries
    """
    # Generate cache key from URL
    playlist_id = url.split('/')[-1]

    # Check cache
    if cache:
        cached_data = cache.get_playlist_cache(playlist_id, 'tidal')
        if (cached_data):
            _log.info("Using cached Tidal playlist data")
            return cached_data

    try:
        from beetsplug.tidal import TidalPlugin
    except ModuleNotFoundError:
        _log.error("Tidal plugin not installed")
        return None

    try:
        tidal = TidalPlugin()
        song_list = tidal.import_tidal_playlist(url)

        # Cache successful results
        if cache and song_list:
            cache.set_playlist_cache(playlist_id, 'tidal', song_list)
            _log.info("Cached {} tracks from Tidal playlist", len(song_list))

        return song_list
    except Exception as e:
        _log.error("Unable to initialize Tidal plugin. Error: {}", e)
        return None