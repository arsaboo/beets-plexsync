"""Smart playlist generation and helpers extracted from plexsync.

These functions use the plugin instance (`ps`) to access logging, config,
and Plex/beets objects. Behavior preserved.
"""

from datetime import datetime, timedelta
from typing import Tuple
import time
import os
import copy

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
        # Strongly favor newer, recently played favourites while keeping quality high
        if is_rated:
            weighted_score = (
                (z_age * 0.4)
                + (z_recency * 0.25)
                + (z_rating * 0.2)
                + (z_popularity * 0.1)
                + (-z_play_count * 0.05)
            )
        else:
            # For unrated, heavily prioritize newness and recent activity
            weighted_score = (
                (z_age * 0.45)
                + (z_recency * 0.25)
                + (z_popularity * 0.2)
                + (-z_play_count * 0.1)
            )
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
        # Focus on recent popular tracks: emphasize recency of release and playback
        if is_rated:
            weighted_score = (
                (z_age * 0.35)
                + (z_recency * 0.3)
                + (z_popularity * 0.25)
                + (z_rating * 0.1)
            )
        else:
            weighted_score = (
                (z_age * 0.4)
                + (z_recency * 0.35)
                + (z_popularity * 0.25)
            )
    elif playlist_type == "70s80s_flashback":
        # For 70s/80s Flashback: emphasize nostalgic value with high rating and age (70s/80s era)
        # This playlist focuses on well-rated tracks from that era that may not have been played recently
        if is_rated:
            # For rated tracks, emphasize age (70s/80s) and rating, but also consider recency (to avoid overplayed tracks)
            weighted_score = (z_rating * 0.4) + (z_age * 0.3) + (z_recency * 0.2) + (z_play_count * 0.1)
        else:
            # For unrated tracks from 70s/80s, emphasize recency and age (discovering forgotten gems)
            weighted_score = (z_age * 0.4) + (z_recency * 0.35) + (z_popularity * 0.25)
    elif playlist_type == "highly_rated":
        # For highly rated tracks: emphasize rating above all else, but add some recency to keep variety
        if is_rated:
            weighted_score = (z_rating * 0.7) + (z_recency * 0.2) + (z_popularity * 0.1)
        else:
            # For unrated tracks, use a baseline score to include some variety
            weighted_score = (z_recency * 0.4) + (z_popularity * 0.4) + (z_age * 0.2)
    elif playlist_type == "most_played":
        # For most played tracks: emphasize play count and rating, with some recency
        if is_rated:
            weighted_score = (z_play_count * 0.4) + (z_rating * 0.3) + (z_recency * 0.3)
        else:
            # For unrated tracks, emphasize play count and recency
            weighted_score = (z_play_count * 0.5) + (z_recency * 0.4) + (z_popularity * 0.1)
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
    
    # Standard weighted selection for all playlist types
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


def generate_forgotten_gems(ps, lib, fg_config, plex_lookup, preferred_genres, similar_tracks):
    playlist_name = fg_config.get("name", "Forgotten Gems")
    ps._log.info("Generating {} playlist", playlist_name)
    defaults_cfg = get_plexsync_config(["playlists", "defaults"], dict, {})
    max_tracks = get_config_value(fg_config, defaults_cfg, "max_tracks", 20)
    discovery_ratio = get_config_value(fg_config, defaults_cfg, "discovery_ratio", 30)
    exclusion_days = get_config_value(fg_config, defaults_cfg, "exclusion_days", 30)
    filters = fg_config.get("filters", {})
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
    exclusion_days = get_config_value(rh_config, defaults_cfg, "exclusion_days", 30)
    filters = rh_config.get("filters", {})
    min_year, filters = _apply_recency_guard(ps, rh_config, filters, playlist_name, default_max_age_years=3)
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

    final_tracks = _filter_tracks_by_min_year(ps, final_tracks, min_year, playlist_name)

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
    exclusion_days = get_config_value(ff_config, defaults_cfg, "exclusion_days", 30)
    min_rating = get_config_value(ff_config, defaults_cfg, "min_rating", 6)
    filters = ff_config.get("filters", {})
    min_year, filters = _apply_recency_guard(ps, ff_config, filters, playlist_name, default_max_age_years=7)
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

    final_tracks = _filter_tracks_by_min_year(ps, final_tracks, min_year, playlist_name)

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


def generate_70s80s_flashback(ps, lib, fb_config, plex_lookup, preferred_genres, similar_tracks):
    playlist_name = fb_config.get("name", "70s/80s Flashback")
    ps._log.info("Generating {} playlist", playlist_name)
    defaults_cfg = get_plexsync_config(["playlists", "defaults"], dict, {})
    max_tracks = get_config_value(fb_config, defaults_cfg, "max_tracks", 20)
    discovery_ratio = get_config_value(fb_config, defaults_cfg, "discovery_ratio", 30)
    exclusion_days = get_config_value(fb_config, defaults_cfg, "exclusion_days", 30)
    filters = fb_config.get("filters", {})
    
    # Get all tracks from the Plex library
    _t0 = time.time()
    all_library_tracks = ps.music.search(libtype="track")
    ps._log.debug(
        "Fetched all tracks for 70s/80s Flashback (no server filters) -> {} in {:.2f}s",
        len(all_library_tracks), time.time() - _t0,
    )
    
    # Filter for tracks from the 1970s and 1980s (70s/80s) first
    decade_filtered_tracks = []
    for track in all_library_tracks:
        year = getattr(track, 'year', None)
        if year and 1970 <= year <= 1989:
            decade_filtered_tracks.append(track)
    
    ps._log.debug("Filtered to {} tracks from 1970-1989", len(decade_filtered_tracks))
    
    # Apply additional filters if specified
    if filters:
        decade_filtered_tracks = apply_playlist_filters(ps, decade_filtered_tracks, filters)
    
    # Convert to beets items
    final_tracks = []
    for track in decade_filtered_tracks:
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
    unrated_tracks_count, rated_tracks_count = calculate_playlist_proportions(ps, max_tracks, discovery_ratio)
    
    # Select tracks with special scoring for 70s/80s flashback playlist
    selected_rated = select_tracks_weighted(ps, rated_tracks, rated_tracks_count, playlist_type="70s80s_flashback")
    selected_unrated = select_tracks_weighted(ps, unrated_tracks, unrated_tracks_count, playlist_type="70s80s_flashback")
    
    # Fill remaining slots if needed
    if len(selected_unrated) < unrated_tracks_count:
        additional_count = min(unrated_tracks_count - len(selected_unrated), max_tracks - len(selected_rated) - len(selected_unrated))
        remaining_rated = [t for t in rated_tracks if t not in selected_rated]
        additional_rated = select_tracks_weighted(ps, remaining_rated, additional_count, playlist_type="70s80s_flashback")
        selected_rated.extend(additional_rated)
    
    selected_tracks = selected_rated + selected_unrated
    if len(selected_tracks) > max_tracks:
        selected_tracks = selected_tracks[:max_tracks]
    
    import random
    random.shuffle(selected_tracks)
    
    if not selected_tracks:
        ps._log.warning("No tracks matched criteria for 70s/80s Flashback playlist")
        return
    
    try:
        ps._plex_clear_playlist(playlist_name)
        ps._log.info("Cleared existing 70s/80s Flashback playlist")
    except Exception:
        ps._log.debug("No existing 70s/80s Flashback playlist found")
    
    ps._plex_add_playlist_item(selected_tracks, playlist_name)
    ps._log.info("Successfully updated {} playlist with {} tracks", playlist_name, len(selected_tracks))


def generate_highly_rated_tracks(ps, lib, hr_config, plex_lookup, preferred_genres, similar_tracks):
    playlist_name = hr_config.get("name", "Highly Rated Tracks")
    ps._log.info("Generating {} playlist", playlist_name)
    defaults_cfg = get_plexsync_config(["playlists", "defaults"], dict, {})
    max_tracks = get_config_value(hr_config, defaults_cfg, "max_tracks", 20)
    exclusion_days = get_config_value(hr_config, defaults_cfg, "exclusion_days", 30)
    filters = hr_config.get("filters", {})
    
    # Get all tracks from the Plex library
    _t0 = time.time()
    all_library_tracks = ps.music.search(libtype="track")
    ps._log.debug(
        "Fetched all tracks for Highly Rated Tracks (no server filters) -> {} in {:.2f}s",
        len(all_library_tracks), time.time() - _t0,
    )
    
    # Apply filters if specified
    if filters:
        all_library_tracks = apply_playlist_filters(ps, all_library_tracks, filters)
    
    # Convert to beets items
    final_tracks = []
    for track in all_library_tracks:
        try:
            beets_item = plex_lookup.get(track.ratingKey)
            if beets_item:
                final_tracks.append(beets_item)
        except Exception as e:
            ps._log.debug("Error converting track {}: {}", track.title, e)
    
    # Filter for highly rated tracks (rating >= 7)
    highly_rated_tracks = []
    for track in final_tracks:
        rating = float(getattr(track, 'plex_userrating', 0))
        if rating >= 7.0:  # High rating threshold
            highly_rated_tracks.append(track)
    
    ps._log.debug("Filtered to {} highly rated tracks (rating >= 7.0)", len(highly_rated_tracks))
    
    # Select tracks using weighted scoring (with slight recency factor to keep rotation)
    selected_tracks = select_tracks_weighted(ps, highly_rated_tracks, max_tracks, playlist_type="highly_rated")
    
    if not selected_tracks:
        ps._log.warning("No tracks matched criteria for Highly Rated Tracks playlist")
        return
    
    try:
        ps._plex_clear_playlist(playlist_name)
        ps._log.info("Cleared existing Highly Rated Tracks playlist")
    except Exception:
        ps._log.debug("No existing Highly Rated Tracks playlist found")
    
    ps._plex_add_playlist_item(selected_tracks, playlist_name)
    ps._log.info("Successfully updated {} playlist with {} tracks", playlist_name, len(selected_tracks))


def generate_most_played_tracks(ps, lib, mp_config, plex_lookup, preferred_genres, similar_tracks):
    playlist_name = mp_config.get("name", "Most Played Tracks")
    ps._log.info("Generating {} playlist", playlist_name)
    defaults_cfg = get_plexsync_config(["playlists", "defaults"], dict, {})
    max_tracks = get_config_value(mp_config, defaults_cfg, "max_tracks", 20)
    exclusion_days = get_config_value(mp_config, defaults_cfg, "exclusion_days", 30)
    filters = mp_config.get("filters", {})
    
    # Get all tracks from the Plex library
    _t0 = time.time()
    all_library_tracks = ps.music.search(libtype="track")
    ps._log.debug(
        "Fetched all tracks for Most Played Tracks (no server filters) -> {} in {:.2f}s",
        len(all_library_tracks), time.time() - _t0,
    )
    
    # Apply filters if specified
    if filters:
        all_library_tracks = apply_playlist_filters(ps, all_library_tracks, filters)
    
    # Convert to beets items using the plex lookup
    final_tracks = []
    for track in all_library_tracks:
        try:
            beets_item = plex_lookup.get(track.ratingKey)
            if beets_item:
                final_tracks.append(beets_item)
        except Exception as e:
            ps._log.debug("Error converting track {}: {}", track.title, e)
    
    # Sort by play count in descending order (no hard cutoff)
    # Prioritize plex_viewcount if available
    def get_play_count(track):
        # Check plex view count
        return getattr(track, 'plex_viewcount', 0) or 0
    
    sorted_tracks = sorted(final_tracks, key=get_play_count, reverse=True)
    
    ps._log.debug("Sorted {} tracks by play count for Most Played playlist", len(sorted_tracks))
    
    # Select the top tracks by play count, using weighted selection to add some variety
    selected_tracks = select_tracks_weighted(ps, sorted_tracks, max_tracks, playlist_type="most_played")
    
    if not selected_tracks:
        ps._log.warning("No tracks matched criteria for Most Played Tracks playlist")
        return
    
    try:
        ps._plex_clear_playlist(playlist_name)
        ps._log.info("Cleared existing Most Played Tracks playlist")
    except Exception:
        ps._log.debug("No existing Most Played Tracks playlist found")
    
    ps._plex_add_playlist_item(selected_tracks, playlist_name)
    ps._log.info("Successfully updated {} playlist with {} tracks", playlist_name, len(selected_tracks))



