"""JioSaavn playlist importer for Harmony."""

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

from jiosaavn import JioSaavn

logger = logging.getLogger("harmony.providers.jiosaavn")

saavn = JioSaavn()


def _parse_title(title_orig: str) -> tuple[str, str]:
    """Parse soundtrack-style titles into title and album."""
    if '(From "' in title_orig:
        title = re.sub(r"\(From.*\)", "", title_orig)
        album = re.sub(r'^[^"]+"|(?<!^)"[^"]+"|"[^"]+$', "", title_orig)
    elif '[From "' in title_orig:
        title = re.sub(r"\[From.*\]", "", title_orig)
        album = re.sub(r'^[^"]+"|(?<!^)"[^"]+$', "", title_orig)
    else:
        title = title_orig
        album = ""
    return title.strip(), album.strip()


def _clean_album_name(album_orig: str) -> str:
    """Clean album name by removing common suffixes and extracting movie name."""
    album_orig = (
        album_orig.replace("(Original Motion Picture Soundtrack)", "")
        .replace("- Hindi", "")
        .strip()
    )
    if '(From "' in album_orig:
        album = re.sub(r'^[^"]+"|(?<!^)"[^"]+$', "", album_orig)
    elif '[From "' in album_orig:
        album = re.sub(r'^[^"]+"|(?<!^)"[^"]+$', "", album_orig)
    else:
        album = album_orig
    return album


async def _get_playlist_songs(playlist_url: str) -> Dict[str, Any]:
    """Get playlist songs by URL."""
    return await saavn.get_playlist_songs(playlist_url)


def import_jiosaavn_playlist(
    url: str,
    cache: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Import JioSaavn playlist with caching.

    Args:
        url: URL of the JioSaavn playlist
        cache: Cache object for storing results

    Returns:
        List of song dictionaries
    """
    playlist_id = url.split("/")[-1]

    if cache:
        cached_data = cache.get_playlist_cache(playlist_id, "jiosaavn")
        if cached_data:
            logger.info("Using cached JioSaavn playlist data")
            return cached_data

    song_list: List[Dict[str, Any]] = []

    try:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                data = asyncio.run_coroutine_threadsafe(
                    _get_playlist_songs(url), loop
                ).result()
            else:
                data = loop.run_until_complete(_get_playlist_songs(url))
        except (RuntimeError, AssertionError):
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            data = new_loop.run_until_complete(_get_playlist_songs(url))
            new_loop.close()

        if not data or "data" not in data or "list" not in data["data"]:
            logger.error("Invalid response from JioSaavn API")
            return song_list

        songs = data["data"]["list"]
        for song in songs:
            try:
                if ('From "' in song["title"]) or ("From &quot" in song["title"]):
                    title_orig = song["title"].replace("&quot;", '"')
                    title, album = _parse_title(title_orig)
                else:
                    title = song["title"]
                    album = _clean_album_name(song["more_info"]["album"])

                year = song.get("year", None)

                try:
                    artist = song["more_info"]["artistMap"]["primary_artists"][0]["name"]
                except (KeyError, IndexError):
                    try:
                        artist = song["more_info"]["artistMap"]["featured_artists"][0][
                            "name"
                        ]
                    except (KeyError, IndexError):
                        continue

                song_list.append(
                    {
                        "title": title.strip(),
                        "album": album.strip(),
                        "artist": artist.strip(),
                        "year": year,
                    }
                )

            except Exception as exc:
                logger.debug(f"Error processing JioSaavn song: {exc}")
                continue

        if song_list and cache:
            cache.set_playlist_cache(playlist_id, "jiosaavn", song_list)
            logger.info(f"Cached {len(song_list)} tracks from JioSaavn playlist")

    except Exception as exc:
        logger.error(f"Error importing JioSaavn playlist: {exc}")

    return song_list
