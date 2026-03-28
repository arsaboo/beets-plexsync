"""Plex playlist operations extracted from plexsync.

These helpers encapsulate low-level Plex operations and log consistently.
They are intentionally thin to avoid behavior changes.
"""

from typing import Iterable, Sequence, Set, Tuple

from plexapi import exceptions


def batch_fetch_plex_items(plex, rating_keys: Sequence[str], logger,
                           extra_exceptions: Tuple = ()) -> Set:
    """Batch-fetch Plex items by rating key with individual-fetch fallback.

    Returns a set of fetched Plex items.  *extra_exceptions* is appended to
    the default (NotFound, AttributeError) tuple for both the batch call and
    the per-item fallback.
    """
    if not rating_keys:
        return set()

    catch = (exceptions.NotFound, AttributeError) + extra_exceptions
    plex_set: Set = set()
    try:
        ekey = f'/library/metadata/{",".join(rating_keys)}'
        plex_set.update(plex.fetchItems(ekey))
    except catch as e:
        logger.warning("Batch fetch failed, falling back to individual fetches. Error: {}", e)
        for rating_key in rating_keys:
            try:
                plex_set.add(plex.fetchItem(int(rating_key)))
            except catch as e:
                logger.warning("Item with ratingKey {} not found in Plex library. Error: {}", rating_key, e)
    return plex_set


def sort_plex_playlist(plex, playlist_name: str, sort_field: str, logger) -> None:
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


def _resolve_plex_items(plex, items: Iterable, logger):
    """Normalize incoming items to Plex items via rating key.

    Supports objects with either `plex_ratingkey` or `ratingKey` attributes.
    Uses batch fetching via fetchItems for efficiency.
    """
    rating_keys = []
    items_without_keys = []
    for item in items:
        rating_key = getattr(item, 'plex_ratingkey', None) or getattr(item, 'ratingKey', None)
        if rating_key:
            rating_keys.append(str(rating_key))
        else:
            items_without_keys.append(item)

    for item in items_without_keys:
        logger.warning("{} does not have plex_ratingkey or ratingKey attribute. Item details: {}", item, vars(item))

    return batch_fetch_plex_items(plex, rating_keys, logger)


def plex_add_playlist_item(plex, items: Iterable, playlist_name: str, logger) -> None:
    """Add items to a Plex playlist (no duplicates)."""
    if not items:
        logger.warning("No items to add to playlist {}", playlist_name)
        return

    try:
        plst = plex.playlist(playlist_name)
        playlist_set = set(plst.items())
    except exceptions.NotFound:
        plst = None
        playlist_set = set()

    plex_set = _resolve_plex_items(plex, items, logger)
    to_add = plex_set - playlist_set
    logger.info("Adding {} tracks to {} playlist", len(to_add), playlist_name)
    if plst is None:
        logger.info("{} playlist will be created", playlist_name)
        plex.createPlaylist(playlist_name, items=list(to_add))
    else:
        try:
            plst.addItems(items=list(to_add))
        except exceptions.BadRequest as e:
            logger.error("Error adding items {} to {} playlist. Error: {}", to_add, playlist_name, e)

    # Sort by recency, matches original behavior
    try:
        sort_plex_playlist(plex, playlist_name, "lastViewedAt", logger)
    except Exception:
        # Non-fatal if sorting fails
        pass


def plex_playlist_to_collection(music, playlist_name: str, logger) -> None:
    """Convert a Plex playlist to a Plex collection, de-duplicated."""
    try:
        plst = music.playlist(playlist_name)
        playlist_set = set(plst.items())
    except exceptions.NotFound:
        logger.error("{} playlist not found", playlist_name)
        return

    try:
        col = music.collection(playlist_name)
        collection_set = set(col.items())
    except exceptions.NotFound:
        col = None
        collection_set = set()

    to_add = playlist_set - collection_set
    logger.info("Adding {} tracks to {} collection", len(to_add), playlist_name)
    if col is None:
        logger.info("{} collection will be created", playlist_name)
        music.createCollection(playlist_name, items=list(to_add))
    else:
        try:
            col.addItems(items=list(to_add))
        except exceptions.BadRequest as e:
            logger.error("Error adding items {} to {} collection. Error: {}", to_add, playlist_name, e)


def plex_remove_playlist_item(plex, items: Iterable, playlist_name: str, logger) -> None:
    """Remove items from a Plex playlist if present."""
    try:
        plst = plex.playlist(playlist_name)
        playlist_set = set(plst.items())
    except exceptions.NotFound:
        logger.error("{} playlist not found", playlist_name)
        return

    from requests.exceptions import ConnectionError, ContentDecodingError

    rating_keys = [str(k) for item in items
                   if (k := getattr(item, 'plex_ratingkey', None))]
    if not rating_keys:
        return

    plex_set = batch_fetch_plex_items(
        plex, rating_keys, logger,
        extra_exceptions=(ContentDecodingError, ConnectionError),
    )

    to_remove = plex_set.intersection(playlist_set)
    logger.info("Removing {} tracks from {} playlist", len(to_remove), playlist_name)
    plst.removeItems(items=list(to_remove))


def plex_clear_playlist(plex, playlist_name: str) -> None:
    """Clear all items from a Plex playlist."""
    plist = plex.playlist(playlist_name)
    tracks = plist.items()
    for track in tracks:
        plist.removeItems(track)

