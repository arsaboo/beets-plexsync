"""Interactive manual search for track matching with candidate confirmation."""

from __future__ import annotations

import logging
import sys
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("harmony.manual_search")

# Enable ANSI color support on Windows using colorama
if sys.platform == 'win32':
    try:
        import colorama
        colorama.just_fix_windows_console()
    except ImportError:
        # Fallback to ctypes if colorama not available
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass


# UI helpers - simplified for terminal interaction
def print_(text: str) -> None:
    """Print to console."""
    print(text)


def input_(prompt: str) -> str:
    """Get user input."""
    return input(prompt)


def colorize(color: str, text: str) -> str:
    """Simple colorization (can be enhanced with colorama)."""
    colors = {
        "text_highlight": "\033[1;36m",      # Bold cyan
        "text_highlight_minor": "\033[0;36m", # Cyan
        "action": "\033[1;33m",               # Bold yellow
        "text_success": "\033[0;32m",         # Green (for matching parts)
        "text_warning": "\033[0;33m",         # Yellow/Orange
        "text_error": "\033[0;31m",           # Red
        "key_highlight": "\033[1;36m",        # Bold cyan (for action keys - was white, not visible on light backgrounds)
        "dim": "\033[2m",                     # Dim/gray
        "reset": "\033[0m",
    }
    return f"{colors.get(color, '')}{text}{colors.get('reset', '')}"


def input_yn(prompt: str, default: bool = True) -> bool:
    """Get yes/no input from user."""
    default_str = "Y/n" if default else "y/N"
    response = input_(f"{prompt} ({default_str}): ").strip().lower()
    if not response:
        return default
    return response in ("y", "yes")


def _format_action_prompt(options: Tuple[str, ...]) -> str:
    """Format action options with highlighted first letters."""
    formatted = []
    for opt in options:
        # Get the first letter and rest of the word
        first_letter = opt[0].lower()
        rest = opt[1:].lower()
        formatted.append(f"{colorize('key_highlight', first_letter)}{rest}")
    return ", ".join(formatted)


def input_options(
    options: Tuple[str, ...],
    numrange: Optional[Tuple[int, int]] = None,
    default: int = 1
) -> str | int:
    """Get option selection from user."""
    # Format actions with highlighted first letters
    formatted_actions = _format_action_prompt(options)
    
    if numrange:
        prompt_text = f"→ {colorize('key_highlight', '#')} selection (default {default}), {formatted_actions}? "
    else:
        prompt_text = f"→ {formatted_actions}? "
    
    while True:
        response = input_(prompt_text).strip()

        if not response:
            if numrange:
                return default
            return ""

        if response.lower() in ("?", "h", "help"):
            return "?"

        try:
            num = int(response)
            if numrange and numrange[0] <= num <= numrange[1]:
                return num
        except ValueError:
            pass

        response_lower = response.lower()
        for option in options:
            if option.lower().startswith(response_lower):
                return response_lower[0]

        print_(colorize("text_error", "Invalid selection. Please try again."))


def highlight_matches(source: str | None, target: str | None) -> str:
    """Highlight exact matching parts between source and target strings.
    
    Args:
        source: Query string (what we're searching for)
        target: Candidate string (what we found)
        
    Returns:
        Target string with matching words highlighted in green
    """
    if source is None or target is None:
        return target or "Unknown"

    # If exact match, highlight the whole thing
    if source and target and source.lower() == target.lower():
        return colorize('text_success', target)

    from difflib import SequenceMatcher
    import re

    def fuzzy_score(a: str, b: str) -> float:
        """Calculate similarity ratio between two strings."""
        return SequenceMatcher(None, a.lower(), b.lower()).ratio()

    # Split source into words, handling both spaces and commas
    # This handles cases like "Ranveer, Aditya Dhar, Shashwat"
    source_text = source.replace(',', ' ').replace('|', ' ') if source else ""
    source_words = [w for w in source_text.lower().split() if w]
    
    # Split target into words
    target_words = target.lower().split() if target else []
    
    highlighted_words: list[str] = []
    original_target_words = target.split()
    
    for i, target_word in enumerate(target_words):
        word_matched = False
        # Remove non-word characters for comparison
        clean_target_word = re.sub(r'[^\w]', '', target_word)
        
        if not clean_target_word:  # Skip empty words (e.g., standalone commas)
            highlighted_words.append(original_target_words[i])
            continue

        for source_word in source_words:
            clean_source_word = re.sub(r'[^\w]', '', source_word)
            if not clean_source_word:
                continue
                
            # Check for exact match or fuzzy match above 80% threshold
            if (
                clean_source_word == clean_target_word
                or fuzzy_score(clean_source_word, clean_target_word) > 0.8
            ):
                highlighted_words.append(
                    colorize('text_success', original_target_words[i])
                )
                word_matched = True
                break

        if not word_matched:
            # Keep original word without highlighting
            highlighted_words.append(original_target_words[i])

    return ' '.join(highlighted_words)


def _render_actions() -> str:
    """Render available actions in compact format with highlighted keys."""
    return (
        f"  {colorize('key_highlight', '#')}: Select match by number  "
        f"{colorize('key_highlight', 'a')}: Abort  "
        f"{colorize('key_highlight', 's')}: Skip  "
        f"{colorize('key_highlight', 'e')}: Enter manual search  "
        f"{colorize('key_highlight', 'r')}: Refresh index"
    )


def _run_manual_search_queries(
    backend,
    title: str,
    album: str,
    artist: str
) -> List[Dict]:
    """Run search queries against backend API."""
    tracks = []

    if title and artist:
        try:
            tracks = backend.search_tracks(title=title, artist=artist, limit=50)
            logger.debug(f"Artist+Title search found {len(tracks)} tracks")
        except Exception as exc:
            logger.debug(f"Artist+Title search failed: {exc}")

    if not tracks and title and album:
        try:
            tracks = backend.search_tracks(title=title, album=album, limit=50)
            logger.debug(f"Album+Title search found {len(tracks)} tracks")
        except Exception as exc:
            logger.debug(f"Album+Title search failed: {exc}")

    if not tracks and title:
        try:
            tracks = backend.search_tracks(title=title, limit=100)
            logger.debug(f"Title-only search found {len(tracks)} tracks")
        except Exception as exc:
            logger.debug(f"Title-only search failed: {exc}")

    return tracks


def _filter_tracks(
    backend,
    tracks: List[Dict],
    title: str,
    album: str,
    artist: str
) -> List[Dict]:
    """Filter tracks based on query criteria."""
    if not any([title, album, artist]):
        return tracks

    return tracks


def _store_negative_cache(
    cache,
    song_dict: Dict[str, str],
    original_query: Optional[Dict[str, str]]
) -> None:
    """Store negative cache entry (track not found)."""
    try:
        if original_query and original_query.get("title"):
            cache.set(original_query, None)
            logger.debug(f"Stored negative cache for original query: {original_query}")
    except Exception as exc:
        logger.debug(f"Failed to store negative cache: {exc}")


def _cache_selection(
    cache,
    song_dict: Dict[str, str],
    selected_track: Dict,
    original_query: Optional[Dict[str, str]]
) -> None:
    """Cache the user's selection for the ORIGINAL query only."""
    try:
        rating_key = selected_track.get("backend_id") or selected_track.get("plex_ratingkey")
        if not rating_key:
            logger.warning("Selected track has no backend_id")
            return

        if original_query:
            cache.set(original_query, rating_key)
            logger.debug(
                f"Cached selection for original query: {original_query} -> {rating_key}"
            )
    except Exception as exc:
        logger.error(f"Failed to cache selection: {exc}")


def manual_track_search(
    backend,
    cache,
    matching_module,
    original_query: Optional[Dict[str, str]] = None
) -> Optional[Dict]:
    """Interactively search for a track."""
    print_(colorize("text_highlight", "\nManual Search"))
    if original_query:
        print_(
            colorize(
                "text_highlight_minor",
                f"Original: {original_query.get('artist', '')} - "
                f"{original_query.get('title', '')} "
                f"({original_query.get('album', '')})",
            )
        )
    print_("Enter search criteria (empty to skip):")

    title = input_(colorize("text_highlight_minor", "Title: ")).strip()
    album = input_(colorize("text_highlight_minor", "Album: ")).strip()
    artist = input_(colorize("text_highlight_minor", "Artist: ")).strip()

    logger.debug(f"Searching with title='{title}', album='{album}', artist='{artist}'")

    tracks = _run_manual_search_queries(backend, title, album, artist)
    if not tracks:
        logger.info("No matching tracks found")
        retry = input_yn("No matches found. Try again?", default=False)
        if retry:
            return manual_track_search(backend, cache, matching_module, original_query)
        return None

    filtered_tracks = _filter_tracks(backend, tracks, title, album, artist)
    if not filtered_tracks:
        logger.info("No matching tracks found after filtering")
        return None

    song_dict = {
        "title": title or "",
        "album": album or "",
        "artist": artist or "",
    }

    sorted_tracks = _find_closest_match(song_dict, filtered_tracks, matching_module)

    while True:
        # Display search query
        query_info = f"{artist}|{album}|{title}"
        print_(colorize("dim", f"\nSearching for track info: {query_info}"))
        
        # Show what was found
        if sorted_tracks:
            track = sorted_tracks[0][0]
            found_info = f"{{'title': '{track.get('title', '')}', 'album': '{track.get('album', '')}', 'artist': '{track.get('artist', '')}'}}"
            print_(colorize("dim", f"Found track info: {found_info}"))
        
        # Display header
        header = (
            colorize("text_error", "\nReview candidate matches for: ")
            + colorize("text_highlight_minor", f"None - {artist}|{album}|{title}")
        )
        print_(header)

        # Display candidates
        for index, (track, score) in enumerate(sorted_tracks, start=1):
            track_artist = track.get("artist", "")
            track_title = track.get("title", "")
            track_album = track.get("album", "")

            # Apply fuzzy highlighting to each field (matching parts in GREEN)
            highlighted_album = highlight_matches(album, track_album)
            highlighted_title = highlight_matches(title, track_title)
            highlighted_artist = highlight_matches(artist, track_artist)

            # Determine match score color
            if score >= 0.8:
                score_color = "text_success"
            elif score >= 0.5:
                score_color = "text_warning"
            else:
                score_color = "text_error"

            # Format: index. ALBUM - TITLE - ARTIST (Match: score, Sources: direct)
            line1 = (
                f"{colorize('action', str(index))}. "
                f"{highlighted_album} - "
                f"{highlighted_title} - "
                f"{highlighted_artist} "
                f"(Match: {colorize(score_color, f'{score:.2f}')}, Sources: direct)"
            )
            print_(line1)
            
            # Show query basis on second line (dim)
            line2 = f"    Based on query: {album} - {title} - {artist}"
            print_(colorize("dim", line2))

        # Actions (compact single line)
        print_(colorize("text_error", "\nActions:"))
        print_(_render_actions())

        sel = input_options(("aBort", "Skip", "Enter", "Refresh"), numrange=(1, len(sorted_tracks)), default=1)
        if sel == "?":
            continue
        if sel in ("r", "R"):
            # Trigger index refresh and retry search
            return "refresh"
        break

    if sel in ("b", "B"):
        return None
    if sel in ("s", "S"):
        _store_negative_cache(cache, song_dict, original_query)
        return None
    if sel in ("e", "E"):
        return manual_track_search(backend, cache, matching_module, original_query)

    selected_track = sorted_tracks[sel - 1][0] if sel > 0 else None
    if selected_track:
        _cache_selection(cache, song_dict, selected_track, original_query)
    return selected_track


def _find_closest_match(
    song: Dict[str, str],
    tracks: List[Dict],
    matching_module
) -> List[Tuple[Dict, float]]:
    """Find closest matching track from a list."""
    scored = []
    for track in tracks:
        match_score = matching_module.plex_track_distance(song, track)
        scored.append((track, match_score.similarity))

    return sorted(scored, key=lambda x: x[1], reverse=True)


def review_candidate_confirmations(
    backend,
    cache,
    matching_module,
    candidates: List[Dict[str, Any]],
    current_song: Dict[str, str],
    current_cache_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Review queued candidates and prompt user for confirmation."""
    if not candidates:
        return {"action": "skip"}

    unique_candidates: Dict[Any, Dict] = {}
    for candidate in candidates:
        track = candidate.get("track")
        if not track:
            continue

        # Use backend_id/plex_ratingkey as primary key, fallback to title+artist+album
        rating_key = track.get("backend_id") or track.get("plex_ratingkey")
        if rating_key is not None:
            # CRITICAL: Normalize to int to ensure consistent key types
            # Plex ratingKey can be int or string depending on source
            try:
                rating_key = int(rating_key)
            except (ValueError, TypeError):
                # If conversion fails, use as-is (shouldn't happen with Plex IDs)
                pass
        
        if not rating_key:
            # Create a composite key from title, artist, album for deduplication
            title = (track.get("title") or "").lower().strip()
            artist = (track.get("artist") or "").lower().strip()
            album = (track.get("album") or "").lower().strip()
            rating_key = f"{title}|{artist}|{album}"
            
            # Skip if all fields are empty
            if not title and not artist and not album:
                continue
        
        # Debug log the rating key, type, and source
        candidate_source = candidate.get("source", "")
        candidate_sim = candidate.get("similarity", 0)
        rating_key_str = str(rating_key)[:50] if rating_key else "None"
        logger.debug(
            f"Processing candidate: key={rating_key_str} (type={type(rating_key).__name__}), "
            f"source={candidate_source}, sim={candidate_sim:.2f}"
        )
        
        # Additional debug: log current dict state
        logger.debug(f"  → Current unique_candidates keys: {list(unique_candidates.keys())[:5]}")  # First 5 only

        if rating_key in unique_candidates:
            # Found duplicate - merge sources and keep higher similarity
            existing = unique_candidates[rating_key]
            
            # Get the existing sources list (must get reference, not copy)
            if "sources" not in existing:
                existing["sources"] = [existing.get("source", "")]
            
            existing_sources = existing["sources"]
            new_source = candidate.get("source", "")
            
            logger.debug(
                f"  → Found existing with sources={existing_sources}, "
                f"adding source={new_source}"
            )
            
            if new_source and new_source not in existing_sources:
                existing_sources.append(new_source)

            # Keep the higher similarity score
            if candidate.get("similarity", 0) > existing.get("similarity", 0):
                existing["similarity"] = candidate["similarity"]
            
            logger.debug(
                f"  → MERGED duplicate: sources={existing_sources}, sim={existing['similarity']:.2f}"
            )
        else:
            # First time seeing this track
            candidate["sources"] = [candidate.get("source", "")]
            unique_candidates[rating_key] = candidate
            logger.debug(
                f"  → ADDED as new candidate (dict now has {len(unique_candidates)} entries)"
            )

    if not unique_candidates:
        return {"action": "skip"}
    
    # Debug: Log final deduplication results
    logger.debug(f"Deduplication complete: {len(candidates)} candidates → {len(unique_candidates)} unique")
    for key, cand in list(unique_candidates.items())[:3]:  # Log first 3 only
        logger.debug(
            f"  Final entry: key={key}, sources={cand.get('sources', [])}, sim={cand.get('similarity', 0):.2f}"
        )

    sorted_candidates = sorted(
        unique_candidates.values(),
        key=lambda x: x.get("similarity", 0),
        reverse=True
    )

    while True:
        # Display search query info
        query_artist = current_song.get('artist', '')
        query_title = current_song.get('title', '')
        query_album = current_song.get('album', '')
        query_info = f"{query_artist}|{query_album}|{query_title}"
        print_(colorize("dim", f"\nSearching for track info: {query_info}"))
        
        # Show first candidate as "Found track info"
        if sorted_candidates:
            track = sorted_candidates[0]["track"]
            found_info = f"{{'title': '{track.get('title', '')}', 'album': '{track.get('album', '')}', 'artist': '{track.get('artist', '')}'}}"
            print_(colorize("dim", f"Found track info: {found_info}"))
        
        # Display header
        print_(
            colorize("text_error", "\nReview candidate matches for: ")
            + colorize("text_highlight_minor", f"None - {query_artist}|{query_album}|{query_title}")
        )

        # Display candidates
        for index, candidate in enumerate(sorted_candidates, start=1):
            track = candidate["track"]
            similarity = candidate.get("similarity", 0)
            sources = candidate.get("sources", [])

            track_artist = track.get("artist", "")
            track_title = track.get("title", "")
            track_album = track.get("album", "")

            # Apply fuzzy highlighting to each field using WHOLE QUERY
            # This ensures terms are highlighted even if they're in the wrong query field
            # (e.g., YouTube titles like "Song | Artists | Album" parsed incorrectly)
            query_combined = f"{query_title or ''} {query_artist or ''} {query_album or ''}".strip()
            
            highlighted_album = highlight_matches(query_combined, track_album)
            highlighted_title = highlight_matches(query_combined, track_title)
            highlighted_artist = highlight_matches(query_combined, track_artist)

            # Determine match score color
            if similarity >= 0.8:
                score_color = "text_success"
            elif similarity >= 0.5:
                score_color = "text_warning"
            else:
                score_color = "text_error"

            # Format sources
            sources_str = ", ".join(sources) if sources else "direct"

            # Line 1: index. ALBUM - TITLE - ARTIST (Match: score, Sources: sources)
            line1 = (
                f"{colorize('action', str(index))}. "
                f"{highlighted_album} - "
                f"{highlighted_title} - "
                f"{highlighted_artist} "
                f"(Match: {colorize(score_color, f'{similarity:.2f}')}, Sources: {sources_str})"
            )
            print_(line1)
            
            # Line 2: Based on query (dim)
            line2 = f"    Based on query: {query_album} - {query_title} - {query_artist}"
            print_(colorize("dim", line2))

        # Actions (compact)
        print_(colorize("text_error", "\nActions:"))
        print_(_render_actions())

        sel = input_options(
            ("aBort", "Skip", "Enter", "Refresh"),
            numrange=(1, len(sorted_candidates)),
            default=1
        )
        if sel == "?":
            continue
        if sel in ("r", "R"):
            # Trigger index refresh and retry search
            return {"action": "refresh"}
        break

    if sel in ("b", "B"):
        return {"action": "abort"}
    if sel in ("s", "S"):
        return {"action": "skip"}
    if sel in ("e", "E"):
        return {
            "action": "manual",
            "original_song": current_song,
        }

    if isinstance(sel, int) and 1 <= sel <= len(sorted_candidates):
        candidate = sorted_candidates[sel - 1]
        return {
            "action": "selected",
            "track": candidate["track"],
            "similarity": candidate.get("similarity", 0),
            "cache_key": candidate.get("cache_key") or current_cache_key,
            "sources": candidate.get("sources", []),
            "original_song": candidate.get("song") or current_song,
        }

    return {"action": "skip"}

