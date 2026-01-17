"""JioSaavn playlist importer for Harmony."""

import asyncio
import logging
from typing import Any, Dict, List, Optional

# Fix for pydantic compatibility issue with jiosaavn package
try:
    import pydantic.typing
    if not hasattr(pydantic.typing, 'Annotated'):
        # Monkey-patch the missing Annotated import
        from typing import Annotated
        pydantic.typing.Annotated = Annotated
except ImportError:
    pass

from jiosaavn import JioSaavn
from harmony.utils.parsing import parse_soundtrack_title, clean_album_name, clean_html_entities

logger = logging.getLogger("harmony.providers.jiosaavn")

saavn = JioSaavn()


def _get_playlist_songs(playlist_url: str) -> Dict[str, Any]:
    """Get playlist songs by URL."""
    return saavn.playlist(url=playlist_url)


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
        data = _get_playlist_songs(url)

        if not data or "songs" not in data:
            logger.error("Invalid response from JioSaavn API")
            return song_list

        songs = data["songs"]
        for song in songs:
            try:
                if ('From "' in song["songName"]) or ("From &quot" in song["songName"]):
                    title_orig = song["songName"].replace("&quot;", '"')
                    title, album = parse_soundtrack_title(title_orig)
                else:
                    title = song["songName"]
                    album = clean_album_name(song["albumName"]) or ""

                year = song.get("releaseDate", None)

                # Extract artist from primaryArtists
                primary_artists = song.get("primaryArtists", "")
                if primary_artists:
                    # Take the first artist if there are multiple
                    artist = clean_html_entities(primary_artists.split(",")[0].strip())
                else:
                    # Try featuredArtists as fallback
                    featured_artists = song.get("featuredArtists", "")
                    if featured_artists:
                        artist = clean_html_entities(featured_artists.split(",")[0].strip())
                    else:
                        continue

                song_list.append(
                    {
                        "title": clean_html_entities(title.strip()),
                        "album": clean_html_entities(album.strip()),
                        "artist": artist,
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
