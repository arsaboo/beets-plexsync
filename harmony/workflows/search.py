"""Backend-agnostic search pipeline with multi-strategy matching and manual confirmation."""

from __future__ import annotations

import re
import logging
import time
from typing import Optional, List, Dict, Any

from harmony.core.matching import plex_track_distance, clean_text_for_matching
from harmony.core.cache import Cache
from harmony.models import Track

logger = logging.getLogger("harmony.search")

_ARTIST_JOINER_RE = re.compile(r"\s*(?:,|;|&| and |\+|/)\s*")
_FEATURE_SPLIT_RE = re.compile(r"\s*(?:feat\.?|ft\.?|featuring|with)\s+", re.IGNORECASE)


def _get_negative_cache_ttl(harmony_app) -> int:
    """Get the negative cache TTL from harmony_app config, with fallback."""
    if harmony_app and hasattr(harmony_app, 'negative_cache_ttl'):
        return harmony_app.negative_cache_ttl
    return 30  # Default: 30 days


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

    main_section = (
        _FEATURE_SPLIT_RE.split(normalized, maxsplit=1)[0].strip() if normalized else ""
    )
    add_variant(main_section)

    for source in filter(None, [normalized, main_section]):
        for part in _ARTIST_JOINER_RE.split(source):
            add_variant(part)

    return variants


def _track_matches_artist_variants(track: dict, variants: list[str]) -> bool:
    """Check if any candidate artist appears in the track artist string.
    
    Uses exact substring matching first, then falls back to fuzzy matching
    for close matches (similarity >= 0.85).
    """
    if not variants:
        return True

    artist_name = track.get("artist", "")
    if not artist_name:
        return False

    lower_artist = artist_name.lower()
    
    # Try exact substring match first (fastest)
    for variant in variants:
        if variant and variant.lower() in lower_artist:
            return True
    
    # Fall back to fuzzy matching for close matches
    from harmony.core.matching import calculate_string_similarity
    for variant in variants:
        if variant and calculate_string_similarity(variant, artist_name) >= 0.85:
            return True
    
    return False


def search_backend_song(
    backend,
    cache: Cache,
    vector_index,
    song: dict,
    beets_vector_index=None,
    beets_lookup=None,
    manual_search: bool = False,
    llm_attempted: bool = False,
    use_local_candidates: bool = True,
    llm_agent = None,
    candidate_queue: Optional[List[Dict]] = None,
    matching_module = None,
    harmony_app = None,  # For incremental refresh support
) -> Optional[dict]:
    """Fetch a track using multi-strategy search.

    This implements a 5-stage search pipeline:
    1. Cache lookup
    2. Local vector index candidates (with direct ratingKey matching)
    3. Multi-strategy backend search (6 strategies)
    4. LLM enhancement (if enabled and not already attempted)
    5. Manual search with candidate confirmation queue

    Args:
    backend: Backend instance for search operations
        cache: Cache instance for caching results
        vector_index: VectorIndex instance for local candidate lookup
        song: Dict with 'title', 'artist', 'album' keys
        manual_search: Whether to prompt user for manual confirmation
        llm_attempted: Internal flag to prevent recursion into LLM search
        use_local_candidates: Whether to use vector index candidates
        llm_agent: Optional LLM agent for metadata enhancement
        candidate_queue: Optional list to queue candidates for confirmation
        matching_module: Module containing plex_track_distance function
        beets_vector_index: Optional VectorIndex built from beets items
        beets_lookup: Optional dict of plex_ratingkey -> beets item/track

    Returns:
        Dict with track metadata (title, artist, album, backend_id) or None
    """
    start_time = time.perf_counter()
    timing_ms: dict[str, float] = {}

    def _record_timing(name: str, start: float) -> None:
        elapsed = (time.perf_counter() - start) * 1000.0
        timing_ms[name] = timing_ms.get(name, 0.0) + elapsed

    def _log_timing() -> None:
        total_ms = (time.perf_counter() - start_time) * 1000.0
        accounted_ms = sum(timing_ms.values())
        timing_ms["unaccounted_ms"] = max(total_ms - accounted_ms, 0.0)
        title = (song.get("title") or "").strip()
        artist = (song.get("artist") or "").strip()
        album = (song.get("album") or "").strip()
        logger.debug(
            "Search timing title='%s' artist='%s' album='%s' total_ms=%.1f stages=%s",
            title,
            artist,
            album,
            total_ms,
            timing_ms,
        )

    def _return(value: Optional[dict]) -> Optional[dict]:
        _log_timing()
        return value

    def _cache_set(*args, **kwargs) -> None:
        stage_start = time.perf_counter()
        cache.set(*args, **kwargs)
        _record_timing("cache_write_ms", stage_start)

    cache_key = cache._make_cache_key(song)
    logger.debug(f"Generated cache key: '{cache_key}' for song: {song}")

    # Stage 1: Cache Lookup
    cached_result = cache.get(cache_key)
    if cached_result is not None:
        logger.debug(f"Cache HIT for key: '{cache_key}' -> result: {cached_result}")
        if isinstance(cached_result, tuple):
            rating_key, cleaned_metadata = cached_result
            if rating_key == -1 or rating_key is None:
                if cleaned_metadata and not llm_attempted:
                    logger.debug(f"Using cached cleaned metadata: {cleaned_metadata}")
                    result = search_backend_song(
                        backend,
                        cache,
                        vector_index,
                        cleaned_metadata,
                        manual_search=False,
                        llm_attempted=True,
                        use_local_candidates=False,
                        llm_agent=llm_agent,
                        candidate_queue=candidate_queue,
                        matching_module=matching_module,
                    )
                    if result is not None:
                        logger.debug(
                            f"Cached cleaned metadata search succeeded, "
                            f"updating original cache: {song}"
                        )
                        _cache_set(
                            cache_key,
                            result.get("backend_id") or result.get("plex_ratingkey"),
                            result,
                        )
                        return _return(result)
                logger.debug(f"Found cached skip result for: {song}")
                return _return(None)
            if rating_key:
                try:
                    fetched = backend.get_track(str(rating_key))
                    if fetched:
                        logger.debug(f"Found cached match for: {song} -> {fetched.title}")
                        return _return(_track_to_dict(fetched))
                except Exception as exc:
                    logger.debug(f"Failed to fetch cached item {rating_key}: {exc}")
                    _cache_set(cache_key, None)
        else:
            # Legacy cached result (single int/str value)
            if cached_result == -1:
                logger.debug(f"Found legacy cached skip result for: {song}")
                return _return(None)
            if cached_result:
                try:
                    fetched = backend.get_track(str(cached_result))
                    if fetched:
                        logger.debug(f"Found legacy cached match for: {song} -> {fetched.title}")
                        return _return(_track_to_dict(fetched))
                except Exception as exc:
                    logger.debug(f"Failed to fetch legacy cached item {cached_result}: {exc}")
                    _cache_set(cache_key, None)

    # Short-circuit: Cannot search without a title
    if not (song.get("title") or "").strip():
        logger.warning(f"Cannot search without a title, skipping: {song}")
        ttl = _get_negative_cache_ttl(harmony_app)
        _cache_set(cache_key, None, ttl_days=ttl)  # Cache the failure to avoid retries
        return _return(None)

    # Stage 2: Local Candidate Matching (beets + backend index)
    def _resolve_candidate_id(candidate_dict: dict) -> Optional[str]:
        rating_key = candidate_dict.get("backend_id") or candidate_dict.get("plex_ratingkey")
        if rating_key:
            return str(rating_key)
        provider_ids = candidate_dict.get("provider_ids")
        if isinstance(provider_ids, dict) and getattr(backend, "provider_name", ""):
            provider_key = provider_ids.get(backend.provider_name)
            if provider_key:
                return str(provider_key)
        return None

    def _process_local_candidates(local_candidates: list, source_label: str) -> None:
        if not local_candidates:
            return
        summary = [
            f"{cand.metadata.get('title', '')} ({score:.2f})"
            for cand, score in local_candidates[:3]
        ]
        logger.debug(
            f"Local {source_label} candidates for '{song.get('title', '')}': {', '.join(summary)}"
        )

        for candidate_entry, score in local_candidates[:3]:
            candidate_dict = dict(candidate_entry.metadata)
            
            # Use vector index metadata directly for similarity scoring (no network call)
            match_score = plex_track_distance(song, candidate_dict)
            logger.debug(
                f"Vector {source_label} candidate '{candidate_dict.get('title', '')}' "
                f"similarity {match_score.similarity:.2f}"
            )
            
            # High confidence match - fetch from Plex to confirm track exists
            if match_score.similarity >= 0.75:
                resolved_id = _resolve_candidate_id(candidate_dict)
                if resolved_id:
                    try:
                        fetched = backend.get_track(resolved_id)
                        if not fetched:
                            logger.debug(f"High-confidence match {resolved_id} no longer exists in Plex")
                            continue
                        # Use fetched track to ensure we have the latest metadata
                        track_dict = _track_to_dict(fetched)
                        logger.debug(
                            f"Resolved '{song.get('title', '')}' via {source_label} "
                            f"vector index with similarity {match_score.similarity:.2f}"
                        )
                        _cache_match(cache, cache_key, track_dict)
                        raise StopIteration(track_dict)
                    except StopIteration:
                        raise
                    except Exception as exc:
                        logger.debug(
                            f"Failed to fetch high-confidence candidate {resolved_id}: {exc}"
                        )
                        continue
            
            # Medium confidence match - queue for manual confirmation (no network call needed)
            elif match_score.similarity > 0.05 and candidate_queue is not None:
                logger.debug(
                    f"Queueing {source_label} candidate '{candidate_dict.get('title', '')}' "
                    f"with similarity {match_score.similarity:.2f} for confirmation"
                )
                candidate_queue.append({
                    "track": candidate_dict,
                    "similarity": match_score.similarity,
                    "cache_key": cache_key,
                    "source": source_label,
                    "song": dict(song),
                })

    if use_local_candidates:
        stage_start = time.perf_counter()
        try:
            if beets_vector_index is not None:
                query_counts, query_norm = beets_vector_index.build_query_vector(song)
                beets_candidates = beets_vector_index.candidate_scores(
                    query_counts, query_norm, limit=5
                )
                _process_local_candidates(beets_candidates, "beets")
            if vector_index is not None:
                query_counts, query_norm = vector_index.build_query_vector(song)
                local_candidates = vector_index.candidate_scores(
                    query_counts, query_norm, limit=5
                )
                _process_local_candidates(local_candidates, "backend")
        except StopIteration as result:
            return _return(result.args[0])
        except Exception as exc:
            logger.debug(f"Local candidate lookup failed for {song}: {exc}")
        finally:
            _record_timing("local_candidates_ms", stage_start)

    # Multi-strategy search against backend
    tracks = []
    search_strategies_tried: list[str] = []

    # Store title-only results for reuse in multiple strategies
    title_only_tracks = []

    # Determine which strategies to use based on available metadata
    def select_search_strategies(song: dict) -> list[str]:
        """Select search strategies based on available metadata.
        
        Strategies are ordered by precision (most specific first).
        Strategies are skipped if required metadata is missing.
        """
        strategies = []
        
        has_album = bool((song.get("album") or "").strip())
        has_artist = bool((song.get("artist") or "").strip())
        
        # Album+Title: High precision when album is accurate
        if has_album:
            strategies.append("album_title")

        # Title-only: Always try (cached for reuse in later strategies)
        strategies.append("title_only")

        # Artist+Title: High precision for artist-centric searches
        if has_artist:
            strategies.append("artist_title")
        
        # Artist+Fuzzy Title: Handle typos and variations
        if has_artist:
            strategies.append("artist_fuzzy_title")
        
        # Album-only: Useful for soundtracks and compilations
        if has_album:
            strategies.append("album_only")
        
        # Fuzzy Title: Last resort for title variations
        strategies.append("fuzzy_title")
        
        return strategies

    selected_strategies = select_search_strategies(song)
    logger.debug(f"Selected strategies for search: {selected_strategies}")

    try:
        if song.get("artist") is None:
            song["artist"] = ""

        # Strategy 1: Album + Title
        if "album_title" in selected_strategies and len(tracks) == 0:
            search_strategies_tried.append("album_title")
            stage_start = time.perf_counter()
            try:
                tracks = backend.search_tracks(
                    title=song["title"], album=song["album"], limit=50
                )
                logger.debug(f"Strategy 1 (Album+Title): Found {len(tracks)} tracks")
            except Exception as exc:
                logger.debug(f"Strategy 1 failed: {exc}")
                tracks = []
            finally:
                _record_timing("strategy_album_title_ms", stage_start)

        # Strategy 2: Title only (cached for reuse in later strategies)
        if "title_only" in selected_strategies and len(tracks) == 0:
            search_strategies_tried.append("title_only")
            stage_start = time.perf_counter()
            try:
                title_tracks = backend.search_tracks(title=song["title"], limit=50)
                title_only_tracks = title_tracks[:]
                tracks = title_tracks
                logger.debug(f"Strategy 2 (Title-only): Found {len(tracks)} tracks")
            except Exception as exc:
                logger.debug(f"Strategy 2 failed: {exc}")
                title_only_tracks = []
                tracks = []
            finally:
                _record_timing("strategy_title_only_ms", stage_start)

        # Strategy 3: Artist + Title (high precision)
        if "artist_title" in selected_strategies and len(tracks) == 0:
            search_strategies_tried.append("artist_title")
            stage_start = time.perf_counter()
            artist_variants = _split_artist_variants(song["artist"])
            search_artists = artist_variants or [song["artist"]]
            unique_tracks = {}

            if title_only_tracks:
                logger.debug("Reusing title-only results for Strategy 3 (Artist+Title)")
                filtered_tracks = [
                    track
                    for track in title_only_tracks
                    if _track_matches_artist_variants(track, artist_variants)
                ]
                for track in filtered_tracks:
                    key = track.get("backend_id") or track.get("plex_ratingkey") or id(track)
                    if key not in unique_tracks:
                        unique_tracks[key] = track
            else:
                # Make parallel API calls for better performance
                from concurrent.futures import ThreadPoolExecutor, as_completed

                def search_single_artist(artist: str):
                    """Search for tracks with given artist variant."""
                    if not artist:
                        return []
                    try:
                        results = backend.search_tracks(
                            title=song["title"], artist=artist, limit=50
                        )
                        logger.debug(
                            f"Strategy 3 (Artist+Title): Artist '{artist}' "
                            f"-> {len(results)} tracks"
                        )
                        return results
                    except Exception as exc:
                        logger.debug(
                            f"Strategy 3 artist variant '{artist}' failed: {exc}"
                        )
                        return []

                # Execute searches in parallel (up to 4 concurrent threads)
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = {
                        executor.submit(search_single_artist, artist): artist
                        for artist in search_artists
                    }

                    for future in as_completed(futures):
                        candidate_tracks = future.result()
                        for track in candidate_tracks:
                            key = (
                                track.get("backend_id")
                                or track.get("plex_ratingkey")
                                or id(track)
                            )
                            if key not in unique_tracks:
                                unique_tracks[key] = track

            tracks = list(unique_tracks.values())
            _record_timing("strategy_artist_title_ms", stage_start)

        # Strategy 4: Artist + Fuzzy Title
        if "artist_fuzzy_title" in selected_strategies and len(tracks) == 0:
            search_strategies_tried.append("artist_fuzzy_title")
            stage_start = time.perf_counter()
            fuzzy_query = clean_text_for_matching(song["title"])
            artist_variants = _split_artist_variants(song["artist"])
            search_artists = artist_variants or [song["artist"]]
            unique_tracks = {}

            try:
                # Optimization: Filter title-only results with fuzzy matching
                if title_only_tracks:
                    logger.debug(
                        "Reusing Strategy 2 results for Strategy 4 (Artist+Fuzzy Title)"
                    )
                    filtered_tracks = []
                    for track in title_only_tracks:
                        if _track_matches_artist_variants(track, artist_variants):
                            # Fuzzy match on title
                            track_title = track.get("title", "")
                            if track_title:
                                score = plex_track_distance(
                                    {"title": fuzzy_query}, track
                                ).similarity
                                if score >= 0.7:
                                    filtered_tracks.append(track)
                    logger.debug(
                        f"Strategy 4 (Artist+Fuzzy Title): Filtered {len(filtered_tracks)} "
                        f"tracks from Strategy 2 results"
                    )
                    for track in filtered_tracks:
                        key = track.get("backend_id") or track.get("plex_ratingkey") or id(track)
                        if key not in unique_tracks:
                            unique_tracks[key] = track
                else:
                    # Make parallel API calls for better performance
                    from concurrent.futures import ThreadPoolExecutor, as_completed
                    
                    def search_fuzzy_artist(artist: str):
                        """Search for tracks with fuzzy title and artist variant."""
                        if not artist:
                            return []
                        try:
                            results = backend.search_tracks(
                                title=fuzzy_query, artist=artist, limit=100
                            )
                            logger.debug(
                                f"Strategy 4 (Artist+Fuzzy): Artist '{artist}' "
                                f"Query '{fuzzy_query}' -> {len(results)} tracks"
                            )
                            return results
                        except Exception as exc:
                            logger.debug(f"Strategy 4 artist variant failed: {exc}")
                            return []
                    
                    # Execute searches in parallel (up to 4 concurrent threads)
                    with ThreadPoolExecutor(max_workers=4) as executor:
                        futures = {
                            executor.submit(search_fuzzy_artist, artist): artist
                            for artist in search_artists
                        }
                        
                        for future in as_completed(futures):
                            candidate_tracks = future.result()
                            for track in candidate_tracks:
                                key = track.get("backend_id") or track.get("plex_ratingkey") or id(track)
                                if key not in unique_tracks:
                                    unique_tracks[key] = track

                tracks = list(unique_tracks.values())

                # Fallback: Relaxed search if still nothing
                if not tracks and artist_variants:
                    if title_only_tracks:
                        logger.debug(
                            "Reusing Strategy 2 results for Strategy 4 relaxed search"
                        )
                        filtered_tracks = [
                            track
                            for track in title_only_tracks
                            if _track_matches_artist_variants(track, artist_variants)
                        ]
                        logger.debug(
                            f"Strategy 4 (Artist+Fuzzy relaxed): Filtered "
                            f"{len(filtered_tracks)} tracks"
                        )
                        tracks = filtered_tracks
            except Exception as exc:
                logger.debug(f"Artist+fuzzy search strategy failed: {exc}")
            finally:
                _record_timing("strategy_artist_fuzzy_title_ms", stage_start)

        # Strategy 5: Album only
        if "album_only" in selected_strategies and len(tracks) == 0:
            search_strategies_tried.append("album_only")
            stage_start = time.perf_counter()
            try:
                # Optimization: Filter title-only results by album
                if title_only_tracks and song.get("album"):
                    logger.debug("Reusing Strategy 2 results for Strategy 5 (Album-only)")
                    album_title = song["album"].lower()
                    filtered_tracks = [
                        track
                        for track in title_only_tracks
                        if track.get("album", "").lower() == album_title
                    ]
                    logger.debug(
                        f"Strategy 5 (Album-only): Filtered {len(filtered_tracks)} "
                        f"tracks from Strategy 2 results"
                    )
                    tracks = filtered_tracks
                else:
                    tracks = backend.search_tracks(album=song["album"], limit=150)
                    logger.debug(f"Strategy 5 (Album-only): Found {len(tracks)} tracks")
            except Exception as exc:
                logger.debug(f"Strategy 5 failed: {exc}")
                tracks = []
            finally:
                _record_timing("strategy_album_only_ms", stage_start)

        # Strategy 6: Fuzzy Title
        if "fuzzy_title" in selected_strategies and len(tracks) == 0:
            search_strategies_tried.append("fuzzy_title")
            stage_start = time.perf_counter()
            try:
                fuzzy_query = clean_text_for_matching(song["title"])

                # Optimization: Apply fuzzy to title-only results
                if title_only_tracks:
                    logger.debug("Reusing Strategy 2 results for Strategy 6 (Fuzzy Title)")
                    filtered_tracks = []
                    for track in title_only_tracks:
                        track_title = track.get("title", "")
                        if track_title:
                            score = plex_track_distance(
                                {"title": fuzzy_query}, track
                            ).similarity
                            if score >= 0.7:
                                filtered_tracks.append(track)
                    logger.debug(
                        f"Strategy 6 (Fuzzy Title): Filtered {len(filtered_tracks)} "
                        f"tracks from Strategy 2 results"
                    )
                    tracks = filtered_tracks
                else:
                    tracks = backend.search_tracks(title=fuzzy_query, limit=100)
                    logger.debug(
                        f"Strategy 6 (Fuzzy Title): Query '{fuzzy_query}' "
                        f"-> {len(tracks)} tracks"
                    )
            except Exception as exc:
                logger.debug(f"Fuzzy search strategy failed: {exc}")
            finally:
                _record_timing("strategy_fuzzy_title_ms", stage_start)

    except Exception as exc:
        logger.debug(
            f"Error during multi-strategy search for {song.get('album', '')} "
            f"- {song.get('title', '')}: {exc}"
        )
        return _return(None)

    # Process results
    if len(tracks) == 1:
        result = tracks[0]
        stage_start = time.perf_counter()
        similarity = plex_track_distance(song, result).similarity
        _record_timing("single_result_similarity_ms", stage_start)
        logger.debug(
            f"Single-track search result similarity for '{song.get('title', '')}' "
            f"-> {similarity:.2f}"
        )
        if similarity >= 0.75:
            _cache_set(
                cache_key,
                result.get("backend_id") or result.get("plex_ratingkey"),
                result,
            )
            return _return(result)
        logger.debug(
            f"Rejecting single-track result for '{song.get('title', '')}' "
            f"due to low similarity ({similarity:.2f})"
        )
        if candidate_queue is not None and similarity > 0.05:
            # Queue for manual confirmation
            candidate_queue.append({
                "track": result,
                "similarity": similarity,
                "cache_key": cache_key,
                "source": "single",
                "song": dict(song),
            })
        tracks = []

    if len(tracks) > 1:
        stage_start = time.perf_counter()
        sorted_tracks = _find_closest_match(song, tracks)
        _record_timing("find_closest_match_ms", stage_start)
        logger.debug(
            "Closest match scoring on %d tracks", len(tracks)
        )
        logger.debug(
            f"Found {len(sorted_tracks)} tracks for {song['title']} "
            f"using strategies: {', '.join(search_strategies_tried)}"
        )

        best_match = sorted_tracks[0]
        if best_match[1] >= 0.7:
            _cache_set(
                cache_key,
                best_match[0].get("backend_id") or best_match[0].get("plex_ratingkey"),
                best_match[0],
            )
            return _return(best_match[0])
        logger.debug(f"Best match score {best_match[1]} below threshold for: {song['title']}")

    # Stage 4: LLM Enhancement (if enabled and not already attempted)
    # Skip LLM if we have good candidates queued (>= 0.7 similarity) for manual confirmation
    has_good_candidates = (
        candidate_queue is not None 
        and len(candidate_queue) > 0 
        and any(c.get('similarity', 0) >= 0.7 for c in candidate_queue)
    )
    
    if not llm_attempted and llm_agent is not None and not has_good_candidates:
        stage_start = time.perf_counter()
        try:
            from harmony.ai.search import search_track_info

            search_query = f"{song['title']} by {song['artist']}"
            if song.get('album'):
                search_query += f" from {song['album']}"

            logger.debug(f"Attempting LLM-enhanced search for: {search_query}")
            cleaned_metadata = search_track_info(llm_agent, search_query)

            if cleaned_metadata and cleaned_metadata.get('title'):
                logger.debug(f"LLM returned cleaned metadata: {cleaned_metadata}")

                # Recursively search with cleaned metadata
                result = search_backend_song(
                    backend,
                    cache,
                    vector_index,
                    cleaned_metadata,
                    manual_search=False,
                    llm_attempted=True,
                    use_local_candidates=True,
                    llm_agent=llm_agent,
                    candidate_queue=candidate_queue,
                    matching_module=matching_module,
                )

                if result is not None:
                    logger.debug(
                        f"LLM-cleaned search succeeded, caching for original query: {song}"
                    )
                    _cache_set(
                        cache_key,
                        result.get("backend_id") or result.get("plex_ratingkey"),
                        result,
                    )
                    return _return(result)
                else:
                    # Cache negative result with cleaned metadata for future use
                    logger.debug(
                        f"LLM-cleaned search also failed, caching negative with metadata"
                    )
                    _cache_set(cache_key, None, cleaned_metadata)
        except Exception as exc:
            logger.debug(f"LLM enhancement failed: {exc}")
        finally:
            _record_timing("llm_ms", stage_start)

    # Stage 5: Manual Search with Candidate Confirmation Queue
    if manual_search:
        try:
            from harmony.workflows.manual_search import (
                review_candidate_confirmations,
                manual_track_search,
            )

            # First, review any queued candidates
            if candidate_queue and len(candidate_queue) > 0:
                # Import matching module if not provided
                if matching_module is None:
                    import harmony.core.matching as matching_module

                selection = review_candidate_confirmations(
                    backend=backend,
                    cache=cache,
                    matching_module=matching_module,
                    candidates=list(candidate_queue),
                    current_song=song,
                    current_cache_key=cache_key,
                )

                # Clear the queue after review
                candidate_queue.clear()

                action = selection.get("action")
                if action == "selected":
                    track = selection.get("track")
                    if track is not None:
                        chosen_cache_key = selection.get("cache_key") or cache_key
                        original_song = selection.get("original_song") or song
                        title = track.get('title', '') or "<unknown>"
                        logger.debug(
                            f"User accepted queued candidate '{title}' for "
                            f"'{original_song.get('title', '')}'"
                        )
                        _cache_set(
                            chosen_cache_key,
                            track.get("backend_id") or track.get("plex_ratingkey"),
                            track,
                        )
                        return _return(track)
                elif action == "manual":
                    manual_query = selection.get("original_song") or song
                    result = manual_track_search(
                        backend=backend,
                        cache=cache,
                        matching_module=matching_module,
                        original_query=manual_query,
                    )
                    if result is not None:
                        logger.debug(
                            f"Manual search succeeded, caching for original query: {manual_query}"
                        )
                        _cache_set(
                            cache_key,
                            result.get("backend_id") or result.get("plex_ratingkey"),
                            result,
                        )
                        return _return(result)
                elif action == "abort":
                    return _return(None)
                elif action == "skip":
                    pass  # Continue to prompt below

            # Prompt for manual search if no candidates or user wants it
            logger.info(
                f"\nTrack {song.get('album', 'Unknown')} - {song.get('artist', 'Unknown')} - "
                f"{song['title']} not found in backend (tried strategies: "
                f"{', '.join(search_strategies_tried) if search_strategies_tried else 'none'})"
            )

            response = input("\nSearch manually? (Y/n/r to refresh index): ").strip().lower()
            if response in ('r', 'refresh'):
                # Trigger incremental refresh
                if harmony_app is not None and hasattr(harmony_app, 'incremental_refresh_vector_index'):
                    logger.info("Refreshing vector index with new Plex tracks...")
                    added = harmony_app.incremental_refresh_vector_index(limit=100)
                    if added > 0:
                        logger.info(f"Added {added} new tracks to index. Retrying search...")
                        # Retry search with updated index
                        return _return(search_backend_song(
                            backend, cache, vector_index, song,
                            beets_vector_index=beets_vector_index,
                            beets_lookup=beets_lookup,
                            manual_search=manual_search,
                            llm_attempted=llm_attempted,
                            use_local_candidates=True,  # Re-enable for retry
                            llm_agent=llm_agent,
                            candidate_queue=candidate_queue,
                            matching_module=matching_module,
                            harmony_app=harmony_app,
                        ))
                    else:
                        logger.info("No new tracks found.")
                        return _return(None)
                else:
                    logger.warning("Refresh not available (harmony_app not provided)")
                return _return(None)
            
            if not response or response in ('y', 'yes'):
                # Import matching module if not provided
                if matching_module is None:
                    import harmony.core.matching as matching_module

                result = manual_track_search(
                    backend=backend,
                    cache=cache,
                    matching_module=matching_module,
                    original_query=song,
                )
                
                # Check if user requested refresh from within manual search
                if result == "refresh":
                    if harmony_app is not None and hasattr(harmony_app, 'incremental_refresh_vector_index'):
                        logger.info("Refreshing vector index with new Plex tracks...")
                        added = harmony_app.incremental_refresh_vector_index(limit=100)
                        if added > 0:
                            logger.info(f"Added {added} new tracks to index. Retrying search...")
                            # Retry search with updated index
                            return _return(search_backend_song(
                                backend, cache, vector_index, song,
                                beets_vector_index=beets_vector_index,
                                beets_lookup=beets_lookup,
                                manual_search=manual_search,
                                llm_attempted=llm_attempted,
                                use_local_candidates=True,
                                llm_agent=llm_agent,
                                candidate_queue=candidate_queue,
                                matching_module=matching_module,
                                harmony_app=harmony_app,
                            ))
                        else:
                            logger.info("No new tracks found. Please try manual search again.")
                            # Return to manual search
                            result = manual_track_search(
                                backend=backend,
                                cache=cache,
                                matching_module=matching_module,
                                original_query=song,
                            )
                    else:
                        logger.warning("Refresh not available (harmony_app not provided)")
                        result = None
                
                if result is not None:
                    logger.debug(
                        f"Manual search succeeded, caching for original query: {song}"
                    )
                    _cache_set(
                        cache_key,
                        result.get("backend_id") or result.get("plex_ratingkey"),
                        result,
                    )
                    return _return(result)
        except Exception as exc:
            logger.error(f"Manual search failed: {exc}")

    logger.debug(
        f"All search strategies failed for: {song} "
        f"(tried: {', '.join(search_strategies_tried) if search_strategies_tried else 'none'})"
    )
    _cache_set(cache_key, None)
    return _return(None)


def search_plex_song(*args, **kwargs) -> Optional[dict]:
    """Backward-compatible wrapper for the search pipeline."""
    return search_backend_song(*args, **kwargs)


def _find_closest_match(
    song: dict, tracks: list[dict]
) -> list[tuple[dict, float]]:
    """Find closest matching track from a list of tracks.

    Returns sorted list of (track, similarity_score) tuples.
    """
    scored = []
    for track in tracks:
        match_score = plex_track_distance(song, track)
        scored.append((track, match_score.similarity))

    return sorted(scored, key=lambda x: x[1], reverse=True)


def _track_to_dict(track) -> dict:
    """Convert a Track or raw backend object to Harmony dict format."""
    try:
        if isinstance(track, dict):
            return track

        if isinstance(track, Track):
            return {
                "title": track.title,
                "artist": track.artist,
                "album": track.album,
                "backend_id": track.backend_id,
                "plex_ratingkey": track.plex_ratingkey,
                "provider_ids": track.metadata.get("provider_ids", {}),
            }

        artist_name = ""
        try:
            if hasattr(track, "artist") and callable(track.artist):
                artist_name = track.artist().title
            elif hasattr(track, "originalTitle"):
                artist_name = track.originalTitle
        except Exception:
            pass

        return {
            "title": getattr(track, "title", ""),
            "artist": artist_name,
            "album": getattr(track, "parentTitle", ""),
            "backend_id": getattr(track, "ratingKey", None),
            "plex_ratingkey": getattr(track, "ratingKey", None),
        }
    except Exception as exc:
        logger.debug(f"Failed to convert track to dict: {exc}")
        return {}


def _cache_match(cache: Cache, cache_key: str, track_dict: dict) -> None:
    """Cache a matched track with metadata logging."""
    try:
        logger.debug(
            f"Caching result for key '{cache_key}' -> "
            f"title='{track_dict.get('title')}', "
            f"artist='{track_dict.get('artist')}', "
            f"album='{track_dict.get('album')}', "
            f"backend_id={track_dict.get('backend_id') or track_dict.get('plex_ratingkey')}"
        )
        cache.set(
            cache_key,
            track_dict.get("backend_id") or track_dict.get("plex_ratingkey"),
            track_dict,
        )
    except Exception as exc:
        logger.debug(
            f"Caching result for key '{cache_key}' but failed to collect metadata: {exc}"
        )
        cache.set(cache_key, None)
