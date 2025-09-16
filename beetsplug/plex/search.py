"""Shared Plex search helpers extracted from plexsync."""

from __future__ import annotations
import re

from beets import ui

from beetsplug.core.config import get_plexsync_config
from beetsplug.ai.llm import search_track_info
from beetsplug.core.matching import clean_text_for_matching


_ARTIST_JOINER_RE = re.compile(r"\s*(?:,|;|&| and |\+|/)\s*")
_FEATURE_SPLIT_RE = re.compile(r"\s*(?:feat\.?|ft\.?|featuring|with)\s+", re.IGNORECASE)

def _split_artist_variants(artist: str | None) -> list[str]:
    """Return candidate artist strings for relaxed matching."""
    if not artist:
        return []

    seen: set[str] = set()
    variants: list[str] = []

    def add_variant(value: str | None) -> None:
        if not value:
            return
        candidate = value.strip()
        if not candidate:
            return
        key = candidate.lower()
        if key not in seen:
            variants.append(candidate)
            seen.add(key)

    normalized = artist.strip()
    add_variant(normalized)

    main_section = _FEATURE_SPLIT_RE.split(normalized, maxsplit=1)[0].strip() if normalized else ""
    add_variant(main_section)

    for source in filter(None, [normalized, main_section]):
        for part in _ARTIST_JOINER_RE.split(source):
            add_variant(part)

    return variants


def _track_matches_artist_variants(track, variants: list[str]) -> bool:
    """Check if any candidate artist appears in the Plex track artist string."""
    if not variants:
        return True
    try:
        artist_name = getattr(track, "originalTitle", None) or track.artist().title
    except Exception:  # noqa: BLE001 - avoid breaking search flow on Plex errors
        artist_name = ""
    artist_name = artist_name or ""
    lower_artist = artist_name.lower()
    for variant in variants:
        if variant and variant.lower() in lower_artist:
            return True
    return False


def search_plex_song(plugin, song, manual_search=None, llm_attempted=False):
    """Fetch a Plex track using multi-strategy search for the given song.

    Parameters mirror the original PlexSync.search_plex_song method but
    take the plugin instance explicitly so this function can be reused by other
    callers.
    """
    if manual_search is None:
        manual_search = get_plexsync_config("manual_search", bool, False)

    cache_key = plugin.cache._make_cache_key(song)
    plugin._log.debug("Generated cache key: '{}' for song: {}", cache_key, song)

    cached_result = plugin.cache.get(cache_key)
    if cached_result is not None:
        plugin._log.debug("Cache HIT for key: '{}' -> result: {}", cache_key, cached_result)
    else:
        plugin._log.debug("Cache MISS for key: '{}'", cache_key)
        plugin.cache.debug_cache_keys(song)

    if cached_result is not None:
        if isinstance(cached_result, tuple):
            rating_key, cleaned_metadata = cached_result
            if rating_key == -1 or rating_key is None:
                if cleaned_metadata and not llm_attempted:
                    plugin._log.debug("Using cached cleaned metadata: {}", cleaned_metadata)
                    result = search_plex_song(plugin, cleaned_metadata, False, llm_attempted=True)
                    if result is not None:
                        plugin._log.debug(
                            "Cached cleaned metadata search succeeded, updating original cache: {}",
                            song,
                        )
                        plugin._cache_result(cache_key, result)
                        return result
                    plugin._log.debug(
                        "Cached cleaned metadata search also failed, respecting original skip for: {}",
                        song,
                    )
                    return None
                plugin._log.debug("Found cached skip result for: {}", song)
                return None

            try:
                if rating_key:
                    cached_track = plugin.music.fetchItem(rating_key)
                    plugin._log.debug("Found cached match for: {} -> {}", song, cached_track.title)
                    return cached_track
            except Exception as exc:  # noqa: BLE001 - want to log original cache issue
                plugin._log.debug("Failed to fetch cached item {}: {}", rating_key, exc)
                plugin.cache.set(cache_key, None)
        else:
            if cached_result == -1 or cached_result is None:
                plugin._log.debug("Found legacy cached skip result for: {}", song)
                return None
            try:
                if cached_result:
                    cached_track = plugin.music.fetchItem(cached_result)
                    plugin._log.debug(
                        "Found legacy cached match for: {} -> {}", song, cached_track.title
                    )
                    return cached_track
            except Exception as exc:  # noqa: BLE001 - log for debugging
                plugin._log.debug("Failed to fetch legacy cached item {}: {}", cached_result, exc)
                plugin.cache.set(cache_key, None)

    tracks = []
    search_strategies_tried: list[str] = []

    try:
        if song["artist"] is None:
            song["artist"] = ""

        if song["album"]:
            search_strategies_tried.append("album_title")
            tracks = plugin.music.searchTracks(
                **{"album.title": song["album"], "track.title": song["title"]}, limit=50
            )
            plugin._log.debug("Strategy 1 (Album+Title): Found {} tracks", len(tracks))

        if len(tracks) == 0:
            search_strategies_tried.append("title_only")
            tracks = plugin.music.searchTracks(**{"track.title": song["title"]}, limit=50)
            plugin._log.debug("Strategy 2 (Title-only): Found {} tracks", len(tracks))

        if len(tracks) == 0 and song.get("artist"):
            search_strategies_tried.append("artist_title")
            artist_variants = _split_artist_variants(song["artist"])
            search_artists = artist_variants or [song["artist"]]
            unique_tracks = {}
            for artist_variant in search_artists:
                if not artist_variant:
                    continue
                candidate_tracks = plugin.music.searchTracks(
                    **{"artist.title": artist_variant, "track.title": song["title"]},
                    limit=50,
                )
                plugin._log.debug(
                    "Strategy 3 (Artist+Title): Artist '{}' -> {} tracks",
                    artist_variant,
                    len(candidate_tracks),
                )
                for track in candidate_tracks:
                    rating_key = getattr(track, "ratingKey", None)
                    key = rating_key if rating_key is not None else id(track)
                    if key not in unique_tracks:
                        unique_tracks[key] = track
            tracks = list(unique_tracks.values())

        if len(tracks) == 0 and song.get("artist") and song.get("title"):
            try:
                search_strategies_tried.append("artist_fuzzy_title")
                fuzzy_query = clean_text_for_matching(song["title"])
                artist_variants = _split_artist_variants(song["artist"])
                search_artists = artist_variants or [song["artist"]]
                unique_tracks = {}
                for artist_variant in search_artists:
                    if not artist_variant:
                        continue
                    candidate_tracks = plugin.music.searchTracks(
                        **{"artist.title": artist_variant, "track.title": fuzzy_query},
                        limit=100,
                    )
                    plugin._log.debug(
                        "Strategy 4 (Artist+Fuzzy Title): Artist '{}' Query '{}' -> {} tracks",
                        artist_variant,
                        fuzzy_query,
                        len(candidate_tracks),
                    )
                    for track in candidate_tracks:
                        rating_key = getattr(track, "ratingKey", None)
                        key = rating_key if rating_key is not None else id(track)
                        if key not in unique_tracks:
                            unique_tracks[key] = track
                tracks = list(unique_tracks.values())
                if not tracks and artist_variants:
                    loose_candidates = plugin.music.searchTracks(
                        **{"track.title": fuzzy_query}, limit=100
                    )
                    plugin._log.debug(
                        "Strategy 4 (Artist+Fuzzy Title relaxed): Query '{}' -> {} tracks before filtering",
                        fuzzy_query,
                        len(loose_candidates),
                    )
                    filtered_tracks = [
                        track
                        for track in loose_candidates
                        if _track_matches_artist_variants(track, artist_variants)
                    ]
                    plugin._log.debug(
                        "Strategy 4 (Artist+Fuzzy Title relaxed): Filtered to {} tracks",
                        len(filtered_tracks),
                    )
                    tracks = filtered_tracks
            except Exception as exc:  # noqa: BLE001 - log but continue
                plugin._log.debug("Artist+fuzzy search strategy failed: {}", exc)

        if len(tracks) == 0 and song.get("album"):
            search_strategies_tried.append("album_only")
            tracks = plugin.music.searchTracks(
                **{"album.title": song["album"]}, limit=150
            )
            plugin._log.debug("Strategy 5 (Album-only): Found {} tracks", len(tracks))

        if len(tracks) == 0 and song.get("artist"):
            search_strategies_tried.append("artist_only")
            tracks = plugin.music.searchTracks(
                **{"artist.title": song["artist"]}, limit=150
            )
            plugin._log.debug("Strategy 6 (Artist-only): Found {} tracks", len(tracks))

        if len(tracks) == 0 and song.get("title"):
            try:
                search_strategies_tried.append("fuzzy_title")
                fuzzy_query = clean_text_for_matching(song["title"])
                tracks = plugin.music.searchTracks(
                    **{"track.title": fuzzy_query}, limit=100
                )
                plugin._log.debug(
                    "Strategy 7 (Fuzzy Title): Query '{}' -> {} tracks",
                    fuzzy_query,
                    len(tracks),
                )
            except Exception as exc:  # noqa: BLE001 - log but continue
                plugin._log.debug("Fuzzy search strategy failed: {}", exc)

    except Exception as exc:  # noqa: BLE001 - catch plexapi errors and continue
        plugin._log.debug(
            "Error during multi-strategy search for {} - {}. Error: {}",
            song.get("album", ""),
            song.get("title", ""),
            exc,
        )
        return None

    if len(tracks) == 1:
        result = tracks[0]
        plugin._cache_result(cache_key, result)
        return result
    if len(tracks) > 1:
        sorted_tracks = plugin.find_closest_match(song, tracks)
        plugin._log.debug(
            "Found {} tracks for {} using strategies: {}",
            len(sorted_tracks),
            song["title"],
            ", ".join(search_strategies_tried),
        )

        if manual_search and sorted_tracks:
            result = plugin._handle_manual_search(sorted_tracks, song, original_query=song)
            if result is not None:
                plugin._cache_result(cache_key, result)
            return result

        best_match = sorted_tracks[0]
        if best_match[1] >= 0.7:
            plugin._cache_result(cache_key, best_match[0])
            return best_match[0]
        plugin._log.debug(
            "Best match score {} below threshold for: {}", best_match[1], song["title"]
        )

    cleaned_metadata_for_negative = None
    if (
        not llm_attempted
        and plugin.search_llm
        and get_plexsync_config("use_llm_search", bool, False)
    ):
        search_query = f"{song['title']} by {song['artist']}"
        if song.get('album'):
            search_query += f" from {song['album']}"

        plugin._log.debug(
            "Attempting LLM cleanup for: {} using strategies: {}",
            search_query,
            ", ".join(search_strategies_tried),
        )
        cleaned_metadata = search_track_info(search_query)
        if cleaned_metadata:
            cleaned_song = {
                "title": cleaned_metadata.get("title", song["title"]),
                "album": cleaned_metadata.get("album", song.get("album")),
                "artist": cleaned_metadata.get("artist", song.get("artist")),
            }
            plugin._log.debug("Using LLM cleaned metadata: {}", cleaned_song)

            result = search_plex_song(plugin, cleaned_song, False, llm_attempted=True)
            if result is not None:
                plugin._log.debug(
                    "LLM-cleaned search succeeded, caching for original query: {}",
                    song,
                )
                plugin._cache_result(cache_key, result)
                return result
            cleaned_metadata_for_negative = cleaned_song

    if manual_search:
        plugin._log.info(
            "\nTrack {} - {} - {} not found in Plex (tried strategies: {})",
            song.get("album", "Unknown"),
            song.get("artist", "Unknown"),
            song["title"],
            ", ".join(search_strategies_tried) if search_strategies_tried else "none",
        )
        prompt = ui.colorize('text_highlight', "\nSearch manually?") + " (Y/n)"
        if ui.input_yn(prompt):
            result = plugin.manual_track_search(song)
            if result is not None:
                plugin._log.debug("Manual search succeeded, caching for original query: {}", song)
                plugin._cache_result(cache_key, result)
                return result

    plugin._log.debug(
        "All search strategies failed for: {} (tried: {})",
        song,
        ", ".join(search_strategies_tried) if search_strategies_tried else "none",
    )
    if cleaned_metadata_for_negative is not None:
        plugin._cache_result(cache_key, None, cleaned_metadata_for_negative)
    else:
        plugin._cache_result(cache_key, None)
    return None
