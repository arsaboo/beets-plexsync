"""Track matching and search functionality for PlexSync plugin."""

import re
from difflib import SequenceMatcher

from beetsplug.utils import (
    clean_string, calculate_string_similarity, calculate_artist_similarity
)


def plex_track_distance(track, title, album, artist):
    """Calculate distance between a Plex track and provided metadata.

    Args:
        track: Plex track object
        title: Title to match
        album: Album to match
        artist: Artist to match

    Returns:
        float: Distance score (0.0-1.0, lower is better)
    """
    # Get track metadata
    track_title = track.title
    track_album = track.parentTitle
    track_artist = getattr(track, 'originalTitle', None) or track.artist().title

    # Calculate individual similarities
    title_sim = calculate_string_similarity(title, track_title)

    # Album similarity - handle None values
    if album and track_album:
        album_sim = calculate_string_similarity(album, track_album)
    elif not album and not track_album:
        album_sim = 1.0  # Both are None/empty, perfect match
    else:
        album_sim = 0.0  # One is None, the other isn't

    # Artist similarity
    artist_sim = calculate_artist_similarity(artist, track_artist)

    # Calculate weighted distance
    # Title and artist are more important than album
    weights = {
        'title': 0.5,
        'album': 0.2,
        'artist': 0.3
    }

    # Convert similarities to distances (1.0 - similarity)
    title_dist = 1.0 - title_sim
    album_dist = 1.0 - album_sim
    artist_dist = 1.0 - artist_sim

    # Calculate weighted distance
    distance = (
        weights['title'] * title_dist +
        weights['album'] * album_dist +
        weights['artist'] * artist_dist
    )

    return distance


def get_best_match(tracks, title, album, artist, threshold=0.4):
    """Get the best matching track from a list of tracks.

    Args:
        tracks: List of Plex track objects
        title: Title to match
        album: Album to match
        artist: Artist to match
        threshold: Maximum distance for a match (lower is stricter)

    Returns:
        tuple: (best_match, distance) or (None, 1.0) if no match found
    """
    if not tracks:
        return None, 1.0

    # Calculate distances for all tracks
    track_distances = []
    for track in tracks:
        distance = plex_track_distance(track, title, album, artist)
        track_distances.append((track, distance))

    # Sort by distance (lower is better)
    track_distances.sort(key=lambda x: x[1])

    # Get best match
    best_match, best_distance = track_distances[0]

    # Return best match if it's below threshold
    if best_distance <= threshold:
        return best_match, best_distance

    return None, 1.0


def search_tracks_by_metadata(music, title, album=None, artist=None, limit=20):
    """Search for tracks in Plex library by metadata.

    Args:
        music: Plex music library section
        title: Track title to search for
        album: Optional album title
        artist: Optional artist name
        limit: Maximum number of results

    Returns:
        list: List of matching Plex track objects
    """
    search_params = {}

    # Add search parameters if provided
    if title:
        search_params["track.title"] = title
    if album:
        search_params["album.title"] = album
    if artist:
        search_params["artist.title"] = artist

    # If no parameters provided, return empty list
    if not search_params:
        return []

    # Search for tracks
    try:
        return music.searchTracks(**search_params, limit=limit)
    except Exception as e:
        print(f"Error searching for tracks: {e}")
        return []


def filter_tracks_by_similarity(tracks, title, album=None, artist=None, threshold=0.3):
    """Filter tracks by similarity to provided metadata.

    Args:
        tracks: List of Plex track objects
        title: Title to match
        album: Album to match
        artist: Artist to match
        threshold: Maximum distance for a match (lower is stricter)

    Returns:
        list: List of (track, distance) tuples for tracks below threshold
    """
    if not tracks:
        return []

    # Calculate distances for all tracks
    track_distances = []
    for track in tracks:
        distance = plex_track_distance(track, title, album, artist)
        if distance <= threshold:
            track_distances.append((track, distance))

    # Sort by distance (lower is better)
    track_distances.sort(key=lambda x: x[1])

    return track_distances
