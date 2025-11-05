"""Helpers for importing playlists into Plex."""

from __future__ import annotations

from beets import ui

from beetsplug.core.config import get_plexsync_config
from beetsplug.providers.gaana import import_gaana_playlist
from beetsplug.providers.youtube import import_yt_playlist, import_yt_search
from beetsplug.providers.tidal import import_tidal_playlist
from beetsplug.plex import smartplaylists


def import_playlist(plugin, playlist, playlist_url=None, listenbrainz=False):
    """Import a playlist into Plex using the plugin context."""
    if listenbrainz:
        try:
            from beetsplug.listenbrainz import ListenBrainzPlugin
        except ModuleNotFoundError:
            plugin._log.error("ListenBrainz plugin not installed")
            return

        try:
            lb = ListenBrainzPlugin()
        except Exception as exc:  # noqa: BLE001 - propagate details to log
            plugin._log.error("Unable to initialize ListenBrainz plugin. Error: {}", exc)
            return

        plugin._log.info("Importing weekly jams playlist")
        weekly_jams = lb.get_weekly_jams()
        plugin._log.info("Importing {} songs from Weekly Jams", len(weekly_jams))
        add_songs_to_plex(plugin, "Weekly Jams", weekly_jams)

        plugin._log.info("Importing weekly exploration playlist")
        weekly_exploration = lb.get_weekly_exploration()
        plugin._log.info(
            "Importing {} songs from Weekly Exploration", len(weekly_exploration)
        )
        add_songs_to_plex(plugin, "Weekly Exploration", weekly_exploration)
        return

    if playlist_url is None or ("http://" not in playlist_url and "https://" not in playlist_url):
        raise ui.UserError("Playlist URL not provided")

    if "apple" in playlist_url:
        songs = plugin.import_apple_playlist(playlist_url)
    elif "jiosaavn" in playlist_url:
        songs = plugin.import_jiosaavn_playlist(playlist_url)
    elif "gaana.com" in playlist_url:
        songs = import_gaana_playlist(playlist_url, plugin.cache)
    elif "spotify" in playlist_url:
        songs = plugin.import_spotify_playlist(plugin.get_playlist_id(playlist_url))
    elif "youtube" in playlist_url:
        songs = import_yt_playlist(playlist_url, plugin.cache)
    elif "tidal" in playlist_url:
        songs = import_tidal_playlist(playlist_url, plugin.cache)
    else:
        songs = []
        plugin._log.error("Playlist URL not supported")

    plugin._log.info("Importing {} songs from {}", len(songs), playlist_url)
    add_songs_to_plex(plugin, playlist, songs)


def add_songs_to_plex(plugin, playlist, songs, manual_search=None):
    """Add a list of songs to a Plex playlist via the plugin."""
    if manual_search is None:
        manual_search = get_plexsync_config("manual_search", bool, False)

    songs_to_process = list(songs or [])
    progress = plugin.create_progress_counter(
        len(songs_to_process),
        f"Matching Plex tracks for {playlist}",
        unit="song",
    )

    # Pre-queue background searches for all songs
    for song in songs_to_process:
        if hasattr(plugin, 'queue_background_search'):
            plugin.queue_background_search(song)
    
    song_list = []
    try:
        for song in songs_to_process:
            found = plugin.search_plex_song(song, manual_search)
            if found is not None:
                song_list.append(found)
            if progress is not None:
                progress.update()
    finally:
        if progress is not None:
            try:
                progress.close()
            except Exception:  # noqa: BLE001 - progress is optional feedback
                plugin._log.debug("Unable to close progress counter for playlist {}", playlist)

    if not song_list:
        plugin._log.warning("No songs found to add to playlist {}", playlist)
        return

    plugin._plex_add_playlist_item(song_list, playlist)


def import_search(plugin, playlist, search, limit=10):
    """Import search results into Plex for the given playlist."""
    plugin._log.info("Searching for {}", search)
    songs = list(import_yt_search(search, limit, plugin.cache) or [])
    progress = plugin.create_progress_counter(
        len(songs),
        f"Resolving search results for {playlist}",
        unit="song",
    )
    # Pre-queue background searches for all songs
    for song in songs:
        if hasattr(plugin, 'queue_background_search'):
            plugin.queue_background_search(song)
    
    song_list = []
    try:
        for song in songs:
            found = plugin.search_plex_song(song)
            if found is not None:
                song_list.append(found)
            if progress is not None:
                progress.update()
    finally:
        if progress is not None:
            try:
                progress.close()
            except Exception:  # noqa: BLE001 - best effort feedback
                plugin._log.debug("Unable to close search progress counter for playlist {}", playlist)
    plugin._plex_add_playlist_item(song_list, playlist)


def generate_imported_playlist(plugin, lib, playlist_config, plex_lookup=None):
    """Generate imported playlist from various sources based on config."""
    from datetime import datetime as _dt
    from beetsplug.core.config import get_config_value, get_plexsync_config
    from beetsplug.providers.m3u8 import import_m3u8_playlist
    from beetsplug.providers.http_post import import_post_playlist

    playlist_name = playlist_config.get("name", "Imported Playlist")
    sources = playlist_config.get("sources", [])
    max_tracks = playlist_config.get("max_tracks", None)
    import os
    log_file = os.path.join(plugin.config_dir, f"{playlist_name.lower().replace(' ', '_')}_import.log")
    
    with open(log_file, 'w', encoding='utf-8') as f:
        f.write(f"Import log for playlist: {playlist_name}\n")
        f.write(f"Import started at: {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("-" * 80 + "\n\n")
    
    defaults_cfg = get_plexsync_config(["playlists", "defaults"], dict, {})
    manual_search = get_config_value(
        playlist_config, defaults_cfg, "manual_search", get_plexsync_config("manual_search", bool, False)
    )
    clear_playlist = get_config_value(
        playlist_config, defaults_cfg, "clear_playlist", False
    )
    
    if not sources:
        plugin._log.warning("No sources defined for imported playlist {}", playlist_name)
        return
    
    plugin._log.info("Generating imported playlist {} from {} sources", playlist_name, len(sources))
    all_tracks = []
    source_progress = plugin.create_progress_counter(
        total=len(sources),
        desc=f"{playlist_name[:18]} src",
        unit="source",
    )
    
    try:
        for source in sources:
            try:
                tracks = []
                src_desc = None
                # String source (URL or file)
                if isinstance(source, str):
                    src_desc = source
                    low = source.lower()
                    if low.endswith('.m3u8'):
                        # Resolve relative path under config dir
                        if not os.path.isabs(source):
                            source = os.path.join(plugin.config_dir, source)
                        plugin._log.info("Importing from M3U8: {}", source)
                        tracks = import_m3u8_playlist(source, plugin.cache)
                    elif 'spotify' in low:
                        from beetsplug.providers.spotify import get_playlist_id as _get_pl_id
                        plugin._log.info("Importing from Spotify URL")
                        tracks = plugin.import_spotify_playlist(_get_pl_id(source))
                    elif 'jiosaavn' in low:
                        plugin._log.info("Importing from JioSaavn URL")
                        tracks = plugin.import_jiosaavn_playlist(source)
                    elif 'apple' in low:
                        plugin._log.info("Importing from Apple Music URL")
                        tracks = plugin.import_apple_playlist(source)
                    elif 'gaana' in low:
                        plugin._log.info("Importing from Gaana URL")
                        tracks = import_gaana_playlist(source, plugin.cache)
                    elif 'youtube' in low:
                        plugin._log.info("Importing from YouTube URL")
                        tracks = import_yt_playlist(source, plugin.cache)
                    elif 'tidal' in low:
                        plugin._log.info("Importing from Tidal URL")
                        tracks = import_tidal_playlist(source, plugin.cache)
                    else:
                        plugin._log.warning("Unsupported string source: {}", source)
                # Dict source (typed)
                elif isinstance(source, dict):
                    source_type = source.get("type")
                    src_desc = source_type or "Unknown"
                    if source_type == "Apple Music":
                        plugin._log.info("Importing from Apple Music: {}", source.get("name", ""))
                        tracks = plugin.import_apple_playlist(source.get("url", ""))
                    elif source_type == "JioSaavn":
                        plugin._log.info("Importing from JioSaavn: {}", source.get("name", ""))
                        tracks = plugin.import_jiosaavn_playlist(source.get("url", ""))
                    elif source_type == "Gaana":
                        plugin._log.info("Importing from Gaana: {}", source.get("name", ""))
                        tracks = import_gaana_playlist(source.get("url", ""), plugin.cache)
                    elif source_type == "Spotify":
                        plugin._log.info("Importing from Spotify: {}", source.get("name", ""))
                        from beetsplug.providers.spotify import get_playlist_id as _get_pl_id
                        tracks = plugin.import_spotify_playlist(_get_pl_id(source.get("url", "")))
                    elif source_type == "YouTube":
                        plugin._log.info("Importing from YouTube: {}", source.get("name", ""))
                        tracks = import_yt_playlist(source.get("url", ""), plugin.cache)
                    elif source_type == "Tidal":
                        plugin._log.info("Importing from Tidal: {}", source.get("name", ""))
                        tracks = import_tidal_playlist(source.get("url", ""), plugin.cache)
                    elif source_type == "M3U8":
                        fp = source.get("filepath", "")
                        if fp and not os.path.isabs(fp):
                            fp = os.path.join(plugin.config_dir, fp)
                        plugin._log.info("Importing from M3U8: {}", fp)
                        tracks = import_m3u8_playlist(fp, plugin.cache)
                    elif source_type == "POST":
                        plugin._log.info("Importing from POST endpoint")
                        tracks = import_post_playlist(source, plugin.cache)
                    else:
                        plugin._log.warning("Unsupported source type: {}", source_type)
                else:
                    src_desc = str(type(source))
                    plugin._log.warning("Invalid source format: {}", src_desc)

                if tracks:
                    plugin._log.info("Imported {} tracks from {}", len(tracks), src_desc)
                    all_tracks.extend(tracks)
            except Exception as e:
                plugin._log.error("Error importing from {}: {}", src_desc or "Unknown", e)
                continue
            finally:
                if source_progress is not None:
                    try:
                        source_progress.update()
                    except Exception:
                        plugin._log.debug("Failed to update source progress for {}", playlist_name)
    finally:
        if source_progress is not None:
            try:
                source_progress.close()
            except Exception:
                plugin._log.debug("Failed to close source progress for {}", playlist_name)
    
    unique_tracks = []
    seen = set()
    for t in all_tracks:
        # Some sources may set explicit None values; normalize to empty strings before lowercasing
        key = (
            (t.get('title') or '').lower(),
            (t.get('artist') or '').lower(),
            (t.get('album') or '').lower(),
        )
        if key not in seen:
            seen.add(key)
            unique_tracks.append(t)
    
    plugin._log.info("Found {} unique tracks across sources", len(unique_tracks))
    
    matched_songs = []
    match_progress = plugin.create_progress_counter(
        total=len(unique_tracks),
        desc=f"{playlist_name[:18]} match",
        unit="track",
    )
    
    # Pre-queue background searches for all songs
    for song in unique_tracks:
        if hasattr(plugin, 'queue_background_search'):
            plugin.queue_background_search(song)
    
    try:
        for song in unique_tracks:
            found = plugin.search_plex_song(song, manual_search)
            if found is not None:
                matched_songs.append(found)
            if match_progress is not None:
                try:
                    match_progress.update()
                except Exception:
                    plugin._log.debug("Failed to update match progress for {}", playlist_name)
    finally:
        if match_progress is not None:
            try:
                match_progress.close()
            except Exception:
                plugin._log.debug("Failed to close match progress for {}", playlist_name)
    
    plugin._log.info("Matched {} tracks in Plex", len(matched_songs))
    
    if max_tracks:
        matched_songs = matched_songs[:max_tracks]
    
    # Apply filters to matched songs if filters are defined in the playlist config
    filters = playlist_config.get("filters", {})
    if filters:
        matched_songs = smartplaylists.apply_playlist_filters(plugin, matched_songs, filters)
        
    unique_matched = []
    seen_keys = set()
    for track in matched_songs:
        key = getattr(track, 'ratingKey', None)
        if key and key not in seen_keys:
            seen_keys.add(key)
            unique_matched.append(track)
    
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write("\nImport Summary:\n")
        f.write(f"Total tracks fetched from sources: {len(all_tracks)}\n")
        f.write(f"Unique tracks after de-duplication: {len(unique_tracks)}\n")
        if filters:
            f.write(f"Tracks after applying filters: {len(matched_songs)}\n")
        f.write(f"Tracks matched and added: {len(unique_matched)}\n")
        f.write(f"\nImport completed at: {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    plugin._log.info("Found {} unique tracks after filtering (see {} for details)", len(unique_matched), log_file)
    
    if clear_playlist:
        try:
            plugin._plex_clear_playlist(playlist_name)
            plugin._log.info("Cleared existing playlist {}", playlist_name)
        except Exception:
            plugin._log.debug("No existing playlist {} found", playlist_name)
    
    if unique_matched:
        plugin._plex_add_playlist_item(unique_matched, playlist_name)
        plugin._log.info("Successfully created playlist {} with {} tracks", playlist_name, len(unique_matched))
    else:
        plugin._log.warning("No tracks remaining after filtering for {}", playlist_name)