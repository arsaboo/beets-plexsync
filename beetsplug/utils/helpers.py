from __future__ import annotations

import re

from beets import ui


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
        return ui.colorize('text_success', target)

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
                    ui.colorize('text_success', original_target_words[i])
                )
                word_matched = True
                break

        if not word_matched:
            highlighted_words.append(original_target_words[i])

    return ' '.join(highlighted_words)
