"""
Logic for generating smart playlists like Daily Discovery, Forgotten Gems,
and processing imported playlists based on various criteria.
"""
import logging
import os
import random
import enlighten
from datetime import datetime, timedelta
import numpy as np
from scipy import stats
from plexapi import exceptions # For plex_clear_playlist and plex_add_playlist_item potentially

from beetsplug.matching import clean_string # If needed for parsing import logs
# Assuming plex_utils contains plex_clear_playlist and plex_add_playlist_item
# If not, these need to be passed or this module needs a PlexServer instance.
# For now, let's assume they might be passed or this module focuses on track selection logic.
# from beetsplug import plex_utils # This might create circular dependency if plex_utils calls smart_playlists

_log = logging.getLogger('beets.plexsync.smart_playlists')


# Helper: Calculate playlist proportions (moved from PlexSync)
def calculate_playlist_proportions(max_tracks, discovery_ratio):
    unrated_tracks_count = min(int(max_tracks * (discovery_ratio / 100)), max_tracks)
    rated_tracks_count = max_tracks - unrated_tracks_count
    return unrated_tracks_count, rated_tracks_count

# Helper: Validate filter config (moved from PlexSync)
def validate_filter_config(filter_config):
    if not isinstance(filter_config, dict):
        return False, "Filter configuration must be a dictionary"
    for section in ['exclude', 'include']:
        if section in filter_config:
            if not isinstance(filter_config[section], dict):
                return False, "{} section must be a dictionary".format(section)
            section_config = filter_config[section]
            if 'genres' in section_config and not isinstance(section_config['genres'], list):
                return False, "{}.genres must be a list".format(section)
            if 'years' in section_config:
                years = section_config['years']
                if not isinstance(years, dict): return False, "{}.years must be a dictionary".format(section)
                if 'before' in years and not isinstance(years['before'], int): return False, "{}.years.before must be an integer".format(section)
                if 'after' in years and not isinstance(years['after'], int): return False, "{}.years.after must be an integer".format(section)
                if 'between' in years and (not isinstance(years['between'], list) or len(years['between']) != 2 or not all(isinstance(y, int) for y in years['between'])):
                    return False, "{}.years.between must be a list of two integers".format(section)
    if 'min_rating' in filter_config and (not isinstance(filter_config['min_rating'], (int, float)) or not 0 <= filter_config['min_rating'] <= 10):
        return False, "min_rating must be a number between 0 and 10"
    return True, ""

# Helper: Apply exclusion filters (moved from PlexSync)
def _apply_exclusion_filters(tracks, exclude_config):
    import xml.etree.ElementTree as ET # Keep import local if only used here
    filtered_tracks = []
    original_count = len(tracks)
    for track in tracks:
        try:
            if 'genres' in exclude_config and hasattr(track, 'genres') and any(g.tag.lower() in (ge.lower() for ge in exclude_config['genres']) for g in track.genres):
                continue
            if 'years' in exclude_config:
                years_config = exclude_config['years']
                if 'before' in years_config and hasattr(track, 'year') and track.year is not None and track.year < years_config['before']:
                    continue
                if 'after' in years_config and hasattr(track, 'year') and track.year is not None and track.year > years_config['after']:
                    continue
            filtered_tracks.append(track)
        except (ET.ParseError, Exception) as e: # ET.ParseError might not be relevant for Plex track objects
            _log.debug("Skipping track due to exception in exclusion filter: {}", e)
            continue
    _log.debug("Exclusion filters removed {} tracks", original_count - len(filtered_tracks))
    return filtered_tracks

# Helper: Apply inclusion filters (moved from PlexSync)
def _apply_inclusion_filters(tracks, include_config):
    import xml.etree.ElementTree as ET # Keep import local
    filtered_tracks = []
    original_count = len(tracks)
    for track in tracks:
        try:
            if 'genres' in include_config and not (hasattr(track, 'genres') and any(g.tag.lower() in (gi.lower() for gi in include_config['genres']) for g in track.genres)):
                continue
            if 'years' in include_config and 'between' in include_config['years']:
                start_year, end_year = include_config['years']['between']
                if not (hasattr(track, 'year') and track.year is not None and start_year <= track.year <= end_year):
                    continue
            filtered_tracks.append(track)
        except (ET.ParseError, Exception) as e: # ET.ParseError might not be relevant
            _log.debug("Skipping track due to exception in inclusion filter: {}", e)
            continue
    _log.debug("Inclusion filters removed {} tracks", original_count - len(filtered_tracks))
    return filtered_tracks

# Helper: Apply all playlist filters (moved from PlexSync)
def apply_playlist_filters(tracks, filter_config):
    if not tracks: return tracks
    is_valid, error = validate_filter_config(filter_config)
    if not is_valid:
        _log.error("Invalid filter configuration: {}", error)
        return tracks

    _log.debug("Applying filters to {} tracks", len(tracks))
    filtered_tracks = list(tracks) # Make a copy

    if 'exclude' in filter_config:
        _log.debug("Applying exclusion filters...")
        filtered_tracks = _apply_exclusion_filters(filtered_tracks, filter_config['exclude'])
    if 'include' in filter_config:
        _log.debug("Applying inclusion filters...")
        filtered_tracks = _apply_inclusion_filters(filtered_tracks, filter_config['include'])
    if 'min_rating' in filter_config:
        min_rating = filter_config['min_rating']
        original_count = len(filtered_tracks)
        unrated = [t for t in filtered_tracks if not hasattr(t, 'userRating') or t.userRating is None or float(t.userRating or 0) == 0]
        rated_above_min = [t for t in filtered_tracks if hasattr(t, 'userRating') and t.userRating is not None and float(t.userRating or 0) >= min_rating]
        filtered_tracks = rated_above_min + unrated
        _log.debug("Rating filter (>= {}): {} -> {} tracks ({} rated, {} unrated)", min_rating, original_count, len(filtered_tracks), len(rated_above_min), len(unrated))

    _log.debug("Filter application complete: {} -> {} tracks", len(tracks), len(filtered_tracks))
    return filtered_tracks


# Helper: Calculate track score (moved from PlexSync)
def calculate_track_score(track, base_time=None, tracks_context=None, rating_mean=5, rating_std=2.5, days_mean=365, days_std=180, pop_mean=30, pop_std=20, age_mean_val=30, age_std_val=10): # Added _val to age_mean, age_std
    if base_time is None: base_time = datetime.now()
    rating = float(getattr(track, 'plex_userrating', 0))
    last_played = getattr(track, 'plex_lastviewedat', None)
    popularity = float(getattr(track, 'spotify_track_popularity', 0)) # Assuming this field might exist on beets items
    release_year = getattr(track, 'year', None)
    age = base_time.year - int(release_year) if release_year and isinstance(release_year, (int, str)) and str(release_year).isdigit() else 0

    days_since_played = np.random.exponential(365) if last_played is None else min((base_time - datetime.fromtimestamp(last_played)).days, 1095)

    if tracks_context: # If context is provided, recalculate means and stds
        all_ratings = [float(getattr(t, 'plex_userrating', 0)) for t in tracks_context]
        all_days = [(base_time - datetime.fromtimestamp(getattr(t, 'plex_lastviewedat', base_time - timedelta(days=365)))).days for t in tracks_context]
        all_popularity = [float(getattr(t, 'spotify_track_popularity', 0)) for t in tracks_context]
        all_ages = [base_time.year - int(getattr(t, 'year', base_time.year)) for t in tracks_context]
        rating_mean, rating_std = np.mean(all_ratings), np.std(all_ratings) or 1
        days_mean, days_std = np.mean(all_days), np.std(all_days) or 1
        pop_mean, pop_std = np.mean(all_popularity), np.std(all_popularity) or 1
        age_mean_val, age_std_val = np.mean(all_ages), np.std(all_ages) or 1

    z_rating = np.clip((rating - rating_mean) / rating_std if rating > 0 else -2.0, -3, 3)
    z_recency = np.clip(-(days_since_played - days_mean) / days_std, -3, 3)
    z_popularity = np.clip((popularity - pop_mean) / pop_std, -3, 3)
    z_age = np.clip(-(age - age_mean_val) / age_std_val, -3, 3)

    is_rated = rating > 0
    weighted_score = ((z_rating * 0.5) + (z_recency * 0.1) + (z_popularity * 0.1) + (z_age * 0.2) if is_rated
                      else (z_recency * 0.2) + (z_popularity * 0.5) + (z_age * 0.3))

    final_score = stats.norm.cdf(weighted_score * 1.5) * 100 + np.random.normal(0, 0.5)
    if not is_rated and final_score < 50: final_score = 50 + (final_score / 2)

    _log.debug("Score for {}: rating={:.2f}(z={:.2f}), days={:.0f}(z={:.2f}), pop={:.2f}(z={:.2f}), age={:.0f}(z={:.2f}), final={:.2f}",
               track.title, rating, z_rating, days_since_played, z_recency, popularity, z_popularity, age, z_age, final_score)
    return max(0, min(100, final_score))

# Helper: Select tracks weighted (moved from PlexSync)
def select_tracks_weighted(tracks, num_tracks, base_time=None):
    if not tracks: return []
    if base_time is None: base_time = datetime.now()

    # Use default means/stds for scoring if no context is passed to calculate_track_score
    track_scores = [(track, calculate_track_score(track, base_time)) for track in tracks]

    scores = np.array([score for _, score in track_scores])
    if not np.any(scores): # Handle case where all scores are zero
        probabilities = None # Will lead to uniform random choice
    else:
        # Softmax with temperature to control randomness
        probabilities = np.exp(scores / 10.0) / np.sum(np.exp(scores / 10.0))
        if np.isnan(probabilities).any(): # Fallback if softmax results in NaN (e.g. extreme score differences)
            _log.warning("Softmax resulted in NaN probabilities, falling back to uniform random choice.")
            probabilities = None

    selected_indices = np.random.choice(len(tracks), size=min(num_tracks, len(tracks)), replace=False, p=probabilities)
    selected_tracks = [tracks[i] for i in selected_indices]

    for i, track in enumerate(selected_tracks):
        score = track_scores[selected_indices[i]][1]
        _log.debug("Selected: {} - {} (Score: {:.2f}, Rating: {}, Plays: {})",
                   getattr(track, 'album', 'N/A'), track.title, score, getattr(track, 'plex_userrating', 0), getattr(track, 'plex_viewcount', 0))
    return selected_tracks


# --- Smart Playlist Generation Functions ---

def generate_daily_discovery_playlist(plex_music_library, beets_lib, plex_lookup, preferred_genres, similar_tracks_from_history, playlist_config, defaults_config, plex_clear_playlist_func, plex_add_playlist_item_func):
    """
    Generates the 'Daily Discovery' playlist.
    All inputs are expected to be pre-fetched/configured.
    plex_clear_playlist_func and plex_add_playlist_item_func are functions from plex_utils.
    """
    playlist_name = playlist_config.get("name", "Daily Discovery")
    _log.info("Generating {} playlist", playlist_name)

    max_tracks = playlist_config.get("max_tracks", defaults_config.get("max_tracks", 20))
    discovery_ratio = playlist_config.get("discovery_ratio", defaults_config.get("discovery_ratio", 30))

    # Convert Plex track objects (similar_tracks_from_history) to beets items if needed by filters/scoring
    # For now, assume similar_tracks_from_history are Plex track objects, and filtering can handle them.
    # If filters or scoring expect beets items, conversion using plex_lookup is needed here.

    filters = playlist_config.get("filters", {})
    filtered_similar_tracks = apply_playlist_filters(similar_tracks_from_history, filters) if filters else similar_tracks_from_history
    _log.debug("After filtering similar_tracks: {} tracks remain", len(filtered_similar_tracks))

    # Convert to beets items for final selection and scoring
    # (as calculate_track_score might expect beets item attributes like 'plex_userrating')
    beets_items_for_playlist = []
    for plex_track in filtered_similar_tracks:
        beets_item = plex_lookup.get(plex_track.ratingKey)
        if beets_item:
            # Augment beets_item with necessary fields from plex_track if not already synced
            # e.g., beets_item.plex_userrating = plex_track.userRating (if not already up-to-date)
            beets_items_for_playlist.append(beets_item)
        else:
            _log.debug("Plex track {} (key {}) not found in beets_lookup for Daily Discovery.", plex_track.title, plex_track.ratingKey)

    rated_tracks = [t for t in beets_items_for_playlist if float(getattr(t, 'plex_userrating', 0) or 0) > 0]
    unrated_tracks = [t for t in beets_items_for_playlist if float(getattr(t, 'plex_userrating', 0) or 0) == 0]

    unrated_count, rated_count = calculate_playlist_proportions(max_tracks, discovery_ratio)

    selected_rated = select_tracks_weighted(rated_tracks, rated_count)
    selected_unrated = select_tracks_weighted(unrated_tracks, unrated_count)

    # Fill up if one category is short
    if len(selected_unrated) < unrated_count:
        needed = unrated_count - len(selected_unrated)
        remaining_rated = [t for t in rated_tracks if t not in selected_rated]
        selected_rated.extend(select_tracks_weighted(remaining_rated, needed))
    elif len(selected_rated) < rated_count:
        needed = rated_count - len(selected_rated)
        remaining_unrated = [t for t in unrated_tracks if t not in selected_unrated]
        selected_unrated.extend(select_tracks_weighted(remaining_unrated, needed))

    final_playlist_tracks = selected_rated + selected_unrated
    random.shuffle(final_playlist_tracks)
    final_playlist_tracks = final_playlist_tracks[:max_tracks]

    if not final_playlist_tracks:
        _log.warning("No tracks matched criteria for {} playlist", playlist_name)
        return

    try:
        plex_clear_playlist_func(playlist_name) # Assumes this function takes plex_instance implicitly or is bound
        _log.info("Cleared existing {} playlist", playlist_name)
    except exceptions.NotFound:
        _log.debug("No existing {} playlist found to clear", playlist_name)

    plex_add_playlist_item_func(final_playlist_tracks, playlist_name) # Same assumption for plex_add_playlist_item_func
    _log.info("Successfully updated {} playlist with {} tracks", playlist_name, len(final_playlist_tracks))


def get_preferred_attributes_from_history(plex_music_library, history_days, exclusion_days):
    """
    Determines preferred genres and identifies similar tracks based on listening history.
    This is a data gathering step.
    """
    _log.info("Determining preferred attributes from listening history (%d days, excluding last %d days)", history_days, exclusion_days)

    # Fetch tracks played in the configured history period
    history_tracks = plex_music_library.search(filters={"track.lastViewedAt>>": "{}d".format(history_days)}, libtype="track")

    genre_counts = {}
    potential_similar_tracks = set() # Store Plex track objects

    # Tracks played in the exclusion period (more recent than history_days, but within exclusion_days)
    recently_played_keys = {
        track.ratingKey for track in plex_music_library.search(filters={"track.lastViewedAt>>": "{}d".format(exclusion_days)}, libtype="track")
    }
    _log.debug("Found %d recently played track keys for exclusion.", len(recently_played_keys))

    for track in history_tracks:
        track_genres_tags = {genre.tag.lower() for genre in track.genres if genre and genre.tag}
        for genre_tag in track_genres_tags:
            genre_counts[genre_tag] = genre_counts.get(genre_tag, 0) + 1

        try:
            sonic_matches = track.sonicallySimilar()
            for match in sonic_matches:
                # Filter similar tracks: not recently played, shares a genre from the seed track, and rating criteria
                match_rating = getattr(match, "userRating", -1.0) # Default -1 for unrated
                if match.ratingKey not in recently_played_keys:
                    match_genres_tags = {g.tag.lower() for g in match.genres if g and g.tag}
                    if not track_genres_tags.isdisjoint(match_genres_tags): # Shares at least one genre
                         if match_rating == -1.0 or match_rating is None or match_rating >= 4.0 : # Unrated or highly rated
                            potential_similar_tracks.add(match)
        except Exception as e:
            _log.debug("Error getting sonically similar tracks for %s: %s", track.title, e)

    sorted_genres = sorted(genre_counts, key=genre_counts.get, reverse=True)[:5] # Top 5 preferred genres
    _log.info("Top preferred genres based on history: %s", sorted_genres)
    _log.info("Found %d potential similar tracks from history analysis.", len(potential_similar_tracks))

    return sorted_genres, list(potential_similar_tracks)


def get_filtered_library_tracks_for_gems(plex_music_library, config_filters, exclusion_days):
    """
    Get filtered library tracks using Plex's advanced filters, specifically for Forgotten Gems.
    This is a data gathering step.
    """
    _log.info("Fetching filtered library tracks for Forgotten Gems (excluding last {} days)", exclusion_days)
    advanced_filters = {'and': []}

    if config_filters:
        if 'include' in config_filters and 'genres' in config_filters['include']:
            include_genres = list(set(g.lower() for g in config_filters['include']['genres']))
            if include_genres:
                advanced_filters['and'].append({'or': [{'genre': genre} for genre in include_genres]})

        if 'exclude' in config_filters and 'genres' in config_filters['exclude']:
            exclude_genres = list(set(g.lower() for g in config_filters['exclude']['genres']))
            if exclude_genres: # Plex API might use genre! for multiple exclusions
                advanced_filters['and'].append({'genre!': exclude_genres})

        if 'include' in config_filters and 'years' in config_filters['include'] and 'between' in config_filters['include']['years']:
            start_year, end_year = config_filters['include']['years']['between']
            advanced_filters['and'].extend([{'year>>': start_year}, {'year<<': end_year}])

        if 'min_rating' in config_filters:
             advanced_filters['and'].append({'or': [{'userRating': 0}, {'userRating>>': config_filters['min_rating']}]})

    if exclusion_days > 0:
        advanced_filters['and'].append({'lastViewedAt<<': "-{}d".format(exclusion_days)}) # Tracks not played recently

    _log.debug("Using advanced filters for Forgotten Gems library scan: {}", advanced_filters)
    try:
        tracks = plex_music_library.searchTracks(filters=advanced_filters, limit=None) # Get all matching
        _log.info("Found {} tracks from library matching Forgotten Gems criteria.", len(tracks))
        return tracks
    except Exception as e:
        _log.error("Error searching library with advanced filters for Forgotten Gems: {}. Filter: {}", e, advanced_filters)
        return []


def generate_forgotten_gems_playlist(plex_music_library, beets_lib, plex_lookup, playlist_config, defaults_config, library_tracks_for_gems, similar_tracks_from_history, plex_clear_playlist_func, plex_add_playlist_item_func):
    """
    Generates the 'Forgotten Gems' playlist.
    library_tracks_for_gems and similar_tracks_from_history are pre-fetched Plex track objects.
    """
    playlist_name = playlist_config.get("name", "Forgotten Gems")
    _log.info("Generating {} playlist", playlist_name)

    max_tracks = playlist_config.get("max_tracks", defaults_config.get("max_tracks", 20))
    discovery_ratio = playlist_config.get("discovery_ratio", defaults_config.get("discovery_ratio", 30)) # Ratio of unrated/new to rated/known

    # Combine library tracks and similar tracks, ensuring uniqueness
    combined_track_pool_plex = {track.ratingKey: track for track in library_tracks_for_gems}
    for track in similar_tracks_from_history:
        if track.ratingKey not in combined_track_pool_plex:
            combined_track_pool_plex[track.ratingKey] = track

    _log.debug("Combined pool for Forgotten Gems has {} unique Plex tracks.", len(combined_track_pool_plex))

    # Convert to beets items for scoring and final selection
    beets_items_for_playlist = []
    for plex_track in combined_track_pool_plex.values():
        beets_item = plex_lookup.get(plex_track.ratingKey)
        if beets_item:
            beets_items_for_playlist.append(beets_item)
        else:
            _log.debug("Plex track {} (key {}) not found in beets_lookup for Forgotten Gems.", plex_track.title, plex_track.ratingKey)

    # Apply further filters if specified in this playlist's config (on beets_items if filters expect that)
    filters = playlist_config.get("filters", {}) # These filters are in addition to the initial library scan filters
    if filters: # This might be redundant if get_filtered_library_tracks already applied them.
                # However, this allows filtering the 'similar_tracks_from_history' part of the pool too.
        beets_items_for_playlist = apply_playlist_filters(beets_items_for_playlist, filters)

    _log.debug("After applying specific playlist filters, {} beets items remain for Forgotten Gems.", len(beets_items_for_playlist))

    rated_tracks = [t for t in beets_items_for_playlist if float(getattr(t, 'plex_userrating', 0) or 0) > 0]
    unrated_tracks = [t for t in beets_items_for_playlist if float(getattr(t, 'plex_userrating', 0) or 0) == 0]

    unrated_count, rated_count = calculate_playlist_proportions(max_tracks, discovery_ratio)

    selected_rated = select_tracks_weighted(rated_tracks, rated_count)
    selected_unrated = select_tracks_weighted(unrated_tracks, unrated_count)

    # Fill up logic (same as Daily Discovery)
    if len(selected_unrated) < unrated_count:
        needed = unrated_count - len(selected_unrated)
        remaining_rated = [t for t in rated_tracks if t not in selected_rated]
        selected_rated.extend(select_tracks_weighted(remaining_rated, needed))
    elif len(selected_rated) < rated_count:
        needed = rated_count - len(selected_rated)
        remaining_unrated = [t for t in unrated_tracks if t not in selected_unrated]
        selected_unrated.extend(select_tracks_weighted(remaining_unrated, needed))

    final_playlist_tracks = selected_rated + selected_unrated
    random.shuffle(final_playlist_tracks)
    final_playlist_tracks = final_playlist_tracks[:max_tracks]

    if not final_playlist_tracks:
        _log.warning("No tracks matched criteria for {} playlist", playlist_name)
        return

    try:
        plex_clear_playlist_func(playlist_name)
        _log.info("Cleared existing {} playlist", playlist_name)
    except exceptions.NotFound:
        _log.debug("No existing {} playlist found to clear", playlist_name)

    plex_add_playlist_item_func(final_playlist_tracks, playlist_name)
    _log.info("Successfully updated {} playlist with {} tracks", playlist_name, len(final_playlist_tracks))

# generate_imported_playlist and process_import_logs will be handled by PlexSync directly,
# as they involve more direct interaction with Plex for matching and UI for manual search.
# However, the track import part of generate_imported_playlist will use playlist_importers.py.
# The log processing might also need a helper if parsing becomes complex.
