import json
import logging
import requests
from bs4 import BeautifulSoup

_log = logging.getLogger('beets.plexsync.apple')

def import_apple_playlist(url, cache=None, headers=None):
    """Import Apple Music playlist with caching.

    Args:
        url: URL of the Apple Music playlist
        cache: Cache object for storing results
        headers: HTTP headers for the request

    Returns:
        list: List of song dictionaries
    """
    # Generate cache key from URL
    playlist_id = url.split('/')[-1]

    # Check cache
    if cache:
        cached_data = cache.get_playlist_cache(playlist_id, 'apple')
        if (cached_data):
            _log.info(f"Using cached Apple Music playlist data")
            return cached_data

    if headers is None:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 0.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "DNT": "1",  # Do Not Track Request Header
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
        }

    song_list = []

    try:
        # Send a GET request to the URL and get the HTML content
        response = requests.get(url, headers=headers)
        content = response.text

        # Create a BeautifulSoup object with the HTML content
        soup = BeautifulSoup(content, "html.parser")
        try:
            data = soup.find("script", id="serialized-server-data").text
        except AttributeError:
            _log.debug(f"Error parsing Apple Music playlist")
            return None

        # load the data as a JSON object
        data = json.loads(data)

        # Extract songs from the sections
        try:
            songs = data[0]["data"]["sections"][1]["items"]
        except (KeyError, IndexError) as e:
            _log.error(f"Failed to extract songs from Apple Music data: {e}")
            return None

        # Loop through each song element
        for song in songs:
            try:
                # Find and store the song title
                title = song["title"].strip()
                album = song["tertiaryLinks"][0]["title"]
                # Find and store the song artist
                artist = song["subtitleLinks"][0]["title"]
                # Create a dictionary with the song information
                song_dict = {
                    "title": title.strip(),
                    "album": album.strip(),
                    "artist": artist.strip(),
                }
                # Append the dictionary to the list of songs
                song_list.append(song_dict)
            except (KeyError, IndexError) as e:
                _log.debug(f"Error processing song {song.get('title', 'Unknown')}: {e}")
                continue

        if song_list and cache:
            cache.set_playlist_cache(playlist_id, 'apple', song_list)
            _log.info(f"Cached {len(song_list)} tracks from Apple Music playlist")

    except Exception as e:
        _log.error(f"Error importing Apple Music playlist: {e}")
        return []

    return song_list