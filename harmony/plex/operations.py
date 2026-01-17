"""Plex playlist operations.

These helpers encapsulate low-level Plex operations and log consistently.
"""

import logging
from typing import Iterable

from plexapi import exceptions

logger = logging.getLogger("harmony.plex.operations")


def sort_plex_playlist(plex, playlist_name: str, sort_field: str) -> None:
    """Sort a Plex playlist by a given datetime field (desc)."""
    playlist = plex.playlist(playlist_name)
    items = playlist.items()
    sorted_items = sorted(
        items,
        key=lambda x: (getattr(x, sort_field).timestamp() if getattr(x, sort_field) is not None else 0),
        reverse=True,
    )
    playlist.removeItems(items)
    for item in sorted_items:
        playlist.addItems(item)


def _resolve_plex_items(plex, items: Iterable):
    """Normalize incoming items to Plex items via rating key.

    Supports objects with either `plex_ratingkey` or `ratingKey` attributes,
    or dicts with 'plex_ratingkey' or 'ratingKey' keys.
    """
    plex_set = set()
    for item in items:
        try:
            # Handle dict items
            if isinstance(item, dict):
                rating_key = item.get('plex_ratingkey') or item.get('ratingKey')
            else:
                # Handle object items
                rating_key = getattr(item, 'plex_ratingkey', None) or getattr(item, 'ratingKey', None)

            if rating_key:
                plex_set.add(plex.fetchItem(rating_key))
            else:
                logger.warning(f"{item} does not have plex_ratingkey or ratingKey")
        except (exceptions.NotFound, AttributeError) as e:
            logger.warning(f"{item} not found in Plex library. Error: {e}")
            continue
    return plex_set


def plex_add_playlist_item(plex, items: Iterable, playlist_name: str) -> None:
    """Add items to a Plex playlist (no duplicates)."""
    if not items:
        logger.warning(f"No items to add to playlist {playlist_name}")
        return

    try:
        plst = plex.playlist(playlist_name)
        playlist_set = set(plst.items())
    except exceptions.NotFound:
        plst = None
        playlist_set = set()

    plex_set = _resolve_plex_items(plex, items)
    to_add = plex_set - playlist_set
    logger.info(f"Adding {len(to_add)} tracks to {playlist_name} playlist")
    if plst is None:
        logger.info(f"{playlist_name} playlist will be created")
        plex.createPlaylist(playlist_name, items=list(to_add))
    else:
        try:
            plst.addItems(items=list(to_add))
        except exceptions.BadRequest as e:
            logger.error(f"Error adding items to {playlist_name} playlist. Error: {e}")

    # Sort by recency, matches original behavior
    try:
        sort_plex_playlist(plex, playlist_name, "lastViewedAt")
    except Exception as e:
        # Non-fatal if sorting fails
        logger.debug(f"Could not sort playlist {playlist_name}: {e}")


def plex_playlist_to_collection(music, playlist_name: str) -> None:
    """Convert a Plex playlist to a Plex collection, de-duplicated."""
    try:
        plst = music.playlist(playlist_name)
        playlist_set = set(plst.items())
    except exceptions.NotFound:
        logger.error(f"{playlist_name} playlist not found")
        return

    try:
        col = music.collection(playlist_name)
        collection_set = set(col.items())
    except exceptions.NotFound:
        col = None
        collection_set = set()

    to_add = playlist_set - collection_set
    logger.info(f"Adding {len(to_add)} tracks to {playlist_name} collection")
    if col is None:
        logger.info(f"{playlist_name} collection will be created")
        music.createCollection(playlist_name, items=list(to_add))
    else:
        try:
            col.addItems(items=list(to_add))
        except exceptions.BadRequest as e:
            logger.error(f"Error adding items to {playlist_name} collection. Error: {e}")


def plex_remove_playlist_item(plex, items: Iterable, playlist_name: str) -> None:
    """Remove items from a Plex playlist if present."""
    try:
        plst = plex.playlist(playlist_name)
        playlist_set = set(plst.items())
    except exceptions.NotFound:
        logger.error(f"{playlist_name} playlist not found")
        return

    plex_set = set()
    from requests.exceptions import ConnectionError, ContentDecodingError

    for item in items:
        try:
            plex_set.add(plex.fetchItem(item.plex_ratingkey))
        except (exceptions.NotFound, AttributeError, ContentDecodingError, ConnectionError) as e:
            logger.warning(f"{item} not found in Plex library. Error: {e}")
            continue

    to_remove = plex_set.intersection(playlist_set)
    logger.info(f"Removing {len(to_remove)} tracks from {playlist_name} playlist")
    plst.removeItems(items=list(to_remove))


def plex_clear_playlist(plex, playlist_name: str) -> None:
    """Clear all items from a Plex playlist."""
    plist = plex.playlist(playlist_name)
    tracks = plist.items()
    if tracks:
        plist.removeItems(items=tracks)
