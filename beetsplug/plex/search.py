"""Shared Plex search helpers extracted from plexsync."""

from __future__ import annotations
import re

from beets import ui
from beets.util.color import colorize

from beetsplug.core.config import get_plexsync_config
from beetsplug.utils.prompt_logging import prompt_guard
from beetsplug.ai.llm import search_track_info
from beetsplug.core.matching import clean_text_for_matching, get_fuzzy_score
from beetsplug.plex import manual_search as manual_search_ui
from beetsplug.plex.queues import LLMEnhancementItem, ManualPromptItem


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

    # Exact substring match first (fastest)
    for variant in variants:
        if variant and variant.lower() in lower_artist:
            return True

    # Fuzzy fallback: split compound artist strings (feat., &, etc.) and compare each segment
    # so that "Beyonce" matches "Beyoncé feat. Jay-Z" via its first segment
    artist_segments = [
        seg.strip()
        for part in _FEATURE_SPLIT_RE.split(artist_name)
        for seg in _ARTIST_JOINER_RE.split(part)
        if seg.strip()
    ]
    for variant in variants:
        if not variant or len(variant) < 4:
            continue
        for segment in artist_segments:
            # Skip segments shorter than 4 chars — sub-4-char strings can hit 0.85
            # on unrelated names (e.g. "Cher" vs "Che" = 0.857).
            if len(segment) >= 4 and get_fuzzy_score(variant, segment) >= 0.85:
                return True

    return False


def _exact_title_matches(tracks, query_title: str, artist_variants: list[str]):
    """Return the subset of tracks whose title exactly equals the query.

    Fuzzy title distance barely moves for near-identical titles that differ
    by only a short suffix (e.g. "..., Pt. 1" vs "..., Pt. 2" of the same
    medley/theme), so multiple distinct tracks can tie at the same
    find_closest_match score - and a stable sort then always returns
    whichever part happened to come first from Plex, regardless of which
    part was actually queried. An exact (case/whitespace-insensitive) title
    match - further narrowed by artist when available - must win before
    falling back to fuzzy ranking, or a request for Pt. 3 can silently
    resolve to Pt. 1.
    """
    normalized_query = (query_title or "").strip().lower()
    if not normalized_query:
        return []
    exact = [
        track
        for track in tracks
        if (getattr(track, "title", "") or "").strip().lower() == normalized_query
    ]
    if len(exact) > 1 and artist_variants:
        artist_filtered = [t for t in exact if _track_matches_artist_variants(t, artist_variants)]
        if artist_filtered:
            exact = artist_filtered
    return exact


def _log_cache_match_details(plugin, cache_key: str, track) -> None:
    """Log the Plex track metadata before caching the match."""
    if track is None:
        return
    try:
        rating_key = getattr(track, "ratingKey", None)
        title = getattr(track, "title", "") or "<unknown>"
        album = getattr(track, "parentTitle", "") or "<unknown>"
        try:
            artist = getattr(track, "originalTitle", None) or track.artist().title
        except Exception:  # noqa: BLE001 - tolerate Plex API lookup issues
            artist = ""
        artist = artist or "<unknown>"
        plugin._log.debug(
            "Caching result for key '{}' -> title='{}', artist='{}', album='{}', rating_key={}",
            cache_key,
            title,
            artist,
            album,
            rating_key,
        )
    except Exception as exc:  # noqa: BLE001 - logging should never break caching
        plugin._log.debug("Caching result for key '{}' but failed to collect metadata: {}", cache_key, exc)


def search_plex_song(
    plugin,
    song,
    manual_search=None,
    llm_attempted=False,
    use_local_candidates=True,
    playlist_id=None,
    use_cache=True,
):
    """Fetch a Plex track using multi-strategy search for the given song.

    Parameters mirror the original PlexSync.search_plex_song method but
    take the plugin instance explicitly so this function can be reused by other
    callers.

    ``use_cache=False`` skips both the initial cache *read* and every cache
    *write* for this call. Callers that need a fresh, authoritative search
    against live Plex state (e.g. a forced library resync) should pass
    ``use_cache=False`` rather than relying on a cache table populated by
    unrelated features (playlist import, etc.) - and, importantly, rather
    than paying for a SQLite write (new connection + commit, no WAL) on
    every single item: under a threaded bulk resync those writes serialize
    on the cache db's write lock and can dominate wall-clock time.
    """
    if manual_search is None:
        manual_search = get_plexsync_config("manual_search", bool, False)

    # Normalize into a fresh dict with title/artist always present (as
    # strings) so the direct song["title"]/song["artist"] indexing below
    # can't raise KeyError on a song missing a key -- copied so we never
    # mutate the caller's original dict.
    if isinstance(song, dict):
        song = dict(song)
    else:
        song = {"title": str(song)}
    song["title"] = song.get("title") or ""
    song["artist"] = song.get("artist") or ""
    song.setdefault("album", None)

    cache_result = plugin._cache_result if use_cache else (lambda *a, **k: None)

    cache_key = plugin.cache._make_cache_key(song)
    plugin._log.debug("Generated cache key: '{}' for song: {}", cache_key, song)
    if not hasattr(plugin, "_candidate_confirmations"):
        plugin._candidate_confirmations = []
    depth = getattr(plugin, "_candidate_confirmation_depth", 0)
    if depth == 0:
        plugin._candidate_confirmations = []
    plugin._candidate_confirmation_depth = depth + 1

    def _finish(result=None):
        plugin._candidate_confirmation_depth -= 1
        if plugin._candidate_confirmation_depth <= 0:
            plugin._candidate_confirmation_depth = 0
            if hasattr(plugin, "_candidate_confirmations"):
                plugin._candidate_confirmations = []
        return result

    cached_result = plugin.cache.get(cache_key) if use_cache else None
    if not use_cache:
        plugin._log.debug("Cache read bypassed for key: '{}'", cache_key)
    elif cached_result is not None:
        plugin._log.debug("Cache HIT for key: '{}' -> result: {}", cache_key, cached_result)
    else:
        plugin._log.debug("Cache MISS for key: '{}'", cache_key)

    if cached_result is not None:
        if isinstance(cached_result, tuple):
            rating_key, cleaned_metadata = cached_result
            if rating_key == -1 or rating_key is None:
                if cleaned_metadata and not llm_attempted:
                    plugin._log.debug("Using cached cleaned metadata: {}", cleaned_metadata)
                    result = search_plex_song(plugin, cleaned_metadata, False, llm_attempted=True, playlist_id=playlist_id)
                    if result is not None:
                        plugin._log.debug(
                            "Cached cleaned metadata search succeeded, updating original cache: {}",
                            song,
                        )
                        cache_result(cache_key, result)
                        return _finish(result)
                    plugin._log.debug(
                        "Cached cleaned metadata search also failed, respecting original skip for: {}",
                        song,
                    )
                    return _finish(None)
                plugin._log.debug("Found cached skip result for: {}", song)
                return _finish(None)

            try:
                if rating_key:
                    cached_track = plugin.music.fetchItem(rating_key)
                    plugin._log.debug("Found cached match for: {} -> {}", song, cached_track.title)
                    return _finish(cached_track)
            except Exception as exc:  # noqa: BLE001 - want to log original cache issue
                plugin._log.debug("Failed to fetch cached item {}: {}", rating_key, exc)
                plugin.cache.set(cache_key, None)
        else:
            if cached_result == -1:
                plugin._log.debug("Found legacy cached skip result for: {}", song)
                return _finish(None)
            try:
                if cached_result:
                    cached_track = plugin.music.fetchItem(cached_result)
                    plugin._log.debug(
                        "Found legacy cached match for: {} -> {}", song, cached_track.title
                    )
                    return _finish(cached_track)
            except Exception as exc:  # noqa: BLE001 - log for debugging
                plugin._log.debug("Failed to fetch legacy cached item {}: {}", cached_result, exc)
                plugin.cache.set(cache_key, None)

    candidate_variants: list[tuple[dict[str, str], float]] = []
    local_candidates = []
    if use_local_candidates and hasattr(plugin, "get_local_beets_candidates"):
        try:
            local_candidates = plugin.get_local_beets_candidates(song)
        except Exception as exc:  # noqa: BLE001
            plugin._log.debug("Local beets candidate lookup failed for {}: {}", song, exc)
            local_candidates = []

        # If no candidates, retry with swapped title/artist (handles misparse from providers)
        if not local_candidates and song.get("title") and song.get("artist"):
            swapped = {"title": song["artist"], "artist": song["title"], "album": song.get("album")}
            try:
                local_candidates = plugin.get_local_beets_candidates(swapped)
                if local_candidates:
                    plugin._log.debug(
                        "Retrying local candidate search with swapped title/artist for '{}'",
                        song.get("title", ""),
                    )
            except Exception as exc:  # noqa: BLE001
                plugin._log.debug("Swapped title/artist candidate lookup failed: {}", exc)
                local_candidates = []

        if local_candidates:
            summary = [
                f"{cand.metadata.get('title', '')} ({cand.score:.2f})"
                for cand in local_candidates[:3]
            ]
            plugin._log.debug(
                "Local beets candidates for '{}': {}", song.get("title", ""), ", ".join(summary)
            )

            if hasattr(plugin, "_try_candidate_direct_match"):
                for candidate in local_candidates[:3]:
                    direct_match = plugin._try_candidate_direct_match(candidate, song, cache_key)
                    if direct_match is not None:
                        plugin._log.debug(
                            "Resolved '{}' via cached Plex ratingKey using beets metadata",
                            song.get("title", ""),
                        )
                        _log_cache_match_details(plugin, cache_key, direct_match)
                        cache_result(cache_key, direct_match)
                        return _finish(direct_match)

            if hasattr(plugin, "_prepare_candidate_variants"):
                candidate_variants = plugin._prepare_candidate_variants(local_candidates, song)

    if candidate_variants:
        variant_attempted = False
        for variant_metadata, variant_score in candidate_variants[:5]:
            title = (variant_metadata.get("title") or "").strip()
            album = (variant_metadata.get("album") or "").strip()
            artist = (variant_metadata.get("artist") or "").strip()

            # Skip variants that provide no useful metadata
            if not any((title, album, artist)):
                continue

            variant_attempted = True
            plugin._log.debug(
                "Trying beets vector candidate variant (score {:.2f}): title='{}', album='{}', artist='{}'",
                variant_score,
                title or "<unknown>",
                album or "<unknown>",
                artist or "<unknown>",
            )

            # Avoid recursion loops by disabling local candidate lookup in nested calls.
            variant_song = {"title": title, "album": album, "artist": artist}
            try:
                variant_result = search_plex_song(
                    plugin,
                    variant_song,
                    manual_search=False,
                    llm_attempted=True,
                    use_local_candidates=False,
                    playlist_id=playlist_id,
                )
            except RecursionError as exc:  # pragma: no cover - defensive
                plugin._log.debug("Variant recursion failed for {}: {}", variant_song, exc)
                continue

            if variant_result is not None:
                if hasattr(plugin, "_match_score_for_query"):
                    similarity = plugin._match_score_for_query(song, variant_result)
                    plugin._log.debug(
                        "Variant '{}' similarity to query '{}' -> {:.2f}",
                        title or "<unknown>",
                        song.get("title", ""),
                        similarity,
                    )
                    if similarity < 0.8:
                        plugin._log.debug(
                            "Rejecting variant '{}' for '{}' due to low similarity ({:.2f})",
                            title or "<unknown>",
                            song.get("title", ""),
                            similarity,
                        )
                        if hasattr(plugin, "_queue_candidate_confirmation") and cache_key:
                            plugin._queue_candidate_confirmation(
                                track=variant_result,
                                similarity=similarity,
                                cache_key=cache_key,
                                source="variant",
                                original_song=song,
                            )
                        continue

                plugin._log.debug(
                    "Resolved '{}' via beets candidate variant '{}'",
                    song.get("title", ""),
                    title,
                )
                _log_cache_match_details(plugin, cache_key, variant_result)
                cache_result(cache_key, variant_result)
                return _finish(variant_result)

        if variant_attempted:
            # Record that we attempted local candidate variants for debugging output.
            search_strategies_marker = "beets_variant"
        else:
            search_strategies_marker = None
    else:
        search_strategies_marker = None

    tracks = []
    search_strategies_tried: list[str] = []
    if search_strategies_marker:
        search_strategies_tried.append(search_strategies_marker)
    
    # Store results from Strategy 2 (Title-only) for reuse in other strategies
    title_only_tracks = []

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
            # Store results for reuse in other strategies
            title_only_tracks = tracks[:]

        if len(tracks) == 0 and song.get("artist"):
            search_strategies_tried.append("artist_title")
            artist_variants = _split_artist_variants(song["artist"])
            search_artists = artist_variants or [song["artist"]]
            unique_tracks = {}
            
            # Optimization: If we have title-only results, filter them instead of making new API calls
            if title_only_tracks:
                plugin._log.debug("Reusing Strategy 2 results for Strategy 3 (Artist+Title)")
                filtered_tracks = [
                    track for track in title_only_tracks
                    if _track_matches_artist_variants(track, artist_variants)
                ]
                plugin._log.debug(
                    "Strategy 3 (Artist+Title): Filtered {} tracks from Strategy 2 results",
                    len(filtered_tracks),
                )
                # Deduplicate filtered tracks
                for track in filtered_tracks:
                    rating_key = getattr(track, "ratingKey", None)
                    key = rating_key if rating_key is not None else id(track)
                    if key not in unique_tracks:
                        unique_tracks[key] = track
            else:
                # Original approach when no title-only results are available
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
                
                # Optimization: If we have title-only results, filter them instead of making new API calls
                if title_only_tracks:
                    plugin._log.debug("Reusing Strategy 2 results for Strategy 4 (Artist+Fuzzy Title)")
                    # Filter by artist and apply fuzzy matching to title
                    filtered_tracks = []
                    for track in title_only_tracks:
                        if _track_matches_artist_variants(track, artist_variants):
                            # Apply fuzzy matching to the title
                            try:
                                track_title = getattr(track, "title", "")
                                if track_title:
                                    fuzzy_score = get_fuzzy_score(track_title, fuzzy_query)
                                    # Use a reasonable threshold for fuzzy matching
                                    if fuzzy_score >= 0.7:
                                        filtered_tracks.append(track)
                            except Exception:
                                # If fuzzy matching fails, include the track
                                filtered_tracks.append(track)
                    plugin._log.debug(
                        "Strategy 4 (Artist+Fuzzy Title): Filtered {} tracks from Strategy 2 results",
                        len(filtered_tracks),
                    )
                    # Deduplicate filtered tracks
                    for track in filtered_tracks:
                        rating_key = getattr(track, "ratingKey", None)
                        key = rating_key if rating_key is not None else id(track)
                        if key not in unique_tracks:
                            unique_tracks[key] = track
                else:
                    # Original approach when no title-only results are available
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
                
                # Fallback to relaxed search if still no tracks
                if not tracks and artist_variants:
                    if title_only_tracks:
                        # Even more optimization: filter title-only results for relaxed search
                        plugin._log.debug("Reusing Strategy 2 results for Strategy 4 relaxed search")
                        filtered_tracks = [
                            track
                            for track in title_only_tracks
                            if _track_matches_artist_variants(track, artist_variants)
                        ]
                        plugin._log.debug(
                            "Strategy 4 (Artist+Fuzzy Title relaxed): Filtered {} tracks from Strategy 2 results",
                            len(filtered_tracks),
                        )
                        tracks = filtered_tracks
                    else:
                        # Original approach
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
            # Optimization: Filter title-only results by album if available
            if title_only_tracks and song.get("album"):
                plugin._log.debug("Reusing Strategy 2 results for Strategy 5 (Album-only)")
                album_title = song["album"].lower()
                filtered_tracks = [
                    track for track in title_only_tracks
                    if getattr(track, "parentTitle", "").lower() == album_title
                ]
                plugin._log.debug(
                    "Strategy 5 (Album-only): Filtered {} tracks from Strategy 2 results",
                    len(filtered_tracks),
                )
                tracks = filtered_tracks
            else:
                # Original approach
                tracks = plugin.music.searchTracks(
                    **{"album.title": song["album"]}, limit=150
                )
                plugin._log.debug("Strategy 5 (Album-only): Found {} tracks", len(tracks))



        if len(tracks) == 0 and song.get("title"):
            try:
                search_strategies_tried.append("fuzzy_title")
                fuzzy_query = clean_text_for_matching(song["title"])
                # Optimization: Apply fuzzy matching to title-only results if available
                if title_only_tracks:
                    plugin._log.debug("Reusing Strategy 2 results for Strategy 6 (Fuzzy Title)")
                    filtered_tracks = []
                    for track in title_only_tracks:
                        try:
                            track_title = getattr(track, "title", "")
                            if track_title:
                                fuzzy_score = get_fuzzy_score(track_title, fuzzy_query)
                                # Use a reasonable threshold for fuzzy matching
                                if fuzzy_score >= 0.7:
                                    filtered_tracks.append(track)
                        except Exception:
                            # If fuzzy matching fails, include the track
                            filtered_tracks.append(track)
                    plugin._log.debug(
                        "Strategy 6 (Fuzzy Title): Filtered {} tracks from Strategy 2 results",
                        len(filtered_tracks),
                    )
                    tracks = filtered_tracks
                else:
                    # Original approach
                    tracks = plugin.music.searchTracks(
                        **{"track.title": fuzzy_query}, limit=100
                    )
                    plugin._log.debug(
                        "Strategy 6 (Fuzzy Title): Query '{}' -> {} tracks",
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
        return _finish(None)

    if len(tracks) == 1:
        result = tracks[0]
        accept_result = True
        similarity = None
        if hasattr(plugin, "_match_score_for_query"):
            similarity = plugin._match_score_for_query(song, result)
            plugin._log.debug(
                "Single-track search result similarity for '{}' -> {:.2f}",
                song.get("title", ""),
                similarity,
            )
            if similarity < 0.8:
                plugin._log.debug(
                    "Rejecting single-track result for '{}' due to low similarity ({:.2f})",
                    song.get("title", ""),
                    similarity,
                )
                if hasattr(plugin, "_queue_candidate_confirmation") and cache_key:
                    plugin._queue_candidate_confirmation(
                        track=result,
                        similarity=similarity,
                        cache_key=cache_key,
                        source="single",
                        original_song=song,
                    )
                accept_result = False
        if accept_result:
            _log_cache_match_details(plugin, cache_key, result)
            cache_result(cache_key, result)
            return _finish(result)
        tracks = []
    if len(tracks) > 1:
        exact_title_matches = _exact_title_matches(
            tracks, song.get("title", ""), _split_artist_variants(song.get("artist"))
        )
        if exact_title_matches:
            plugin._log.debug(
                "Narrowed {} candidates to {} exact title match(es) for '{}' before fuzzy ranking",
                len(tracks),
                len(exact_title_matches),
                song.get("title", ""),
            )
            tracks = exact_title_matches
        sorted_tracks = plugin.find_closest_match(song, tracks)
        plugin._log.debug(
            "Found {} tracks for {} using strategies: {}",
            len(sorted_tracks),
            song["title"],
            ", ".join(search_strategies_tried),
        )

        if manual_search and sorted_tracks:
            manual_queue = getattr(plugin, "_manual_prompt_queue", None)
            manual_queue_enabled = get_plexsync_config(
                ["search", "manual_prompt_queue_enabled"],
                bool,
                True,
            )
            if playlist_id and manual_queue is not None and manual_queue_enabled:
                # Convert sorted_tracks to candidate format and queue
                candidates = [
                    {
                        "track": track,
                        "similarity": score,
                        "cache_key": cache_key,
                        "source": ", ".join(search_strategies_tried),
                        "song": dict(song),
                    }
                    for track, score in sorted_tracks
                ]
                manual_queue.enqueue(
                    ManualPromptItem(
                        song=dict(song),
                        cache_key=cache_key,
                        candidates=candidates,
                        search_strategies_tried=list(search_strategies_tried),
                        playlist_id=str(playlist_id),
                    )
                )
                return _finish(None)
            # Fallback: show prompt immediately
            result = plugin._handle_manual_search(sorted_tracks, song, original_query=song)
            if result is not None:
                _log_cache_match_details(plugin, cache_key, result)
                cache_result(cache_key, result)
            return _finish(result)

        best_match = sorted_tracks[0]
        if best_match[1] >= 0.7:
            _log_cache_match_details(plugin, cache_key, best_match[0])
            cache_result(cache_key, best_match[0])
            return _finish(best_match[0])
        plugin._log.debug(
            "Best match score {} below threshold for: {}", best_match[1], song["title"]
        )

    cleaned_metadata_for_negative = None
    _candidate_confirmations = getattr(plugin, "_candidate_confirmations", None)
    _has_good_candidates = bool(
        _candidate_confirmations
        and any(c.get("similarity", 0) >= 0.7 for c in _candidate_confirmations)
    )
    if (
        not llm_attempted
        and plugin.search_llm
        and get_plexsync_config("use_llm_search", bool, False)
        and not _has_good_candidates
    ):
        search_query = f"{song['title']} by {song['artist']}"
        if song.get('album'):
            search_query += f" from {song['album']}"

        llm_queue = getattr(plugin, "_llm_enhancement_queue", None)
        background_enabled = get_plexsync_config(["llm", "background_enhancement"], bool, True)
        if playlist_id and llm_queue is not None and background_enabled:
            plugin._log.debug(
                "Enqueueing LLM cleanup for: {} using strategies: {}",
                search_query,
                ", ".join(search_strategies_tried),
            )
            llm_queue.enqueue(
                LLMEnhancementItem(
                    cache_key=cache_key,
                    search_query=search_query,
                    song=dict(song),
                    playlist_id=str(playlist_id),
                )
            )
        else:
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

                result = search_plex_song(plugin, cleaned_song, False, llm_attempted=True, playlist_id=playlist_id)
                if result is not None:
                    plugin._log.debug(
                        "LLM-cleaned search succeeded, caching for original query: {}",
                        song,
                    )
                    _log_cache_match_details(plugin, cache_key, result)
                    cache_result(cache_key, result)
                    return _finish(result)
                cleaned_metadata_for_negative = cleaned_song

    if manual_search:
        manual_queue = getattr(plugin, "_manual_prompt_queue", None)
        manual_queue_enabled = get_plexsync_config(
            ["search", "manual_prompt_queue_enabled"],
            bool,
            True,
        )
        plugin._log.debug(
            "Manual prompt queue check: playlist_id={}, manual_queue={}, manual_queue_enabled={}",
            playlist_id,
            "initialized" if manual_queue is not None else "None",
            manual_queue_enabled,
        )
        if playlist_id and manual_queue is not None and manual_queue_enabled:
            candidate_queue = getattr(plugin, "_candidate_confirmations", None)
            candidates = list(candidate_queue or [])
            if hasattr(plugin, "_candidate_confirmations"):
                plugin._candidate_confirmations = []
            manual_queue.enqueue(
                ManualPromptItem(
                    song=dict(song),
                    cache_key=cache_key,
                    candidates=candidates,
                    search_strategies_tried=list(search_strategies_tried),
                    playlist_id=str(playlist_id),
                )
            )
            return _finish(None)

        manual_prompt_needed = True
        candidate_queue = getattr(plugin, "_candidate_confirmations", None)
        if candidate_queue:
            selection = manual_search_ui.review_candidate_confirmations(
                plugin,
                list(candidate_queue),
                song,
                current_cache_key=cache_key,
            )
            if hasattr(plugin, "_candidate_confirmations"):
                plugin._candidate_confirmations = []

            action = selection.get("action")
            if action == "selected":
                track = selection.get("track")
                if track is not None:
                    chosen_cache_key = selection.get("cache_key") or cache_key
                    sources = selection.get("sources") or []
                    original_song = selection.get("original_song") or song
                    title = getattr(track, "title", "") or "<unknown>"
                    plugin._log.debug(
                        "User accepted queued candidate '{}' (sources: {}) for '{}'",
                        title,
                        ", ".join(sources) if sources else "candidate",
                        original_song.get("title", ""),
                    )
                    _log_cache_match_details(plugin, chosen_cache_key, track)
                    cache_result(chosen_cache_key, track)
                    return _finish(track)
            elif action == "manual":
                manual_prompt_needed = False
                manual_query = selection.get("original_song") or song
                result = plugin.manual_track_search(manual_query)
                if result is not None:
                    plugin._log.debug(
                        "Manual search succeeded, caching for original query: {}", manual_query
                    )
                    _log_cache_match_details(plugin, cache_key, result)
                    cache_result(cache_key, result)
                    return _finish(result)
            elif action == "abort":
                return _finish(None)
            elif action == "skip":
                # User chose to skip - cache negative result and return immediately
                plugin._log.debug(
                    "User skipped candidate review for: {}", song.get("title", "")
                )
                if cleaned_metadata_for_negative is not None:
                    cache_result(cache_key, None, cleaned_metadata_for_negative)
                else:
                    cache_result(cache_key, None)
                return _finish(None)
            else:
                manual_prompt_needed = True
        else:
            if hasattr(plugin, "_candidate_confirmations"):
                plugin._candidate_confirmations = []

        if manual_prompt_needed:
            plugin._log.info(
                "\nTrack {} - {} - {} not found in Plex (tried strategies: {})",
                song.get("album", "Unknown"),
                song.get("artist", "Unknown"),
                song["title"],
                ", ".join(search_strategies_tried) if search_strategies_tried else "none",
            )
            prompt = colorize('text_highlight', "\nSearch manually?") + " (Y/n)"
            with prompt_guard():
                if ui.input_yn(prompt):
                    result = plugin.manual_track_search(song)
                    if result is not None:
                        plugin._log.debug(
                            "Manual search succeeded, caching for original query: {}", song
                        )
                        _log_cache_match_details(plugin, cache_key, result)
                        cache_result(cache_key, result)
                        return _finish(result)

    plugin._log.debug(
        "All search strategies failed for: {} (tried: {})",
        song,
        ", ".join(search_strategies_tried) if search_strategies_tried else "none",
    )
    if cleaned_metadata_for_negative is not None:
        cache_result(cache_key, None, cleaned_metadata_for_negative)
    else:
        cache_result(cache_key, None)
    return _finish(None)
