"""Utilities for transferring Plex playlists to Spotify."""

from __future__ import annotations

from collections import Counter

# Spotify's /v1/tracks accepts up to 50 ids per request.
_BATCH = 50

# Resolution outcomes. Only ``failed`` means the provider could not be
# consulted, which is what must forbid destructive cleanup downstream.
CACHED = "cached"
RELINKED = "relinked"
SEARCHED = "searched"
NO_MATCH = "no match"
FAILED = "failed"


def _availability_entry(track):
    """Reduce a ``/v1/tracks`` item to ``{id, playable, reason, links}``.

    With a market, Spotify reports playability directly and answers an id it
    cannot serve in that market with a relinked copy of the same recording
    (different ``id``, ``linked_from`` pointing back at the requested one).
    Without a market both fields are omitted and ``available_markets`` is the
    only signal left, where an empty list means the id plays nowhere.
    """
    if not track:
        # A null entry is authoritative: the id no longer resolves.
        return {"id": None, "playable": False, "reason": "gone", "links": []}
    linked = track.get("linked_from") or {}
    playable = track.get("is_playable")
    if playable is None:
        markets = track.get("available_markets")
        playable = True if markets is None else bool(markets)
    return {
        "id": track.get("id"),
        "playable": bool(playable),
        "reason": (track.get("restrictions") or {}).get("reason"),
        "links": [linked["id"]] if linked.get("id") else [],
    }


def _batch_check_availability(plugin, track_ids, market=None):
    """Check availability in batches of 50.

    Entries are keyed by the *requested* id: Spotify aligns the response with
    the request positionally and may answer with a different id for the same
    recording, so the returned object's id cannot be the key.

    ``playable`` is ``True``/``False`` when Spotify answered, or ``None`` when
    the check failed and the caller must not authorize removals.
    """
    availability = {}
    ids = [tid for tid in track_ids if tid]
    for i in range(0, len(ids), _BATCH):
        batch = ids[i:i + _BATCH]
        try:
            result = plugin.sp.tracks(batch, market=market)
            tracks = result["tracks"]
            if len(tracks) != len(batch):
                raise ValueError("Incomplete Spotify availability response")
        except Exception as exc:  # noqa: BLE001 - indeterminate, caller decides
            plugin._log.debug("Batch availability check failed: {}", exc)
            for tid in batch:
                availability[tid] = {
                    "id": tid, "playable": None, "reason": "error", "links": [],
                }
            continue
        for track_id, track in zip(batch, tracks):
            availability[track_id] = _availability_entry(track)
    return availability


def build_id_groups(track_ids, availability):
    """Map Spotify ids that hold the same recording onto one representative.

    Relinking means one recording can appear as the id stored in beets, the id
    Spotify serves for the account's market, and the copy an earlier run already
    put in the destination playlist. Grouping them keeps the reconcile step from
    adding a second copy of a track that is present under another id, or from
    removing the copy that is there.
    """
    parent = {}

    def find(value):
        parent.setdefault(value, value)
        root = value
        while parent[root] != root:
            root = parent[root]
        while parent[value] != root:
            parent[value], value = root, parent[value]
        return root

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for track_id in track_ids:
        if not track_id:
            continue
        find(track_id)
        entry = availability.get(track_id)
        if not entry:
            continue
        for related in [entry.get("id"), *entry.get("links")]:
            if related and related != track_id:
                union(track_id, related)

    return {track_id: find(track_id) for track_id in list(parent)}


def _search_for_track(plugin, beets_item, market):
    """Search Spotify for a beets item, mapping failures to outcomes."""
    try:
        found = plugin._search_spotify_track(beets_item, raise_on_error=True, market=market)
    except Exception as exc:  # noqa: BLE001 - caller must suppress removals
        plugin._log.error("Spotify search failed for {}: {}", beets_item, exc)
        return None, FAILED
    if found:
        return found, SEARCHED
    plugin._log.info("No playable Spotify match found for {}", beets_item)
    return None, NO_MATCH


def _resolve_spotify_track(plugin, beets_item, availability, market=None):
    """Resolve a Spotify track ID for a beets item.

    Uses the pre-computed ``availability`` map from batch checking when available,
    avoiding per-track ``sp.track()`` calls. Returns ``(track_id, outcome)``.
    """
    try:
        spotify_track_id = getattr(beets_item, "spotify_track_id", None)
    except Exception as exc:  # noqa: BLE001 - malformed local metadata
        plugin._log.debug("Spotify track_id lookup failed in beets: {}", exc)
        spotify_track_id = None

    if spotify_track_id:
        entry = availability.get(spotify_track_id)
        if entry is None:
            # Not covered by the batch check; ask once directly.
            try:
                entry = _availability_entry(plugin.sp.track(spotify_track_id, market=market))
            except Exception as exc:  # noqa: BLE001 - retain known ID safely
                plugin._log.debug(
                    "Error checking track availability {}; retaining cached ID: {}",
                    spotify_track_id,
                    exc,
                )
                return spotify_track_id, FAILED

        if entry["playable"] is None:
            plugin._log.debug(
                "Availability check failed for {}; retaining cached ID", spotify_track_id
            )
            return spotify_track_id, FAILED
        if entry["playable"]:
            resolved = entry["id"] or spotify_track_id
            if resolved != spotify_track_id:
                plugin._log.debug(
                    "Spotify serves {} as the playable copy {}", spotify_track_id, resolved
                )
                return resolved, RELINKED
            return resolved, CACHED
        plugin._log.debug(
            "Track {} is not playable ({}), searching for alternatives",
            spotify_track_id,
            entry["reason"],
        )

    return _search_for_track(plugin, beets_item, market)


def plex_to_spotify(plugin, lib, playlist, query_args=None):
    """Transfer a Plex playlist to Spotify using the plugin context."""
    plugin.authenticate_spotify()
    plex_playlist_items = list(plugin.plex.playlist(playlist).items())
    plugin._log.debug("Total items in Plex playlist: {}", len(plex_playlist_items))

    # Only the playlist's own rating keys are resolved against beets; a whole
    # library scan is not needed to transfer one playlist.
    selection = plugin._resolve_playlist_items(lib, plex_playlist_items, query_args)
    if query_args:
        plugin._log.info(
            "Query selected {} of {} Plex playlist items",
            len(selection.items),
            selection.plex_total,
        )
    else:
        plugin._log.debug(
            "Mapped {} of {} Plex playlist items to Beets",
            len(selection.items),
            selection.plex_total,
        )
    if selection.missing:
        plugin._log.info(
            "{} Plex playlist items have no Beets mapping; check their plex_ratingkey mappings before retrying",
            len(selection.missing),
        )
        for item in selection.missing[:5]:
            plugin._log.debug(
                "Item not found in Beets: {} - {}",
                getattr(item, "parentTitle", ""),
                getattr(item, "title", ""),
            )

    market = plugin._spotify_market()
    cached_ids = [
        sid for sid in (getattr(item, "spotify_track_id", None) for item in selection.items) if sid
    ]
    unique_cached = sorted(set(cached_ids))
    plugin._log.debug("Batch-checking availability for {} cached Spotify IDs", len(unique_cached))
    availability = _batch_check_availability(plugin, unique_cached, market)

    spotify_tracks = []
    outcomes = Counter()
    progress = plugin.create_progress_counter(
        len(selection.items),
        f"Resolving Spotify matches for {playlist}",
        unit="track",
    )
    try:
        for beets_item in selection.items:
            plugin._log.debug("Beets item: {}", beets_item)
            spotify_track_id, outcome = _resolve_spotify_track(
                plugin, beets_item, availability, market
            )
            outcomes[outcome] += 1
            if spotify_track_id:
                spotify_tracks.append(spotify_track_id)
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
    plugin._log.debug(
        "Spotify resolution outcomes: {}",
        ", ".join(f"{count} {outcome}" for outcome, count in sorted(outcomes.items())) or "none",
    )

    # One recording can be addressed by several ids (beets' cached id plus the
    # copy Spotify serves for it); keep the first id seen for each of them.
    id_groups = build_id_groups(unique_cached, availability)
    seen_groups = set()
    deduplicated_tracks = []
    for track_id in spotify_tracks:
        group = id_groups.get(track_id, track_id)
        if group not in seen_groups:
            seen_groups.add(group)
            deduplicated_tracks.append(track_id)

    if len(deduplicated_tracks) < len(spotify_tracks):
        plugin._log.info(
            "Removed {} duplicate tracks from playlist transfer",
            len(spotify_tracks) - len(deduplicated_tracks),
        )

    if not deduplicated_tracks:
        plugin._log.error(
            "Refusing to update Spotify playlist {} because no tracks resolved",
            playlist,
        )
        return False

    blockers = []
    if selection.missing:
        blockers.append(f"{len(selection.missing)} Plex items are not in the Beets library")
    if outcomes[FAILED]:
        blockers.append(f"{outcomes[FAILED]} Spotify lookups failed")
    if blockers:
        plugin._log.warning(
            "Updating {} in add-only mode: {}", playlist, "; ".join(blockers)
        )
    return plugin.add_tracks_to_spotify_playlist(
        playlist,
        deduplicated_tracks,
        allow_removals=not blockers,
        id_groups=id_groups,
    )
