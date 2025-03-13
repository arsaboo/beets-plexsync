"""Core shared functionality for the PlexSync plugin."""

import re
from datetime import datetime, timedelta
import json
import logging

from beets import config
from beetsplug.utils import (
    clean_string, get_fuzzy_score, clean_text_for_matching,
    calculate_string_similarity, calculate_artist_similarity
)

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

# Add other core shared functions that multiple modules depend on
