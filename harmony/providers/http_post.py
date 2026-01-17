"""HTTP POST playlist importer for Harmony."""

import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("harmony.providers.post")


def import_post_playlist(
    source_config: Dict[str, Any],
    cache: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Import playlist from a POST request endpoint with caching.

    Args:
        source_config: Dict containing server_url, headers, and payload
        cache: Cache object for storing results

    Returns:
        List of song dictionaries
    """
    playlist_url = source_config.get("payload", {}).get("playlist_url")
    if not playlist_url:
        logger.error("No playlist_url provided in POST request payload")
        return []

    playlist_id = playlist_url.split("/")[-1]

    if cache:
        cached_data = cache.get_playlist_cache(playlist_id, "post")
        if cached_data:
            logger.info("Using cached POST request playlist data")
            return cached_data

    server_url = source_config.get("server_url")
    if not server_url:
        logger.error("No server_url provided for POST request")
        return []

    headers = source_config.get("headers", {})
    payload = source_config.get("payload", {})

    try:
        response = requests.post(server_url, headers=headers, json=payload, timeout=20)
        response.raise_for_status()

        data = response.json()
        if not isinstance(data, dict) or "song_list" not in data:
            logger.error("Invalid response format. Expected 'song_list' in JSON response")
            return []

        song_list: List[Dict[str, Any]] = []
        for song in data["song_list"]:
            song_dict = {
                "title": song.get("title", "").strip(),
                "artist": song.get("artist", "").strip(),
                "album": song.get("album", "").strip() if song.get("album") else None,
            }
            if "year" in song and song["year"]:
                try:
                    song_dict["year"] = int(song["year"])
                except (ValueError, TypeError):
                    pass

            if song_dict["title"] and song_dict["artist"]:
                song_list.append(song_dict)

        if song_list and cache:
            cache.set_playlist_cache(playlist_id, "post", song_list)
            logger.info(f"Cached {len(song_list)} tracks from POST playlist source")

        return song_list

    except requests.exceptions.RequestException as exc:
        logger.error(f"Error making POST request: {exc}")
        return []
    except ValueError as exc:
        logger.error(f"Error parsing JSON response: {exc}")
        return []
    except Exception as exc:
        logger.error(f"Unexpected error during POST request: {exc}")
        return []
