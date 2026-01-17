"""Gaana playlist importer for Harmony."""

import json
import logging
import re
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from harmony.utils.parsing import parse_soundtrack_title, clean_album_name, clean_html_entities

logger = logging.getLogger("harmony.providers.gaana")


def _extract_redux_data(html: str) -> Optional[Dict[str, Any]]:
    """Extract REDUX_DATA from Gaana HTML.
    
    Args:
        html: HTML content from Gaana page
        
    Returns:
        Parsed Redux data dict, or None if not found
    """
    soup = BeautifulSoup(html, 'html.parser')
    
    # Find script containing REDUX_DATA
    for script in soup.find_all('script'):
        if script.string and 'window.REDUX_DATA' in script.string:
            script_content = script.string
            
            # Extract JSON by finding matching braces
            start = script_content.find('window.REDUX_DATA = ') + len('window.REDUX_DATA = ')
            
            # Count braces to find the end
            brace_count = 0
            in_string = False
            escape = False
            
            for i, char in enumerate(script_content[start:], start=start):
                if escape:
                    escape = False
                    continue
                if char == '\\':
                    escape = True
                    continue
                if char == '"' and not escape:
                    in_string = not in_string
                if not in_string:
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            end = i + 1
                            redux_json = script_content[start:end]
                            try:
                                return json.loads(redux_json)
                            except json.JSONDecodeError as e:
                                logger.error(f"Failed to parse Redux JSON: {e}")
                                return None
    
    return None


def import_gaana_playlist(
    url: str,
    cache: Optional[Any] = None,
    headers: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Import Gaana playlist with caching.

    Args:
        url: URL of the Gaana playlist
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
        cached_data = cache.get_playlist_cache(playlist_id, "gaana")
        if cached_data:
            logger.info(f"Using cached tracks for Gaana playlist {playlist_id}")
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
        }

    song_list: List[Dict[str, Any]] = []

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        # Extract Redux data
        redux_data = _extract_redux_data(response.text)
        
        if not redux_data:
            logger.error("Failed to extract Redux data from Gaana page")
            return []
        
        # Navigate to tracks
        try:
            playlist_detail = redux_data['playlist']['playlistDetail']
            tracks = playlist_detail['tracks']
        except (KeyError, TypeError) as exc:
            logger.error(f"Failed to extract tracks from Redux data: {exc}")
            return []
        
        if not tracks:
            logger.warning(f"No tracks found in Gaana playlist {playlist_id}")
            return []
        
        # Process each track
        for track in tracks:
            try:
                title_orig = track['track_title'].strip()
                album_orig = track.get('album_title', '').strip()
                
                # Extract first artist name (tracks can have multiple artists)
                artists = track.get('artist', [])
                if not artists:
                    logger.debug(f"Skipping track '{title_orig}' - no artist info")
                    continue
                
                artist_name = artists[0].get('name', '').strip()
                if not artist_name:
                    logger.debug(f"Skipping track '{title_orig}' - empty artist name")
                    continue
                
                # Parse title for "From..." clauses (common in Indian music)
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
                artist_name = clean_html_entities(artist_name)
                
                song_list.append(
                    {
                        "title": title.strip(),
                        "album": album.strip(),
                        "artist": artist_name.strip(),
                    }
                )
            except (KeyError, IndexError, AttributeError) as exc:
                logger.debug(f"Error processing Gaana track: {exc}")
                continue

        if song_list and cache:
            cache.set_playlist_cache(playlist_id, "gaana", song_list)
            logger.info(f"Cached {len(song_list)} tracks from Gaana playlist")

    except Exception as exc:
        logger.error(f"Error importing Gaana playlist: {exc}")
        return []

    return song_list
