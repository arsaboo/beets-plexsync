from __future__ import annotations

"""Shared helpers for interactive manual Plex searches."""

from typing import Iterable, Dict, List, Optional

from beets import ui
from beets.ui import input_, print_

from beetsplug.utils.helpers import highlight_matches
from beetsplug.core.matching import get_fuzzy_score


def _render_actions() -> str:
    return (
        ui.colorize('action', 'a') + ui.colorize('text_highlight_minor', ': Abort') + '   '
        + ui.colorize('action', 's') + ui.colorize('text_highlight_minor', ': Skip') + '   '
        + ui.colorize('action', 'e') + ui.colorize('text_highlight_minor', ': Enter manual search') + '\n'
    )


def review_candidate_confirmations(
    plugin,
    queued_candidates: List[Dict[str, object]],
    fallback_song: Optional[Dict[str, str]] = None,
    *,
    current_cache_key: Optional[str] = None,
) -> Dict[str, object]:
    """Present queued confirmation candidates and return the user's choice."""
    if not queued_candidates:
        return {"action": "skip"}

    aggregated: List[Dict[str, object]] = []
    dedupe_map: Dict[object, Dict[str, object]] = {}

    for candidate in queued_candidates:
        track = candidate.get("track")
        if track is None:
            continue

        try:
            rating_key = getattr(track, "ratingKey", None)
        except Exception:  # noqa: BLE001 - tolerate unexpected track objects
            rating_key = None
        dedupe_key = rating_key if rating_key is not None else id(track)

        similarity = float(candidate.get("similarity", 0.0) or 0.0)
        cache_key = candidate.get("cache_key")
        source = candidate.get("source") or "candidate"

        entry = dedupe_map.get(dedupe_key)
        if entry is None:
            entry = {
                "track": track,
                "similarity": similarity,
                "sources": {source},
                "cache_keys": {cache_key} if cache_key else set(),
                "candidates": [candidate],
                "original_songs": [],
            }
            dedupe_map[dedupe_key] = entry
            aggregated.append(entry)
        else:
            entry["similarity"] = max(entry["similarity"], similarity)
            entry["sources"].add(source)
            if cache_key:
                entry["cache_keys"].add(cache_key)
            entry["candidates"].append(candidate)

        original_song = candidate.get("song")
        if isinstance(original_song, dict):
            entry["original_songs"].append(original_song)

    if not aggregated:
        return {"action": "skip"}

    aggregated.sort(key=lambda item: item.get("similarity", 0.0), reverse=True)

    reference_song = fallback_song or {}
    ref_title = reference_song.get("title", "")
    ref_album = reference_song.get("album", "Unknown")
    ref_artist = reference_song.get("artist", "")

    header = (
        ui.colorize('text_highlight', '\nReview candidate matches for: ')
        + ui.colorize('text_highlight_minor', f"{ref_album} - {ref_title} - {ref_artist}")
    )
    print_(header)

    for index, entry in enumerate(aggregated, start=1):
        track = entry["track"]
        try:
            track_title = getattr(track, "title", "") or "<unknown>"
            track_album = getattr(track, "parentTitle", "") or "<unknown>"
            track_artist = getattr(track, "originalTitle", None) or track.artist().title
        except Exception:  # noqa: BLE001 - tolerate Plex quirks
            track_title = getattr(track, "title", "") or "<unknown>"
            track_album = getattr(track, "parentTitle", "") or "<unknown>"
            track_artist = "<unknown>"

        query_song = entry["original_songs"][0] if entry["original_songs"] else reference_song
        query_title = (query_song or {}).get("title", "")
        query_album = (query_song or {}).get("album", "")
        query_artist = (query_song or {}).get("artist", "")

        highlighted_title = highlight_matches(query_title, track_title)
        highlighted_album = highlight_matches(query_album or ref_album, track_album)
        highlighted_artist = highlight_matches(query_artist or ref_artist, track_artist)

        similarity = entry.get("similarity", 0.0) or 0.0
        if similarity >= 0.8:
            score_color = 'text_success'
        elif similarity >= 0.5:
            score_color = 'text_warning'
        else:
            score_color = 'text_error'

        sources = ", ".join(sorted(entry.get("sources", []))) or "candidate"
        print_(
            f"{ui.colorize('action', str(index))}. {highlighted_album} - {highlighted_title} - "
            f"{highlighted_artist} (Match: {ui.colorize(score_color, f'{similarity:.2f}')}, "
            f"Sources: {ui.colorize('text_highlight_minor', sources)})"
        )

        if query_song and (
            query_title != ref_title or query_album != ref_album or query_artist != ref_artist
        ):
            print_(
                ui.colorize(
                    'text_highlight_minor',
                    f"   Based on query: {query_album or 'Unknown'} - {query_title} - {query_artist}",
                )
            )

    print_(ui.colorize('text_highlight', '\nActions:'))
    print_(ui.colorize('text_highlight_minor', '  #: Select match by number'))
    print_(_render_actions())

    selection = ui.input_options(
        ("aBort", "Skip", "Enter manual search"),
        numrange=(1, len(aggregated)),
        default=1,
    )

    if isinstance(selection, int) and selection > 0:
        entry = aggregated[selection - 1]
        cache_keys = entry.get("cache_keys") or set()
        chosen_cache_key = None
        if current_cache_key and current_cache_key in cache_keys:
            chosen_cache_key = current_cache_key
        elif cache_keys:
            # Stable choice: prefer cache key from first candidate appended.
            for candidate in entry.get("candidates", []):
                candidate_cache_key = candidate.get("cache_key")
                if candidate_cache_key:
                    chosen_cache_key = candidate_cache_key
                    break
        if not chosen_cache_key:
            chosen_cache_key = current_cache_key

        chosen_candidate = None
        for candidate in entry.get("candidates", []):
            candidate_cache_key = candidate.get("cache_key")
            if candidate_cache_key == chosen_cache_key:
                chosen_candidate = candidate
                break
        if chosen_candidate is None and entry.get("candidates"):
            chosen_candidate = entry["candidates"][0]

        return {
            "action": "selected",
            "track": entry["track"],
            "cache_key": chosen_cache_key,
            "similarity": entry.get("similarity", 0.0),
            "sources": sorted(entry.get("sources", [])),
            "original_song": (chosen_candidate or {}).get("song")
            if isinstance(chosen_candidate, dict)
            else None,
        }

    if selection in ("b", "B"):
        return {"action": "abort"}
    if selection in ("s", "S"):
        return {"action": "skip"}
    if selection in ("e", "E"):
        return {"action": "manual"}

    return {"action": "skip"}


def handle_manual_search(plugin, sorted_tracks, song, original_query=None):
    """Display the manual selection UI and return the chosen Plex track."""
    source_title = song.get("title", "")
    source_album = song.get("album", "Unknown")
    source_artist = song.get("artist", "")

    header = (
        ui.colorize('text_highlight', '\nChoose candidates for: ')
        + ui.colorize('text_highlight_minor', f"{source_album} - {source_title} - {source_artist}")
    )
    print_(header)

    for index, (track, score) in enumerate(sorted_tracks, start=1):
        track_artist = getattr(track, 'originalTitle', None) or track.artist().title
        highlighted_title = highlight_matches(source_title, track.title)
        highlighted_album = highlight_matches(source_album, track.parentTitle)
        highlighted_artist = highlight_matches(source_artist, track_artist)

        if score >= 0.8:
            score_color = 'text_success'
        elif score >= 0.5:
            score_color = 'text_warning'
        else:
            score_color = 'text_error'

        print_(
            f"{index}. {highlighted_album} - {highlighted_title} - {highlighted_artist} "
            f"(Match: {ui.colorize(score_color, f'{score:.2f}')})"
        )

    print_(ui.colorize('text_highlight', '\nActions:'))
    print_(ui.colorize('text_highlight_minor', '  #: Select match by number'))
    print_(_render_actions())

    sel = ui.input_options(("aBort", "Skip", "Enter"), numrange=(1, len(sorted_tracks)), default=1)

    if sel in ("b", "B"):
        return None
    if sel in ("s", "S"):
        _store_negative_cache(plugin, song, original_query)
        return None
    if sel in ("e", "E"):
        return manual_track_search(plugin, original_query if original_query is not None else song)

    selected_track = sorted_tracks[sel - 1][0] if sel > 0 else None
    if selected_track:
        _cache_selection(plugin, song, selected_track, original_query)
    return selected_track


def manual_track_search(plugin, original_query=None):
    """Interactively search for a Plex track."""
    print_(ui.colorize('text_highlight', '\nManual Search'))
    print_('Enter search criteria (empty to skip):')

    title = input_(ui.colorize('text_highlight_minor', 'Title: ')).strip()
    album = input_(ui.colorize('text_highlight_minor', 'Album: ')).strip()
    artist = input_(ui.colorize('text_highlight_minor', 'Artist: ')).strip()

    plugin._log.debug("Searching with title='{}', album='{}', artist='{}'", title, album, artist)

    tracks = _run_manual_search_queries(plugin, title, album, artist)
    if not tracks:
        plugin._log.info("No matching tracks found")
        return None

    filtered_tracks = _filter_tracks(plugin, tracks, title, album, artist)
    if not filtered_tracks:
        plugin._log.info("No matching tracks found after filtering")
        return None

    song_dict = {
        "title": title or "",
        "album": album or "",
        "artist": artist or "",
    }

    sorted_tracks = plugin.find_closest_match(song_dict, filtered_tracks)
    header = (
        ui.colorize('text_highlight', '\nChoose candidates for: ')
        + ui.colorize('text_highlight_minor', f"{album} - {title} - {artist}")
    )
    print_(header)

    for index, (track, score) in enumerate(sorted_tracks, start=1):
        track_artist = getattr(track, 'originalTitle', None) or track.artist().title
        highlighted_title = highlight_matches(title, track.title)
        highlighted_album = highlight_matches(album, track.parentTitle)
        highlighted_artist = highlight_matches(artist, track_artist)

        if score >= 0.8:
            score_color = 'text_success'
        elif score >= 0.5:
            score_color = 'text_warning'
        else:
            score_color = 'text_error'

        print_(
            f"{ui.colorize('action', str(index))}. {highlighted_album} - {highlighted_title} - "
            f"{highlighted_artist} (Match: {ui.colorize(score_color, f'{score:.2f}')})"
        )

    print_(ui.colorize('text_highlight', '\nActions:'))
    print_(ui.colorize('text_highlight_minor', '  #: Select match by number'))
    print_(_render_actions())

    sel = ui.input_options(("aBort", "Skip", "Enter"), numrange=(1, len(sorted_tracks)), default=1)

    if sel in ("b", "B"):
        return None
    if sel in ("s", "S"):
        _store_negative_cache(plugin, song_dict, original_query)
        return None
    if sel in ("e", "E"):
        return manual_track_search(plugin, original_query)

    selected_track = sorted_tracks[sel - 1][0] if sel > 0 else None
    if selected_track:
        _cache_selection(plugin, song_dict, selected_track, original_query)
    return selected_track


def _run_manual_search_queries(plugin, title: str, album: str, artist: str):
    tracks = []
    try:
        if album and any(x in album.lower() for x in ('movie', 'soundtrack', 'original')):
            tracks = plugin.music.searchTracks(**{"album.title": album}, limit=100)
            plugin._log.debug("Album-first search found {} tracks", len(tracks))

        if not tracks and album and title:
            tracks = plugin.music.searchTracks(
                **{"album.title": album, "track.title": title},
                limit=100,
            )
            plugin._log.debug("Combined album-title search found {} tracks", len(tracks))

        if not tracks and album:
            tracks = plugin.music.searchTracks(**{"album.title": album}, limit=100)
            plugin._log.debug("Album-only search found {} tracks", len(tracks))

        if not tracks and title:
            tracks = plugin.music.searchTracks(**{"track.title": title}, limit=100)
            plugin._log.debug("Title-only search found {} tracks", len(tracks))

        if not tracks and artist:
            tracks = plugin.music.searchTracks(**{"artist.title": artist}, limit=100)
            plugin._log.debug("Artist-only search found {} tracks", len(tracks))
    except Exception as exc:
        plugin._log.error("Error during manual search query: {}", exc)
        tracks = []
    return tracks


def _filter_tracks(plugin, tracks: Iterable, title: str, album: str, artist: str):
    filtered = []
    for track in tracks:
        track_artist = getattr(track, 'originalTitle', None) or track.artist().title
        track_album = track.parentTitle
        track_title = track.title

        plugin._log.debug("Considering track: {} - {} - {}", track_album, track_title, track_artist)

        title_match = not title or get_fuzzy_score(title.lower(), track_title.lower()) > 0.4
        album_match = not album or get_fuzzy_score(album.lower(), track_album.lower()) > 0.4

        artist_match = True
        if artist:
            track_artists = {a.strip().lower() for a in track_artist.split(',')}
            search_artists = {a.strip().lower() for a in artist.split(',')}
            common_artists = track_artists.intersection(search_artists)
            total_artists = track_artists.union(search_artists)
            artist_score = len(common_artists) / len(total_artists) if total_artists else 0
            artist_match = artist_score >= 0.3

        perfect_album = album and track_album and album.lower() == track_album.lower()
        strong_title = title and get_fuzzy_score(title.lower(), track_title.lower()) > 0.8
        standard_match = title_match and album_match and artist_match

        if perfect_album or strong_title or standard_match:
            filtered.append(track)
            plugin._log.debug(
                "Matched: {} - {} - {} (Perfect album: {}, Strong title: {}, Standard: {})",
                track_album,
                track_title,
                track_artist,
                perfect_album,
                strong_title,
                standard_match,
            )
    return filtered


def _store_negative_cache(plugin, song, original_query=None):
    plugin._log.debug("User skipped, storing negative cache result.")
    query = None
    if original_query and original_query.get('title') and original_query['title'].strip():
        query = original_query
    elif song.get('title') and song['title'].strip():
        query = song

    if query:
        cache_key = plugin.cache._make_cache_key(query)
        plugin._cache_result(cache_key, None)
    else:
        plugin._log.debug("No suitable query to store negative cache against for skip.")


def _cache_selection(plugin, song, track, original_query=None):
    """Cache the manual selection result where appropriate."""

    cached_original = False
    if original_query:
        original_key = plugin.cache._make_cache_key(original_query)
        plugin._log.debug("Caching result for original query key: {}", original_query)
        plugin._cache_result(original_key, track)
        cached_original = True

    if not cached_original:
        plugin._log.debug(
            "Skipping cache write for manual search input: {} (no original query)", song
        )
