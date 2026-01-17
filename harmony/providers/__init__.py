"""Provider adapters for Harmony.

These modules provide native implementations for importing playlists from
external sources. Each provider handles its own parsing and fallback logic.
"""

import logging

logger = logging.getLogger("harmony.providers")

try:
    from harmony.providers.m3u8 import import_m3u8_playlist
except ImportError as e:
    logger.warning(f"M3U8 provider import failed: {e}")
    def import_m3u8_playlist(*args, **kwargs):
        raise NotImplementedError("M3U8 provider unavailable")

try:
    from harmony.providers.spotify import import_spotify_playlist, extract_playlist_id as get_spotify_id
    def get_playlist_id(url):
        """Backward compat wrapper"""
        return get_spotify_id(url)
except ImportError as e:
    logger.warning(f"Spotify provider import failed: {e}")
    def import_spotify_playlist(*args, **kwargs):
        raise NotImplementedError("Spotify provider unavailable")
    def get_playlist_id(*args, **kwargs):
        raise NotImplementedError("Spotify provider unavailable")

try:
    from harmony.providers.youtube import import_yt_playlist, import_yt_search
except ImportError as e:
    logger.warning(f"YouTube provider import failed: {e}")
    def import_yt_playlist(*args, **kwargs):
        raise NotImplementedError("YouTube provider unavailable")
    def import_yt_search(*args, **kwargs):
        raise NotImplementedError("YouTube provider unavailable")

try:
    from harmony.providers.apple import import_apple_playlist
except ImportError as e:
    logger.warning(f"Apple Music provider import failed: {e}")
    def import_apple_playlist(*args, **kwargs):
        raise NotImplementedError("Apple Music provider unavailable")

try:
    from harmony.providers.tidal import import_tidal_playlist
except ImportError as e:
    logger.warning(f"Tidal provider import failed: {e}")
    def import_tidal_playlist(*args, **kwargs):
        raise NotImplementedError("Tidal provider unavailable")

try:
    from harmony.providers.gaana import import_gaana_playlist
except ImportError as e:
    logger.warning(f"Gaana provider import failed: {e}")
    def import_gaana_playlist(*args, **kwargs):
        raise NotImplementedError("Gaana provider unavailable")

try:
    from harmony.providers.jiosaavn import import_jiosaavn_playlist
except ImportError as e:
    logger.warning(f"JioSaavn provider import failed: {e}")
    def import_jiosaavn_playlist(*args, **kwargs):
        raise NotImplementedError("JioSaavn provider unavailable")

try:
    from harmony.providers.http_post import import_post_playlist
except ImportError as e:
    logger.warning(f"POST provider import failed: {e}")
    def import_post_playlist(*args, **kwargs):
        raise NotImplementedError("POST endpoint provider unavailable")

__all__ = [
    "import_spotify_playlist",
    "get_playlist_id",
    "import_yt_playlist",
    "import_yt_search",
    "import_apple_playlist",
    "import_tidal_playlist",
    "import_gaana_playlist",
    "import_jiosaavn_playlist",
    "import_m3u8_playlist",
    "import_post_playlist",
]
