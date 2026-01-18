"""Qobuz playlist importer for Harmony (web scraping)."""

import json
import logging
import re
from typing import List, Dict, Any, Optional

import requests
from bs4 import BeautifulSoup

from harmony.utils.parsing import parse_soundtrack_title, clean_album_name, clean_html_entities

logger = logging.getLogger("harmony.providers.qobuz")


def extract_playlist_id(url: str) -> Optional[str]:
    """Extract Qobuz playlist ID from URL.
    
    Supports:
    - https://www.qobuz.com/gb-en/playlists/bollywood/22893019
    - https://www.qobuz.com/{lang}/playlists/{category}/{id}
    
    Args:
        url: Qobuz playlist URL
    
    Returns:
        Playlist ID (numeric string), or None if not found
    """
    try:
        match = re.search(r"/playlists/([^/]+)/(\d+)", url)
        if match:
            return match.group(2)  # Return the numeric ID
    except Exception as e:
        logger.debug(f"Error extracting Qobuz playlist ID: {e}")
    return None


def import_qobuz_playlist(
    url: str,
    cache: Optional[Any] = None,
    headers: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Import Qobuz playlist via web scraping.
    
    Args:
        url: Qobuz playlist URL
        cache: Cache object for storing results
        headers: HTTP headers for request
    
    Returns:
        List of track dictionaries with title, artist, album
    """
    playlist_id = extract_playlist_id(url)
    if not playlist_id:
        logger.error(f"Could not extract Qobuz playlist ID from: {url}")
        return []
    
    if cache:
        cached_data = cache.get_playlist_cache(playlist_id, "qobuz")
        if cached_data:
            logger.info("Using cached Qobuz playlist data")
            return cached_data
    
    if headers is None:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/91.0.4472.124 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    
    song_list: List[Dict[str, Any]] = []
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Try to find JSON data in script tags (similar to Apple Music approach)
        found_data = False
        for script in soup.find_all("script"):
            if not script.string:
                continue
            
            script_str = str(script.string)
            
            # Look for playlist data patterns
            if "playlist" in script_str.lower() or "tracks" in script_str.lower():
                try:
                    # Try to extract JSON from script
                    # Find JSON object
                    start = script_str.find("{")
                    if start == -1:
                        continue
                    
                    # Try to parse JSON
                    json_data = json.loads(script_str[start:])
                    
                    # Extract tracks from JSON structure
                    tracks = _extract_tracks_from_json(json_data)
                    if tracks:
                        song_list.extend(tracks)
                        found_data = True
                        break
                except (json.JSONDecodeError, ValueError) as e:
                    logger.debug(f"Error parsing Qobuz JSON: {e}")
                    continue
        
        if not found_data:
            # Fallback: Try parsing HTML table/list structure
            tracks = _extract_tracks_from_html(soup)
            song_list.extend(tracks)
        
        # Clean and normalize tracks
        for track in song_list:
            if "title" in track:
                track["title"] = clean_html_entities(track["title"])
                track["title"], movie_name = parse_soundtrack_title(track["title"])
            if "artist" in track:
                track["artist"] = clean_html_entities(track["artist"])
            if "album" in track and track["album"]:
                track["album"] = clean_html_entities(track["album"])
                track["album"] = clean_album_name(track["album"])
        
        if song_list and cache:
            cache.set_playlist_cache(playlist_id, "qobuz", song_list)
            logger.info(f"Cached {len(song_list)} tracks from Qobuz playlist")
        
        logger.info(f"Imported {len(song_list)} tracks from Qobuz playlist: {playlist_id}")
        
    except Exception as exc:
        logger.error(f"Error importing Qobuz playlist: {exc}")
        return []
    
    return song_list


def _extract_tracks_from_json(data: Any) -> List[Dict[str, Any]]:
    """Extract tracks from Qobuz JSON data.
    
    Recursively searches for track arrays in JSON structure.
    
    Args:
        data: JSON data (dict or list)
    
    Returns:
        List of track dictionaries
    """
    tracks = []
    
    # Recursively search for track arrays
    if isinstance(data, dict):
        # Look for common track list keys
        for key in ["tracks", "items", "playlistItems", "songs", "track"]:
            if key in data and isinstance(data[key], list):
                for item in data[key]:
                    track = _parse_track_item(item)
                    if track:
                        tracks.append(track)
                if tracks:
                    break
        
        # Recurse into nested structures
        if not tracks:
            for value in data.values():
                if isinstance(value, (dict, list)):
                    tracks.extend(_extract_tracks_from_json(value))
                    if tracks:
                        break
    
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                tracks.extend(_extract_tracks_from_json(item))
                if tracks:
                    break
    
    return tracks


def _parse_track_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Parse a single track item from JSON.
    
    Args:
        item: Track data dictionary
    
    Returns:
        Normalized track dictionary, or None if invalid
    """
    if not isinstance(item, dict):
        return None
    
    # Try common field names for title
    title = (
        item.get("title") or
        item.get("name") or
        item.get("track_title") or
        item.get("trackName")
    )
    
    # Try common field names for artist
    artist = None
    if "artist" in item:
        artist_data = item["artist"]
        if isinstance(artist_data, dict):
            artist = artist_data.get("name") or artist_data.get("performer")
        elif isinstance(artist_data, str):
            artist = artist_data
    
    if not artist:
        artist = (
            item.get("artist_name") or
            item.get("performer") or
            item.get("interpret", {}).get("name") if isinstance(item.get("interpret"), dict) else item.get("interpret")
        )
    
    # Try common field names for album
    album = None
    if "album" in item:
        album_data = item["album"]
        if isinstance(album_data, dict):
            album = album_data.get("title") or album_data.get("name")
        elif isinstance(album_data, str):
            album = album_data
    
    if not album:
        album = (
            item.get("album_title") or
            item.get("release_title")
        )
    
    if title:
        return {
            "title": title.strip(),
            "artist": artist.strip() if artist else "Unknown Artist",
            "album": album.strip() if album else None,
        }
    return None


def _extract_tracks_from_html(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """Extract tracks from Qobuz HTML structure (fallback).
    
    This is a best-effort fallback since HTML structure may vary.
    
    Args:
        soup: BeautifulSoup object of playlist page
    
    Returns:
        List of track dictionaries
    """
    tracks = []
    
    # Look for table rows or list items containing track info
    # Try various patterns as Qobuz may use different structures
    for element in soup.find_all(["tr", "li", "div"], class_=re.compile(r"track|item", re.I)):
        # Try to find track info patterns
        artist_elem = element.find(["span", "a", "div"], class_=re.compile(r"artist", re.I))
        title_elem = element.find(["span", "a", "div"], class_=re.compile(r"title|track", re.I))
        
        if artist_elem and title_elem:
            artist = artist_elem.get_text(strip=True)
            title = title_elem.get_text(strip=True)
            
            if artist and title:
                tracks.append({
                    "title": title,
                    "artist": artist,
                    "album": None
                })
    
    return tracks
