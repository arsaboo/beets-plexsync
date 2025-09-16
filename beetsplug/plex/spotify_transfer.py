from __future__ import annotations

from beetsplug.plex import smartplaylists as sp_mod

"""Utilities for transferring Plex playlists to Spotify."""

def plex_to_spotify(plugin, lib, playlist, query_args=None):
    """Transfer a Plex playlist to Spotify using the plugin context."""
    plugin.authenticate_spotify()
    plex_playlist = plugin.plex.playlist(playlist)
    plex_playlist_items = list(plex_playlist.items())
    plugin._log.debug("Total items in Plex playlist: {}", len(plex_playlist_items))

    plex_lookup = sp_mod.build_plex_lookup(plugin, lib)
    spotify_tracks = []

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

    progress = plugin.create_progress_counter(
        len(plex_playlist_items),
        f"Resolving Spotify matches for {playlist}",
        unit="track",
    )
    try:
        for item in plex_playlist_items:
            plugin._log.debug("Processing {}", item.ratingKey)
            beets_item = plex_lookup.get(item.ratingKey)
            if not beets_item:
                plugin._log.debug(
                    "Library not synced. Item not found in Beets: {} - {}",
                    item.parentTitle,
                    item.title,
                )
                if progress is not None:
                    progress.update()
                continue

            if query_rating_keys is not None and item.ratingKey not in query_rating_keys:
                plugin._log.debug(
                    "Item filtered out by query: {} - {} - {}",
                    beets_item.artist,
                    beets_item.album,
                    beets_item.title,
                )
                if progress is not None:
                    progress.update()
                continue

            plugin._log.debug("Beets item: {}", beets_item)
            spotify_track_id = _resolve_spotify_track(plugin, beets_item)
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

    plugin.add_tracks_to_spotify_playlist(playlist, spotify_tracks)

def _resolve_spotify_track(plugin, beets_item):
    spotify_track_id = None
    try:
        spotify_track_id = getattr(beets_item, 'spotify_track_id', None)
        plugin._log.debug("Spotify track id in beets: {}", spotify_track_id)

        if spotify_track_id:
            try:
                track_info = plugin.sp.track(spotify_track_id)
                if (
                    not track_info
                    or not track_info.get('is_playable', True)
                    or track_info.get('restrictions', {}).get('reason') == 'unavailable'
                    or not track_info.get('available_markets')
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
