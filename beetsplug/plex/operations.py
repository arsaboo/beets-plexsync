"""Low-level Plex playlist and collection operations."""

from collections import Counter
from typing import Iterable, Sequence, Set, Tuple

from plexapi import exceptions


def _rating_key(item):
    """Return an item's Plex rating key as a string, when available."""
    value = getattr(item, "plex_ratingkey", None) or getattr(item, "ratingKey", None)
    return str(value) if value else None


def batch_fetch_plex_items(plex, rating_keys: Sequence[str], logger,
                           extra_exceptions: Tuple = ()) -> Set:
    """Batch-fetch Plex items by rating key with individual-fetch fallback."""
    if not rating_keys:
        return set()

    catch = (exceptions.NotFound, AttributeError) + extra_exceptions
    plex_set: Set = set()
    try:
        ekey = f'/library/metadata/{",".join(rating_keys)}'
        plex_set.update(plex.fetchItems(ekey))
    except catch as exc:
        logger.warning(
            "Batch fetch failed, falling back to individual fetches. Error: {}",
            exc,
        )
        for rating_key in rating_keys:
            try:
                plex_set.add(plex.fetchItem(int(rating_key)))
            except (*catch, ValueError) as item_exc:
                logger.warning(
                    "Item with ratingKey {} not found in Plex library. Error: {}",
                    rating_key,
                    item_exc,
                )
    return plex_set


def _resolve_plex_items_ordered(plex, items: Iterable, logger):
    """Resolve items to unique Plex objects while preserving requested order.

    Returns ``(resolved, missing_keys)``. Membership is compared explicitly by
    rating key rather than by PlexAPI object hashing; PlexAPI hashes include the
    title even though equality only compares the metadata key.
    """
    requested_keys = []
    seen = set()
    unkeyed_count = 0
    for item in items:
        rating_key = _rating_key(item)
        if not rating_key:
            unkeyed_count += 1
            logger.warning(
                "{} does not have plex_ratingkey or ratingKey attribute. Item details: {}",
                item,
                vars(item) if hasattr(item, "__dict__") else repr(item),
            )
            continue
        if rating_key not in seen:
            requested_keys.append(rating_key)
            seen.add(rating_key)

    fetched = batch_fetch_plex_items(plex, requested_keys, logger)
    fetched_by_key = {
        key: item for item in fetched if (key := _rating_key(item)) is not None
    }
    resolved = [fetched_by_key[key] for key in requested_keys if key in fetched_by_key]
    missing = [key for key in requested_keys if key not in fetched_by_key]
    missing.extend([None] * unkeyed_count)
    return resolved, missing


def _playlist_keys(items):
    return [key for item in items if (key := _rating_key(item)) is not None]


def _fresh_playlist(plex, playlist_name):
    """Fetch a fresh playlist object so its item list reflects recent writes."""
    return plex.playlist(playlist_name)


def _current_state(plex, playlist_name):
    """Fetch a fresh playlist plus its items and rating keys in one call."""
    playlist = _fresh_playlist(plex, playlist_name)
    items = list(playlist.items())
    return playlist, items, _playlist_keys(items)


def _remove_playlist_keys(plex, playlist_name, keys):
    """Remove key occurrences, refreshing only between duplicate batches.

    PlexAPI caches ``Playlist.items()`` and resolves removals by rating key. One
    cached object can safely remove distinct keys, but repeated keys would reuse
    the same ``playlistItemID``. Batch one occurrence per key, then refresh.
    """
    pending = Counter(keys)
    if None in pending:
        raise RuntimeError(
            f"Plex returned an item without a ratingKey in {playlist_name!r}"
        )

    while pending:
        playlist = _fresh_playlist(plex, playlist_name)
        items_by_key = {}
        for item in playlist.items():
            items_by_key.setdefault(_rating_key(item), item)

        batch = []
        for key in list(pending):
            candidate = items_by_key.get(key)
            if candidate is None:
                raise RuntimeError(
                    f"ratingKey {key} disappeared while updating {playlist_name!r}"
                )
            batch.append(candidate)
            pending[key] -= 1
            if pending[key] <= 0:
                del pending[key]
        playlist.removeItems(items=batch)


def _reorder_playlist(playlist, desired_keys, logger) -> bool:
    """Best-effort in-place reorder without removing playlist membership."""
    current = list(playlist.items())
    current_keys = _playlist_keys(current)
    if current_keys == desired_keys:
        return True

    by_key = {_rating_key(item): item for item in current}
    for index, desired_key in enumerate(desired_keys):
        if index < len(current_keys) and current_keys[index] == desired_key:
            continue
        item = by_key.get(desired_key)
        if item is None:
            logger.error("Cannot reorder playlist; ratingKey {} is missing", desired_key)
            return False
        after = by_key.get(desired_keys[index - 1]) if index else None
        playlist.moveItem(item, after=after)
        current_keys.remove(desired_key)
        current_keys.insert(index, desired_key)

    return True


def sort_plex_playlist(plex, playlist_name: str, sort_field: str, logger) -> bool:
    """Sort a Plex playlist in place without removing its contents."""
    playlist = _fresh_playlist(plex, playlist_name)
    items = list(playlist.items())
    sorted_items = sorted(
        items,
        key=lambda item: (
            getattr(item, sort_field).timestamp()
            if getattr(item, sort_field, None) is not None
            else 0
        ),
        reverse=True,
    )
    return _reorder_playlist(playlist, _playlist_keys(sorted_items), logger)


def _resolve_plex_items(plex, items: Iterable, logger):
    """Normalize incoming items to Plex items via rating key."""
    resolved, _missing = _resolve_plex_items_ordered(plex, items, logger)
    return set(resolved)


def plex_add_playlist_item(plex, items: Iterable, playlist_name: str, logger) -> bool:
    """Append missing items to a Plex playlist without introducing duplicates."""
    items = list(items)
    if not items:
        logger.warning("No items to add to playlist {}", playlist_name)
        return False

    resolved, missing = _resolve_plex_items_ordered(plex, items, logger)
    if missing:
        logger.warning(
            "Could not resolve {} requested tracks for {} playlist",
            len(missing),
            playlist_name,
        )
    if not resolved:
        logger.error("No resolvable items to add to playlist {}", playlist_name)
        return False

    try:
        playlist = _fresh_playlist(plex, playlist_name)
        existing_keys = set(_playlist_keys(playlist.items()))
    except exceptions.NotFound:
        logger.info("{} playlist will be created", playlist_name)
        plex.createPlaylist(playlist_name, items=resolved)
        return True

    to_add = [item for item in resolved if _rating_key(item) not in existing_keys]
    logger.info("Adding {} tracks to {} playlist", len(to_add), playlist_name)
    if not to_add:
        return False

    playlist.addItems(items=to_add)
    return True


def plex_replace_playlist_items(plex, items: Iterable, playlist_name: str, logger) -> bool:
    """Reconcile a playlist safely, then best-effort apply the requested order.

    Missing target items are added and verified before obsolete entries are
    removed. A write failure can therefore leave a superset, but never empties
    a valid playlist. Empty or incompletely-resolved targets are rejected.
    """
    items = list(items)
    if not items:
        logger.error("Refusing to replace {} playlist with an empty target", playlist_name)
        return False

    target, missing = _resolve_plex_items_ordered(plex, items, logger)
    if missing:
        logger.error(
            "Refusing to replace {} playlist: {} requested tracks could not be resolved",
            playlist_name,
            len(missing),
        )
        return False
    if not target:
        logger.error("Refusing to replace {} playlist with no resolvable tracks", playlist_name)
        return False

    target_keys = _playlist_keys(target)
    target_key_set = set(target_keys)

    try:
        playlist, current_items, current_keys = _current_state(plex, playlist_name)
    except exceptions.NotFound:
        logger.info("{} playlist will be created", playlist_name)
        created = plex.createPlaylist(playlist_name, items=target)
        created_keys = _playlist_keys(created.items())
        if Counter(created_keys) != Counter(target_keys):
            raise RuntimeError(
                f"Plex did not create {playlist_name!r} with the complete target"
            )
        if created_keys != target_keys:
            try:
                _reorder_playlist(created, target_keys, logger)
            except Exception as exc:  # noqa: BLE001 - membership is already correct
                logger.error("Created {} but could not apply target order: {}", playlist_name, exc)
        return True

    changed = False
    current_key_set = set(current_keys)
    missing_from_playlist = [
        item for item in target if _rating_key(item) not in current_key_set
    ]

    if missing_from_playlist:
        playlist.addItems(items=missing_from_playlist)
        changed = True
        playlist, current_items, current_keys = _current_state(plex, playlist_name)
        absent = target_key_set - set(current_keys)
        if absent:
            raise RuntimeError(
                f"Plex did not add {len(absent)} required tracks to {playlist_name!r}"
            )

    # Remove obsolete entries and duplicate occurrences only after every target
    # key has been verified present.
    kept = set()
    keys_to_remove = []
    for item in current_items:
        key = _rating_key(item)
        if key not in target_key_set or key in kept:
            keys_to_remove.append(key)
        else:
            kept.add(key)

    if keys_to_remove:
        _remove_playlist_keys(plex, playlist_name, keys_to_remove)
        changed = True
        playlist, current_items, current_keys = _current_state(plex, playlist_name)

    final_keys = current_keys
    if Counter(final_keys) != Counter(target_keys):
        raise RuntimeError(
            f"Plex playlist {playlist_name!r} membership differs from the requested target"
        )

    if final_keys != target_keys:
        try:
            if _reorder_playlist(playlist, target_keys, logger):
                changed = True
            else:
                logger.error("Updated {} membership but could not apply target order", playlist_name)
        except Exception as exc:  # noqa: BLE001 - membership is already correct
            # A sequence of move requests may have partially succeeded.
            changed = True
            logger.error("Updated {} membership but reordering failed: {}", playlist_name, exc)

    if changed:
        logger.info("Updated {} playlist with {} tracks", playlist_name, len(target_keys))
    else:
        logger.debug("{} playlist already matches the requested target", playlist_name)
    return changed


def plex_playlist_to_collection(music, playlist_name: str, logger) -> None:
    """Convert a Plex playlist to a Plex collection, de-duplicated."""
    try:
        playlist_items = list(music.playlist(playlist_name).items())
    except exceptions.NotFound:
        logger.error("{} playlist not found", playlist_name)
        return

    try:
        collection = music.collection(playlist_name)
        collection_keys = set(_playlist_keys(collection.items()))
    except exceptions.NotFound:
        collection = None
        collection_keys = set()

    to_add = [item for item in playlist_items if _rating_key(item) not in collection_keys]
    logger.info("Adding {} tracks to {} collection", len(to_add), playlist_name)
    if collection is None:
        logger.info("{} collection will be created", playlist_name)
        music.createCollection(playlist_name, items=to_add)
    elif to_add:
        collection.addItems(items=to_add)


def plex_remove_playlist_item(plex, items: Iterable, playlist_name: str, logger) -> None:
    """Remove items from a Plex playlist if present."""
    try:
        playlist = _fresh_playlist(plex, playlist_name)
        playlist_items = list(playlist.items())
    except exceptions.NotFound:
        logger.error("{} playlist not found", playlist_name)
        return

    requested_keys = {
        key for item in items if (key := _rating_key(item)) is not None
    }
    if not requested_keys:
        return

    to_remove = [item for item in playlist_items if _rating_key(item) in requested_keys]
    logger.info("Removing {} tracks from {} playlist", len(to_remove), playlist_name)
    if to_remove:
        playlist.removeItems(items=to_remove)


def plex_clear_playlist(plex, playlist_name: str) -> None:
    """Clear all items from a Plex playlist."""
    playlist = plex.playlist(playlist_name)
    tracks = playlist.items()
    if tracks:
        playlist.removeItems(tracks)
