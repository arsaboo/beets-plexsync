"""Smart playlist generation and helpers extracted from plexsync.

These functions use the plugin instance (`ps`) to access logging, config,
and Plex/beets objects. Behavior preserved.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Tuple
import time
import os
import copy
import copy

import json

from beets import config
from beetsplug.core.config import get_config_value, get_plexsync_config
from beetsplug.core.vector_index import BeetsVectorIndex
from beetsplug.providers.gaana import import_gaana_playlist
from beetsplug.providers.tidal import import_tidal_playlist
from beetsplug.providers.youtube import import_yt_playlist
from beetsplug.providers.m3u8 import import_m3u8_playlist
from beetsplug.providers.http_post import import_post_playlist

# Module-level random number generator to avoid global seeding
import numpy as np
_module_rng = np.random.default_rng()




def _resolve_min_year(ps, playlist_config, default_max_age_years, playlist_label):
    now_year = datetime.now().year
    min_year = None

    explicit_year = playlist_config.get("min_year") or playlist_config.get("min_release_year")
    if explicit_year is not None:
        try:
            min_year = int(explicit_year)
        except (TypeError, ValueError):
            ps._log.debug("Ignoring invalid min_year '{}' for {} playlist", explicit_year, playlist_label)

    if min_year is None:
        max_age_config = playlist_config.get("max_age_years")
        if max_age_config is not None:
            try:
                max_age_years = int(max_age_config)
                if max_age_years >= 0:
                    min_year = now_year - max_age_years
            except (TypeError, ValueError):
                ps._log.debug(
                    "Ignoring invalid max_age_years '{}' for {} playlist",
                    max_age_config,
                    playlist_label,
                )

    if min_year is None:
        min_year = now_year - default_max_age_years

    min_year = max(min_year, now_year - 100)
    return min_year


def _ensure_min_year_filter(filter_config, min_year):
    if min_year is None:
        return filter_config, False

    filters = copy.deepcopy(filter_config) if filter_config else {}

    include = filters.get("include") if isinstance(filters.get("include"), dict) else {}
    years = include.get("years") if isinstance(include.get("years"), dict) else {}

    has_year_constraint = False
    if years:
        has_year_constraint = any(key in years for key in ("after", "before", "between"))

    exclude = filters.get("exclude") if isinstance(filters.get("exclude"), dict) else {}
    exc_years = exclude.get("years") if isinstance(exclude.get("years"), dict) else {}
    if exc_years:
        has_year_constraint = True

    if has_year_constraint:
        if include:
            filters["include"] = include
        return filters, False

    years = dict(years) if years else {}
    years["after"] = min_year
    include = dict(include) if include else {}
    include["years"] = years
    filters["include"] = include
    return filters, True


def _apply_recency_guard(ps, playlist_config, filters, playlist_label, default_max_age_years):
    min_year = _resolve_min_year(ps, playlist_config, default_max_age_years, playlist_label)
    adjusted_filters, injected = _ensure_min_year_filter(filters, min_year)
    if injected:
        ps._log.debug(
            "Applying default min release year {} to {} playlist filters",
            min_year,
            playlist_label,
        )
    return min_year, adjusted_filters


def _filter_tracks_by_min_year(ps, tracks, min_year, playlist_label):
    if min_year is None or not tracks:
        return tracks

    filtered = []
    dropped = 0
    for track in tracks:
        year = getattr(track, "year", None)
        try:
            if year is None:
                raise ValueError
            year_int = int(year)
        except (TypeError, ValueError):
            dropped += 1
            continue

        if year_int >= min_year:
            filtered.append(track)
        else:
            dropped += 1

    if dropped and filtered:
        ps._log.debug(
            "Removed {} tracks older than {} for {} playlist",
            dropped,
            min_year,
            playlist_label,
        )
        return filtered

    if dropped and not filtered:
        ps._log.debug(
            "Min year {} removed all tracks for {} playlist; retaining original pool",
            min_year,
            playlist_label,
        )

    return tracks



def get_preferred_attributes(ps) -> Tuple[list, list]:
    # Defaults from config
    defaults_cfg = get_plexsync_config(["playlists", "defaults"], dict, {})

    history_days = get_config_value(config["plexsync"], defaults_cfg, "history_days", 15)
    exclusion_days = get_config_value(config["plexsync"], defaults_cfg, "exclusion_days", 30)

    # Fetch tracks for the longer period (exclusion_days)
    all_tracks = ps.music.search(filters={"track.lastViewedAt>>": f"{exclusion_days}d"}, libtype="track")

    now = datetime.now()
    history_cutoff = now - timedelta(days=history_days)

    history_tracks = [
        track for track in all_tracks if track.lastViewedAt and track.lastViewedAt > history_cutoff
    ]

    recently_played = {track.ratingKey for track in all_tracks}

    genre_counts = {}
    similar_tracks = set()

    def get_sonic_matches(track):
        track_genres = set()
        for genre in track.genres:
            if genre:
                genre_str = str(genre.tag).lower()
                track_genres.add(genre_str)

        local_similar_tracks = set()
        try:
            sonic_matches = track.sonicallySimilar()
            for match in sonic_matches:
                rating = getattr(match, "userRating", -1)
                if (
                    match.ratingKey not in recently_played
                    and any(g.tag.lower() in track_genres for g in match.genres)
                    and (rating is None or rating == -1 or rating >= 4)
                ):
                    local_similar_tracks.add(match)
        except Exception as e:
            ps._log.debug("Error getting similar tracks for {}: {}", track.title, e)
        return local_similar_tracks, track_genres

    with ThreadPoolExecutor() as executor:
        results = executor.map(get_sonic_matches, history_tracks)

    for local_similar_tracks, track_genres in results:
        similar_tracks.update(local_similar_tracks)
        for genre in track_genres:
            genre_counts[genre] = genre_counts.get(genre, 0) + 1

    sorted_genres = sorted(genre_counts, key=genre_counts.get, reverse=True)[:5]
    ps._log.debug("Top genres: {}", sorted_genres)
    ps._log.debug("Found {} similar tracks after filtering", len(similar_tracks))
    return sorted_genres, list(similar_tracks)


# Define default scoring weights for each playlist type
#
# Metric explanations:
# - z_rating: Standardized rating score (user rating); positive = higher rating than average
#   Computed as: (rating - rating_mean) / rating_std
#   Effect: Positive weights favor highly-rated tracks
#
# - z_recency: Standardized recency score (time since last played); positive = longer since played
#   Computed as: (days_since_played - days_mean) / days_std
#   Effect: Positive weights favor tracks not played recently (good for "forgotten" playlists)
#           Negative weights favor recently played tracks (good for "fresh" playlists)
#
# - z_play_count: Standardized play count; positive = higher than average play count
#   Computed as: (play_count - play_count_mean) / play_count_std
#   Effect: Positive weights favor frequently played tracks (like "most played")
#           Negative weights favor rarely played tracks (like "forgotten gems")
#
# - z_popularity: Standardized popularity score; positive = more popular than average
#   Computed as: (popularity - popularity_mean) / popularity_std
#   Effect: Positive weights favor popular tracks, negative weights favor obscure tracks
#
# - z_age: Standardized age (years since release); positive = newer than average tracks
#   Computed as: -(age - age_mean) / age_std
#   Effect: Positive weights favor newer tracks (good for "recent hits", "fresh favorites")
#           Negative weights favor older tracks (good for "forgotten gems" to find old overlooked tracks)
#
DEFAULT_SCORING_WEIGHTS = {
    "forgotten_gems": {
        "rated_weights": {
            "z_rating": 0.3,
            "z_recency": 0.3,
            "z_play_count": -0.2,  # Negative weight - penalizes frequently played tracks
            "z_popularity": 0.1,
            "z_age": -0.1  # Slightly negative weight - slightly favors older tracks that may be forgotten
        },
        "unrated_weights": {
            "z_recency": 0.4,
            "z_play_count": -0.3,  # Negative weight - penalizes frequently played tracks
            "z_popularity": 0.2,
            "z_age": -0.1  # Slightly negative weight - slightly favors older tracks that may be forgotten
        }
    },
    "fresh_favorites": {
        "rated_weights": {
            "z_age": 0.4,
            "z_recency": 0.25,
            "z_rating": 0.2,
            "z_popularity": 0.1,
            "z_play_count": -0.05  # Slightly negative to avoid overplayed tracks
        },
        "unrated_weights": {
            "z_age": 0.45,
            "z_recency": 0.25,
            "z_popularity": 0.2,
            "z_play_count": -0.1
        }
    },
    "daily_discovery": {
        "rated_weights": {
            "z_rating": 0.35,
            "z_recency": 0.15,
            "z_popularity": 0.25,
            "z_age": 0.25
        },
        "unrated_weights": {
            "z_popularity": 0.4,
            "z_recency": 0.3,
            "z_age": 0.3
        }
    },
    "recent_hits": {
        "rated_weights": {
            "z_age": 0.35,
            "z_recency": 0.3,
            "z_popularity": 0.25,
            "z_rating": 0.1
        },
        "unrated_weights": {
            "z_age": 0.4,
            "z_recency": 0.35,
            "z_popularity": 0.25
        }
    },
    "70s80s_flashback": {
        "rated_weights": {
            "z_rating": 0.4,
            "z_recency": 0.3,
            "z_play_count": -0.2  # Negative weight to avoid overplayed tracks
            # Note: z_age is omitted since all tracks are from the same era (1970s-80s)
        },
        "unrated_weights": {
            "z_recency": 0.4,
            "z_popularity": 0.35,
            "z_play_count": -0.25  # Negative weight to avoid overplayed tracks
        }
    },
    "highly_rated": {
        "rated_weights": {
            "z_rating": 0.7,
            "z_recency": 0.2,
            "z_popularity": 0.1,
            "z_age": 0.1  # Add small age weight for some variety in rated tracks
        },
        "unrated_weights": {
            "z_recency": 0.4,
            "z_popularity": 0.4,
            "z_age": 0.2
        }
    },
    "most_played": {
        "rated_weights": {
            "z_play_count": 0.4,
            "z_rating": 0.3,
            "z_recency": 0.3
        },
        "unrated_weights": {
            "z_play_count": 0.5,
            "z_recency": 0.4,
            "z_popularity": 0.1
        }
    },
    # Default fallback for other playlist types
    "default": {
        "rated_weights": {
            "z_rating": 0.5,
            "z_recency": 0.1,
            "z_popularity": 0.1,
            "z_age": 0.2
        },
        "unrated_weights": {
            "z_recency": 0.2,
            "z_popularity": 0.5,
            "z_age": 0.3
        }
    }
}


def get_scoring_weights(playlist_type):
    """Get scoring weights for a specific playlist type."""
    return DEFAULT_SCORING_WEIGHTS.get(playlist_type, DEFAULT_SCORING_WEIGHTS["default"])


def _compute_context_stats(tracks, base_time):
    """Compute normalization stats once for a track pool."""
    import numpy as _np
    ratings = []
    days_since = []
    play_counts = []
    popularities = []
    ages = []

    for t in tracks:
        # Ratings
        try:
            ratings.append(float(getattr(t, 'plex_userrating', 0) or 0))
        except (TypeError, ValueError):
            ratings.append(0.0)

        # Days since played
        ts = getattr(t, 'plex_lastviewedat', None)
        if ts is None:
            # Assume not played in last year for normalization default
            days_since.append(365)
        else:
            try:
                dt = datetime.fromtimestamp(float(ts))
                days = max((base_time - dt).days, 0)
                days_since.append(min(days, 1095))
            except (ValueError, TypeError, OSError, OverflowError):
                days_since.append(365)

        # Play count
        try:
            play_counts.append(int(getattr(t, 'plex_viewcount', 0) or 0))
        except (TypeError, ValueError):
            play_counts.append(0)

        # Popularity
        try:
            popularities.append(float(getattr(t, 'spotify_track_popularity', 0) or 0))
        except (TypeError, ValueError):
            popularities.append(0.0)

        # Age
        y = getattr(t, 'year', None)
        try:
            y = int(y)
            ages.append(max(base_time.year - y, 0))
        except (TypeError, ValueError):
            ages.append(0)

    arr = lambda x: _np.array(x, dtype=float)
    r, d, pc, pop, ag = arr(ratings), arr(days_since), arr(play_counts), arr(popularities), arr(ages)

    stats = {
        'rating_mean': float(r.mean()) if r.size else 0.0,
        'rating_std': float(r.std()) or 1.0,
        'days_mean': float(d.mean()) if d.size else 365.0,
        'days_std': float(d.std()) or 1.0,
        'days_90th_percentile': float(np.percentile(d, 90)) if d.size else 365.0,  # 90th percentile for unrated track fallback
        'play_count_mean': float(pc.mean()) if pc.size else 0.0,
        'play_count_std': float(pc.std()) or 1.0,
        'popularity_mean': float(pop.mean()) if pop.size else 0.0,
        'popularity_std': float(pop.std()) or 1.0,
        'age_mean': float(ag.mean()) if ag.size else 0.0,
        'age_std': float(ag.std()) or 1.0,
    }
    return stats


def calculate_track_score(ps, track, base_time=None, tracks_context=None, playlist_type=None, tracks_context_stats=None):
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
        # Use a deterministic fallback instead of exponential randomness to avoid
        # wildly different rankings for otherwise identical unrated tracks
        # Use the 90th percentile from library distribution if available, otherwise default to 365 days
        if tracks_context_stats and 'days_90th_percentile' in tracks_context_stats:
            days_since_played = tracks_context_stats['days_90th_percentile']
        else:
            days_since_played = 365  # Default to one year ago
    else:
        try:
            days = (base_time - datetime.fromtimestamp(float(last_played))).days
            days_since_played = min(days, 1095)
        except (ValueError, TypeError, OSError, OverflowError):
            days_since_played = 365

    if tracks_context_stats is not None:
        rating_mean = tracks_context_stats['rating_mean']
        rating_std = tracks_context_stats['rating_std'] or 1
        days_mean = tracks_context_stats['days_mean']
        days_std = tracks_context_stats['days_std'] or 1
        play_count_mean = tracks_context_stats['play_count_mean']
        play_count_std = tracks_context_stats['play_count_std'] or 1
        popularity_mean = tracks_context_stats['popularity_mean']
        popularity_std = tracks_context_stats['popularity_std'] or 1
        age_mean = tracks_context_stats['age_mean']
        age_std = tracks_context_stats['age_std'] or 1
    elif tracks_context:
        all_ratings = [float(getattr(t, 'plex_userrating', 0) or 0) for t in tracks_context]
        # Safe days computation for context
        all_days = []
        for t in tracks_context:
            ts = getattr(t, 'plex_lastviewedat', None)
            if ts is None:
                all_days.append(365)
            else:
                try:
                    dt = datetime.fromtimestamp(float(ts))
                    all_days.append(min((base_time - dt).days, 1095))
                except (ValueError, TypeError, OSError, OverflowError):
                    all_days.append(365)
        all_play_counts = [int(getattr(t, 'plex_viewcount', 0) or 0) for t in tracks_context]
        all_popularity = [float(getattr(t, 'spotify_track_popularity', 0) or 0) for t in tracks_context]
        all_ages = []
        for t in tracks_context:
            y = getattr(t, 'year', None)
            try:
                y = int(y)
                all_ages.append(max(base_time.year - y, 0))
            except (TypeError, ValueError):
                all_ages.append(0)

        rating_mean, rating_std = np.mean(all_ratings), np.std(all_ratings) or 1
        days_mean, days_std = np.mean(all_days), np.std(all_days) or 1
        play_count_mean, play_count_std = np.mean(all_play_counts), np.std(all_play_counts) or 1
        popularity_mean, popularity_std = np.mean(all_popularity), np.std(all_popularity) or 1
        age_mean, age_std = np.mean(all_ages), np.std(all_ages) or 1
    else:
        rating_mean, rating_std = 5, 2.5
        days_mean, days_std = 365, 180
        play_count_mean, play_count_std = 10, 15
        popularity_mean, popularity_std = 30, 20
        age_mean, age_std = 30, 10

    z_rating = (rating - rating_mean) / rating_std if rating > 0 else -2.0
    z_recency = (days_since_played - days_mean) / days_std
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

    # Get scoring weights based on playlist type
    weights = get_scoring_weights(playlist_type)
    weight_set = weights["rated_weights"] if is_rated else weights["unrated_weights"]

    # Calculate weighted score using the appropriate weights
    weighted_score = 0
    for metric, weight in weight_set.items():
        metric_value = locals()[metric]  # Get the z-score value for this metric
        weighted_score += metric_value * weight

    final_score = stats.norm.cdf(weighted_score * 1.5) * 100
    noise = _np.random.normal(0, 0.5)
    final_score = final_score + noise
    if not is_rated and final_score < 50:
        final_score = 50 + (final_score / 2)

    return max(0, min(100, final_score))


def select_tracks_weighted(ps, tracks, num_tracks, playlist_type=None):
    import numpy as np
    global _module_rng
    if not tracks:
        return []

    # Standard weighted selection for all playlist types
    base_time = datetime.now()

    # Precompute stats once to avoid O(n^2) behavior on large pools
    context_stats = _compute_context_stats(tracks, base_time)

    # Compute scores using precomputed stats
    track_scores = [
        (track, calculate_track_score(ps, track, base_time, tracks_context_stats=context_stats, playlist_type=playlist_type))
        for track in tracks
    ]
    scores = np.array([score for _, score in track_scores])

    # Add a small amount of random noise to scores to prevent deterministic outcomes
    # This ensures even tracks with similar scores have variation in selection
    noise = _module_rng.normal(0, 1.0, size=len(scores))
    scores_with_noise = scores + noise

    # Stable softmax
    x = scores_with_noise / 10.0
    x = x - x.max()
    exp_x = np.exp(x)
    probabilities = exp_x / exp_x.sum()

    selected_indices = _module_rng.choice(
        len(tracks), size=min(num_tracks, len(tracks)), replace=False, p=probabilities
    )
    selected_tracks = [tracks[i] for i in selected_indices]

    # Avoid verbose per-track logging; summarize and sample a few examples
    try:
        sel_scores = [track_scores[i][1] for i in selected_indices]
        mean_score = float(np.mean(sel_scores)) if sel_scores else 0.0
        rating_values = []
        play_values = []
        age_values = []
        days_since_played_values = []
        for tr in selected_tracks:
            rating_val = getattr(tr, 'plex_userrating', 0)
            try:
                rating_values.append(float(rating_val if rating_val is not None else 0))
            except (TypeError, ValueError):
                rating_values.append(0.0)
            play_val = getattr(tr, 'plex_viewcount', 0)
            try:
                play_values.append(int(play_val if play_val not in (None, '') else 0))
            except (TypeError, ValueError):
                play_values.append(0)
            release_year = getattr(tr, 'year', None)
            if release_year not in (None, ''):
                try:
                    year_int = int(release_year)
                    age_values.append(max(base_time.year - year_int, 0))
                except (TypeError, ValueError):
                    pass
            last_played_ts = getattr(tr, 'plex_lastviewedat', None)
            if last_played_ts:
                try:
                    last_played_dt = datetime.fromtimestamp(float(last_played_ts))
                    delta_days = max((base_time - last_played_dt).days, 0)
                    days_since_played_values.append(delta_days)
                except (ValueError, TypeError, OSError, OverflowError):
                    pass
        avg_rating = (sum(rating_values) / len(rating_values)) if rating_values else None
        avg_plays = (sum(play_values) / len(play_values)) if play_values else None
        avg_age = (sum(age_values) / len(age_values)) if age_values else None
        avg_days_since_played = (
            sum(days_since_played_values) / len(days_since_played_values)
            if days_since_played_values else None
        )
        rating_str = f"{avg_rating:.2f}" if avg_rating is not None else 'N/A'
        plays_str = f"{avg_plays:.2f}" if avg_plays is not None else 'N/A'
        age_str = f"{avg_age:.1f}" if avg_age is not None else 'N/A'
        days_str = f"{avg_days_since_played:.1f}" if avg_days_since_played is not None else 'N/A'
        ps._log.debug(
            "Selected {} tracks (avg score {:.2f}, avg rating {}, avg plays {}, avg age {}, avg days since played {})",
            len(selected_tracks), mean_score, rating_str, plays_str, age_str, days_str,
        )
        sample_count = min(10, len(selected_tracks))
        if sample_count:
            ps._log.debug("Sample selections (up to {}):", sample_count)
            for idx in range(sample_count):
                tr = selected_tracks[idx]
                sc = sel_scores[idx]
                # Calculate age and last played info for debugging
                release_year = getattr(tr, 'year', None)
                age = "Unknown"
                if release_year:
                    try:
                        age = int(release_year)
                    except (ValueError, TypeError):
                        age = "N/A"

                last_played_timestamp = getattr(tr, 'plex_lastviewedat', None)
                last_played = "Never"
                if last_played_timestamp:
                    try:
                        last_played = datetime.fromtimestamp(last_played_timestamp).strftime('%Y-%m-%d')
                    except (ValueError, OSError):
                        last_played = "Invalid Date"

                ps._log.debug(
                    " • {} - {} (Score: {:.2f}, Rating: {}, Plays: {}, Age: {}, Last Played: {})",
                    getattr(tr, 'album', ''), getattr(tr, 'title', ''), sc,
                    getattr(tr, 'plex_userrating', 0), getattr(tr, 'plex_viewcount', 0),
                    age, last_played
                )
    except Exception:
        # Never fail selection due to logging issues
        pass

    return selected_tracks


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


def _get_with_cache(ps, cache_key, func):
    """Helper to cache results of a function call."""
    if cache_key in ps._server_query_cache:
        ps._log.debug("Using cached results for key: {}", cache_key)
        return ps._server_query_cache[cache_key]

    results = func()
    ps._server_query_cache[cache_key] = results
    return results


def _get_library_tracks(ps, preferred_genres, filters, exclusion_days):

    adv_filters = build_advanced_filters(filters, exclusion_days, preferred_genres)
    if adv_filters:
        cache_key = json.dumps(adv_filters, sort_keys=True)
        try:
            ps._log.debug("Using server-side filters: {}", adv_filters)
            _t0 = time.time()

            tracks = _get_with_cache(ps, cache_key, lambda: ps.music.searchTracks(filters=adv_filters))

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


def generate_unified_playlist(ps, lib, playlist_config, plex_lookup, preferred_genres, similar_tracks, playlist_type):
    """
    Unified function to generate different types of smart playlists.

    Args:
        ps: Plugin instance
        lib: Beets library instance
        playlist_config: Configuration for the playlist
        plex_lookup: Plex lookup dictionary
        preferred_genres: List of preferred genres
        similar_tracks: List of similar tracks to recently played
        playlist_type: Type of playlist to generate (e.g., 'daily_discovery', 'forgotten_gems')
    """
    playlist_name = playlist_config.get("name", f"{playlist_type.replace('_', ' ').title()}")
    ps._log.info("Generating {} playlist", playlist_name)

    defaults_cfg = get_plexsync_config(["playlists", "defaults"], dict, {})
    max_tracks = get_config_value(playlist_config, defaults_cfg, "max_tracks", 20)
    discovery_ratio = get_config_value(playlist_config, defaults_cfg, "discovery_ratio", 30)
    exclusion_days = get_config_value(playlist_config, defaults_cfg, "exclusion_days", 30)
    filters = playlist_config.get("filters", {})

    # Special handling for certain playlist types
    special_handling = playlist_type in ["70s80s_flashback", "highly_rated", "most_played"]

    if special_handling:
        # For special playlist types that work with beets items instead of Plex tracks
        all_beets_items = []
        for item in lib.items():
            if hasattr(item, "plex_ratingkey") and item.plex_ratingkey:
                all_beets_items.append(item)

        ps._log.debug("Found {} tracks with Plex sync data", len(all_beets_items))

        # Apply filters to beets items
        filtered_items = []
        for item in all_beets_items:
            include_item = True

            # Apply year filters if they exist in config
            if filters.get('include', {}).get('years'):
                years_config = filters['include']['years']
                item_year = getattr(item, 'year', None)
                if 'after' in years_config and item_year and item_year <= years_config['after']:
                    include_item = False
                if 'before' in years_config and item_year and item_year >= years_config['before']:
                    include_item = False
                if 'between' in years_config and item_year:
                    start_year, end_year = years_config['between']
                    if not (start_year <= item_year <= end_year):
                        include_item = False

            # Apply genre filters if they exist
            if include_item and filters.get('include', {}).get('genres'):
                item_genres = set()
                if item.genre:
                    if isinstance(item.genre, str):
                        item_genres = set(g.lower().strip() for g in item.genre.split(','))
                    else:
                        item_genres = set(str(g).lower().strip() for g in item.genre)
                include_genres = set(g.lower().strip() for g in filters['include']['genres'])
                if not (item_genres & include_genres):
                    include_item = False

            # Apply exclude filters
            if include_item and filters.get('exclude', {}).get('genres'):
                item_genres = set()
                if item.genre:
                    if isinstance(item.genre, str):
                        item_genres = set(g.lower().strip() for g in item.genre.split(','))
                    else:
                        item_genres = set(str(g).lower().strip() for g in item.genre)
                exclude_genres = set(g.lower().strip() for g in filters['exclude']['genres'])
                if item_genres & exclude_genres:
                    include_item = False

            # Apply exclude years
            if include_item and filters.get('exclude', {}).get('years'):
                years_config = filters['exclude']['years']
                item_year = getattr(item, 'year', None)
                if 'before' in years_config and item_year and item_year < years_config['before']:
                    include_item = False
                if 'after' in years_config and item_year and item_year > years_config['after']:
                    include_item = False

            # Apply min rating filter
            if include_item and 'min_rating' in filters:
                rating = getattr(item, 'rating', 0) or getattr(item, 'plex_userrating', 0) or 0
                # Ensure both values are numeric for comparison
                try:
                    rating = float(rating) if rating is not None else 0
                except (ValueError, TypeError):
                    rating = 0

                if rating > 0: # Only apply min_rating to rated tracks
                    min_rating = filters['min_rating']
                    if rating < min_rating:
                        include_item = False

            # Special handling for 70s80s_flashback - only include tracks from 1970-1989
            if playlist_type == "70s80s_flashback":
                item_year = getattr(item, 'year', None)
                if not (item_year and 1970 <= item_year <= 1990):
                    include_item = False

            if include_item:
                filtered_items.append(item)

        # For highly_rated playlist, further filter for ratings >= 7
        if playlist_type == "highly_rated":
            highly_rated_items = []
            for item in filtered_items:
                rating = getattr(item, 'plex_userrating', 0) or 0
                # Ensure rating is numeric for comparison
                try:
                    rating = float(rating) if rating is not None else 0
                except (ValueError, TypeError):
                    rating = 0
                if rating >= 7.0:  # High rating threshold
                    highly_rated_items.append(item)
            filtered_items = highly_rated_items
            ps._log.debug("Filtered to {} highly rated tracks (rating >= 7.0)", len(filtered_items))

        # For most_played playlist, sort by play count
        if playlist_type == "most_played":
            def get_play_count(item):
                return getattr(item, 'plex_viewcount', 0) or 0

            sorted_items = sorted(filtered_items, key=get_play_count, reverse=True)
            filtered_items = sorted_items
            ps._log.debug("Sorted {} tracks by play count for Most Played playlist", len(filtered_items))

        # Separate rated and unrated tracks
        rated_items = []
        unrated_items = []
        for item in filtered_items:
            rating = getattr(item, 'plex_userrating', 0) or 0
            if rating > 0:
                rated_items.append(item)
            else:
                unrated_items.append(item)

        ps._log.debug("Split into {} rated and {} unrated tracks", len(rated_items), len(unrated_items))

        # Select tracks using weighted scoring
        if playlist_type == "most_played":
            # For most played, use the sorted list directly but apply weighted selection for variety
            selected_items = select_tracks_weighted(ps, filtered_items, max_tracks, playlist_type=playlist_type)
        else:
            rated_tracks_count = int(max_tracks * (1 - discovery_ratio / 100))
            unrated_tracks_count = int(max_tracks * (discovery_ratio / 100))

            selected_rated = select_tracks_weighted(ps, rated_items, rated_tracks_count, playlist_type=playlist_type)
            selected_unrated = select_tracks_weighted(ps, unrated_items, unrated_tracks_count, playlist_type=playlist_type)

            # Fill remaining slots if needed
            if len(selected_unrated) < unrated_tracks_count:
                additional_count = min(unrated_tracks_count - len(selected_unrated),
                                      max_tracks - len(selected_rated) - len(selected_unrated))
                remaining_rated = [t for t in rated_items if t not in selected_rated]
                additional_rated = select_tracks_weighted(ps, remaining_rated, additional_count, playlist_type=playlist_type)
                selected_rated.extend(additional_rated)

            selected_items = selected_rated + selected_unrated
    else:
        # Regular handling for playlists that use Plex tracks directly
        if playlist_type == "daily_discovery":
            # Daily Discovery uses both sonic analysis and library tracks
            matched_sonic_tracks = []
            for plex_track in similar_tracks:
                try:
                    beets_item = plex_lookup.get(plex_track.ratingKey)
                    if beets_item:
                        matched_sonic_tracks.append(plex_track)
                except Exception as e:
                    ps._log.debug("Error processing sonic track {}: {}", plex_track.title, e)
                    continue

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
        else:
            # For other playlist types, use standard library tracks
            all_library_tracks = _get_library_tracks(ps, preferred_genres, filters, exclusion_days)

            # Skip redundant client-side filtering when server-side filters fully covered them
            if filters:
                try:
                    adv = build_advanced_filters(filters, exclusion_days)
                except Exception:
                    adv = None
                if not adv:
                    all_library_tracks = apply_playlist_filters(ps, all_library_tracks, filters)

            unique_tracks = []
            for track in all_library_tracks:
                try:
                    beets_item = plex_lookup.get(track.ratingKey)
                    if beets_item:
                        unique_tracks.append(beets_item)
                except Exception as e:
                    ps._log.debug("Error converting track {}: {}", track.title, e)

            # Apply year-based filtering for certain playlist types
            if playlist_type == "recent_hits":
                min_year, _ = _apply_recency_guard(ps, playlist_config, filters, playlist_name, default_max_age_years=3)
                unique_tracks = _filter_tracks_by_min_year(ps, unique_tracks, min_year, playlist_name)
            elif playlist_type == "fresh_favorites":
                min_year, _ = _apply_recency_guard(ps, playlist_config, filters, playlist_name, default_max_age_years=7)
                unique_tracks = _filter_tracks_by_min_year(ps, unique_tracks, min_year, playlist_name)
                # Apply min rating filter for fresh favorites - keep tracks with rating >= min_rating AND unrated tracks
                def _safe_float_rating(track):
                    rating = getattr(track, 'plex_userrating', 0) or 0
                    try:
                        return float(rating) if rating is not None else 0
                    except (ValueError, TypeError):
                        return 0

                min_rating = get_config_value(playlist_config, defaults_cfg, "min_rating", 6)
                unique_tracks = [t for t in unique_tracks if
                                _safe_float_rating(t) == 0 or  # Keep unrated tracks
                                _safe_float_rating(t) >= min_rating]  # Keep rated tracks that meet min rating

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
        selected_rated = select_tracks_weighted(ps, rated_tracks, rated_tracks_count, playlist_type=playlist_type)
        selected_unrated = select_tracks_weighted(ps, unrated_tracks, unrated_tracks_count, playlist_type=playlist_type)

        # Fill remaining slots if needed
        if len(selected_unrated) < unrated_tracks_count:
            additional_count = min(unrated_tracks_count - len(selected_unrated),
                                  max_tracks - len(selected_rated) - len(selected_unrated))
            remaining_rated = [t for t in rated_tracks if t not in selected_rated]
            additional_rated = select_tracks_weighted(ps, remaining_rated, additional_count, playlist_type=playlist_type)
            selected_rated.extend(additional_rated)

        selected_items = selected_rated + selected_unrated

    # Ensure we don't exceed max_tracks
    if len(selected_items) > max_tracks:
        selected_items = selected_items[:max_tracks]

    import random
    random.shuffle(selected_items)

    if not selected_items:
        ps._log.warning("No tracks matched criteria for {} playlist", playlist_name)
        return

    # Convert beets items to Plex tracks for special playlists
    if special_handling:
        plex_tracks = []
        for item in selected_items:
            if hasattr(item, "plex_ratingkey") and item.plex_ratingkey:
                try:
                    plex_track = ps.plex.fetchItem(item.plex_ratingkey)
                    plex_tracks.append(plex_track)
                except Exception as e:
                    ps._log.debug("Could not fetch Plex track for item: {} - Error: {}", item, e)
                    # Fallback: try to find by metadata
                    try:
                        tracks = ps.music.searchTracks(title=getattr(item, 'title', ''),
                                                      artist=getattr(item, 'artist', ''),
                                                      album=getattr(item, 'album', ''))
                        if tracks:
                            plex_tracks.append(tracks[0])
                    except Exception:
                        continue

        if not plex_tracks:
            ps._log.warning("Could not find any Plex tracks for {} playlist", playlist_name)
            return

        selected_tracks = plex_tracks
    else:
        selected_tracks = selected_items

    try:
        ps._plex_clear_playlist(playlist_name)
        ps._log.info("Cleared existing {} playlist", playlist_name)
    except Exception:
        ps._log.debug("No existing {} playlist found", playlist_name)

    ps._plex_add_playlist_item(selected_tracks, playlist_name)
    ps._log.info("Successfully updated {} playlist with {} tracks", playlist_name, len(selected_tracks))


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
    generate_unified_playlist(ps, lib, dd_config, plex_lookup, preferred_genres, similar_tracks, "daily_discovery")

def generate_forgotten_gems(ps, lib, fg_config, plex_lookup, preferred_genres, similar_tracks):
    generate_unified_playlist(ps, lib, fg_config, plex_lookup, preferred_genres, similar_tracks, "forgotten_gems")


def generate_recent_hits(ps, lib, rh_config, plex_lookup, preferred_genres, similar_tracks):
    generate_unified_playlist(ps, lib, rh_config, plex_lookup, preferred_genres, similar_tracks, "recent_hits")


def generate_fresh_favorites(ps, lib, ff_config, plex_lookup, preferred_genres, similar_tracks):
    generate_unified_playlist(ps, lib, ff_config, plex_lookup, preferred_genres, similar_tracks, "fresh_favorites")


def generate_70s80s_flashback(ps, lib, fb_config, plex_lookup, preferred_genres, similar_tracks):
    generate_unified_playlist(ps, lib, fb_config, plex_lookup, preferred_genres, similar_tracks, "70s80s_flashback")


def generate_highly_rated_tracks(ps, lib, hr_config, plex_lookup, preferred_genres, similar_tracks):
    generate_unified_playlist(ps, lib, hr_config, plex_lookup, preferred_genres, similar_tracks, "highly_rated")


def generate_most_played_tracks(ps, lib, mp_config, plex_lookup, preferred_genres, similar_tracks):
    generate_unified_playlist(ps, lib, mp_config, plex_lookup, preferred_genres, similar_tracks, "most_played")
