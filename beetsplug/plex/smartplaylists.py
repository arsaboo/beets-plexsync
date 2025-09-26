"""Smart playlist generation and helpers extracted from plexsync.

These functions use the plugin instance (`ps`) to access logging, config,
and Plex/beets objects. Behavior preserved.
"""

from datetime import datetime, timedelta
from typing import Tuple
import time
import os

from beets import config
from beetsplug.core.config import get_config_value, get_plexsync_config
from beetsplug.providers.gaana import import_gaana_playlist
from beetsplug.providers.tidal import import_tidal_playlist
from beetsplug.providers.youtube import import_yt_playlist
from beetsplug.providers.m3u8 import import_m3u8_playlist
from beetsplug.providers.http_post import import_post_playlist


def build_plex_lookup(ps, lib):
    ps._log.debug("Building lookup dictionary for Plex rating keys")
    plex_lookup = {}
    for item in lib.items():
        if hasattr(item, "plex_ratingkey"):
            plex_lookup[item.plex_ratingkey] = item
    return plex_lookup


def get_preferred_attributes(ps) -> Tuple[list, list]:
    # Defaults from config
    defaults_cfg = get_plexsync_config(["playlists", "defaults"], dict, {})

    history_days = get_config_value(config["plexsync"], defaults_cfg, "history_days", 15)
    exclusion_days = get_config_value(config["plexsync"], defaults_cfg, "exclusion_days", 30)

    tracks = ps.music.search(filters={"track.lastViewedAt>>": f"{history_days}d"}, libtype="track")

    genre_counts = {}
    similar_tracks = set()

    recently_played = set(
        track.ratingKey
        for track in ps.music.search(filters={"track.lastViewedAt>>": f"{exclusion_days}d"}, libtype="track")
    )

    for track in tracks:
        track_genres = set()
        for genre in track.genres:
            if genre:
                genre_str = str(genre.tag).lower()
                genre_counts[genre_str] = genre_counts.get(genre_str, 0) + 1
                track_genres.add(genre_str)

        try:
            sonic_matches = track.sonicallySimilar()
            for match in sonic_matches:
                rating = getattr(match, "userRating", -1)
                if (
                    match.ratingKey not in recently_played
                    and any(g.tag.lower() in track_genres for g in match.genres)
                    and (rating is None or rating == -1 or rating >= 4)
                ):
                    similar_tracks.add(match)
        except Exception as e:
            ps._log.debug("Error getting similar tracks for {}: {}", track.title, e)

    sorted_genres = sorted(genre_counts, key=genre_counts.get, reverse=True)[:5]
    ps._log.debug("Top genres: {}", sorted_genres)
    ps._log.debug("Found {} similar tracks after filtering", len(similar_tracks))
    return sorted_genres, list(similar_tracks)


def calculate_track_score(ps, track, base_time=None, tracks_context=None, playlist_type=None):
    import numpy as np
    from scipy import stats

    if base_time is None:
        base_time = datetime.now()

    rating = float(getattr(track, 'plex_userrating', 0))
    last_played = getattr(track, 'plex_lastviewedat', None)
    play_count = getattr(track, 'plex_viewcount', 0)
    popularity = float(getattr(track, 'spotify_track_popularity', 0))
    release_year = getattr(track, 'year', None)

    if release_year:
        try:
            release_year = int(release_year)
            age = base_time.year - release_year
        except ValueError:
            age = 0
    else:
        age = 0

    if last_played is None:
        days_since_played = np.random.exponential(365)
    else:
        days = (base_time - datetime.fromtimestamp(last_played)).days
        days_since_played = min(days, 1095)

    if tracks_context:
        all_ratings = [float(getattr(t, 'plex_userrating', 0)) for t in tracks_context]
        all_days = [
            (base_time - datetime.fromtimestamp(getattr(t, 'plex_lastviewedat', base_time - timedelta(days=365)))).days
            for t in tracks_context
        ]
        all_play_counts = [getattr(t, 'plex_viewcount', 0) for t in tracks_context]
        all_popularity = [float(getattr(t, 'spotify_track_popularity', 0)) for t in tracks_context]
        all_ages = [base_time.year - int(getattr(t, 'year', base_time.year)) for t in tracks_context]

        rating_mean, rating_std = np.mean(all_ratings), np.std(all_ratings) or 1
        days_mean, days_std = np.mean(all_days), np.std(all_days) or 1
        play_count_mean, play_count_std = np.mean(all_play_counts), np.std(all_play_counts) or 1
        popularity_mean, popularity_std = np.mean(all_popularity), np.std(all_popularity) or 1
        age_mean, age_std = np.mean(all_ages), np.std(all_ages) or 1
    else:
        rating_mean, rating_std = 5, 2.5
        days_mean, days_std = 365, 180
        play_count_mean, play_count_std = 10, 15  # Default mean for play count
        popularity_mean, popularity_std = 30, 20
        age_mean, age_std = 30, 10

    z_rating = (rating - rating_mean) / rating_std if rating > 0 else -2.0
    z_recency = -(days_since_played - days_mean) / days_std
    z_play_count = (play_count - play_count_mean) / play_count_std
    z_popularity = (popularity - popularity_mean) / popularity_std
    z_age = -(age - age_mean) / age_std

    import numpy as _np
    z_rating = _np.clip(z_rating, -3, 3)
    z_recency = _np.clip(z_recency, -3, 3)
    z_play_count = _np.clip(z_play_count, -3, 3)
    z_popularity = _np.clip(z_popularity, -3, 3)
    z_age = _np.clip(z_age, -3, 3)

    is_rated = rating > 0
    
    # Different weightings for different playlist types
    if playlist_type == "forgotten_gems":
        # For forgotten gems, penalize high play count, emphasize low play + long time since play
        if is_rated:
            # Higher weight on recency (forgotten) and negative weight on play count (frequently played)
            weighted_score = (z_rating * 0.3) + (z_recency * 0.3) + (-z_play_count * 0.2) + (z_popularity * 0.1) + (z_age * 0.1)
        else:
            # For unrated tracks, emphasize recency and low play count even more
            weighted_score = (z_recency * 0.4) + (-z_play_count * 0.3) + (z_popularity * 0.2) + (z_age * 0.1)
    elif playlist_type == "fresh_favorites":
        # Heavily weight rating and recency (newer = better), moderately penalize high play count
        if is_rated:
            weighted_score = (z_rating * 0.35) + (-z_age * 0.25) + (z_popularity * 0.2) + (-z_play_count * 0.15) + (z_recency * 0.05)
        else:
            # For unrated, prioritize newness and popularity, avoid overplayed
            weighted_score = (-z_age * 0.35) + (z_popularity * 0.3) + (-z_play_count * 0.25) + (z_recency * 0.1)
    elif playlist_type == "daily_discovery":
        # Focus on discovery: emphasize unrated tracks with high popularity relative to user's preferences
        # For rated tracks: maintain good balance of rating and discovery potential
        if is_rated:
            # Slightly reduce rating weight to allow for more discovery, increase popularity and age factors
            weighted_score = (z_rating * 0.35) + (z_recency * 0.15) + (z_popularity * 0.25) + (z_age * 0.25)
        else:
            # For unrated tracks: emphasize popularity and recency to surface new discoveries
            weighted_score = (z_popularity * 0.4) + (z_recency * 0.3) + (z_age * 0.3)
    elif playlist_type == "recent_hits":
        # Focus on recent popular tracks: emphasize popularity, recency, and recent release
        if is_rated:
            # For rated tracks: emphasize popularity and recency of additions/plays
            weighted_score = (z_rating * 0.25) + (z_recency * 0.3) + (z_popularity * 0.35) + (-z_age * 0.1)  # Negative age = newer = better
        else:
            # For unrated tracks: focus on popularity and newness
            weighted_score = (z_popularity * 0.5) + (-z_age * 0.3) + (z_recency * 0.2)
    else:  # Default fallback for other playlists
        if is_rated:
            weighted_score = (z_rating * 0.5) + (z_recency * 0.1) + (z_popularity * 0.1) + (z_age * 0.2)
        else:
            weighted_score = (z_recency * 0.2) + (z_popularity * 0.5) + (z_age * 0.3)

    final_score = stats.norm.cdf(weighted_score * 1.5) * 100
    noise = _np.random.normal(0, 0.5)
    final_score = final_score + noise
    if not is_rated and final_score < 50:
        final_score = 50 + (final_score / 2)

    return max(0, min(100, final_score))


def select_tracks_weighted(ps, tracks, num_tracks, playlist_type=None):
    import numpy as np
    if not tracks:
        return []
    base_time = datetime.now()
    track_scores = [(track, calculate_track_score(ps, track, base_time, playlist_type=playlist_type)) for track in tracks]
    scores = np.array([score for _, score in track_scores])
    probabilities = np.exp(scores / 10) / sum(np.exp(scores / 10))
    selected_indices = np.random.choice(
        len(tracks), size=min(num_tracks, len(tracks)), replace=False, p=probabilities
    )
    selected_tracks = [tracks[i] for i in selected_indices]

    # Avoid verbose per-track logging; summarize and sample a few examples
    try:
        sel_scores = [track_scores[i][1] for i in selected_indices]
        mean_score = float(np.mean(sel_scores)) if sel_scores else 0.0
        ps._log.debug(
            "Selected {} tracks (avg score {:.2f})",
            len(selected_tracks), mean_score,
        )
        sample_count = min(5, len(selected_tracks))
        if sample_count:
            ps._log.debug("Sample selections (up to {}):", sample_count)
            for idx in range(sample_count):
                tr = selected_tracks[idx]
                sc = sel_scores[idx]
                ps._log.debug(
                    " • {} - {} (Score: {:.2f}, Rating: {}, Plays: {})",
                    getattr(tr, 'album', ''), getattr(tr, 'title', ''), sc,
                    getattr(tr, 'plex_userrating', 0), getattr(tr, 'plex_viewcount', 0),
                )
    except Exception:
        # Never fail selection due to logging issues
        pass

    return selected_tracks


def calculate_playlist_proportions(ps, max_tracks, discovery_ratio):
    unrated_tracks_count = min(int(max_tracks * (discovery_ratio / 100)), max_tracks)
    rated_tracks_count = max_tracks - unrated_tracks_count
    return unrated_tracks_count, rated_tracks_count


def validate_filter_config(ps, filter_config):
    if not isinstance(filter_config, dict):
        return False, "Filter configuration must be a dictionary"
    for section in ['exclude', 'include']:
        if section in filter_config:
            section_config = filter_config[section]
            if not isinstance(section_config, dict):
                return False, f"{section} section must be a dictionary"
            if 'genres' in section_config:
                if not isinstance(section_config['genres'], list):
                    return False, f"{section}.genres must be a list"
            if 'years' in section_config:
                years = section_config['years']
                if not isinstance(years, dict):
                    return False, f"{section}.years must be a dictionary"
                if 'before' in years and not isinstance(years['before'], int):
                    return False, f"{section}.years.before must be an integer"
                if 'after' in years and not isinstance(years['after'], int):
                    return False, f"{section}.years.after must be an integer"
                if 'between' in years:
                    if not isinstance(years['between'], list) or len(years['between']) != 2:
                        return False, f"{section}.years.between must be a list of two integers"
                    if not all(isinstance(y, int) for y in years['between']):
                        return False, f"{section}.years.between values must be integers"
    if 'min_rating' in filter_config:
        if not isinstance(filter_config['min_rating'], (int, float)):
            return False, "min_rating must be a number"
        if not 0 <= filter_config['min_rating'] <= 10:
            return False, "min_rating must be between 0 and 10"
    return True, ""


def _apply_exclusion_filters(ps, tracks, exclude_config):
    import xml.etree.ElementTree as ET
    filtered_tracks = []
    original_count = len(tracks)

    # Pre-build exclusion sets for faster membership tests
    exclude_genres_set = set(g.lower() for g in exclude_config.get('genres', []) if isinstance(g, str))
    years_config = exclude_config.get('years', {}) or {}
    year_before = years_config.get('before')
    year_after = years_config.get('after')

    for track in tracks:
        try:
            # Genre exclusion via set intersection
            if exclude_genres_set and hasattr(track, 'genres'):
                track_genres = {getattr(g, 'tag', '').lower() for g in (track.genres or []) if getattr(g, 'tag', None)}
                if track_genres & exclude_genres_set:
                    continue

            # Year bounds
            if year_before is not None and hasattr(track, 'year') and track.year is not None and track.year < year_before:
                continue
            if year_after is not None and hasattr(track, 'year') and track.year is not None and track.year > year_after:
                continue

            filtered_tracks.append(track)
        except (ET.ParseError, Exception) as e:
            ps._log.debug("Skipping track due to exception in exclusion filter: {}", e)
            continue
    ps._log.debug("Exclusion filters removed {} tracks", original_count - len(filtered_tracks))
    return filtered_tracks


def _apply_inclusion_filters(ps, tracks, include_config):
    import xml.etree.ElementTree as ET
    filtered_tracks = []
    original_count = len(tracks)

    # Pre-build inclusion sets for faster membership tests
    include_genres_set = set(g.lower() for g in include_config.get('genres', []) if isinstance(g, str))
    years_config = include_config.get('years', {}) or {}
    between = years_config.get('between') if isinstance(years_config.get('between'), list) else None
    start_year = between[0] if between and len(between) == 2 else None
    end_year = between[1] if between and len(between) == 2 else None

    for track in tracks:
        try:
            # Genre inclusion via set intersection (require at least one match if list provided)
            if include_genres_set:
                if not hasattr(track, 'genres'):
                    continue
                track_genres = {getattr(g, 'tag', '').lower() for g in (track.genres or []) if getattr(g, 'tag', None)}
                if not (track_genres & include_genres_set):
                    continue

            # Year range inclusion
            if start_year is not None and end_year is not None:
                if not (hasattr(track, 'year') and track.year is not None and start_year <= track.year <= end_year):
                    continue

            filtered_tracks.append(track)
        except (ET.ParseError, Exception) as e:
            ps._log.debug("Skipping track due to exception in inclusion filter: {}", e)
            continue
    ps._log.debug("Inclusion filters removed {} tracks", original_count - len(filtered_tracks))
    return filtered_tracks


def apply_playlist_filters(ps, tracks, filter_config):
    if not tracks:
        return tracks
    is_valid, error = validate_filter_config(ps, filter_config)
    if not is_valid:
        ps._log.error("Invalid filter configuration: {}", error)
        return tracks
    total_start = time.time()
    ps._log.debug("Applying filters to {} tracks", len(tracks))
    filtered_tracks = tracks[:]
    if 'exclude' in filter_config:
        exc_start = time.time()
        ps._log.debug("Applying exclusion filters...")
        before = len(filtered_tracks)
        filtered_tracks = _apply_exclusion_filters(ps, filtered_tracks, filter_config['exclude'])
        ps._log.debug(
            "Exclusion filters removed {} tracks in {:.2f}s",
            before - len(filtered_tracks), time.time() - exc_start,
        )
    if 'include' in filter_config:
        inc_start = time.time()
        ps._log.debug("Applying inclusion filters...")
        before = len(filtered_tracks)
        filtered_tracks = _apply_inclusion_filters(ps, filtered_tracks, filter_config['include'])
        ps._log.debug(
            "Inclusion filters removed {} tracks in {:.2f}s",
            before - len(filtered_tracks), time.time() - inc_start,
        )
    if 'min_rating' in filter_config:
        rt_start = time.time()
        min_rating = filter_config['min_rating']
        original_count = len(filtered_tracks)
        unrated_tracks = [
            track for track in filtered_tracks
            if not hasattr(track, 'userRating') or track.userRating is None or float(track.userRating or 0) == 0
        ]
        rated_tracks = [
            track for track in filtered_tracks
            if hasattr(track, 'userRating') and track.userRating is not None and float(track.userRating or 0) >= min_rating
        ]
        filtered_tracks = rated_tracks + unrated_tracks
        ps._log.debug(
            "Rating filter (>= {}): {} -> {} tracks ({} rated, {} unrated) in {:.2f}s",
            min_rating, original_count, len(filtered_tracks), len(rated_tracks), len(unrated_tracks),
            time.time() - rt_start,
        )
    ps._log.debug(
        "Filter application complete: {} -> {} tracks in {:.2f}s",
        len(tracks), len(filtered_tracks), time.time() - total_start,
    )
    return filtered_tracks


def generate_daily_discovery(ps, lib, dd_config, plex_lookup, preferred_genres, similar_tracks):
    playlist_name = dd_config.get("name", "Daily Discovery")
    ps._log.info("Generating {} playlist", playlist_name)
    defaults_cfg = get_plexsync_config(["playlists", "defaults"], dict, {})
    max_tracks = get_config_value(dd_config, defaults_cfg, "max_tracks", 20)
    discovery_ratio = get_config_value(dd_config, defaults_cfg, "discovery_ratio", 30)
    exclusion_days = get_config_value(dd_config, defaults_cfg, "exclusion_days", 30)
    filters = dd_config.get("filters", {})
    
    # Get tracks from sonic analysis (similar tracks to recently played)
    matched_sonic_tracks = []
    for plex_track in similar_tracks:
        try:
            beets_item = plex_lookup.get(plex_track.ratingKey)
            if beets_item:
                matched_sonic_tracks.append(plex_track)
        except Exception as e:
            ps._log.debug("Error processing sonic track {}: {}", plex_track.title, e)
            continue
    
    # Also include tracks from the entire library that match user's genre preferences
    ps._log.debug("Collecting additional tracks from library for discovery...")
    all_library_tracks = _get_library_tracks(ps, preferred_genres, filters, exclusion_days)
    
    # Filter library tracks
    if filters:
        try:
            adv = build_advanced_filters(filters, exclusion_days)
        except Exception:
            adv = None
        if not adv:
            all_library_tracks = apply_playlist_filters(ps, all_library_tracks, filters)
    
    # Convert library tracks to beets items
    library_final_tracks = []
    for track in all_library_tracks:
        try:
            beets_item = plex_lookup.get(track.ratingKey)
            if beets_item:
                library_final_tracks.append(beets_item)
        except Exception as e:
            ps._log.debug("Error converting library track {}: {}", track.title, e)
    
    # Combine both sources of potential discovery tracks
    all_potential_tracks = matched_sonic_tracks + library_final_tracks
    ps._log.debug("Found {} sonic analysis tracks and {} library tracks for discovery", 
                  len(matched_sonic_tracks), len(library_final_tracks))
    
    # Final track selection after removing duplicates
    unique_tracks = []
    seen_keys = set()
    for track in all_potential_tracks:
        key = getattr(track, 'ratingKey', None)
        if key and key not in seen_keys:
            seen_keys.add(key)
            unique_tracks.append(track if hasattr(track, 'plex_userrating') else 
                                plex_lookup.get(key) if key and key in plex_lookup else track)
    
    # Separate rated and unrated tracks
    rated_tracks = []
    unrated_tracks = []
    for track in unique_tracks:
        if track:  # Make sure track exists
            rating = float(getattr(track, 'plex_userrating', 0))
            if rating > 0:
                rated_tracks.append(track)
            else:
                unrated_tracks.append(track)
    
    ps._log.debug("Split into {} rated and {} unrated tracks", len(rated_tracks), len(unrated_tracks))
    
    # Calculate track proportions based on discovery_ratio
    unrated_tracks_count, rated_tracks_count = calculate_playlist_proportions(ps, max_tracks, discovery_ratio)
    
    # Select tracks using weighted scoring
    selected_rated = select_tracks_weighted(ps, rated_tracks, rated_tracks_count, playlist_type="daily_discovery")
    selected_unrated = select_tracks_weighted(ps, unrated_tracks, unrated_tracks_count, playlist_type="daily_discovery")
    
    # Fill remaining slots if needed
    if len(selected_unrated) < unrated_tracks_count:
        additional_count = min(unrated_tracks_count - len(selected_unrated), max_tracks - len(selected_rated) - len(selected_unrated))
        remaining_rated = [t for t in rated_tracks if t not in selected_rated]
        additional_rated = select_tracks_weighted(ps, remaining_rated, additional_count, playlist_type="daily_discovery")
        selected_rated.extend(additional_rated)
    
    selected_tracks = selected_rated + selected_unrated
    if len(selected_tracks) > max_tracks:
        selected_tracks = selected_tracks[:max_tracks]
    
    import random
    random.shuffle(selected_tracks)
    ps._log.info("Selected {} rated tracks and {} unrated tracks", len(selected_rated), len(selected_unrated))
    if not selected_tracks:
        ps._log.warning("No tracks matched criteria for Daily Discovery playlist")
        return
    try:
        ps._plex_clear_playlist(playlist_name)
        ps._log.info("Cleared existing Daily Discovery playlist")
    except Exception:
        ps._log.debug("No existing Daily Discovery playlist found")
    ps._plex_add_playlist_item(selected_tracks, playlist_name)
    ps._log.info("Successfully updated {} playlist with {} tracks", playlist_name, len(selected_tracks))


def build_advanced_filters(filter_config, exclusion_days, preferred_genres=None):
    adv = {'and': []}
    if filter_config:
        include = filter_config.get('include', {}) or {}
        exclude = filter_config.get('exclude', {}) or {}
        # Combine preferred and included genres for a single OR query
        all_genres = set(g.lower() for g in (preferred_genres or []))
        inc_genres = include.get('genres')
        if inc_genres:
            all_genres.update(g.lower() for g in inc_genres)
        if all_genres:
            adv['and'].append({'or': [{'genre': g} for g in all_genres]})
        # Exclude genres
        exc_genres = exclude.get('genres')
        if exc_genres:
            adv['and'].append({'genre!': list(exc_genres)})
        # Include years
        inc_years = include.get('years') or {}
        if 'between' in inc_years and isinstance(inc_years['between'], list) and len(inc_years['between']) == 2:
            start_year, end_year = inc_years['between']
            adv['and'].append({'and': [{'year>>': start_year}, {'year<<': end_year}]})
        if 'after' in inc_years:
            adv['and'].append({'year>>': inc_years['after']})
        if 'before' in inc_years:
            adv['and'].append({'year<<': inc_years['before']})
        # Exclude years (translate to constraints)
        exc_years = exclude.get('years') or {}
        if 'before' in exc_years:
            # Exclude anything strictly before X => require year >= X
            adv['and'].append({'year>>': exc_years['before']})
        if 'after' in exc_years:
            # Exclude anything strictly after Y => require year <= Y
            adv['and'].append({'year<<': exc_years['after']})
        # Rating filter at top-level of filter_config
        if 'min_rating' in filter_config:
            mr = filter_config['min_rating']
            adv['and'].append({'or': [{'userRating': 0}, {'userRating>>': mr}]})
    # Exclude recent plays
    if exclusion_days and exclusion_days > 0:
        adv['and'].append({'lastViewedAt<<': f'-{exclusion_days}d'})
    # Clean up if empty
    if not adv['and']:
        return None
    return adv

def _get_library_tracks(ps, preferred_genres, filters, exclusion_days):
    adv_filters = build_advanced_filters(filters, exclusion_days, preferred_genres)
    if adv_filters:
        try:
            ps._log.debug("Using server-side filters: {}", adv_filters)
            _t0 = time.time()
            tracks = ps.music.searchTracks(filters=adv_filters)
            ps._log.debug(
                "Server-side filter fetched {} tracks in {:.2f}s",
                len(tracks), time.time() - _t0,
            )
        except Exception as e:
            ps._log.debug("Server-side filter failed (falling back to client filter): {}", e)
            _t0 = time.time()
            tracks = ps.music.search(libtype="track")
            ps._log.debug(
                "Client-side fetch (no server filters) returned {} tracks in {:.2f}s",
                len(tracks), time.time() - _t0,
            )
    else:
        # No filters specified; fetch all tracks (may be large)
        _t0 = time.time()
        tracks = ps.music.search(libtype="track")
        ps._log.debug(
            "Fetched all tracks (no filters) -> {} in {:.2f}s",
            len(tracks), time.time() - _t0,
        )

    # Optional candidate pool cap to avoid huge post-filtering work
    try:
        defaults_cfg = get_plexsync_config(["playlists", "defaults"], dict, {})
        max_pool = get_config_value(defaults_cfg, defaults_cfg, "max_candidate_pool", None)
        if max_pool:
            import random
            if len(tracks) > int(max_pool):
                tracks = random.sample(tracks, int(max_pool))
                ps._log.debug("Capped candidate pool to {} tracks", max_pool)
    except Exception:
        pass

    return tracks


def generate_forgotten_gems(ps, lib, ug_config, plex_lookup, preferred_genres, similar_tracks):
    playlist_name = ug_config.get("name", "Forgotten Gems")
    ps._log.info("Generating {} playlist", playlist_name)
    defaults_cfg = get_plexsync_config(["playlists", "defaults"], dict, {})
    max_tracks = get_config_value(ug_config, defaults_cfg, "max_tracks", 20)
    discovery_ratio = get_config_value(ug_config, defaults_cfg, "discovery_ratio", 30)
    exclusion_days = get_config_value(ug_config, defaults_cfg, "exclusion_days", 30)
    filters = ug_config.get("filters", {})
    ps._log.debug("Collecting candidate tracks avoiding recent plays...")
    all_library_tracks = _get_library_tracks(ps, preferred_genres, filters, exclusion_days)
    # Skip redundant client-side filtering when server-side filters fully covered them
    if filters:
        try:
            adv = build_advanced_filters(filters, exclusion_days)
        except Exception:
            adv = None
        if not adv:
            all_library_tracks = apply_playlist_filters(ps, all_library_tracks, filters)
    final_tracks = []
    for track in all_library_tracks:
        try:
            beets_item = plex_lookup.get(track.ratingKey)
            if beets_item:
                final_tracks.append(beets_item)
        except Exception as e:
            ps._log.debug("Error converting track {}: {}", track.title, e)
    rated_tracks = []
    unrated_tracks = []
    for track in final_tracks:
        rating = float(getattr(track, 'plex_userrating', 0))
        if rating > 0:
            rated_tracks.append(track)
        else:
            unrated_tracks.append(track)
    unrated_tracks_count, rated_tracks_count = calculate_playlist_proportions(ps, max_tracks, discovery_ratio)
    selected_rated = select_tracks_weighted(ps, rated_tracks, rated_tracks_count, playlist_type="forgotten_gems")
    selected_unrated = select_tracks_weighted(ps, unrated_tracks, unrated_tracks_count, playlist_type="forgotten_gems")
    if len(selected_unrated) < unrated_tracks_count:
        additional_count = min(unrated_tracks_count - len(selected_unrated), max_tracks - len(selected_rated) - len(selected_unrated))
        remaining_rated = [t for t in rated_tracks if t not in selected_rated]
        additional_rated = select_tracks_weighted(ps, remaining_rated, additional_count, playlist_type="forgotten_gems")
        selected_rated.extend(additional_rated)
    selected_tracks = selected_rated + selected_unrated
    if len(selected_tracks) > max_tracks:
        selected_tracks = selected_tracks[:max_tracks]
    import random
    random.shuffle(selected_tracks)
    if not selected_tracks:
        ps._log.warning("No tracks matched criteria for Forgotten Gems playlist")
        return
    try:
        ps._plex_clear_playlist(playlist_name)
        ps._log.info("Cleared existing Forgotten Gems playlist")
    except Exception:
        ps._log.debug("No existing Forgotten Gems playlist found")
    ps._plex_add_playlist_item(selected_tracks, playlist_name)
    ps._log.info("Successfully updated {} playlist with {} tracks", playlist_name, len(selected_tracks))


def generate_recent_hits(ps, lib, rh_config, plex_lookup, preferred_genres, similar_tracks):
    playlist_name = rh_config.get("name", "Recent Hits")
    ps._log.info("Generating {} playlist", playlist_name)
    defaults_cfg = get_plexsync_config(["playlists", "defaults"], dict, {})
    max_tracks = get_config_value(rh_config, defaults_cfg, "max_tracks", 20)
    discovery_ratio = get_config_value(rh_config, defaults_cfg, "discovery_ratio", 20)
    exclusion_days = get_config_value(rh_config, defaults_cfg, "exclusion_days", 0)
    filters = rh_config.get("filters", {})
    ps._log.debug("Collecting recent tracks...")
    all_library_tracks = _get_library_tracks(ps, preferred_genres, filters, exclusion_days)
    # Skip redundant client-side filtering when server-side filters fully covered them
    if filters:
        try:
            adv = build_advanced_filters(filters, exclusion_days)
        except Exception:
            adv = None
        if not adv:
            all_library_tracks = apply_playlist_filters(ps, all_library_tracks, filters)
    final_tracks = []
    for track in all_library_tracks:
        try:
            beets_item = plex_lookup.get(track.ratingKey)
            if beets_item:
                final_tracks.append(beets_item)
        except Exception as e:
            ps._log.debug("Error converting track {}: {}", track.title, e)
    
    # Separate rated and unrated tracks
    rated_tracks = []
    unrated_tracks = []
    for track in final_tracks:
        rating = float(getattr(track, 'plex_userrating', 0))
        if rating > 0:
            rated_tracks.append(track)
        else:
            unrated_tracks.append(track)
    
    ps._log.debug("Split into {} rated and {} unrated tracks", len(rated_tracks), len(unrated_tracks))
    
    # Calculate track proportions based on discovery_ratio
    # For Recent Hits, we typically want more highly-rated popular tracks
    unrated_tracks_count, rated_tracks_count = calculate_playlist_proportions(ps, max_tracks, discovery_ratio)
    
    # Select tracks using weighted scoring optimized for recent hits
    selected_rated = select_tracks_weighted(ps, rated_tracks, rated_tracks_count, playlist_type="recent_hits")
    selected_unrated = select_tracks_weighted(ps, unrated_tracks, unrated_tracks_count, playlist_type="recent_hits")
    
    # Fill remaining slots if needed
    if len(selected_unrated) < unrated_tracks_count:
        additional_count = min(unrated_tracks_count - len(selected_unrated), max_tracks - len(selected_rated) - len(selected_unrated))
        remaining_rated = [t for t in rated_tracks if t not in selected_rated]
        additional_rated = select_tracks_weighted(ps, remaining_rated, additional_count, playlist_type="recent_hits")
        selected_rated.extend(additional_rated)
    
    selected_tracks = selected_rated + selected_unrated
    if len(selected_tracks) > max_tracks:
        selected_tracks = selected_tracks[:max_tracks]
    
    import random
    random.shuffle(selected_tracks)
    if not selected_tracks:
        ps._log.warning("No tracks matched criteria for Recent Hits playlist")
        return
    try:
        ps._plex_clear_playlist(playlist_name)
        ps._log.info("Cleared existing Recent Hits playlist")
    except Exception:
        ps._log.debug("No existing Recent Hits playlist found")
    ps._plex_add_playlist_item(selected_tracks, playlist_name)
    ps._log.info("Successfully updated {} playlist with {} tracks", playlist_name, len(selected_tracks))


def generate_fresh_favorites(ps, lib, ff_config, plex_lookup, preferred_genres, similar_tracks):
    playlist_name = ff_config.get("name", "Fresh Favorites")
    ps._log.info("Generating {} playlist", playlist_name)
    defaults_cfg = get_plexsync_config(["playlists", "defaults"], dict, {})
    max_tracks = get_config_value(ff_config, defaults_cfg, "max_tracks", 100)
    discovery_ratio = get_config_value(ff_config, defaults_cfg, "discovery_ratio", 25)
    exclusion_days = get_config_value(ff_config, defaults_cfg, "exclusion_days", 21)
    min_rating = get_config_value(ff_config, defaults_cfg, "min_rating", 6)
    filters = ff_config.get("filters", {})
    ps._log.debug("Collecting candidate tracks avoiding recent plays...")
    all_library_tracks = _get_library_tracks(ps, preferred_genres, filters, exclusion_days)
    # Skip redundant client-side filtering when server-side filters fully covered them
    if filters:
        try:
            adv = build_advanced_filters(filters, exclusion_days)
        except Exception:
            adv = None
        if not adv:
            all_library_tracks = apply_playlist_filters(ps, all_library_tracks, filters)
    final_tracks = []
    for track in all_library_tracks:
        try:
            beets_item = plex_lookup.get(track.ratingKey)
            if beets_item:
                final_tracks.append(beets_item)
        except Exception as e:
            ps._log.debug("Error converting track {}: {}", track.title, e)
    rated_tracks = []
    unrated_tracks = []
    for track in final_tracks:
        rating = float(getattr(track, 'plex_userrating', 0))
        # Apply min_rating filter for rated tracks only
        if rating > 0 and rating >= min_rating:
            rated_tracks.append(track)
        elif rating == 0:  # Only include unrated tracks
            unrated_tracks.append(track)
    ps._log.debug("Found {} rated and {} unrated tracks", len(rated_tracks), len(unrated_tracks))
    unrated_tracks_count, rated_tracks_count = calculate_playlist_proportions(ps, max_tracks, discovery_ratio)
    selected_rated = select_tracks_weighted(ps, rated_tracks, rated_tracks_count, playlist_type="fresh_favorites")
    selected_unrated = select_tracks_weighted(ps, unrated_tracks, unrated_tracks_count, playlist_type="fresh_favorites")
    if len(selected_unrated) < unrated_tracks_count:
        additional_count = min(unrated_tracks_count - len(selected_unrated), max_tracks - len(selected_rated) - len(selected_unrated))
        remaining_rated = [t for t in rated_tracks if t not in selected_rated]
        additional_rated = select_tracks_weighted(ps, remaining_rated, additional_count, playlist_type="fresh_favorites")
        selected_rated.extend(additional_rated)
    selected_tracks = selected_rated + selected_unrated
    if len(selected_tracks) > max_tracks:
        selected_tracks = selected_tracks[:max_tracks]
    import random
    random.shuffle(selected_tracks)
    if not selected_tracks:
        ps._log.warning("No tracks matched criteria for Fresh Favorites playlist")
        return
    try:
        ps._plex_clear_playlist(playlist_name)
        ps._log.info("Cleared existing Fresh Favorites playlist")
    except Exception:
        ps._log.debug("No existing Fresh Favorites playlist found")
    ps._plex_add_playlist_item(selected_tracks, playlist_name)
    ps._log.info("Successfully updated {} playlist with {} tracks", playlist_name, len(selected_tracks))


def generate_imported_playlist(ps, lib, playlist_config, plex_lookup=None):
    playlist_name = playlist_config.get("name", "Imported Playlist")
    sources = playlist_config.get("sources", [])
    max_tracks = playlist_config.get("max_tracks", None)
    import os
    log_file = os.path.join(ps.config_dir, f"{playlist_name.lower().replace(' ', '_')}_import.log")
    from datetime import datetime as _dt
    with open(log_file, 'w', encoding='utf-8') as f:
        f.write(f"Import log for playlist: {playlist_name}\n")
        f.write(f"Import started at: {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("-" * 80 + "\n\n")
    defaults_cfg = get_plexsync_config(["playlists", "defaults"], dict, {})
    manual_search = get_config_value(
        playlist_config, defaults_cfg, "manual_search", get_plexsync_config("manual_search", bool, False)
    )
    clear_playlist = get_config_value(
        playlist_config, defaults_cfg, "clear_playlist", False
    )
    if not sources:
        ps._log.warning("No sources defined for imported playlist {}", playlist_name)
        return
    ps._log.info("Generating imported playlist {} from {} sources", playlist_name, len(sources))
    all_tracks = []
    source_progress = ps.create_progress_counter(
        total=len(sources),
        desc=f"{playlist_name[:18]} src",
        unit="source",
    )
    try:
        for source in sources:
            try:
                tracks = []
                src_desc = None
                # String source (URL or file)
                if isinstance(source, str):
                    src_desc = source
                    low = source.lower()
                    if low.endswith('.m3u8'):
                        # Resolve relative path under config dir
                        if not os.path.isabs(source):
                            source = os.path.join(ps.config_dir, source)
                        ps._log.info("Importing from M3U8: {}", source)
                        tracks = import_m3u8_playlist(source, ps.cache)
                    elif 'spotify' in low:
                        from beetsplug.providers.spotify import get_playlist_id as _get_pl_id
                        ps._log.info("Importing from Spotify URL")
                        tracks = ps.import_spotify_playlist(_get_pl_id(source))
                    elif 'jiosaavn' in low:
                        ps._log.info("Importing from JioSaavn URL")
                        tracks = ps.import_jiosaavn_playlist(source)
                    elif 'apple' in low:
                        ps._log.info("Importing from Apple Music URL")
                        tracks = ps.import_apple_playlist(source)
                    elif 'gaana' in low:
                        ps._log.info("Importing from Gaana URL")
                        tracks = import_gaana_playlist(source, ps.cache)
                    elif 'youtube' in low:
                        ps._log.info("Importing from YouTube URL")
                        tracks = import_yt_playlist(source, ps.cache)
                    elif 'tidal' in low:
                        ps._log.info("Importing from Tidal URL")
                        tracks = import_tidal_playlist(source, ps.cache)
                    else:
                        ps._log.warning("Unsupported string source: {}", source)
                # Dict source (typed)
                elif isinstance(source, dict):
                    source_type = source.get("type")
                    src_desc = source_type or "Unknown"
                    if source_type == "Apple Music":
                        ps._log.info("Importing from Apple Music: {}", source.get("name", ""))
                        tracks = ps.import_apple_playlist(source.get("url", ""))
                    elif source_type == "JioSaavn":
                        ps._log.info("Importing from JioSaavn: {}", source.get("name", ""))
                        tracks = ps.import_jiosaavn_playlist(source.get("url", ""))
                    elif source_type == "Gaana":
                        ps._log.info("Importing from Gaana: {}", source.get("name", ""))
                        tracks = import_gaana_playlist(source.get("url", ""), ps.cache)
                    elif source_type == "Spotify":
                        ps._log.info("Importing from Spotify: {}", source.get("name", ""))
                        from beetsplug.providers.spotify import get_playlist_id as _get_pl_id
                        tracks = ps.import_spotify_playlist(_get_pl_id(source.get("url", "")))
                    elif source_type == "YouTube":
                        ps._log.info("Importing from YouTube: {}", source.get("name", ""))
                        tracks = import_yt_playlist(source.get("url", ""), ps.cache)
                    elif source_type == "Tidal":
                        ps._log.info("Importing from Tidal: {}", source.get("name", ""))
                        tracks = import_tidal_playlist(source.get("url", ""), ps.cache)
                    elif source_type == "M3U8":
                        fp = source.get("filepath", "")
                        if fp and not os.path.isabs(fp):
                            fp = os.path.join(ps.config_dir, fp)
                        ps._log.info("Importing from M3U8: {}", fp)
                        tracks = import_m3u8_playlist(fp, ps.cache)
                    elif source_type == "POST":
                        ps._log.info("Importing from POST endpoint")
                        tracks = import_post_playlist(source, ps.cache)
                    else:
                        ps._log.warning("Unsupported source type: {}", source_type)
                else:
                    src_desc = str(type(source))
                    ps._log.warning("Invalid source format: {}", src_desc)

                if tracks:
                    ps._log.info("Imported {} tracks from {}", len(tracks), src_desc)
                    all_tracks.extend(tracks)
            except Exception as e:
                ps._log.error("Error importing from {}: {}", src_desc or "Unknown", e)
                continue
            finally:
                if source_progress is not None:
                    try:
                        source_progress.update()
                    except Exception:
                        ps._log.debug("Failed to update source progress for {}", playlist_name)
    finally:
        if source_progress is not None:
            try:
                source_progress.close()
            except Exception:
                ps._log.debug("Failed to close source progress for {}", playlist_name)
    unique_tracks = []
    seen = set()
    for t in all_tracks:
        # Some sources may set explicit None values; normalize to empty strings before lowercasing
        key = (
            (t.get('title') or '').lower(),
            (t.get('artist') or '').lower(),
            (t.get('album') or '').lower(),
        )
        if key not in seen:
            seen.add(key)
            unique_tracks.append(t)
    ps._log.info("Found {} unique tracks across sources", len(unique_tracks))
    matched_songs = []
    match_progress = ps.create_progress_counter(
        total=len(unique_tracks),
        desc=f"{playlist_name[:18]} match",
        unit="track",
    )
    try:
        for song in unique_tracks:
            found = ps.search_plex_song(song, manual_search)
            if found is not None:
                matched_songs.append(found)
            if match_progress is not None:
                try:
                    match_progress.update()
                except Exception:
                    ps._log.debug("Failed to update match progress for {}", playlist_name)
    finally:
        if match_progress is not None:
            try:
                match_progress.close()
            except Exception:
                ps._log.debug("Failed to close match progress for {}", playlist_name)
    ps._log.info("Matched {} tracks in Plex", len(matched_songs))
    if max_tracks:
        matched_songs = matched_songs[:max_tracks]
    unique_matched = []
    seen_keys = set()
    for track in matched_songs:
        key = getattr(track, 'ratingKey', None)
        if key and key not in seen_keys:
            seen_keys.add(key)
            unique_matched.append(track)
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write("\nImport Summary:\n")
        f.write(f"Total tracks fetched from sources: {len(all_tracks)}\n")
        f.write(f"Unique tracks after de-duplication: {len(unique_tracks)}\n")
        f.write(f"Tracks matched and added: {len(unique_matched)}\n")
        f.write(f"\nImport completed at: {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    ps._log.info("Found {} unique tracks after filtering (see {} for details)", len(unique_matched), log_file)
    if clear_playlist:
        try:
            ps._plex_clear_playlist(playlist_name)
            ps._log.info("Cleared existing playlist {}", playlist_name)
        except Exception:
            ps._log.debug("No existing playlist {} found", playlist_name)
    if unique_matched:
        ps._plex_add_playlist_item(unique_matched, playlist_name)
        ps._log.info("Successfully created playlist {} with {} tracks", playlist_name, len(unique_matched))
    else:
        ps._log.warning("No tracks remaining after filtering for {}", playlist_name)
