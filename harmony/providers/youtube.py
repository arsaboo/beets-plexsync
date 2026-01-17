"""Native YouTube provider for Harmony (no beetsplug dependency)."""

import logging
import re
from typing import List, Dict, Any, Optional

logger = logging.getLogger("harmony.providers.youtube")


def extract_playlist_id(url: str) -> Optional[str]:
    """Extract YouTube playlist ID from URL.

    Supports:
    - https://www.youtube.com/playlist?list=PLAYLIST_ID
    - https://music.youtube.com/playlist?list=PLAYLIST_ID
    - youtube.com/playlist?list=PLAYLIST_ID
    """
    try:
        # Match pattern: list=PLAYLIST_ID
        match = re.search(r"list=([a-zA-Z0-9_-]+)", url)
        if match:
            return match.group(1)
    except Exception as e:
        logger.debug(f"Error extracting playlist ID: {e}")
    return None


def import_yt_playlist(
    url: str,
    cache: Optional[Any] = None
) -> List[Dict[str, Any]]:
    """Import tracks from a YouTube Music playlist URL.

    This requires yt-dlp or youtube-dl for parsing.
    Falls back gracefully if not available.

    Args:
        url: YouTube Music playlist URL
        cache: Optional cache object for storing results

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
            cached_data = cache.get_playlist_cache(playlist_id, "youtube_tracks")
            if cached_data:
                logger.info(f"Using cached YouTube playlist data for {playlist_id}")
                return cached_data
        except Exception as e:
            logger.debug(f"Cache lookup failed: {e}")

    song_list: List[Dict[str, Any]] = []

    # Try yt-dlp first (preferred, more reliable)
    try:
        import yt_dlp

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            playlist_info = ydl.extract_info(url, download=False)

            if not playlist_info:
                logger.error(f"Failed to extract YouTube playlist info: {url}")
                return []

            # Process entries
            entries = playlist_info.get("entries", [])
            for entry in entries:
                if not entry:
                    continue

                try:
                    title = entry.get("title") or entry.get("id", "Unknown")
                    uploader = entry.get("uploader", "Unknown")

                    # Try to parse "Artist - Song" format from title
                    artist = uploader
                    if " - " in title and len(title.split(" - ")) == 2:
                        artist, title = title.split(" - ", 1)

                    song_list.append({
                        "title": title.strip(),
                        "artist": artist.strip(),
                        "album": None,  # YouTube doesn't provide album info
                    })
                except (KeyError, AttributeError) as e:
                    logger.debug(f"Error processing YouTube entry: {e}")

        logger.info(f"Imported {len(song_list)} tracks from YouTube via yt-dlp: {playlist_id}")

        # Cache results
        if song_list and cache:
            try:
                cache.set_playlist_cache(playlist_id, "youtube_tracks", song_list)
            except Exception as e:
                logger.debug(f"Failed to cache YouTube data: {e}")

        return song_list

    except ImportError:
        logger.debug("yt-dlp not available, trying youtube-dl")
    except Exception as e:
        logger.warning(f"yt-dlp import failed: {e}")

    # Fallback to youtube-dl
    try:
        import youtube_dl

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
        }

        with youtube_dl.YoutubeDL(ydl_opts) as ydl:
            playlist_info = ydl.extract_info(url, download=False)

            if not playlist_info:
                logger.error(f"Failed to extract YouTube playlist info: {url}")
                return []

            entries = playlist_info.get("entries", [])
            for entry in entries:
                if not entry:
                    continue

                try:
                    title = entry.get("title", "Unknown")
                    uploader = entry.get("uploader", "Unknown")

                    # Try to parse "Artist - Song" format
                    artist = uploader
                    if " - " in title and len(title.split(" - ")) == 2:
                        artist, title = title.split(" - ", 1)

                    song_list.append({
                        "title": title.strip(),
                        "artist": artist.strip(),
                        "album": None,
                    })
                except (KeyError, AttributeError) as e:
                    logger.debug(f"Error processing YouTube entry: {e}")

        logger.info(f"Imported {len(song_list)} tracks from YouTube: {playlist_id}")

        # Cache results
        if song_list and cache:
            try:
                cache.set_playlist_cache(playlist_id, "youtube_tracks", song_list)
            except Exception as e:
                logger.debug(f"Failed to cache YouTube data: {e}")

        return song_list

    except ImportError:
        logger.error("YouTube support requires 'yt-dlp' or 'youtube-dl' packages")
        return []
    except Exception as e:
        logger.error(f"YouTube import failed: {e}")
        return []


def import_yt_search(
    query: str,
    cache: Optional[Any] = None,
    max_results: int = 10
) -> List[Dict[str, Any]]:
    """Search YouTube for tracks by query.

    Args:
        query: Search query
        cache: Optional cache object
        max_results: Maximum results to return

    Returns:
        List of track dictionaries
    """
    song_list: List[Dict[str, Any]] = []

    try:
        import yt_dlp

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "default_search": "ytsearch",
            "max_results": max_results,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            search_results = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)

            for entry in search_results.get("entries", []):
                try:
                    title = entry.get("title", "Unknown")
                    uploader = entry.get("uploader", "Unknown")

                    song_list.append({
                        "title": title.strip(),
                        "artist": uploader.strip(),
                        "album": None,
                    })
                except (KeyError, AttributeError):
                    pass

        logger.info(f"Found {len(song_list)} YouTube search results for: {query}")
        return song_list

    except ImportError:
        logger.error("YouTube search requires 'yt-dlp' package")
        return []
    except Exception as e:
        logger.error(f"YouTube search failed: {e}")
        return []
