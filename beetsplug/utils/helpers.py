from __future__ import annotations

import math
import os
import re
from datetime import datetime

from beets.util.color import colorize


def plex_key(value):
    """Normalize a Plex rating key for cross-source lookups.

    beets stores ``plex_ratingkey`` as a string while plexapi returns ints, so
    the two sides only meet once reduced to one type. Numeric keys become ints,
    which plexapi's ``fetchItem`` requires; non-numeric keys (GUIDs) keep their
    text form. ``'0'``/``'0.0'`` are the placeholders beets uses for an unsynced
    item. Returns None when there is no usable key.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in ("0", "0.0"):
        return None
    if text.isdigit():
        return int(text)
    return text


def parse_timestamp(value):
    """Coerce a stored Plex timestamp to epoch seconds, or None when absent.

    beets' ``DateType`` writes these columns as ``'YYYY-MM-DD HH:MM:SS'`` text
    but hands them back as plain strings, while direct assignments and older
    rows hold epoch numbers. Every form has to parse, or a track with real play
    history reads as never played.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        stamp = float(value)
        return stamp if stamp > 0 and math.isfinite(stamp) else None
    text = str(value).strip()
    if not text or text in ("0", "0.0"):
        return None
    try:
        stamp = float(text)
    except ValueError:
        try:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return stamp if stamp > 0 and math.isfinite(stamp) else None


def plex_media_path(track):
    """Absolute path of a Plex track's media file, or None when unavailable."""
    try:
        return track.media[0].parts[0].file
    except (AttributeError, IndexError, TypeError):
        return None


def _as_text_path(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        return os.fsdecode(value)
    return str(value)


def same_media_file(beets_path, plex_path, library_dir=None):
    """Compare a beets file path with a Plex media part path.

    Both sides are normalized. A beets path relative to ``library_dir`` also
    matches the Plex path's tail, so one file seen through different mount roots
    compares equal. A missing path equals nothing.
    """
    beets_path = _as_text_path(beets_path)
    plex_path = _as_text_path(plex_path)
    if not beets_path or not plex_path:
        return False
    beets_path = os.path.normpath(beets_path)
    plex_path = os.path.normpath(plex_path)
    if beets_path == plex_path:
        return True
    if library_dir:
        lib = os.path.normpath(_as_text_path(library_dir))
        if beets_path.startswith(lib + os.sep):
            rel = beets_path[len(lib) + 1:]
        elif not os.path.isabs(beets_path):
            rel = beets_path
        else:
            rel = None
        if rel:
            return plex_path.endswith(os.sep + rel)
    return False


def _folded(value):
    return str(value or "").casefold().strip()


def media_identity(item, plex_track, library_dir=None):
    """Classify a Plex track against a beets item's own file.

    Returns ``'same'`` (identical path), ``'moved'`` (same filename and album
    under a different directory, i.e. a rename Plex has not rescanned),
    ``'different'`` (another copy of the song, such as a compilation) or
    ``'unknown'`` (no path on either side to compare).

    File paths are the only signal that cannot be satisfied by a different copy
    of the same song, so callers use this to decide whether Plex ratings and
    play history describe the item in front of them.
    """
    plex_file = plex_media_path(plex_track)
    beets_path = _as_text_path(getattr(item, "path", None))
    if not plex_file or not beets_path:
        return "unknown"
    if same_media_file(beets_path, plex_file, library_dir):
        return "same"

    beets_name = os.path.basename(os.path.normpath(beets_path))
    if not beets_name or beets_name != os.path.basename(os.path.normpath(plex_file)):
        return "different"
    plex_album = _folded(
        getattr(plex_track, "parentTitle", None) or getattr(plex_track, "album", None)
    )
    if plex_album and plex_album == _folded(getattr(item, "album", None)):
        return "moved"
    return "different"


def parse_title(title_orig):
    """Parse title to separate movie soundtrack information."""
    if '(From "' in title_orig:
        title = re.sub(r"\(From.*\)", "", title_orig)
        album = re.sub(r'^[^"]+"|(?<!^)"[^"]+"|"[^"]+$', "", title_orig)
    elif '[From "' in title_orig:
        title = re.sub(r"\[From.*\]", "", title_orig)
        album = re.sub(r'^[^"]+"|(?<!^)"[^"]+$', "", title_orig)
    else:
        title = title_orig
        album = ""
    return title.strip(), album.strip()


def clean_album_name(album_orig):
    """Clean album name by removing common suffixes and extracting movie name."""
    album_orig = (
        album_orig.replace("(Original Motion Picture Soundtrack)", "")
        .replace("- Hindi", "")
        .strip()
    )
    if '(From "' in album_orig:
        album = re.sub(r'^[^"]+"|(?<!^)"[^"]+$', "", album_orig)
    elif '[From "' in album_orig:
        album = re.sub(r'^[^"]+"|(?<!^)"[^"]+$', "", album_orig)
    else:
        album = album_orig
    return album


def highlight_matches(source: str | None, target: str | None) -> str:
    """Highlight exact matching parts between source and target strings."""
    if source is None or target is None:
        return target or "Unknown"

    source_words = source.lower().split() if source else []
    target_words = target.lower().split() if target else []

    if source and target and source.lower() == target.lower():
        return colorize('text_success', target)

    from difflib import SequenceMatcher

    def fuzzy_score(a: str, b: str) -> float:
        return SequenceMatcher(None, a.lower(), b.lower()).ratio()

    highlighted_words: list[str] = []
    original_target_words = target.split()
    for i, target_word in enumerate(target_words):
        word_matched = False
        clean_target_word = re.sub(r'[^\w]', '', target_word)

        for source_word in source_words:
            clean_source_word = re.sub(r'[^\w]', '', source_word)
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
            highlighted_words.append(original_target_words[i])

    return ' '.join(highlighted_words)
