import logging
import requests

_log = logging.getLogger('beets.plexsync.post')

def import_post_playlist(source_config, cache=None):
    """Import playlist from a POST request endpoint with caching.

    Args:
        source_config: Dictionary containing server_url, headers, and payload
        cache: Cache object for storing results

    Returns:
        list: List of song dictionaries
    """
    # Generate cache key from URL in payload
    playlist_url = source_config.get("payload", {}).get("playlist_url")
    if not playlist_url:
        _log.error("No playlist_url provided in POST request payload")
        return []

    playlist_id = playlist_url.split('/')[-1]

    # Check cache
    if cache:
        cached_data = cache.get_playlist_cache(playlist_id, 'post')
        if cached_data:
            _log.info("Using cached POST request playlist data")
            return cached_data

    server_url = source_config.get("server_url")
    if not server_url:
        _log.error("No server_url provided for POST request")
        return []

    headers = source_config.get("headers", {})
    payload = source_config.get("payload", {})

    try:
        response = requests.post(server_url, headers=headers, json=payload)
        response.raise_for_status()  # Raise exception for non-200 status codes

        data = response.json()
        if not isinstance(data, dict) or "song_list" not in data:
            _log.error("Invalid response format. Expected 'song_list' in JSON response")
            return []

        # Convert response to our standard format
        song_list = []
        for song in data["song_list"]:
            song_dict = {
                "title": song.get("title", "").strip(),
                "artist": song.get("artist", "").strip(),
                "album": song.get("album", "").strip() if song.get("album") else None,
            }
            # Add year if available
            if "year" in song and song["year"]:
                try:
                    year = int(song["year"])
                    song_dict["year"] = year
                except (ValueError, TypeError):
                    pass

            if song_dict["title"] and song_dict["artist"]:  # Only add if we have minimum required fields
                song_list.append(song_dict)

        # Cache successful results
        if song_list and cache:
            cache.set_playlist_cache(playlist_id, 'post', song_list)
            _log.info("Cached {} tracks from POST request playlist", len(song_list))

        return song_list

    except requests.exceptions.RequestException as e:
        _log.error("Error making POST request: {}", e)
        return []
    except ValueError as e:
        _log.error("Error parsing JSON response: {}", e)
        return []
    except Exception as e:
        _log.error("Unexpected error during POST request: {}", e)
        return []