from __future__ import annotations

from beetsplug.plex import smartplaylists as sp_mod

"""Utilities for transferring Plex playlists to Spotify."""


def _batch_check_availability(plugin, track_ids):
    """Check availability of Spotify tracks in batches of 50.

    Returns a dict mapping track_id -> bool (True = playable).
    """
    availability = {}
    ids_to_check = [tid for tid in track_ids if tid]
    for i in range(0, len(ids_to_check), 50):
        batch = ids_to_check[i:i + 50]
        try:
            result = plugin.sp.tracks(batch)
            for track_info in result.get("tracks", []):
                if not track_info:
                    continue
                tid = track_info["id"]
                playable = (
                    track_info.get("is_playable", True)
                    and track_info.get("restrictions", {}).get("reason") != "unavailable"
                    # available_markets is omitted by Spotify for OAuth token requests;
                    # treat a missing key as "assume available"
                    and ("available_markets" not in track_info
                         or bool(track_info["available_markets"]))
                )
                availability[tid] = playable
        except Exception as exc:
            plugin._log.debug("Batch availability check failed: {}", exc)
            # Mark all in this batch as unknown (will fall through to search)
            for tid in batch:
                availability[tid] = False
    return availability


def plex_to_spotify(plugin, lib, playlist, query_args=None):
    """Transfer a Plex playlist to Spotify using the plugin context."""
    plugin.authenticate_spotify()
    plex_playlist = plugin.plex.playlist(playlist)
    plex_playlist_items = list(plex_playlist.items())
    plugin._log.debug("Total items in Plex playlist: {}", len(plex_playlist_items))

    plex_lookup = plugin._build_plex_lookup_and_vector_index(lib)

    query_rating_keys = None
    if query_args:
        query_items = lib.items(query_args)
        query_rating_keys = {
            item.plex_ratingkey for item in query_items if hasattr(item, 'plex_ratingkey')
        }
        plugin._log.info(
            "Query matched {} beets items, filtering playlist accordingly",
            len(query_rating_keys),
        )

    # Collect beets items in playlist order, filtering as needed
    ordered_beets_items = []
    for item in plex_playlist_items:
        beets_item = plex_lookup.get(item.ratingKey)
        if not beets_item:
            plugin._log.debug(
                "Library not synced. Item not found in Beets: {} - {}",
                item.parentTitle,
                item.title,
            )
            continue
        if query_rating_keys is not None and item.ratingKey not in query_rating_keys:
            continue
        ordered_beets_items.append(beets_item)

    # Batch-check availability for items that already have a spotify_track_id
    existing_ids = {}
    for beets_item in ordered_beets_items:
        sid = getattr(beets_item, "spotify_track_id", None)
        if sid:
            existing_ids[id(beets_item)] = sid
    if existing_ids:
        unique_ids = list(set(existing_ids.values()))
        plugin._log.debug("Batch-checking availability for {} cached Spotify IDs", len(unique_ids))
        availability = _batch_check_availability(plugin, unique_ids)
    else:
        availability = {}

    spotify_tracks = []
    progress = plugin.create_progress_counter(
        len(ordered_beets_items),
        f"Resolving Spotify matches for {playlist}",
        unit="track",
    )
    try:
        for beets_item in ordered_beets_items:
            plugin._log.debug("Beets item: {}", beets_item)
            spotify_track_id = _resolve_spotify_track(plugin, beets_item, availability)
            if spotify_track_id:
                spotify_tracks.append(spotify_track_id)
            else:
                plugin._log.info("No playable Spotify match found for {}", beets_item)
            if progress is not None:
                progress.update()
    finally:
        if progress is not None:
            try:
                progress.close()
            except Exception:  # noqa: BLE001 - optional UI element
                plugin._log.debug("Unable to close Spotify transfer progress for playlist {}", playlist)

    if query_args:
        plugin._log.info(
            "Found {} Spotify tracks matching query in Plex playlist order",
            len(spotify_tracks),
        )
    else:
        plugin._log.debug(
            "Found {} Spotify tracks in Plex playlist order",
            len(spotify_tracks),
        )

    # Deduplicate while preserving order
    seen = set()
    deduplicated_tracks = []
    for track_id in spotify_tracks:
        if track_id not in seen:
            seen.add(track_id)
            deduplicated_tracks.append(track_id)

    if len(deduplicated_tracks) < len(spotify_tracks):
        plugin._log.info(
            "Removed {} duplicate tracks from playlist transfer",
            len(spotify_tracks) - len(deduplicated_tracks)
        )

    plugin.add_tracks_to_spotify_playlist(playlist, deduplicated_tracks)

def _resolve_spotify_track(plugin, beets_item, availability=None):
    """Resolve a Spotify track ID for a beets item.

    Uses the pre-computed ``availability`` map from batch checking when
    available, avoiding per-track ``sp.track()`` calls.
    """
    if availability is None:
        availability = {}

    spotify_track_id = None
    try:
        spotify_track_id = getattr(beets_item, 'spotify_track_id', None)
        plugin._log.debug("Spotify track id in beets: {}", spotify_track_id)

        if spotify_track_id:
            # Use batch-checked availability if present
            if spotify_track_id in availability:
                if not availability[spotify_track_id]:
                    plugin._log.debug(
                        "Track {} is not playable (batch check), searching for alternatives",
                        spotify_track_id,
                    )
                    spotify_track_id = None
            else:
                # Fallback: single track check (shouldn't happen normally)
                try:
                    track_info = plugin.sp.track(spotify_track_id)
                    if (
                        not track_info
                        or not track_info.get('is_playable', True)
                        or track_info.get('restrictions', {}).get('reason') == 'unavailable'
                        or ('available_markets' in track_info
                            and not track_info['available_markets'])
                    ):
                        plugin._log.debug(
                            "Track {} is not playable or not available, searching for alternatives",
                            spotify_track_id,
                        )
                        spotify_track_id = None
                except Exception as exc:  # noqa: BLE001 - log but continue
                    plugin._log.debug(
                        "Error checking track availability {}: {}",
                        spotify_track_id,
                        exc,
                    )
                    spotify_track_id = None
    except Exception:
        spotify_track_id = None
        plugin._log.debug("Spotify track_id not found in beets")

    if not spotify_track_id:
        spotify_track_id = plugin._search_spotify_track(beets_item)
    return spotify_track_id
