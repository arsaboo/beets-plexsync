"""M3U8 playlist parser for Harmony."""

import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger("harmony.providers.m3u8")


def import_m3u8_playlist(
    filepath: str,
    cache: Optional[Any] = None
) -> List[Dict[str, Any]]:
    """Import M3U8 playlist file and extract track information.

    Supports:
    - #EXTINF: format for artist - title metadata
    - #EXTALB: format for album name

    Args:
        filepath: Path to the M3U8 file
        cache: Optional cache object for storing results

    Returns:
        List of track dictionaries with title, artist, album
    """
    playlist_id = str(Path(filepath).stem)

    # Check cache first
    if cache:
        try:
            cached_data = cache.get_playlist_cache(playlist_id, "m3u8")
            if cached_data:
                logger.info(f"Using cached M3U8 playlist data for {playlist_id}")
                return cached_data
        except Exception as e:
            logger.debug(f"Cache lookup failed: {e}")

    song_list: List[Dict[str, Any]] = []

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]

        i = 0
        while i < len(lines):
            line = lines[i]

            if line.startswith("#EXTINF:"):
                # Extract metadata: #EXTINF:duration,Artist - Title
                meta = line.split(",", 1)[1] if "," in line else ""

                artist = None
                title = None
                album = None

                if " - " in meta:
                    parts = meta.split(" - ", 1)
                    artist = parts[0].strip()
                    title = parts[1].strip()
                else:
                    logger.debug(f"EXTINF missing artist-title separator: {meta}")

                # Check for optional EXTALB line (album)
                next_idx = i + 1
                if next_idx < len(lines) and lines[next_idx].startswith("#EXTALB:"):
                    album = lines[next_idx][8:].strip()
                    next_idx += 1

                # Skip file path (next non-comment line)
                if next_idx < len(lines) and not lines[next_idx].startswith("#"):
                    next_idx += 1

                if title and artist:
                    song_list.append({
                        "title": title,
                        "artist": artist,
                        "album": album,
                    })
                    logger.debug(f"Parsed M3U8 track: {artist} - {title}")

                i = next_idx - 1

            i += 1

        # Cache results
        if song_list and cache:
            try:
                cache.set_playlist_cache(playlist_id, "m3u8", song_list)
                logger.info(f"Cached {len(song_list)} tracks from M3U8: {playlist_id}")
            except Exception as e:
                logger.debug(f"Failed to cache M3U8 data: {e}")

        logger.info(f"Imported {len(song_list)} tracks from M3U8: {filepath}")
        return song_list

    except FileNotFoundError:
        logger.error(f"M3U8 file not found: {filepath}")
        return []
    except Exception as e:
        logger.error(f"Error importing M3U8 playlist {filepath}: {e}")
        return []
