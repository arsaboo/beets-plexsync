"""Smart playlist generation for Plex.

Generates dynamic playlists based on ratings, play counts, genres, and recency.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
import json

logger = logging.getLogger("harmony.plex.smartplaylists")


# Scoring weights for different playlist types.
# Weights apply to z-scored track attributes; higher values increase selection likelihood,
# negative values penalize the attribute, and totals are relative (not required to sum to 1).
DEFAULT_SCORING_WEIGHTS = {
    "forgotten_gems": {
        "rated_weights": {
            "z_rating": 0.3,
            "z_recency": 0.3,
            "z_play_count": -0.2,
            "z_popularity": 0.1,
            "z_age": -0.1
        },
        "unrated_weights": {
            "z_recency": 0.4,
            "z_play_count": -0.3,
            "z_popularity": 0.2,
            "z_age": -0.1
        }
    },
    "fresh_favorites": {
        "rated_weights": {
            "z_age": 0.4,
            "z_recency": 0.25,
            "z_rating": 0.2,
            "z_popularity": 0.1,
            "z_play_count": -0.05
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
            "z_play_count": -0.2
        },
        "unrated_weights": {
            "z_recency": 0.4,
            "z_popularity": 0.35,
            "z_play_count": -0.25
        }
    },
    "highly_rated": {
        "rated_weights": {
            "z_rating": 0.7,
            "z_recency": 0.2,
            "z_popularity": 0.1,
            "z_age": 0.1
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
    "energetic_workout": {
        "rated_weights": {
            "z_rating": 0.3,
            "z_recency": 0.2,
            "z_popularity": 0.3,
            "z_age": 0.2
        },
        "unrated_weights": {
            "z_popularity": 0.4,
            "z_recency": 0.3,
            "z_age": 0.3
        }
    },
    "relaxed_evening": {
        "rated_weights": {
            "z_rating": 0.4,
            "z_recency": 0.3,
            "z_play_count": -0.2,
            "z_popularity": 0.1
        },
        "unrated_weights": {
            "z_recency": 0.4,
            "z_popularity": 0.3,
            "z_play_count": -0.3
        }
    }
}


def get_scoring_weights(playlist_type: str) -> Dict[str, Any]:
    """Get scoring weights for a specific playlist type."""
    return DEFAULT_SCORING_WEIGHTS.get(playlist_type, DEFAULT_SCORING_WEIGHTS.get("daily_discovery", {}))


def compute_z_score(values: List[float]) -> Dict[float, float]:
    """Compute z-scores for a list of values.

    Returns: Dict mapping original value to z-score
    """
    if not values or len(values) < 2:
        return {v: 0.0 for v in values}

    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    std_dev = variance ** 0.5

    if std_dev == 0:
        return {v: 0.0 for v in values}

    return {v: (v - mean) / std_dev for v in values}


def calculate_track_score(
    track: Dict[str, Any],
    base_time: datetime = None,
    tracks_context: List[Dict] = None,
    playlist_type: str = "daily_discovery",
    tracks_context_stats: Dict = None
) -> float:
    """Calculate a weighted score for a track based on playlist type.

    Tracks are scored based on:
    - Rating (userRating)
    - Recency (days since lastViewedAt)
    - Play count
    - Popularity
    - Age (years since release)
    """
    if base_time is None:
        base_time = datetime.now()

    if tracks_context is None:
        tracks_context = []

    def _safe_int(value, default=0) -> int:
        try:
            if value is None:
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    def _safe_float(value, default=0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    weights = get_scoring_weights(playlist_type)
    is_rated = track.get("userRating") and _safe_float(track.get("userRating", 0)) > 0
    weight_set = weights.get("rated_weights" if is_rated else "unrated_weights", {})

    score = 0.0

    # Extract track attributes
    rating = _safe_float(track.get("userRating", 0))
    play_count = _safe_int(track.get("viewCount", 0))
    last_viewed = track.get("lastViewedAt")
    year = _safe_int(track.get("year", 0))
    popularity = _safe_float(track.get("popularity", 0))

    # Recency (days since last viewed)
    if last_viewed:
        try:
            last_viewed_dt = datetime.fromisoformat(str(last_viewed))
            days_since = (base_time - last_viewed_dt).days
        except (ValueError, TypeError):
            days_since = 999
    else:
        days_since = 999

    # Age (years since release)
    age_years = max(0, base_time.year - year) if year else 0

    # Compute z-scores if context available
    z_rating = 0.0
    z_recency = 0.0
    z_play_count = 0.0
    z_popularity = 0.0
    z_age = 0.0

    if tracks_context_stats:
        rating_z_map = compute_z_score([
            _safe_float(t.get("userRating", 0)) for t in tracks_context
        ])
        z_rating = rating_z_map.get(rating, 0.0)

        recency_z_map = compute_z_score([float(t.get("_days_since_played", 0)) for t in tracks_context])
        z_recency = recency_z_map.get(float(days_since), 0.0)

        play_count_z_map = compute_z_score([
            _safe_float(t.get("viewCount", 0)) for t in tracks_context
        ])
        z_play_count = play_count_z_map.get(float(play_count), 0.0)

        popularity_z_map = compute_z_score([
            _safe_float(t.get("popularity", 0)) for t in tracks_context
        ])
        z_popularity = popularity_z_map.get(float(popularity), 0.0)

        age_z_map = compute_z_score([float(t.get("_age_years", 0)) for t in tracks_context])
        z_age = age_z_map.get(float(age_years), 0.0)

    # Apply weights
    if "z_rating" in weight_set:
        score += weight_set["z_rating"] * z_rating
    if "z_recency" in weight_set:
        score += weight_set["z_recency"] * z_recency
    if "z_play_count" in weight_set:
        score += weight_set["z_play_count"] * z_play_count
    if "z_popularity" in weight_set:
        score += weight_set["z_popularity"] * z_popularity
    if "z_age" in weight_set:
        score += weight_set["z_age"] * z_age

    return score


def select_tracks_weighted(
    tracks: List[Dict[str, Any]],
    num_tracks: int,
    playlist_type: str = "daily_discovery"
) -> List[Dict[str, Any]]:
    """Select and sort tracks by weighted score for a playlist type."""
    if not tracks:
        return []

    # Compute context stats for all tracks
    base_time = datetime.now()
    def _safe_int(value, default=0) -> int:
        try:
            if value is None:
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    for track in tracks:
        track["_days_since_played"] = 999
        if track.get("lastViewedAt"):
            try:
                last_viewed_dt = datetime.fromisoformat(str(track.get("lastViewedAt")))
                track["_days_since_played"] = (base_time - last_viewed_dt).days
            except (ValueError, TypeError):
                pass
        track["_age_years"] = max(0, base_time.year - _safe_int(track.get("year", 0)))

    # Score all tracks
    scored_tracks = []
    for track in tracks:
        score = calculate_track_score(
            track,
            base_time=base_time,
            tracks_context=tracks,
            playlist_type=playlist_type
        )
        scored_tracks.append((score, track))

    # Sort by score (descending) and return top N
    scored_tracks.sort(key=lambda x: x[0], reverse=True)
    return [track for score, track in scored_tracks[:num_tracks]]


def filter_by_rating(tracks: List[Dict[str, Any]], min_rating: float = 4.0) -> List[Dict[str, Any]]:
    """Filter tracks by minimum user rating."""
    def _safe_float(value, default=0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    rated = [
        t for t in tracks
        if t.get("userRating") is not None and _safe_float(t.get("userRating", 0)) >= min_rating
    ]
    unrated = [
        t for t in tracks
        if t.get("userRating") is None or _safe_float(t.get("userRating", 0)) == 0
    ]
    return rated + unrated  # Keep rated first, unrated after


def filter_by_year(tracks: List[Dict[str, Any]], min_year: int = None, max_year: int = None) -> List[Dict[str, Any]]:
    """Filter tracks by release year range."""
    filtered = []
    for track in tracks:
        year = track.get("year")
        if year is None:
            filtered.append(track)
            continue
        try:
            year_int = int(year)
            if min_year and year_int < min_year:
                continue
            if max_year and year_int > max_year:
                continue
            filtered.append(track)
        except (TypeError, ValueError):
            filtered.append(track)
    return filtered


def filter_by_recency(tracks: List[Dict[str, Any]], min_days: int = None, max_days: int = None) -> List[Dict[str, Any]]:
    """Filter tracks by days since last played."""
    if min_days is None and max_days is None:
        return tracks

    base_time = datetime.now()
    filtered = []

    for track in tracks:
        last_viewed = track.get("lastViewedAt")
        if not last_viewed:
            if min_days is None or min_days <= 999:
                filtered.append(track)
            continue

        try:
            last_viewed_dt = datetime.fromisoformat(str(last_viewed))
            days_since = (base_time - last_viewed_dt).days
        except (ValueError, TypeError):
            days_since = 999

        if min_days and days_since < min_days:
            continue
        if max_days and days_since > max_days:
            continue
        filtered.append(track)

    return filtered


def _normalize_genre_list(genres: Any) -> List[str]:
    if not genres:
        return []
    if isinstance(genres, str):
        parts = [g.strip() for g in genres.replace(";", ",").split(",")]
        return [g.lower() for g in parts if g]
    if isinstance(genres, list):
        return [str(g).strip().lower() for g in genres if str(g).strip()]
    return []


def filter_by_genres(
    tracks: List[Dict[str, Any]],
    include: List[str] = None,
    exclude: List[str] = None,
) -> List[Dict[str, Any]]:
    """Filter tracks by genre include/exclude lists."""
    include_set = set(g.lower() for g in (include or []))
    exclude_set = set(g.lower() for g in (exclude or []))

    if not include_set and not exclude_set:
        return tracks

    filtered = []
    for track in tracks:
        track_genres = _normalize_genre_list(track.get("genres") or track.get("genre"))
        if exclude_set and any(g in exclude_set for g in track_genres):
            continue
        if include_set and not any(g in include_set for g in track_genres):
            continue
        filtered.append(track)
    return filtered


def filter_by_year_constraints(
    tracks: List[Dict[str, Any]],
    include: Dict[str, Any] = None,
    exclude: Dict[str, Any] = None,
) -> List[Dict[str, Any]]:
    """Filter tracks by include/exclude year constraints."""
    include = include or {}
    exclude = exclude or {}
    inc = include.get("years") or {}
    exc = exclude.get("years") or {}

    if not inc and not exc:
        return tracks

    filtered = []
    for track in tracks:
        year = track.get("year")
        if year is None:
            filtered.append(track)
            continue
        try:
            year_int = int(year)
        except (TypeError, ValueError):
            filtered.append(track)
            continue

        if "between" in inc and isinstance(inc["between"], list) and len(inc["between"]) == 2:
            start_year, end_year = inc["between"]
            if year_int < int(start_year) or year_int > int(end_year):
                continue
        if "after" in inc and year_int < int(inc["after"]):
            continue
        if "before" in inc and year_int > int(inc["before"]):
            continue

        if "before" in exc and year_int < int(exc["before"]):
            continue
        if "after" in exc and year_int > int(exc["after"]):
            continue

        filtered.append(track)
    return filtered


def _value_within_range(value: Optional[float], minimum: Optional[float], maximum: Optional[float]) -> bool:
    if value is None:
        return True
    if minimum is not None and value < minimum:
        return False
    if maximum is not None and value > maximum:
        return False
    return True


def filter_by_mood(
    tracks: List[Dict[str, Any]],
    audiomuse_backend: Any,
    mood_filters: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Filter tracks by AudioMuse mood criteria."""
    if not mood_filters or not audiomuse_backend:
        return tracks

    filtered: List[Dict[str, Any]] = []
    for track in tracks:
        item_id = track.get("audiomuse_item_id")
        if not item_id:
            title = track.get("title")
            artist = track.get("artist")
            if title and artist:
                item_id = audiomuse_backend.search_tracks_by_metadata(title, artist)
                if item_id:
                    track["audiomuse_item_id"] = item_id

        if not item_id:
            filtered.append(track)
            continue

        features = audiomuse_backend.get_track_features(item_id)
        if not features:
            filtered.append(track)
            continue

        energy = features.get("energy")
        tempo = features.get("tempo")
        mood_features = features.get("mood_features", {})
        mood_categories = features.get("mood_categories", {})

        if not _value_within_range(
            energy,
            mood_filters.get("min_energy"),
            mood_filters.get("max_energy"),
        ):
            continue

        if not _value_within_range(
            tempo,
            mood_filters.get("min_tempo"),
            mood_filters.get("max_tempo"),
        ):
            continue

        failed = False
        for feature_name in ["danceable", "aggressive", "happy", "party", "relaxed", "sad"]:
            min_key = f"min_{feature_name}"
            max_key = f"max_{feature_name}"
            if min_key not in mood_filters and max_key not in mood_filters:
                continue
            feature_value = mood_features.get(feature_name)
            if not _value_within_range(feature_value, mood_filters.get(min_key), mood_filters.get(max_key)):
                failed = True
                break
        if failed:
            continue

        preferred_categories = mood_filters.get("mood_categories") or []
        if preferred_categories:
            preferred_normalized = {str(cat).strip().lower() for cat in preferred_categories}
            categories_normalized = {str(cat).strip().lower() for cat in mood_categories.keys()}
            if preferred_normalized and not preferred_normalized.intersection(categories_normalized):
                continue

        filtered.append(track)

    return filtered


def apply_filters(
    tracks: List[Dict[str, Any]],
    min_rating: float = None,
    min_year: int = None,
    max_year: int = None,
    min_days_since_played: int = None,
    max_days_since_played: int = None,
    include: Dict[str, Any] = None,
    exclude: Dict[str, Any] = None,
    mood: Dict[str, Any] = None,
    audiomuse_backend: Any = None,
) -> List[Dict[str, Any]]:
    """Apply multiple filters to a track list."""
    result = tracks

    if include or exclude:
        result = filter_by_genres(
            result,
            include=(include or {}).get("genres"),
            exclude=(exclude or {}).get("genres"),
        )
        result = filter_by_year_constraints(result, include=include, exclude=exclude)

    if min_rating:
        result = filter_by_rating(result, min_rating)

    if min_year or max_year:
        result = filter_by_year(result, min_year, max_year)

    if min_days_since_played or max_days_since_played:
        result = filter_by_recency(result, min_days_since_played, max_days_since_played)

    if mood and audiomuse_backend:
        result = filter_by_mood(result, audiomuse_backend, mood)
        logger.info(f"After mood filtering: {len(result)} tracks")

    return result


def generate_playlist(
    tracks: List[Dict[str, Any]],
    playlist_name: str,
    num_tracks: int = 50,
    playlist_type: str = "daily_discovery",
    history_days: int = None,
    exclusion_days: int = None,
    discovery_ratio: int = None,
    audiomuse_backend: Any = None,
    **filter_kwargs
) -> Dict[str, Any]:
    """Generate a smart playlist from tracks.

    Args:
        tracks: List of track dicts
        playlist_name: Name of the playlist to create
        num_tracks: Number of tracks to include
        playlist_type: Type of playlist (daily_discovery, forgotten_gems, etc)
        **filter_kwargs: Additional filter arguments (min_rating, min_year, etc)

    Returns:
        Dict with playlist_name, selected_tracks, and metadata
    """
    logger.info(f"Generating {playlist_name} ({playlist_type}) with {len(tracks)} candidate tracks")

    # Apply filters
    if exclusion_days:
        filter_kwargs.setdefault("min_days_since_played", exclusion_days)

    include = filter_kwargs.get("include")
    if history_days and (not include or not include.get("genres")):
        preferred_genres = _get_preferred_genres(tracks, history_days)
        if preferred_genres:
            include = dict(include or {})
            include["genres"] = preferred_genres
            filter_kwargs["include"] = include

    filtered = apply_filters(tracks, audiomuse_backend=audiomuse_backend, **filter_kwargs)
    logger.info(f"After filtering: {len(filtered)} tracks")

    # Select and weight tracks
    selected = []
    if discovery_ratio is not None:
        unrated_count = min(int(num_tracks * (discovery_ratio / 100)), num_tracks)
        rated_count = max(num_tracks - unrated_count, 0)

        rated_pool = [t for t in filtered if float(t.get("userRating", 0) or 0) > 0]
        unrated_pool = [t for t in filtered if float(t.get("userRating", 0) or 0) == 0]

        selected.extend(select_tracks_weighted(rated_pool, rated_count, playlist_type))
        selected.extend(select_tracks_weighted(unrated_pool, unrated_count, playlist_type))

        if len(selected) < num_tracks:
            remaining = _dedupe_tracks(filtered, selected)
            selected.extend(select_tracks_weighted(remaining, num_tracks - len(selected), playlist_type))
    else:
        selected = select_tracks_weighted(filtered, num_tracks, playlist_type)

    logger.info(f"Selected {len(selected)} tracks for {playlist_name}")

    return {
        "playlist_name": playlist_name,
        "playlist_type": playlist_type,
        "total_candidates": len(tracks),
        "after_filters": len(filtered),
        "selected_count": len(selected),
        "tracks": selected,
        "generated_at": datetime.now().isoformat()
    }


def _get_preferred_genres(tracks: List[Dict[str, Any]], history_days: int, limit: int = 5) -> List[str]:
    """Return top genres from tracks played within history_days."""
    cutoff = datetime.now() - timedelta(days=history_days)
    counts: Dict[str, int] = {}
    for track in tracks:
        last_viewed = track.get("lastViewedAt")
        if not last_viewed:
            continue
        try:
            last_viewed_dt = datetime.fromisoformat(str(last_viewed))
        except (ValueError, TypeError):
            continue
        if last_viewed_dt < cutoff:
            continue
        for genre in _normalize_genre_list(track.get("genres") or track.get("genre")):
            counts[genre] = counts.get(genre, 0) + 1

    if not counts:
        return []
    return [g for g, _ in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]]


def _dedupe_tracks(tracks: List[Dict[str, Any]], selected: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    selected_keys = set()
    for track in selected:
        key = track.get("plex_ratingkey") or track.get("backend_id") or id(track)
        selected_keys.add(key)
    return [
        track for track in tracks
        if (track.get("plex_ratingkey") or track.get("backend_id") or id(track)) not in selected_keys
    ]
