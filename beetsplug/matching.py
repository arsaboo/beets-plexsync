"""Track matching utilities for PlexSync."""

import re
import difflib
import unicodedata
import logging

logger = logging.getLogger('beets')

def clean_string(input_string):
    """Clean a string for better matching by removing parentheses and standardizing format.

    Args:
        input_string: The string to clean

    Returns:
        str: The cleaned string
    """
    if not input_string:
        return ""

    # Remove content in parentheses
    cleaned = re.sub(r'\([^)]*\)', '', input_string)

    # Remove content in brackets
    cleaned = re.sub(r'\[[^\]]*\]', '', cleaned)

    # Remove common features and remix indicators
    cleaned = re.sub(r'(?i)\s*(?:feat\.?|ft\.?|featuring)\s+.*$', '', cleaned)
    cleaned = re.sub(r'(?i)\s*(?:[-–]\s*)?(?:remix|version|edit|mix).*$', '', cleaned)

    # Normalize whitespace
    cleaned = ' '.join(cleaned.split())

    return cleaned.strip()

def normalize_for_comparison(text):
    """Normalize text for consistent comparison.

    Args:
        text: The text to normalize

    Returns:
        str: Normalized text
    """
    if not text:
        return ""

    # Convert to lowercase
    text = text.lower()

    # Normalize unicode characters
    text = unicodedata.normalize('NFKD', text)

    # Remove non-alphanumeric characters
    text = re.sub(r'[^\w\s]', ' ', text)

    # Normalize whitespace
    text = ' '.join(text.split())

    return text

def fuzzy_match_score(str1, str2):
    """Calculate fuzzy match score between two strings.

    Args:
        str1: First string
        str2: Second string

    Returns:
        float: Match score between 0-1
    """
    if not str1 or not str2:
        return 0

    # Normalize strings for comparison
    norm1 = normalize_for_comparison(str1)
    norm2 = normalize_for_comparison(str2)

    # If either string is empty after normalization
    if not norm1 or not norm2:
        return 0

    # Exact match after normalization
    if norm1 == norm2:
        return 1.0

    # Check if one is a substring of the other
    if norm1 in norm2:
        return 0.9 * (len(norm1) / len(norm2))

    if norm2 in norm1:
        return 0.9 * (len(norm2) / len(norm1))

    # Calculate sequence matcher ratio
    return difflib.SequenceMatcher(None, norm1, norm2).ratio()

def plex_track_distance(beets_item, plex_track, config=None):
    """Calculate distance between a Beets item and a Plex track.

    Args:
        beets_item: Beets Item object
        plex_track: Plex Track object
        config: Optional config with weights

    Returns:
        tuple: (match_score, distance_components)
    """
    if config is None:
        config = {
            'weights': {
                'title': 0.45,
                'artist': 0.35,
                'album': 0.20,
            }
        }

    # Extract track properties
    plex_title = getattr(plex_track, 'title', '')
    plex_album = getattr(plex_track, 'parentTitle', '')

    # Get artist, handling different possible properties
    if hasattr(plex_track, 'originalTitle') and plex_track.originalTitle:
        plex_artist = plex_track.originalTitle
    else:
        try:
            plex_artist = plex_track.artist().title if hasattr(plex_track, 'artist') else ''
        except Exception:
            plex_artist = ''

    # Calculate component scores
    title_score = fuzzy_match_score(beets_item.title, plex_title)
    artist_score = fuzzy_match_score(beets_item.artist, plex_artist)

    # Album is optional
    album_score = 0
    if hasattr(beets_item, 'album') and beets_item.album and plex_album:
        album_score = fuzzy_match_score(beets_item.album, plex_album)

    # Apply weights from config
    weights = config['weights']
    weighted_score = (
        title_score * weights['title'] +
        artist_score * weights['artist'] +
        album_score * weights['album']
    )

    # Compile distance components for debugging
    distance_components = {
        'title': (title_score, weights['title']),
        'artist': (artist_score, weights['artist']),
        'album': (album_score, weights['album']),
    }

    # Log detailed comparison for debugging
    logger.debug(
        "Match comparison: beets='{} - {} - {}', plex='{} - {} - {}', scores=(title:{:.2f}, artist:{:.2f}, album:{:.2f}), total:{:.2f}",
        getattr(beets_item, 'album', ''), beets_item.title, beets_item.artist,
        plex_album, plex_title, plex_artist,
        title_score, artist_score, album_score, weighted_score
    )

    return weighted_score, distance_components

