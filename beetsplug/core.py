"""Core shared functionality for the PlexSync plugin."""

import re
from datetime import datetime
import logging

from beets import config

# Initialize logger
logger = logging.getLogger('beets')

def build_plex_lookup(plugin, lib):
    """Build a lookup dictionary from Plex ratingKey to beets Item."""
    plex_lookup = {}

    try:
        # Try different query approaches
        try:
            # Method 1: Try to get all items with non-null plex_ratingkey
            items = lib.items('plex_ratingkey:+')
        except Exception:
            try:
                # Method 2: Try using MatchQuery directly
                from beets.dbcore.query import MatchQuery
                items = lib.items(MatchQuery('plex_ratingkey', 0, '>', True))
            except Exception:
                # Method 3: Fallback to getting all items and filtering
                plugin._log.debug("Falling back to loading all items and filtering")
                items = []
                # Get items in batches to avoid memory issues
                for item in lib.items():
                    if hasattr(item, 'plex_ratingkey') and item.plex_ratingkey is not None:
                        items.append(item)

        # Build the lookup table
        for item in items:
            if hasattr(item, 'plex_ratingkey') and item.plex_ratingkey is not None:
                plex_lookup[item.plex_ratingkey] = item

        plugin._log.debug("Built Plex lookup with {} items", len(plex_lookup))
    except Exception as e:
        plugin._log.error("Error building Plex lookup: {}", e)

    return plex_lookup

# Add other shared utility functions

def get_plex_song_metadata(plugin, song):
    """Extract and clean metadata from a song dictionary for Plex search.

    Args:
        plugin: The PlexSync plugin instance
        song: Dictionary containing song metadata

    Returns:
        tuple: (title, album, artist) cleaned for searching
    """
    title = song.get("title", "")
    album = song.get("album", "")
    artist = song.get("artist", "")

    # Try to clean up the metadata using LLM if enabled
    if plugin.search_llm:
        try:
            from beetsplug.llm import search_track_info
            cleaned_metadata = search_track_info(plugin.search_llm, song)
            if cleaned_metadata:
                plugin._log.debug("LLM cleaned metadata: {}", cleaned_metadata)
                # Use cleaned metadata for search
                title = cleaned_metadata.get("title", title)
                album = cleaned_metadata.get("album", album)
                artist = cleaned_metadata.get("artist", artist)
        except Exception as e:
            plugin._log.error("Error using LLM for search cleaning: {}", e)

    return title, album, artist

def format_playlist_log_path(config_dir, playlist_name):
    """Generate a standardized log file path for playlist imports.

    Args:
        config_dir: The beets config directory
        playlist_name: Name of the playlist

    Returns:
        str: Path to the log file
    """
    import os
    return os.path.join(config_dir, f"{playlist_name.lower().replace(' ', '_')}_import.log")

def create_playlist_log_header(log_file, playlist_name):
    """Create a standard header for playlist import logs.

    Args:
        log_file: Path to the log file
        playlist_name: Name of the playlist
    """
    from datetime import datetime

    with open(log_file, 'w', encoding='utf-8') as f:
        f.write(f"Import log for playlist: {playlist_name}\n")
        f.write(f"Import started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("-" * 80 + "\n\n")
