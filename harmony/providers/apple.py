"""Apple Music playlist importer for Harmony."""

import json
import logging
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from harmony.utils.parsing import parse_soundtrack_title, clean_album_name, clean_html_entities

logger = logging.getLogger("harmony.providers.apple")


def import_apple_playlist(
    url: str,
    cache: Optional[Any] = None,
    headers: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Import Apple Music playlist with caching.

    Args:
        url: URL of the Apple Music playlist
        cache: Cache object for storing results
        headers: HTTP headers for the request

    Returns:
        List of song dictionaries
    """
    playlist_id = url.split("/")[-1]
    if not playlist_id:
        logger.error(f"Could not extract playlist ID from URL: {url}")
        return []

    if cache:
        cached_data = cache.get_playlist_cache(playlist_id, "apple")
        if cached_data:
            logger.info("Using cached Apple Music playlist data")
            return cached_data

    if headers is None:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/91.0.4472.124 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "DNT": "1",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/webp,*/*;q=0.8"
            ),
        }

    song_list: List[Dict[str, Any]] = []

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        try:
            data = soup.find("script", id="serialized-server-data").text
        except AttributeError:
            logger.debug("Error parsing Apple Music playlist")
            return []

        data = json.loads(data)

        try:
            songs = data[0]["data"]["sections"][1]["items"]
        except (KeyError, IndexError) as exc:
            logger.error(f"Failed to extract songs from Apple Music data: {exc}")
            return []

        for song in songs:
            try:
                title_orig = song["title"].strip()
                album_orig = song["tertiaryLinks"][0]["title"]
                artist_orig = song["subtitleLinks"][0]["title"]
                
                # Parse title for "From..." clauses (common in Bollywood/Indian music)
                if '(From "' in title_orig or '[From "' in title_orig:
                    title, album_from_title = parse_soundtrack_title(title_orig)
                    # Use album from title if present, otherwise use the original album
                    album = album_from_title if album_from_title else album_orig
                else:
                    title = title_orig
                    album = album_orig
                
                # Clean album name (remove OST suffixes, etc.)
                album = clean_album_name(album) or album
                
                # Clean HTML entities from all fields
                title = clean_html_entities(title)
                album = clean_html_entities(album)
                artist = clean_html_entities(artist_orig)
                
                song_list.append(
                    {
                        "title": title.strip(),
                        "album": album.strip(),
                        "artist": artist.strip(),
                    }
                )
            except (KeyError, IndexError) as exc:
                logger.debug(f"Error processing Apple Music song: {exc}")
                continue

        if song_list and cache:
            cache.set_playlist_cache(playlist_id, "apple", song_list)
            logger.info(f"Cached {len(song_list)} tracks from Apple Music playlist")

    except Exception as exc:
        logger.error(f"Error importing Apple Music playlist: {exc}")
        return []

    return song_list
