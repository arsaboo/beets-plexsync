import logging
import os
from pathlib import Path

_log = logging.getLogger('beets.plexsync.m3u8')

def import_m3u8_playlist(filepath, cache=None):
    """Import M3U8 playlist with caching.

    Args:
        filepath: Path to the M3U8 file
        cache: Cache object for storing results

    Returns:
        list: List of song dictionaries
    """
    playlist_id = str(Path(filepath).stem)

    if cache:
        cached_data = cache.get_playlist_cache(playlist_id, 'm3u8')
        if cached_data:
            _log.info("Using cached M3U8 playlist data")
            return cached_data

    song_list = []

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = [line.strip() for line in f if line.strip()]

        i = 0
        while i < len(lines):
            line = lines[i]

            if line.startswith('#EXTINF:'):
                meta = line.split(',', 1)[1]
                _log.debug("EXTINF meta raw line: '{}'", meta)

                if ' - ' in meta:
                    artist, title = meta.split(' - ', 1)
                    artist, title = artist.strip(), title.strip()
                    _log.debug("Parsed EXTINF as artist='{}', title='{}'", artist, title)
                else:
                    _log.warning("EXTINF missing '-': '{}'", meta)
                    artist, title = None, None

                current_song = {
                    'artist': artist,
                    'title': title,
                    'album': None
                }

                # Optional EXTALB line
                next_idx = i + 1
                if next_idx < len(lines) and lines[next_idx].startswith('#EXTALB:'):
                    album = lines[next_idx][8:].strip()
                    current_song['album'] = album if album else None
                    _log.debug("Found album: '{}'", current_song['album'])
                    next_idx += 1

                # Optional file path (we'll skip)
                if next_idx < len(lines) and not lines[next_idx].startswith('#'):
                    next_idx += 1

                # Log before appending:
                _log.debug("Appending song entry: {}", current_song)

                song_list.append(current_song.copy())
                i = next_idx - 1  # Set to the last processed line

            i += 1

        if song_list and cache:
            cache.set_playlist_cache(playlist_id, 'm3u8', song_list)
            _log.info("Cached {} tracks from M3U8 playlist", len(song_list))

        return song_list

    except Exception as e:
        _log.error("Error importing M3U8 playlist '{}': {}", filepath, e)
        return []