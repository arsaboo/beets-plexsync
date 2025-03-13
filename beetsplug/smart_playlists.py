"""Smart playlist generation functionality for PlexSync plugin."""

import os
import random
from datetime import datetime, timedelta

import numpy as np
from scipy import stats

# Import core shared function
from beetsplug.core import build_plex_lookup


def get_preferred_attributes(plugin):
    """Determine preferred genres and similar tracks based on user listening habits."""
    # Get history period from config
    if (
        "playlists" in plugin.config["plexsync"]
        and "defaults" in plugin.config["plexsync"]["playlists"]
    ):
        defaults_cfg = plugin.config["plexsync"]["playlists"]["defaults"].get({})
    else:
        defaults_cfg = {}

    history_days = get_config_value(
        plugin, plugin.config["plexsync"], defaults_cfg, "history_days", 15
    )
    exclusion_days = get_config_value(
        plugin, plugin.config["plexsync"], defaults_cfg, "exclusion_days", 30
    )

    # Fetch tracks played in the configured period
    tracks = plugin.music.search(
        filters={"track.lastViewedAt>>": f"{history_days}d"}, libtype="track"
    )

    # Track genre counts and similar tracks
    genre_counts = {}
    similar_tracks = set()

    recently_played = set(
        track.ratingKey
        for track in plugin.music.search(
            filters={"track.lastViewedAt>>": f"{exclusion_days}d"}, libtype="track"
        )
    )

    for track in tracks:
        # Count genres
        track_genres = set()
        for genre in track.genres:
            if genre:
                genre_str = str(genre.tag).lower()
                genre_counts[genre_str] = genre_counts.get(genre_str, 0) + 1
                track_genres.add(genre_str)

        # Get sonically similar tracks
        try:
            sonic_matches = track.sonicallySimilar()
            # Filter sonic matches
            for match in sonic_matches:
                # Check rating - include unrated (-1) and highly rated (>=4) tracks
                rating = getattr(
                    match, "userRating", -1
                )  # Default to -1 if attribute doesn't exist
                if (
                    match.ratingKey not in recently_played  # Not recently played
                    and any(
                        g.tag.lower() in track_genres for g in match.genres
                    )  # Genre match
                    and (rating is None or rating == -1 or rating >= 4)
                ):  # Rating criteria including None
                    similar_tracks.add(match)
        except Exception as e:
            plugin._log.debug(
                "Error getting similar tracks for {}: {}", track.title, e
            )

    # Sort genres by count
    sorted_genres = sorted(genre_counts, key=genre_counts.get, reverse=True)[:5]
    plugin._log.debug("Top genres: {}", sorted_genres)
    plugin._log.debug("Found {} similar tracks after filtering", len(similar_tracks))

    return sorted_genres, list(similar_tracks)


def get_config_value(plugin, item_cfg, defaults_cfg, key, code_default):
    """Get configuration value with fallbacks."""
    if key in item_cfg:
        val = item_cfg[key]
        return val.get() if hasattr(val, "get") else val
    elif key in defaults_cfg:
        val = defaults_cfg[key]
        return val.get() if hasattr(val, "get") else val
    else:
        return code_default


def calculate_rating_score(plugin, rating):
    """Calculate score based on rating (60% weight)."""
    if not rating or rating <= 0:
        return 0
    score_map = {
        10: 100,
        9: 80,
        8: 60,
        7: 40,
        6: 20
    }
    return score_map.get(int(rating), 0) * 0.6


def calculate_last_played_score(plugin, last_played):
    """Calculate score based on last played date (20% weight)."""
    if not last_played:
        return 100 * 0.2  # Never played gets max score

    days_since_played = (datetime.now() - datetime.fromtimestamp(last_played)).days

    if days_since_played > 180:  # 6 months
        return 80 * 0.2
    elif days_since_played > 90:  # 3 months
        return 60 * 0.2
    elif days_since_played > 30:  # 1 month
        return 40 * 0.2
    else:
        return 20 * 0.2


def calculate_play_count_score(plugin, play_count):
    """Calculate score based on play count (20% weight)."""
    if not play_count or play_count < 0:
        play_count = 0

    if (play_count <= 2):
        return 100 * 0.2
    elif (play_count <= 5):
        return 80 * 0.2
    elif (play_count <= 10):
        return 60 * 0.2
    elif (play_count <= 20):
        return 40 * 0.2
    else:
        return 20 * 0.2


def calculate_track_score(plugin, track, base_time=None, tracks_context=None):
    """Calculate comprehensive score for a track using standardized variables."""
    if base_time is None:
        base_time = datetime.now()

    # Get raw values with better defaults for never played/rated tracks
    rating = float(getattr(track, 'plex_userrating', 0))
    last_played = getattr(track, 'plex_lastviewedat', None)
    popularity = float(getattr(track, 'spotify_track_popularity', 0))
    release_year = getattr(track, 'year', None)

    # Convert release year to age
    if (release_year):
        try:
            release_year = int(release_year)
            age = base_time.year - release_year
        except ValueError:
            age = 0  # Default to 0 if year is invalid
    else:
        age = 0  # Default to 0 if year is missing

    # For never played tracks, use exponential random distribution
    if (last_played is None):
        # Use exponential distribution with mean=365 days
        days_since_played = np.random.exponential(365)
    else:
        days = (base_time - datetime.fromtimestamp(last_played)).days
        # Use exponential decay instead of hard cap
        days_since_played = min(days, 1095)  # Cap at 3 years

    # If we have context tracks, calculate means and stds
    if (tracks_context):
        # Get values for all tracks
        all_ratings = [float(getattr(t, 'plex_userrating', 0)) for t in tracks_context]
        all_days = [
            (base_time - datetime.fromtimestamp(getattr(t, 'plex_lastviewedat', base_time - timedelta(days=365)))).days
            for t in tracks_context
        ]
        all_popularity = [float(getattr(t, 'spotify_track_popularity', 0)) for t in tracks_context]
        all_ages = [base_time.year - int(getattr(t, 'year', base_time.year)) for t in tracks_context]

        # Calculate means and stds
        rating_mean, rating_std = np.mean(all_ratings), np.std(all_ratings) or 1
        days_mean, days_std = np.mean(all_days), np.std(all_days) or 1
        popularity_mean, popularity_std = np.mean(all_popularity), np.std(all_popularity) or 1
        age_mean, age_std = np.mean(all_ages), np.std(all_ages) or 1
    else:
        # Use better population estimates
        rating_mean, rating_std = 5, 2.5        # Ratings 0-10
        days_mean, days_std = 365, 180         # ~1 year mean, 6 months std
        popularity_mean, popularity_std = 30, 20  # Spotify popularity 0-100, adjusted mean
        age_mean, age_std = 30, 10              # Age mean 10 years, std 5 years

    # Calculate z-scores with bounds
    z_rating = (rating - rating_mean) / rating_std if rating > 0 else -2.0
    z_recency = -(days_since_played - days_mean) / days_std  # Negative because fewer days = more recent
    z_popularity = (popularity - popularity_mean) / popularity_std
    z_age = -(age - age_mean) / age_std  # Negative because fewer years = more recent

    # Bound z-scores to avoid extreme values
    z_rating = np.clip(z_rating, -3, 3)
    z_recency = np.clip(z_recency, -3, 3)
    z_popularity = np.clip(z_popularity, -3, 3)
    z_age = np.clip(z_age, -3, 3)

    # Determine if track is rated
    is_rated = rating > 0

    # Apply weights based on rating status
    if (is_rated):
        # For rated tracks: rating=50%, recency=10%, popularity=10%, age=20%
        weighted_score = (z_rating * 0.5) + (z_recency * 0.1) + (z_popularity * 0.1) + (z_age * 0.2)
    else:
        # For unrated tracks: popularity=50%, recency=20%, age=30%
        weighted_score = (z_recency * 0.2) + (z_popularity * 0.5) + (z_age * 0.3)

    # Convert to 0-100 scale using modified percentile calculation
    # Use a steeper sigmoid curve by multiplying weighted_score by 1.5
    final_score = stats.norm.cdf(weighted_score * 1.5) * 100

    # Add very small gaussian noise (reduced from 2 to 0.5) for minor variety
    noise = np.random.normal(0, 0.5)
    final_score = final_score + noise

    # Apply a minimum threshold of 50 for unrated tracks to ensure quality
    if (not is_rated and final_score < 50):
        final_score = 50 + (final_score / 2)  # Scale lower scores up but keep relative ordering

    # Debug logging
    plugin._log.debug(
        "Score components for {}: rating={:.2f} (z={:.2f}), days={:.0f} (z={:.2f}), "
        "popularity={:.2f} (z={:.2f}), age={:.0f} (z={:.2f}), final={:.2f}",
        track.title,
        rating,
        z_rating,
        days_since_played,
        z_recency,
        popularity,
        z_popularity,
        age,
        z_age,
        final_score
    )

    return max(0, min(100, final_score))  # Clamp between 0 and 100


def select_tracks_weighted(plugin, tracks, num_tracks):
    """Select tracks using weighted probability based on scores."""
    if not tracks:
        return []

    # Calculate scores for all tracks
    base_time = datetime.now()
    track_scores = [(track, calculate_track_score(plugin, track, base_time)) for track in tracks]

    # Convert scores to probabilities using softmax
    scores = np.array([score for _, score in track_scores])
    probabilities = np.exp(scores / 10) / sum(np.exp(scores / 10))  # Temperature=10 to control randomness

    # Select tracks based on probabilities
    selected_indices = np.random.choice(
        len(tracks),
        size=min(num_tracks, len(tracks)),
        replace=False,
        p=probabilities
    )

    selected_tracks = [tracks[i] for i in selected_indices]

    # Log selection details for debugging
    for i, track in enumerate(selected_tracks):
        score = track_scores[selected_indices[i]][1]
        plugin._log.debug(
            "Selected: {} - {} (Score: {:.2f}, Rating: {}, Plays: {})",
            track.album,
            track.title,
            score,
            getattr(track, 'plex_userrating', 0),
            getattr(track, 'plex_viewcount', 0)
        )

    return selected_tracks


def calculate_playlist_proportions(plugin, max_tracks, discovery_ratio):
    """Calculate number of rated vs unrated tracks based on discovery ratio.

    Args:
        max_tracks: Total number of tracks desired
        discovery_ratio: Percentage of unrated/discovery tracks desired (0-100)

    Returns:
        tuple: (unrated_tracks_count, rated_tracks_count)
    """
    unrated_tracks_count = min(int(max_tracks * (discovery_ratio / 100)), max_tracks)
    rated_tracks_count = max_tracks - unrated_tracks_count
    return unrated_tracks_count, rated_tracks_count


def validate_filter_config(plugin, filter_config):
    """Validate the filter configuration structure and values.

    Args:
        filter_config: Dictionary containing filter configuration

    Returns:
        tuple: (is_valid: bool, error_message: str)
    """
    if not isinstance(filter_config, dict):
        return False, "Filter configuration must be a dictionary"

    # Check exclude/include sections if they exist
    for section in ['exclude', 'include']:
        if section in filter_config:
            if not isinstance(filter_config[section], dict):
                return False, f"{section} section must be a dictionary"

            section_config = filter_config[section]

            # Validate genres if present
            if 'genres' in section_config:
                if not isinstance(section_config['genres'], list):
                    return False, f"{section}.genres must be a list"

            # Validate years if present
            if 'years' in section_config:
                years = section_config['years']
                if not isinstance(years, dict):
                    return False, f"{section}.years must be a dictionary"

                # Check year values
                if 'before' in years and not isinstance(years['before'], int):
                    return False, f"{section}.years.before must be an integer"
                if 'after' in years and not isinstance(years['after'], int):
                    return False, f"{section}.years.after must be an integer"
                if 'between' in years:
                    if not isinstance(years['between'], list) or len(years['between']) != 2:
                        return False, f"{section}.years.between must be a list of two integers"
                    if not all(isinstance(y, int) for y in years['between']):
                        return False, f"{section}.years.between values must be integers"

    # Validate min_rating if present
    if 'min_rating' in filter_config:
        if not isinstance(filter_config['min_rating'], (int, float)):
            return False, "min_rating must be a number"
        if not 0 <= filter_config['min_rating'] <= 10:
            return False, "min_rating must be between 0 and 10"

    return True, ""


def _apply_exclusion_filters(plugin, tracks, exclude_config):
    """Apply exclusion filters to tracks."""
    filtered_tracks = tracks[:]
    original_count = len(filtered_tracks)

    # Filter by genres
    if 'genres' in exclude_config:
        exclude_genres = [g.lower() for g in exclude_config['genres']]
        filtered_tracks = [
            track for track in filtered_tracks
            if hasattr(track, 'genres') and not any(
                g.tag.lower() in exclude_genres
                for g in track.genres
            )
        ]
        plugin._log.debug(
            "Genre exclusion filter: {} -> {} tracks",
            exclude_genres,
            len(filtered_tracks)
        )

    # Filter by years
    if 'years' in exclude_config:
        years_config = exclude_config['years']

        if 'before' in years_config:
            year_before = years_config['before']
            filtered_tracks = [
                track for track in filtered_tracks
                if not hasattr(track, 'year') or
                track.year is None or
                track.year >= year_before
            ]
            plugin._log.debug(
                "Year before {} filter: {} tracks",
                year_before,
                len(filtered_tracks)
            )

        if 'after' in years_config:
            year_after = years_config['after']
            filtered_tracks = [
                track for track in filtered_tracks
                if not hasattr(track, 'year') or
                track.year is None or
                track.year <= year_after
            ]
            plugin._log.debug(
                "Year after {} filter: {} tracks",
                year_after,
                len(filtered_tracks)
            )

    plugin._log.debug(
        "Exclusion filters removed {} tracks",
        original_count - len(filtered_tracks)
    )
    return filtered_tracks


def _apply_inclusion_filters(plugin, tracks, include_config):
    """Apply inclusion filters to tracks."""
    filtered_tracks = tracks[:]
    original_count = len(filtered_tracks)

    # Filter by genres
    if 'genres' in include_config:
        include_genres = [g.lower() for g in include_config['genres']]
        filtered_tracks = [
            track for track in filtered_tracks
            if hasattr(track, 'genres') and any(
                g.tag.lower() in include_genres
                for g in track.genres
            )
        ]
        plugin._log.debug(
            "Genre inclusion filter: {} -> {} tracks",
            include_genres,
            len(filtered_tracks)
        )

    # Filter by years
    if 'years' in include_config:
        years_config = include_config['years']

        if 'between' in years_config:
            start_year, end_year = years_config['between']
            filtered_tracks = [
                track for track in filtered_tracks
                if hasattr(track, 'year') and
                track.year is not None and
                start_year <= track.year <= end_year
            ]
            plugin._log.debug(
                "Year between {}-{} filter: {} tracks",
                start_year,
                end_year,
                len(filtered_tracks)
            )

    plugin._log.debug(
        "Inclusion filters removed {} tracks",
        original_count - len(filtered_tracks)
    )
    return filtered_tracks


def apply_playlist_filters(plugin, tracks, filter_config):
    """Apply configured filters to a list of tracks.

    Args:
        tracks: List of tracks to filter
        filter_config: Dictionary containing filter configuration

    Returns:
        list: Filtered track list
    """
    if not tracks:
        return tracks

    # Validate filter configuration
    is_valid, error = validate_filter_config(plugin, filter_config)
    if not is_valid:
        plugin._log.error("Invalid filter configuration: {}", error)
        return tracks

    plugin._log.debug("Applying filters to {} tracks", len(tracks))
    filtered_tracks = tracks[:]

    # Apply exclusion filters first
    if 'exclude' in filter_config:
        plugin._log.debug("Applying exclusion filters...")
        filtered_tracks = _apply_exclusion_filters(plugin, filtered_tracks, filter_config['exclude'])

    # Then apply inclusion filters
    if 'include' in filter_config:
        plugin._log.debug("Applying inclusion filters...")
        filtered_tracks = _apply_inclusion_filters(plugin, filtered_tracks, filter_config['include'])

    # Apply rating filter if specified, but preserve unrated tracks
    if 'min_rating' in filter_config:
        min_rating = filter_config['min_rating']
        original_count = len(filtered_tracks)

        # Separate unrated and rated tracks
        unrated_tracks = [
            track for track in filtered_tracks
            if not hasattr(track, 'userRating') or
            track.userRating is None or
            float(track.userRating or 0) == 0
        ]

        rated_tracks = [
            track for track in filtered_tracks
            if hasattr(track, 'userRating') and
            track.userRating is not None and
            float(track.userRating or 0) >= min_rating
        ]

        filtered_tracks = rated_tracks + unrated_tracks

        plugin._log.debug(
            "Rating filter (>= {}): {} -> {} tracks ({} rated, {} unrated)",
            min_rating,
            original_count,
            len(filtered_tracks),
            len(rated_tracks),
            len(unrated_tracks)
        )

    plugin._log.debug(
        "Filter application complete: {} -> {} tracks",
        len(tracks),
        len(filtered_tracks)
    )
    return filtered_tracks


def get_filtered_library_tracks(plugin, preferred_genres, config_filters, exclusion_days=30):
    """Get filtered library tracks using Plex's advanced filters in a single query."""
    try:
        # Build advanced filters structure
        advanced_filters = {'and': []}

        # Handle genre filters
        include_genres = []
        exclude_genres = []

        # Add genres from preferred_genres if no specific inclusion filters
        if preferred_genres:
            include_genres.extend(preferred_genres)

        # Add configured genres
        if config_filters:
            if 'include' in config_filters and 'genres' in config_filters['include']:
                include_genres.extend(g.lower() for g in config_filters['include']['genres'])
            if 'exclude' in config_filters and 'genres' in config_filters['exclude']:
                exclude_genres.extend(g.lower() for g in config_filters['exclude']['genres'])

        # Add genre conditions - using OR for inclusions
        if include_genres:
            include_genres = list(set(include_genres))  # Remove duplicates
            advanced_filters['and'].append({
                'or': [{'genre': genre} for genre in include_genres]
            })

        # Use AND for exclusions with genre! operator
        if exclude_genres:
            exclude_genres = list(set(exclude_genres))  # Remove duplicates
            advanced_filters['and'].append({'genre!': exclude_genres})

        # Handle year filters
        if config_filters:
            if 'include' in config_filters and 'years' in config_filters['include']:
                years_config = config_filters['include']['years']
                if 'between' in years_config:
                    start_year, end_year = years_config['between']
                    advanced_filters['and'].append({
                        'and': [
                            {'year>>': start_year},
                            {'year<<': end_year}
                        ]
                    })

            if 'exclude' in config_filters and 'years' in config_filters['exclude']:
                years_config = config_filters['exclude']['years']
                if 'before' in years_config:
                    advanced_filters['and'].append({'year>>': years_config['before']})
                if 'after' in years_config:
                    advanced_filters['and'].append({'year<<': years_config['after']})

        # Handle rating filter
        if config_filters and 'min_rating' in config_filters:
            advanced_filters['and'].append({
                'or': [
                    {'userRating': 0},  # Unrated
                    {'userRating>>': config_filters['min_rating']}  # Above minimum
                ]
            })

        # Handle recent plays exclusion
        if exclusion_days > 0:
            advanced_filters['and'].append({'lastViewedAt<<': f"-{exclusion_days}d"})

        plugin._log.debug("Using advanced filters: {}", advanced_filters)

        # Use searchTracks with advanced filters
        tracks = plugin.music.searchTracks(filters=advanced_filters)

        plugin._log.debug(
            "Found {} tracks matching all criteria in a single query",
            len(tracks)
        )

        return tracks

    except Exception as e:
        plugin._log.error("Error searching with advanced filters: {}. Filter: {}", e, advanced_filters)
        return []


def generate_daily_discovery(plugin, lib, dd_config, plex_lookup, preferred_genres, similar_tracks):
    """Generate Daily Discovery playlist with improved track selection."""
    from beetsplug.playlist_handlers import plex_clear_playlist, plex_add_playlist_item

    playlist_name = dd_config.get("name", "Daily Discovery")
    plugin._log.info("Generating {} playlist", playlist_name)

    # Get base configuration
    if "playlists" in plugin.config["plexsync"] and "defaults" in plugin.config["plexsync"]["playlists"]:
        defaults_cfg = plugin.config["plexsync"]["playlists"]["defaults"].get({})
    else:
        defaults_cfg = {}

    max_tracks = get_config_value(plugin, dd_config, defaults_cfg, "max_tracks", 20)
    discovery_ratio = get_config_value(plugin, dd_config, defaults_cfg, "discovery_ratio", 30)

    # Use lookup dictionary to convert similar tracks to beets items first
    matched_tracks = []
    for plex_track in similar_tracks:
        try:
            beets_item = plex_lookup.get(plex_track.ratingKey)
            if beets_item:
                matched_tracks.append(plex_track)  # Keep Plex track object for filtering
        except Exception as e:
            plugin._log.debug("Error processing track {}: {}", plex_track.title, e)
            continue

    plugin._log.debug("Found {} initial tracks", len(matched_tracks))

    # Get filters from config
    filters = dd_config.get("filters", {})

    # Apply filters to matched tracks
    if filters:
        plugin._log.debug("Applying filters to {} tracks...", len(matched_tracks))
        filtered_tracks = apply_playlist_filters(plugin, matched_tracks, filters)
        plugin._log.debug("After filtering: {} tracks", len(filtered_tracks))
    else:
        filtered_tracks = matched_tracks

    plugin._log.debug("Processing {} filtered tracks", len(filtered_tracks))

    # Now convert filtered Plex tracks to beets items for final processing
    final_tracks = []
    for track in filtered_tracks:
        try:
            beets_item = plex_lookup.get(track.ratingKey)
            if beets_item:
                final_tracks.append(beets_item)
        except Exception as e:
            plugin._log.debug("Error converting track {}: {}", track.title, e)

    plugin._log.debug("Found {} tracks matching all criteria", len(final_tracks))

    # Split tracks into rated and unrated
    rated_tracks = []
    unrated_tracks = []
    for track in final_tracks:
        rating = float(getattr(track, 'plex_userrating', 0))
        if rating > 0:  # Include all rated tracks
            rated_tracks.append(track)
        else:  # Only truly unrated tracks
            unrated_tracks.append(track)

    plugin._log.debug("Split into {} rated and {} unrated tracks",
                   len(rated_tracks), len(unrated_tracks))

    # Calculate proportions
    unrated_tracks_count, rated_tracks_count = calculate_playlist_proportions(
        plugin, max_tracks, discovery_ratio
    )

    # Select tracks using weighted probability
    selected_rated = select_tracks_weighted(plugin, rated_tracks, rated_tracks_count)
    selected_unrated = select_tracks_weighted(plugin, unrated_tracks, unrated_tracks_count)

    # If we don't have enough unrated tracks, fill with rated ones
    if len(selected_unrated) < unrated_tracks_count:
        additional_count = min(
            unrated_tracks_count - len(selected_unrated),
            max_tracks - len(selected_rated) - len(selected_unrated)
        )
        remaining_rated = [t for t in rated_tracks if t not in selected_rated]
        additional_rated = select_tracks_weighted(plugin, remaining_rated, additional_count)
        selected_rated.extend(additional_rated)

    # Combine and shuffle
    selected_tracks = selected_rated + selected_unrated
    if len(selected_tracks) > max_tracks:
        selected_tracks = selected_tracks[:max_tracks]

    random.shuffle(selected_tracks)

    plugin._log.info(
        "Selected {} rated tracks and {} unrated tracks",
        len(selected_rated),
        len(selected_unrated)
    )

    if not selected_tracks:
        plugin._log.warning("No tracks matched criteria for Daily Discovery playlist")
        return

    # Create/update playlist
    try:
        plex_clear_playlist(plugin, playlist_name)
        plugin._log.info("Cleared existing Daily Discovery playlist")
    except Exception:
        plugin._log.debug("No existing Daily Discovery playlist found")

    plex_add_playlist_item(plugin, selected_tracks, playlist_name)

    plugin._log.info(
        "Successfully updated {} playlist with {} tracks",
        playlist_name,
        len(selected_tracks)
    )


def generate_forgotten_gems(plugin, lib, ug_config, plex_lookup, preferred_genres, similar_tracks):
    """Generate a Forgotten Gems playlist with improved discovery."""
    from beetsplug.playlist_handlers import plex_clear_playlist, plex_add_playlist_item

    playlist_name = ug_config.get("name", "Forgotten Gems")
    plugin._log.info("Generating {} playlist", playlist_name)

    # Get configuration
    if "playlists" in plugin.config["plexsync"] and "defaults" in plugin.config["plexsync"]["playlists"]:
        defaults_cfg = plugin.config["plexsync"]["playlists"]["defaults"].get({})
    else:
        defaults_cfg = {}

    max_tracks = get_config_value(plugin, ug_config, defaults_cfg, "max_tracks", 20)
    discovery_ratio = get_config_value(plugin, ug_config, defaults_cfg, "discovery_ratio", 30)
    exclusion_days = get_config_value(plugin, ug_config, defaults_cfg, "exclusion_days", 30)

    # Get filters from config
    filters = ug_config.get("filters", {})

    # If no genres configured in filters, use preferred_genres
    if not filters:
        filters = {'include': {'genres': preferred_genres}}
    elif 'include' not in filters or 'genres' not in filters['include']:
        if 'include' not in filters:
            filters['include'] = {}
        filters['include']['genres'] = preferred_genres
        plugin._log.debug("Using preferred genres as no genres configured: {}", preferred_genres)

    # Get initial track pool using configured or preferred genres and filters
    plugin._log.info("Searching library with filters...")
    all_library_tracks = get_filtered_library_tracks(
        plugin, [], # No need to pass preferred_genres since they're now in filters if needed
        filters,
        exclusion_days
    )

    # Add similar tracks if they match the filter criteria
    if len(similar_tracks) > 0:
        filtered_similar = apply_playlist_filters(plugin, similar_tracks, filters)

        # Add filtered similar tracks if not already included
        seen_keys = set(track.ratingKey for track in all_library_tracks)
        for track in filtered_similar:
            if track.ratingKey not in seen_keys:
                all_library_tracks.append(track)
                seen_keys.add(track.ratingKey)

        plugin._log.debug(
            "Combined {} library tracks with {} filtered similar tracks",
            len(all_library_tracks), len(filtered_similar)
        )

    # Convert to beets items
    final_tracks = []
    for track in all_library_tracks:
        try:
            beets_item = plex_lookup.get(track.ratingKey)
            if beets_item:
                final_tracks.append(beets_item)
        except Exception as e:
            plugin._log.debug("Error converting track {}: {}", track.title, e)

    plugin._log.debug("Converted {} tracks to beets items", len(final_tracks))

    # Split tracks into rated and unrated
    rated_tracks = []
    unrated_tracks = []
    for track in final_tracks:
        rating = float(getattr(track, 'plex_userrating', 0))
        if rating > 0:  # Include all rated tracks
            rated_tracks.append(track)
        else:  # Only truly unrated tracks
            unrated_tracks.append(track)

    plugin._log.debug("Split into {} rated and {} unrated tracks", len(rated_tracks), len(unrated_tracks))

    # Calculate proportions
    unrated_tracks_count, rated_tracks_count = calculate_playlist_proportions(plugin, max_tracks, discovery_ratio)

    # Select tracks using weighted probability
    selected_rated = select_tracks_weighted(plugin, rated_tracks, rated_tracks_count)
    selected_unrated = select_tracks_weighted(plugin, unrated_tracks, unrated_tracks_count)

    # If we don't have enough unrated tracks, fill with rated ones
    if len(selected_unrated) < unrated_tracks_count:
        additional_count = min(
            unrated_tracks_count - len(selected_unrated),
            max_tracks - len(selected_rated) - len(selected_unrated)
        )
        remaining_rated = [t for t in rated_tracks if t not in selected_rated]
        additional_rated = select_tracks_weighted(plugin, remaining_rated, additional_count)
        selected_rated.extend(additional_rated)

    # Combine and shuffle
    selected_tracks = selected_rated + selected_unrated
    if len(selected_tracks) > max_tracks:
        selected_tracks = selected_tracks[:max_tracks]

    random.shuffle(selected_tracks)

    plugin._log.info("Selected {} rated tracks and {} unrated tracks", len(selected_rated), len(selected_unrated))

    if not selected_tracks:
        plugin._log.warning("No tracks matched criteria for Forgotten Gems playlist")
        return

    # Create/update playlist
    try:
        plex_clear_playlist(plugin, playlist_name)
        plugin._log.info("Cleared existing Forgotten Gems playlist")
    except Exception:
        plugin._log.debug("No existing Forgotten Gems playlist found")

    plex_add_playlist_item(plugin, selected_tracks, playlist_name)

    plugin._log.info("Successfully updated {} playlist with {} tracks", playlist_name, len(selected_tracks))


def generate_imported_playlist(plugin, lib, playlist_config, plex_lookup=None):
    """Generate a playlist by importing from external sources."""
    from beetsplug.playlist_handlers import plex_clear_playlist, plex_add_playlist_item

    playlist_name = playlist_config.get("name", "Imported Playlist")
    sources = playlist_config.get("sources", [])
    max_tracks = playlist_config.get("max_tracks", None)

    # Create log file path in beets config directory
    log_file = os.path.join(plugin.config_dir, f"{playlist_name.lower().replace(' ', '_')}_import.log")

    # Clear/create the log file
    with open(log_file, 'w', encoding='utf-8') as f:
        f.write(f"Import log for playlist: {playlist_name}\n")
        f.write(f"Import started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("-" * 80 + "\n\n")

    # Get config options with defaults
    if (
        "playlists" in plugin.config["plexsync"]
        and "defaults" in plugin.config["plexsync"]["playlists"]
    ):
        defaults_cfg = plugin.config["plexsync"]["playlists"]["defaults"].get({})
    else:
        defaults_cfg = {}

    manual_search = get_config_value(
        plugin,
        playlist_config,
        defaults_cfg,
        "manual_search",
        False  # Default value directly provided
    )
    clear_playlist = get_config_value(
        plugin, playlist_config, defaults_cfg, "clear_playlist", False
    )

    if not sources:
        plugin._log.warning("No sources defined for imported playlist {}", playlist_name)
        return

    plugin._log.info("Generating imported playlist {} from {} sources", playlist_name, len(sources))

    # Import tracks from all sources
    all_tracks = []
    not_found_count = 0

    for source in sources:
        try:
            plugin._log.info("Importing from source: {}", source)
            if isinstance(source, str):  # Handle string sources (URLs and file paths)
                if source.lower().endswith('.m3u8'):
                    # Check if path is absolute, if not make it relative to config dir
                    if not os.path.isabs(source):
                        source = os.path.join(plugin.config_dir, source)
                    tracks = plugin.import_m3u8_playlist(source)
                elif "spotify" in source:
                    tracks = plugin.import_spotify_playlist(plugin.get_playlist_id(source))
                elif "jiosaavn" in source:
                    tracks = plugin.import_jiosaavn_playlist(source)
                elif "apple" in source:
                    tracks = plugin.import_apple_playlist(source)
                elif "gaana" in source:
                    tracks = plugin.import_gaana_playlist(source)
                elif "youtube" in source:
                    tracks = plugin.import_yt_playlist(source)
                elif "tidal" in source:
                    tracks = plugin.import_tidal_playlist(source)
                else:
                    plugin._log.warning("Unsupported source: {}", source)
                    continue
            elif isinstance(source, dict) and source.get("type") == "post":
                tracks = plugin.import_post_playlist(source)
            else:
                plugin._log.warning("Invalid source format: {}", source)
                continue

            if tracks:
                all_tracks.extend(tracks)

        except Exception as e:
            plugin._log.error("Error importing from {}: {}", source, e)
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(f"Error importing from source {source}: {str(e)}\n")
            continue

    if not all_tracks:
        plugin._log.warning("No tracks found from any source for playlist {}", playlist_name)
        return

    # Process tracks through Plex first
    matched_songs = []
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write("\nTracks not found in Plex library:\n")
        f.write("-" * 80 + "\n")

    for track in all_tracks:
        try:
            found = plugin.search_plex_song(track, manual_search)
            if found:
                # Just use Plex rating directly
                plex_rating = float(getattr(found, "userRating", 0) or 0)

                if plex_rating == 0 or plex_rating > 2:  # Include unrated or rating > 2
                    matched_songs.append(found)
                    plugin._log.debug(
                        "Matched in Plex: {} - {} - {} (Rating: {})",
                        getattr(found, 'artist', lambda: {'tag': 'Unknown'})().tag,
                        getattr(found, 'parentTitle', 'Unknown'),
                        getattr(found, 'title', 'Unknown'),
                        plex_rating
                    )
                else:
                    with open(log_file, 'a', encoding='utf-8') as f:
                        f.write(f"Low rated ({plex_rating}): {track.get('artist', 'Unknown')} - {track.get('album', 'Unknown')} - {track.get('title', 'Unknown')}\n")
            else:
                not_found_count += 1
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(f"Not found: {track.get('artist', 'Unknown')} - {track.get('album', 'Unknown')} - {track.get('title', 'Unknown')}\n")
        except Exception as e:
            plugin._log.error("Error processing track {}: {}", track.get('title', 'Unknown'), e)
            not_found_count += 1
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(f"Error: {track.get('artist', 'Unknown')} - {track.get('album', 'Unknown')} - {track.get('title', 'Unknown')} ({str(e)})\n")

    # Get filters from config and apply them
    filters = playlist_config.get("filters", {})
    if filters:
        plugin._log.debug("Applying filters to {} matched tracks...", len(matched_songs))

        # Convert Plex tracks to beets items first
        beets_items = []

        # Use provided lookup dictionary or build new one if not provided
        if plex_lookup is None:
            plugin._log.debug("Building Plex lookup dictionary...")
            plex_lookup = plugin.build_plex_lookup(lib)

        for track in matched_songs:
            try:
                beets_item = plex_lookup.get(track.ratingKey)
                if beets_item:
                    beets_items.append(beets_item)
            except Exception as e:
                plugin._log.debug("Error finding beets item for {}: {}", track.title, e)
                continue

        # Now apply filters to beets items
        filtered_items = []
        for item in beets_items:
            include_item = True

            if 'exclude' in filters:
                if 'years' in filters['exclude']:
                    years_config = filters['exclude']['years']
                    if 'after' in years_config and item.year:
                        if item.year > years_config['after']:
                            include_item = False
                            plugin._log.debug("Excluding {} (year {} > {})",
                                item.title, item.year, years_config['after'])
                    if 'before' in years_config and item.year:
                        if item.year < years_config['before']:
                            include_item = False
                            plugin._log.debug("Excluding {} (year {} < {})",
                                item.title, item.year, years_config['before'])

            if include_item:
                filtered_items.append(item)

        plugin._log.debug("After filtering: {} tracks remain", len(filtered_items))
        matched_songs = filtered_items

    # Deduplicate based on ratingKey for Plex Track objects and plex_ratingkey for beets items
    seen = set()
    unique_matched = []
    for song in matched_songs:
        # Try both ratingKey (Plex Track) and plex_ratingkey (beets Item)
        rating_key = (
            getattr(song, 'ratingKey', None)  # For Plex Track objects
            or getattr(song, 'plex_ratingkey', None)  # For beets Items
        )
        if rating_key and rating_key not in seen:
            seen.add(rating_key)
            unique_matched.append(song)
    # Apply track limit if specified
    if max_tracks:
        unique_matched = unique_matched[:max_tracks]

    # Write summary at the end of log file
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write(f"\nImport Summary:\n")
        f.write("-" * 80 + "\n")
        f.write(f"Total tracks from sources: {len(all_tracks)}\n")
        f.write(f"Tracks not found in Plex: {not_found_count}\n")
        f.write(f"Tracks matched and added: {len(matched_songs)}\n")
        f.write(f"\nImport completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    plugin._log.info(
        "Found {} unique tracks after filtering (see {} for details)",
        len(unique_matched), log_file
    )

    # Create or update playlist based on clear_playlist setting
    if clear_playlist:
        try:
            plex_clear_playlist(plugin, playlist_name)
            plugin._log.info("Cleared existing playlist {}", playlist_name)
        except Exception:
            plugin._log.debug("No existing playlist {} found", playlist_name)

    if unique_matched:
        plex_add_playlist_item(plugin, unique_matched, playlist_name)
        plugin._log.info(
            "Successfully created playlist {} with {} tracks",
            playlist_name,
            len(unique_matched)
        )
    else:
        plugin._log.warning("No tracks remaining after filtering for {}", playlist_name)


def plex_smartplaylists(plugin, lib, playlists_config):
    """Process all playlists at once with a single lookup dictionary."""
    # Build lookup once for all playlists
    plugin._log.info("Building Plex lookup dictionary...")
    # Use the function from core.py
    plex_lookup = build_plex_lookup(plugin, lib)
    plugin._log.debug("Found {} tracks in lookup dictionary", len(plex_lookup))

    # Get preferred attributes once if needed for smart playlists
    preferred_genres = None
    similar_tracks = None
    if any(p.get("id") in ["daily_discovery", "forgotten_gems"] for p in playlists_config):
        preferred_genres, similar_tracks = get_preferred_attributes(plugin)
        plugin._log.debug("Using preferred genres: {}", preferred_genres)
        plugin._log.debug("Processing {} pre-filtered similar tracks", len(similar_tracks))

    # Process each playlist
    for p in playlists_config:
        playlist_type = p.get("type", "smart")
        playlist_id = p.get("id")
        playlist_name = p.get("name", "Unnamed playlist")

        if (playlist_type == "imported"):
            generate_imported_playlist(plugin, lib, p, plex_lookup)  # Pass plex_lookup
        elif playlist_id in ["daily_discovery", "forgotten_gems"]:
            if playlist_id == "daily_discovery":
                generate_daily_discovery(plugin, lib, p, plex_lookup, preferred_genres, similar_tracks)
            else:  # forgotten_gems
                generate_forgotten_gems(plugin, lib, p, plex_lookup, preferred_genres, similar_tracks)
        else:
            plugin._log.warning(
                "Unrecognized playlist configuration '{}' - type: '{}', id: '{}'. "
                "Valid types are 'imported' or 'smart'. "
                "Valid smart playlist IDs are 'daily_discovery' and 'forgotten_gems'.",
                playlist_name, playlist_type, playlist_id
            )
