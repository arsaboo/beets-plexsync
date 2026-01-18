"""Helpers for importing playlists into a backend.

Works with provider modules to import from various sources.
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger("harmony.playlist_import")


def add_songs_to_playlist(
    harmony_app,
    playlist_name: str,
    songs: List[Dict[str, Any]],
    manual_search: bool = False,
    clear_first: bool = False,
    log_file: Optional[str] = None
) -> int:
    """Add a list of songs to a backend playlist.

    Args:
        harmony_app: Harmony app instance with search_song method
        playlist_name: Name of the playlist to create/update
        songs: List of song dicts with title, artist, album
        manual_search: Whether to use manual search for unmatched tracks
        clear_first: Whether to clear playlist before adding
        log_file: Optional path to log file for import results

    Returns:
        Number of tracks successfully added
    """
    if not songs:
        logger.warning(f"No songs to add to {playlist_name}")
        return 0

    logger.info(f"Processing {len(songs)} songs for {playlist_name}")

    backend = getattr(harmony_app, "backend", None) or getattr(harmony_app, "plex", None)
    if backend is None:
        logger.error("No backend available for playlist import")
        return 0

    # Create log file if not provided
    if log_file is None:
        log_dir = getattr(harmony_app.config, 'config_dir', os.path.expanduser('~/.config/harmony'))
        os.makedirs(log_dir, exist_ok=True)
        sanitized_name = playlist_name.lower().replace(' ', '_').replace('/', '_')
        log_file = os.path.join(log_dir, f"{sanitized_name}_import.log")

    # Initialize log file with header
    start_time = datetime.now()
    with open(log_file, 'w', encoding='utf-8') as f:
        f.write(f"# Playlist Import Log: {playlist_name}\n")
        f.write(f"# Started: {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# Total tracks: {len(songs)}\n")
        f.write(f"# Manual search: {manual_search}\n")
        f.write("#\n")
        f.write("# Format: Artist | Album | Title\n")
        f.write("#         (Pipe-separated to handle dashes in metadata)\n")
        f.write("#         (Album may be 'Unknown' if not available)\n")
        f.write("#\n\n")

    song_list = []
    failed_tracks = []

    # Create progress bar using enlighten
    progress_bar = harmony_app.create_progress_counter(
        total=len(songs),
        desc=f"Importing {playlist_name}",
        unit="track"
    )

    try:
        for i, song in enumerate(songs):
            try:
                found = harmony_app.search_song(song, manual_search=manual_search)
                if found:
                    song_list.append(found)
                else:
                    # Log failed track
                    artist = song.get('artist', 'Unknown Artist')
                    album = song.get('album', 'Unknown')
                    title = song.get('title', 'Unknown Title')
                    failed_tracks.append({
                        'artist': artist,
                        'album': album,
                        'title': title
                    })
                    logger.debug(f"Not found: {artist} - {album} - {title}")

                # Update progress bar
                if progress_bar:
                    progress_bar.update()

            except Exception as e:
                logger.error(f"Error processing song {i+1}: {e}")
                if progress_bar:
                    progress_bar.update()
                continue
    finally:
        if progress_bar:
            progress_bar.close()

    # Write failed tracks to log
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write(f"# Successfully matched: {len(song_list)}/{len(songs)}\n")
        f.write(f"# Failed to match: {len(failed_tracks)}\n")
        f.write(f"#\n\n")

        if failed_tracks:
            f.write("# FAILED TRACKS (not found in backend)\n")
            f.write("#\n")
            for track in failed_tracks:
                artist = track['artist']
                album = track['album']
                title = track['title']
                f.write(f"Not found: {artist} | {album} | {title}\n")
                if 'error' in track:
                    f.write(f"  Error: {track['error']}\n")
        else:
            f.write("# All tracks matched successfully!\n")

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        f.write(f"\n# Completed: {end_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# Duration: {duration:.1f} seconds\n")

    if not song_list:
        logger.warning(f"No songs matched for {playlist_name}")
        logger.info(f"Import log saved to: {log_file}")
        return 0

    # Add to backend
    try:
        if clear_first:
            backend.clear_playlist(playlist_name)
        added_count = backend.add_tracks_to_playlist(playlist_name, song_list)
        logger.info(f"Successfully added {added_count} tracks to {playlist_name}")
        logger.info(f"Import log saved to: {log_file}")
        return added_count
    except Exception as e:
        logger.error(f"Error adding songs to {playlist_name}: {e}")
        return 0


def import_from_url(
    harmony_app,
    playlist_name: str,
    url: str,
    manual_search: bool = False
) -> int:
    """Import a playlist from a URL (Spotify, YouTube, Apple Music, Qobuz, etc).

    Args:
        harmony_app: Harmony app instance
        playlist_name: Name of the playlist to create
        url: URL of the playlist to import
        manual_search: Whether to use manual search

    Returns:
        Number of tracks imported
    """
    if not url or ("http://" not in url and "https://" not in url):
        raise ValueError(f"Invalid playlist URL: {url}")

    logger.info(f"Importing from URL: {url}")

    # Determine source type and import
    songs = []
    try:
        if "spotify" in url.lower():
            from harmony.providers import import_spotify_playlist
            # Get Spotify credentials from config
            spotify_config = getattr(harmony_app.config, 'providers', {})
            if isinstance(spotify_config, dict):
                spotify_creds = spotify_config.get('spotify', {})
            else:
                spotify_creds = getattr(spotify_config, 'spotify', {}).model_dump() if hasattr(spotify_config, 'spotify') else {}

            client_id = spotify_creds.get('client_id') if isinstance(spotify_creds, dict) else getattr(spotify_creds, 'client_id', None)
            client_secret = spotify_creds.get('client_secret') if isinstance(spotify_creds, dict) else getattr(spotify_creds, 'client_secret', None)

            songs = import_spotify_playlist(url, harmony_app.cache, client_id, client_secret)
        elif "youtube" in url.lower():
            from harmony.providers import import_yt_playlist
            songs = import_yt_playlist(url, harmony_app.cache)
        elif "apple" in url.lower():
            from harmony.providers import import_apple_playlist
            songs = import_apple_playlist(url, harmony_app.cache)
        elif "tidal" in url.lower():
            from harmony.providers import import_tidal_playlist
            songs = import_tidal_playlist(url, harmony_app.cache)
        elif "gaana" in url.lower():
            from harmony.providers import import_gaana_playlist
            songs = import_gaana_playlist(url, harmony_app.cache)
        elif "jiosaavn" in url.lower():
            from harmony.providers import import_jiosaavn_playlist
            songs = import_jiosaavn_playlist(url, harmony_app.cache)
        elif "qobuz" in url.lower():
            from harmony.providers import import_qobuz_playlist
            songs = import_qobuz_playlist(url, harmony_app.cache)
        else:
            logger.error(f"Unsupported playlist URL source: {url}")
            return 0
    except Exception as e:
        logger.error(f"Error importing from {url}: {e}")
        return 0

    if not songs:
        logger.warning(f"No songs imported from {url}")
        return 0

    logger.info(f"Imported {len(songs)} songs from {url}")

    # Add to backend
    return add_songs_to_playlist(
        harmony_app,
        playlist_name,
        songs,
        manual_search=manual_search
    )


def import_from_file(
    harmony_app,
    playlist_name: str,
    filepath: str,
    manual_search: bool = False
) -> int:
    """Import a playlist from a local file (M3U8, etc).

    Args:
        harmony_app: Harmony app instance
        playlist_name: Name of the playlist to create
        filepath: Path to the playlist file
        manual_search: Whether to use manual search

    Returns:
        Number of tracks imported
    """
    import os

    filepath = _resolve_m3u8_path(harmony_app, filepath, playlist_name)
    logger.info(f"Importing from file: {filepath}")

    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")

    songs = []
    try:
        if filepath.lower().endswith('.m3u8'):
            from harmony.providers import import_m3u8_playlist
            songs = import_m3u8_playlist(filepath, harmony_app.cache)
        else:
            logger.error(f"Unsupported file type: {filepath}")
            return 0
    except Exception as e:
        logger.error(f"Error importing from {filepath}: {e}")
        return 0

    if not songs:
        logger.warning(f"No songs imported from {filepath}")
        return 0

    logger.info(f"Imported {len(songs)} songs from {filepath}")

    # Add to backend
    return add_songs_to_playlist(
        harmony_app,
        playlist_name,
        songs,
        manual_search=manual_search
    )


def import_playlist_from_config(
    harmony_app,
    playlist_config: Dict[str, Any]
) -> int:
    """Import a playlist using configuration dict.

    Args:
        harmony_app: Harmony app instance
        playlist_config: Dict with keys:
            - name: Playlist name
            - sources: List of URLs/filepaths or source dicts
            - manual_search: Whether to use manual search
            - clear_playlist: Whether to clear existing playlist first
            - max_tracks: Max number of tracks (optional)
            - filters: Filter config (optional)

    Returns:
        Number of tracks imported
    """
    playlist_name = playlist_config.get("name", "Imported Playlist")
    sources = playlist_config.get("sources", [])
    manual_search = playlist_config.get("manual_search", False)
    clear_playlist = playlist_config.get("clear_playlist", False)
    max_tracks = playlist_config.get("max_tracks")
    filters = playlist_config.get("filters")

    logger.info(f"Generating imported playlist: {playlist_name}")
    logger.info(f"Processing {len(sources)} sources")

    if not sources:
        logger.warning(f"No sources defined for {playlist_name}")
        return 0

    # Collect all songs from sources
    all_songs = []
    for source in sources:
        try:
            songs = []
            if isinstance(source, str):
                # URL or filepath
                if source.startswith("http://") or source.startswith("https://"):
                    songs = _import_from_url_internal(harmony_app, source)
                elif source.endswith(".m3u8"):
                    songs = _import_from_file_internal(harmony_app, source, playlist_name)
                else:
                    logger.warning(f"Unknown source format: {source}")
            elif isinstance(source, dict):
                # Typed source
                source_type = source.get("type", "").lower()
                if source_type in ["spotify", "youtube", "apple", "tidal", "gaana", "jiosaavn"]:
                    songs = _import_from_url_internal(harmony_app, source.get("url", ""))
                elif source_type == "m3u8":
                    songs = _import_from_file_internal(
                        harmony_app,
                        source.get("filepath", ""),
                        playlist_name,
                    )
                elif source_type == "post":
                    from harmony.providers.http_post import import_post_playlist
                    songs = import_post_playlist(source, harmony_app.cache)
                else:
                    logger.warning(f"Unknown source type: {source_type}")
            else:
                logger.warning(f"Invalid source format: {source}")

            if songs:
                logger.info(f"Imported {len(songs)} from source")
                all_songs.extend(songs)
        except Exception as e:
            logger.error(f"Error importing from source: {e}")
            continue

    logger.info(f"Collected {len(all_songs)} total songs")

    # De-duplicate by (title, artist, album)
    unique_songs = []
    seen = set()
    for song in all_songs:
        key = (
            (song.get("title") or "").lower(),
            (song.get("artist") or "").lower(),
            (song.get("album") or "").lower(),
        )
        if key not in seen:
            seen.add(key)
            unique_songs.append(song)

    logger.info(f"After de-duplication: {len(unique_songs)} songs")

    # Limit tracks if specified
    if max_tracks and len(unique_songs) > max_tracks:
        unique_songs = unique_songs[:max_tracks]
        logger.info(f"Truncated to {max_tracks} tracks")

    # Add to backend
    return add_songs_to_playlist(
        harmony_app,
        playlist_name,
        unique_songs,
        manual_search=manual_search,
        clear_first=clear_playlist
    )


def _import_from_url_internal(harmony_app, url: str) -> List[Dict[str, Any]]:
    """Internal helper to import from URL."""
    try:
        if "spotify" in url.lower():
            from harmony.providers.spotify import import_spotify_playlist
            return import_spotify_playlist(url, harmony_app.cache)
        elif "youtube" in url.lower():
            from harmony.providers.youtube import import_yt_playlist
            return import_yt_playlist(url, harmony_app.cache)
        elif "apple" in url.lower():
            from harmony.providers.apple import import_apple_playlist
            return import_apple_playlist(url, harmony_app.cache)
        elif "tidal" in url.lower():
            from harmony.providers.tidal import import_tidal_playlist
            return import_tidal_playlist(url, harmony_app.cache)
        elif "gaana" in url.lower():
            from harmony.providers.gaana import import_gaana_playlist
            return import_gaana_playlist(url, harmony_app.cache)
        elif "jiosaavn" in url.lower():
            from harmony.providers.jiosaavn import import_jiosaavn_playlist
            return import_jiosaavn_playlist(url, harmony_app.cache)
        elif "qobuz" in url.lower():
            from harmony.providers.qobuz import import_qobuz_playlist
            return import_qobuz_playlist(url, harmony_app.cache)
    except Exception as e:
        logger.error(f"Error importing from {url}: {e}")
    return []


def _import_from_file_internal(
    harmony_app,
    filepath: str,
    playlist_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Internal helper to import from file."""
    try:
        filepath = _resolve_m3u8_path(harmony_app, filepath, playlist_name)
        if filepath.lower().endswith(".m3u8"):
            from harmony.providers.m3u8 import import_m3u8_playlist
            return import_m3u8_playlist(filepath, harmony_app.cache)
    except Exception as e:
        logger.error(f"Error importing from {filepath}: {e}")
    return []


def _resolve_m3u8_path(
    harmony_app,
    filepath: str,
    playlist_name: Optional[str] = None,
) -> str:
    if not filepath:
        if playlist_name:
            filepath = f"{playlist_name}.m3u8"
        else:
            return filepath

    if os.path.isabs(filepath):
        return filepath

    providers = getattr(harmony_app.config, "providers", None)
    base_dir = None
    if providers and hasattr(providers, "m3u8_dir"):
        base_dir = providers.m3u8_dir

    if base_dir:
        return os.path.join(base_dir, filepath)
    return filepath


def process_import_logs(
    harmony_app,
    log_files: Optional[List[str]] = None,
    playlist_name: Optional[str] = None
) -> Dict[str, int]:
    """Process import log files and retry failed tracks with manual search.

    Args:
        harmony_app: Harmony app instance
        log_files: List of log file paths to process (optional)
        playlist_name: If provided, find and process log for this playlist name

    Returns:
        Dict with statistics: processed, matched, failed
    """
    if log_files is None:
        log_files = []

        # If playlist_name provided, find its log file
        if playlist_name:
            log_dir = getattr(harmony_app.config, 'config_dir', os.path.expanduser('~/.config/harmony'))
            sanitized_name = playlist_name.lower().replace(' ', '_').replace('/', '_')
            log_path = os.path.join(log_dir, f"{sanitized_name}_import.log")
            if os.path.exists(log_path):
                log_files.append(log_path)
            else:
                logger.warning(f"Log file not found: {log_path}")
                return {'processed': 0, 'matched': 0, 'failed': 0}
        else:
            # Find all import logs in config directory
            log_dir = getattr(harmony_app.config, 'config_dir', os.path.expanduser('~/.config/harmony'))
            if os.path.exists(log_dir):
                log_files = [
                    os.path.join(log_dir, f)
                    for f in os.listdir(log_dir)
                    if f.endswith('_import.log')
                ]

    if not log_files:
        logger.warning("No import logs found to process")
        return {'processed': 0, 'matched': 0, 'failed': 0}

    total_processed = 0
    total_matched = 0
    total_failed = 0

    for log_file in log_files:
        logger.info(f"Processing import log: {log_file}")

        # Parse failed tracks from log
        failed_tracks = []
        playlist_name_from_log = None

        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()

                    # Extract playlist name from header
                    if line.startswith("# Playlist Import Log:"):
                        playlist_name_from_log = line.split(":", 1)[1].strip()

                    # Parse failed track lines
                    if line.startswith("Not found:"):
                        # Format: "Not found: Artist | Album | Title"
                        parts = line[10:].strip().split(' | ', 2)  # Skip "Not found: "
                        if len(parts) == 3:
                            artist, album, title = parts
                            # Handle "Unknown" album
                            if album.strip() == "Unknown":
                                album = ""
                            failed_tracks.append({
                                'artist': artist.strip(),
                                'album': album.strip(),
                                'title': title.strip()
                            })
                        else:
                            logger.debug(f"Could not parse failed track line: {line}")
        except Exception as e:
            logger.error(f"Error reading log file {log_file}: {e}")
            continue

        if not failed_tracks:
            logger.info(f"No failed tracks found in {log_file}")
            continue

        if not playlist_name_from_log:
            logger.warning(f"Could not determine playlist name from {log_file}")
            continue

        logger.info(f"Found {len(failed_tracks)} failed tracks for {playlist_name_from_log}")
        logger.info("Retrying with manual search enabled...")

        # Retry with manual search
        matched_tracks = []
        still_failed = []

        for i, track in enumerate(failed_tracks):
            try:
                found = harmony_app.search_song(track, manual_search=True)
                if found:
                    matched_tracks.append(found)
                    logger.info(f"Matched: {track['artist']} - {track['title']}")
                else:
                    still_failed.append(track)

                if (i + 1) % max(1, len(failed_tracks) // 10) == 0:
                    logger.debug(f"Progress: {i+1}/{len(failed_tracks)} retried")
            except Exception as e:
                logger.warning(f"Error retrying {track.get('title', 'Unknown')}: {e}")
                still_failed.append(track)

        total_processed += len(failed_tracks)
        total_matched += len(matched_tracks)
        total_failed += len(still_failed)

        # Add matched tracks to playlist
        if matched_tracks:
            try:
                backend = getattr(harmony_app, "backend", None) or getattr(harmony_app, "plex", None)
                if backend is None:
                    raise RuntimeError("No backend available for playlist updates")
                backend.add_tracks_to_playlist(playlist_name_from_log, matched_tracks)
                logger.info(f"Added {len(matched_tracks)} newly matched tracks to {playlist_name_from_log}")
            except Exception as e:
                logger.error(f"Error adding tracks to playlist: {e}")

        # Update log file with remaining failed tracks
        backup_file = log_file + '.backup'
        try:
            os.rename(log_file, backup_file)

            with open(log_file, 'w', encoding='utf-8') as f:
                # Write new header
                f.write(f"# Playlist Import Log: {playlist_name_from_log}\n")
                f.write(f"# Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"# Original failed: {len(failed_tracks)}\n")
                f.write(f"# Newly matched: {len(matched_tracks)}\n")
                f.write(f"# Still failed: {len(still_failed)}\n")
                f.write("#\n")
                f.write("# Format: Artist | Album | Title\n")
                f.write("#\n\n")

                if still_failed:
                    f.write("# FAILED TRACKS (not found in backend)\n")
                    f.write("#\n")
                    for track in still_failed:
                        artist = track['artist']
                        album = track['album'] if track['album'] else 'Unknown'
                        title = track['title']
                        f.write(f"Not found: {artist} | {album} | {title}\n")
                else:
                    f.write("# All tracks matched successfully after retry!\n")
                    f.write("# You can safely delete this log file.\n")

            logger.info(f"Updated log file: {log_file}")
            if len(still_failed) == 0:
                logger.info(f"All tracks matched! Consider deleting: {log_file}")
        except Exception as e:
            logger.error(f"Error updating log file: {e}")
            # Restore backup if update failed
            if os.path.exists(backup_file):
                os.rename(backup_file, log_file)

    logger.info(f"Processing complete: {total_matched}/{total_processed} tracks matched")
    return {
        'processed': total_processed,
        'matched': total_matched,
        'failed': total_failed
    }


def add_songs_to_plex(*args, **kwargs) -> int:
    """Backward-compatible alias."""
    return add_songs_to_playlist(*args, **kwargs)


def batch_import_playlists(
    harmony_app,
    playlist_configs: List[Dict[str, Any]],
    manual_search: bool = False
) -> Dict[str, Any]:
    """Import multiple playlists efficiently by batching fetch and match operations.

    This function optimizes playlist imports by:
    1. Fetching all playlists in parallel (with caching)
    2. Deduplicating songs across playlists
    3. Matching all unique songs in a single batch (better cache utilization)
    4. Building all playlists from matched results

    Args:
        harmony_app: Harmony app instance
        playlist_configs: List of playlist config dicts with 'name' and 'sources'
        manual_search: Whether to use manual search for unmatched tracks

    Returns:
        Dict with results: {
            'playlists': [{'name': str, 'tracks': int, 'songs': list}, ...],
            'total_fetched': int,
            'total_unique': int,
            'total_matched': int
        }
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import hashlib

    logger.info(f"Batch importing {len(playlist_configs)} playlists")

    # Phase 1: Fetch all playlists (parallel with caching)
    logger.info("Phase 1: Fetching all playlists...")
    playlist_sources = []  # List of (name, source, songs)

    def fetch_single_source(name: str, source: str):
        """Fetch songs from a single source."""
        try:
            if source.startswith("http://") or source.startswith("https://"):
                # URL source - use provider import
                songs = []
                if "spotify" in source.lower():
                    from harmony.providers import import_spotify_playlist
                    spotify_config = getattr(harmony_app.config, 'providers', {})
                    if isinstance(spotify_config, dict):
                        spotify_creds = spotify_config.get('spotify', {})
                    else:
                        spotify_creds = getattr(spotify_config, 'spotify', {}).model_dump() if hasattr(spotify_config, 'spotify') else {}
                    client_id = spotify_creds.get('client_id') if isinstance(spotify_creds, dict) else getattr(spotify_creds, 'client_id', None)
                    client_secret = spotify_creds.get('client_secret') if isinstance(spotify_creds, dict) else getattr(spotify_creds, 'client_secret', None)
                    songs = import_spotify_playlist(source, harmony_app.cache, client_id, client_secret)
                elif "youtube" in source.lower():
                    from harmony.providers import import_yt_playlist
                    songs = import_yt_playlist(source, harmony_app.cache)
                elif "apple" in source.lower():
                    from harmony.providers import import_apple_playlist
                    songs = import_apple_playlist(source, harmony_app.cache)
                elif "tidal" in source.lower():
                    from harmony.providers import import_tidal_playlist
                    songs = import_tidal_playlist(source, harmony_app.cache)
                elif "gaana" in source.lower():
                    from harmony.providers import import_gaana_playlist
                    songs = import_gaana_playlist(source, harmony_app.cache)
                elif "jiosaavn" in source.lower():
                    from harmony.providers import import_jiosaavn_playlist
                    songs = import_jiosaavn_playlist(source, harmony_app.cache)
                else:
                    logger.warning(f"Unknown URL source type: {source}")
                    return (name, source, [])

                logger.info(f"Fetched {len(songs)} songs from {source}")
                return (name, source, songs)
            elif source.endswith(".m3u8"):
                # M3U8 file - resolve path using existing helper
                from harmony.providers import import_m3u8_playlist
                
                # Resolve M3U8 path (handles m3u8_dir from config)
                resolved_path = _resolve_m3u8_path(harmony_app, source)
                logger.debug(f"Resolved M3U8 path: {source} -> {resolved_path}")
                
                songs = import_m3u8_playlist(resolved_path, harmony_app.cache)
                logger.info(f"Fetched {len(songs)} songs from {source}")
                return (name, source, songs)
            else:
                logger.warning(f"Unknown source format: {source}")
                return (name, source, [])
        except Exception as e:
            logger.error(f"Error fetching {source}: {e}")
            return (name, source, [])

    # Fetch all sources in parallel
    fetch_tasks = []
    for config in playlist_configs:
        name = config.get('name')
        sources = config.get('sources', [])
        for source in sources:
            fetch_tasks.append((name, source))

    # Fetch all playlists in parallel (no progress bar - too fast)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(fetch_single_source, name, source): (name, source)
            for name, source in fetch_tasks
        }

        for future in as_completed(futures):
            result = future.result()
            playlist_sources.append(result)

    # Group by playlist name
    playlists_by_name = {}
    for name, source, songs in playlist_sources:
        if name not in playlists_by_name:
            playlists_by_name[name] = []
        playlists_by_name[name].extend(songs)

    total_fetched = sum(len(songs) for _, _, songs in playlist_sources)
    logger.info(f"Fetched {total_fetched} total songs from {len(playlist_sources)} sources")

    # Phase 2: Deduplicate songs across all playlists
    logger.info("Phase 2: Deduplicating songs...")

    def song_hash(song: dict) -> str:
        """Create a hash for deduplication."""
        title = (song.get("title") or "").lower().strip()
        artist = (song.get("artist") or "").lower().strip()
        album = (song.get("album") or "").lower().strip()
        return hashlib.md5(f"{title}|{artist}|{album}".encode()).hexdigest()

    # Build unique song list with reverse mapping
    unique_songs = {}  # hash -> song
    song_to_playlists = {}  # hash -> list of playlist names

    for config in playlist_configs:
        name = config.get('name')
        songs = playlists_by_name.get(name, [])

        for song in songs:
            h = song_hash(song)
            if h not in unique_songs:
                unique_songs[h] = song
                song_to_playlists[h] = []
            song_to_playlists[h].append(name)

    total_unique = len(unique_songs)
    logger.info(f"Deduplicated to {total_unique} unique songs (was {total_fetched})")

    # Phase 3: Batch match all unique songs
    logger.info("Phase 3: Matching all unique songs to backend...")

    matched_songs = {}  # hash -> matched track dict
    unique_song_list = list(unique_songs.items())

    progress = harmony_app.create_progress_counter(
        total=len(unique_song_list),
        desc="Matching songs",
        unit="track"
    )

    try:
        for idx, (h, song) in enumerate(unique_song_list, start=1):
            try:
                found = harmony_app.search_song(song, manual_search=manual_search)
                if found:
                    matched_songs[h] = found
                if progress:
                    progress.update(1)
            except Exception as e:
                logger.warning(f"Error matching {song.get('title', 'Unknown')}: {e}")
                if progress:
                    progress.update(1)
                continue
    finally:
        if progress:
            progress.close()

    total_matched = len(matched_songs)
    logger.info(f"Matched {total_matched}/{total_unique} unique songs")

    # Phase 4: Build playlists from matched results
    logger.info("Phase 4: Building playlists...")

    results = []
    backend = getattr(harmony_app, "backend", None) or getattr(harmony_app, "plex", None)

    for config in playlist_configs:
        name = config.get('name')
        songs = playlists_by_name.get(name, [])

        # Map to matched tracks
        playlist_tracks = []
        for song in songs:
            h = song_hash(song)
            if h in matched_songs:
                playlist_tracks.append(matched_songs[h])

        # Add to backend
        if playlist_tracks:
            try:
                # Get or create playlist
                playlist = backend.get_playlist(name)
                if playlist:
                    logger.info(f"Updating existing playlist: {name}")
                    backend.clear_playlist(name)
                else:
                    logger.info(f"Creating new playlist: {name}")

                backend.add_to_playlist(name, playlist_tracks)
                logger.info(f"Added {len(playlist_tracks)} tracks to {name}")

                results.append({
                    'name': name,
                    'tracks': len(playlist_tracks),
                    'songs': playlist_tracks
                })
            except Exception as e:
                logger.error(f"Error building playlist {name}: {e}")
                results.append({
                    'name': name,
                    'tracks': 0,
                    'songs': []
                })
        else:
            logger.warning(f"No tracks matched for playlist: {name}")
            results.append({
                'name': name,
                'tracks': 0,
                'songs': []
            })

    return {
        'playlists': results,
        'total_fetched': total_fetched,
        'total_unique': total_unique,
        'total_matched': total_matched
    }

