"""Helpers for importing playlists into Plex."""

from __future__ import annotations

from beets import ui

from beetsplug.core.config import get_plexsync_config
from beetsplug.providers.gaana import import_gaana_playlist
from beetsplug.providers.youtube import import_yt_playlist, import_yt_search
from beetsplug.providers.tidal import import_tidal_playlist


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
