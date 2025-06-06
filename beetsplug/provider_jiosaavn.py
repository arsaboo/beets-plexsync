import logging
import re
import asyncio
from jiosaavn import JioSaavn
from beetsplug.helpers import parse_title, clean_album_name

_log = logging.getLogger('beets.plexsync.jiosaavn')

# Create JioSaavn instance
saavn = JioSaavn()

async def get_playlist_songs(playlist_url):
    """Get playlist songs by URL.

    Args:
        playlist_url: URL of the JioSaavn playlist

    Returns:
        dict: JioSaavn API response with playlist data
    """
    # Use the async method from saavn
    songs = await saavn.get_playlist_songs(playlist_url)
    # Return a list of songs with details
    return songs


def import_jiosaavn_playlist(url, cache=None):
    """Import JioSaavn playlist with caching.

    Args:
        url: URL of the JioSaavn playlist
        cache: Cache object for storing results

    Returns:
        list: List of song dictionaries
    """
    playlist_id = url.split('/')[-1]

    # Check cache first
    if cache:
        cached_data = cache.get_playlist_cache(playlist_id, 'jiosaavn')
        if cached_data:
            _log.info("Using cached JioSaavn playlist data")
            return cached_data

    song_list = []

    try:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # If already running, schedule the coroutine and wait for result
                import concurrent.futures
                future = asyncio.ensure_future(get_playlist_songs(url))
                # Use run_coroutine_threadsafe for thread-safe execution
                data = asyncio.run_coroutine_threadsafe(get_playlist_songs(url), loop).result()
            else:
                # If not running, just run until complete
                data = loop.run_until_complete(get_playlist_songs(url))
        except (RuntimeError, AssertionError):
            # No event loop or closed, create a new one
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            data = new_loop.run_until_complete(get_playlist_songs(url))
            new_loop.close()

        if not data or "data" not in data or "list" not in data["data"]:
            _log.error("Invalid response from JioSaavn API")
            return song_list

        songs = data["data"]["list"]

        for song in songs:
            try:
                # Process song title
                if ('From "' in song["title"]) or ("From &quot" in song["title"]):
                    title_orig = song["title"].replace("&quot;", '"')
                    title, album = parse_title(title_orig)
                else:
                    title = song["title"]
                    album = clean_album_name(song["more_info"]["album"])

                # Get year if available
                year = song.get("year", None)

                # Get primary artist from artistMap
                try:
                    artist = song["more_info"]["artistMap"]["primary_artists"][0]["name"]
                except (KeyError, IndexError):
                    # Fallback to first featured artist if primary not found
                    try:
                        artist = song["more_info"]["artistMap"]["featured_artists"][0]["name"]
                    except (KeyError, IndexError):
                        # Skip if no artist found
                        continue

                # Create song dictionary with cleaned data
                song_dict = {
                    "title": title.strip(),
                    "album": album.strip(),
                    "artist": artist.strip(),
                    "year": year,
                }

                song_list.append(song_dict)
                _log.debug(f"Added song: {song_dict['title']} - {song_dict['artist']}")

            except Exception as e:
                _log.debug(f"Error processing JioSaavn song: {e}")
                continue

        # Cache successful results
        if song_list and cache:
            cache.set_playlist_cache(playlist_id, 'jiosaavn', song_list)
            _log.info(f"Cached {len(song_list)} tracks from JioSaavn playlist")

    except Exception as e:
        _log.error(f"Error importing JioSaavn playlist: {e}")

    return song_list