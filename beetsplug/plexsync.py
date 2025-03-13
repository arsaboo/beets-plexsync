"""Update and sync Plex music library.

Plex users enter the Plex Token to enable updating.
Put something like the following in your config.yaml to configure:
    plex:
        host: localhost
        token: token
"""

import asyncio
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import List

import confuse
import requests
from beets import config, ui
from beets.dbcore import types
from beets.dbcore.query import MatchQuery
from beets.library import Item, DateType
from beets.plugins import BeetsPlugin
from beets.ui import input_, print_
from plexapi import exceptions
from plexapi.server import PlexServer
from pydantic import BaseModel, Field
from requests.exceptions import ConnectionError, ContentDecodingError

from beetsplug.caching import Cache
from beetsplug.llm import search_track_info
from beetsplug.matching import plex_track_distance, clean_string
from beetsplug.playlist_handlers import (
    import_spotify_playlist, process_spotify_track, import_apple_playlist,
    import_jiosaavn_playlist, import_m3u8_playlist, import_post_playlist,
    add_songs_to_plex, _plex_add_playlist_item, _plex_remove_playlist_item,
    _plex_clear_playlist, _plex_playlist_to_collection, _plex_import_playlist,
    _plex_import_search, _plex2spotify, add_tracks_to_spotify_playlist,
    get_playlist_id, get_playlist_tracks, authenticate_spotify, process_import_logs
)
from beetsplug.smart_playlists import (
    get_preferred_attributes, get_config_value, calculate_rating_score,
    calculate_last_played_score, calculate_play_count_score, calculate_track_score,
    select_tracks_weighted, calculate_playlist_proportions, validate_filter_config,
    _apply_exclusion_filters, _apply_inclusion_filters, apply_playlist_filters,
    get_filtered_library_tracks, generate_daily_discovery, generate_forgotten_gems,
    generate_imported_playlist, plex_smartplaylists  # Changed from _plex_smartplaylists
)
from beetsplug.utils import (
    clean_string, get_fuzzy_score, clean_text_for_matching,
    calculate_string_similarity, calculate_artist_similarity, ensure_float,
    parse_title, clean_album_name, clean_title, get_color_for_score
)


class Song(BaseModel):
    title: str
    artist: str
    album: str
    year: str = Field(description="Year of release")


class SongRecommendations(BaseModel):
    songs: List[Song]


class PlexSync(BeetsPlugin):
    """Define plexsync class."""

    data_source = "Plex"

    item_types = {
        "plex_guid": types.STRING,
        "plex_ratingkey": types.INTEGER,
        "plex_userrating": types.FLOAT,
        "plex_skipcount": types.INTEGER,
        "plex_viewcount": types.INTEGER,
        "plex_lastviewedat": DateType(),
        "plex_lastratedat": DateType(),
        "plex_updated": DateType(),
    }

    class dotdict(dict):
        """dot.notation access to dictionary attributes"""

        __getattr__ = dict.get
        __setattr__ = dict.__setitem__
        __delattr__ = dict.__delitem__

    def __init__(self):
        """Initialize plexsync plugin."""
        super().__init__()

        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "DNT": "1",  # Do Not Track Request Header
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
        }

        # Add event loop initialization
        self.loop = None

        self.config_dir = config.config_dir()
        self.llm_client = None
        self.search_llm = None

        # Initialize cache with plugin instance reference
        cache_path = os.path.join(self.config_dir, 'plexsync_cache.db')
        self.cache = Cache(cache_path, self)

        # Call the setup methods
        try:
            self.setup_llm()
            if config["plexsync"]["use_llm_search"].get(bool):
                self.search_llm = self.llm_client  # Use llm_client directly
        except Exception as e:
            self._log.error("Failed to set up LLM client: {}", e)
            self.llm_client = None
            self.search_llm = None

        # Adding defaults.
        config["plex"].add(
            {
                "host": "localhost",
                "port": 32400,
                "token": "",
                "library_name": "Music",
                "secure": False,
                "ignore_cert_errors": False,
            }
        )

        config["plexsync"].add(
            {
                "tokenfile": "spotify_plexsync.json",
                "manual_search": False,
                "max_tracks": 20,  # Maximum number of tracks for Daily Discovery
                "exclusion_days": 30,  # Days to exclude recently played tracks
                "history_days": 15,  # Days to look back for base tracks
                "discovery_ratio": 30,  # Percentage of discovery tracks (0-100)
                "use_llm_search": False,  # Enable/disable LLM search cleaning
            }
        )
        self.plexsync_token = config["plexsync"]["tokenfile"].get(
            confuse.Filename(in_app_dir=True)
        )

        # add LLM defaults
        config["llm"].add(
            {
                "api_key": "",
                "model": "gpt-3.5-turbo",
                "base_url": "",  # Optional, for other providers
                "search": {
                    "provider": "ollama",
                    "api_key": "",  # Will use base key if empty
                    "base_url": "http://192.168.2.162:3006/api/search",  # Override base_url for search
                    "model": "qwen2.5:72b-instruct",  # Override model for search
                    "embedding_model": "snowflake-arctic-embed2:latest"  # Embedding model
                }
            }
        )

        config["llm"]["api_key"].redact = True

        config["plex"]["token"].redact = True
        baseurl = (
            "http://"
            + config["plex"]["host"].get()
            + ":"
            + str(config["plex"]["port"].get())
        )
        try:
            self.plex = PlexServer(baseurl, config["plex"]["token"].get())
        except exceptions.Unauthorized:
            raise ui.UserError("Plex authorization failed")
        try:
            self.music = self.plex.library.section(config["plex"]["library_name"].get())
        except exceptions.NotFound:
            raise ui.UserError(
                f"{config['plex']['library_name']} \
                library not found"
            )
        self.register_listener("database_change", self.listen_for_db_change)

    def get_event_loop(self):
        """Get or create an event loop."""
        if self.loop is None or self.loop.is_closed():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
        return self.loop

    def listen_for_db_change(self, lib, model):
        """Listens for beets db change and register the update for the end."""
        self.register_listener("cli_exit", self._plexupdate)

    def commands(self):
        """Add beet UI commands to interact with Plex."""
        plexupdate_cmd = ui.Subcommand(
            "plexupdate", help=f"Update {self.data_source} library"
        )

        def func(lib, opts, args):
            self._plexupdate()

        plexupdate_cmd.func = func

        # plexsync command
        sync_cmd = ui.Subcommand("plexsync", help="fetch track attributes from Plex")
        sync_cmd.parser.add_option(
            "-f",
            "--force",
            dest="force_refetch",
            action="store_true",
            default=False,
            help="re-sync Plex data when already present",
        )

        def func_sync(lib, opts, args):
            items = lib.items(ui.decargs(args))
            self._fetch_plex_info(items, ui.should_write(), opts.force_refetch)

        sync_cmd.func = func_sync

        # plexplaylistadd command
        playlistadd_cmd = ui.Subcommand(
            "plexplaylistadd", help="add tracks to Plex playlist"
        )
        playlistadd_cmd.parser.add_option(
            "-m", "--playlist", default="Beets", help="add playlist to Plex"
        )

        def func_playlist_add(lib, opts, args):
            items = lib.items(ui.decargs(args))
            self._plex_add_playlist_item(items, opts.playlist)

        playlistadd_cmd.func = func_playlist_add

        # plexplaylistremove command
        playlistrem_cmd = ui.Subcommand(
            "plexplaylistremove", help="Plex playlist to edit"
        )
        playlistrem_cmd.parser.add_option(
            "-m", "--playlist", default="Beets", help="Plex playlist to edit"
        )

        def func_playlist_rem(lib, opts, args):
            items = lib.items(ui.decargs(args))
            self._plex_remove_playlist_item(items, opts.playlist)

        playlistrem_cmd.func = func_playlist_rem

        # plexsyncrecent command - instead of using the plexsync command which
        # can be slow, we can use the plexsyncrecent command to update info
        # for tracks played in the last X days.
        syncrecent_cmd = ui.Subcommand(
            "plexsyncrecent", help="Sync recently played tracks"
        )
        syncrecent_cmd.parser.add_option(
            "--days", default=7, help="Number of days to be synced"
        )

        def func_sync_recent(lib, opts, args):
            self._update_recently_played(lib, opts.days)

        syncrecent_cmd.func = func_sync_recent

        # plexplaylistimport command
        playlistimport_cmd = ui.Subcommand(
            "plexplaylistimport", help="import playlist in to Plex"
        )

        playlistimport_cmd.parser.add_option(
            "-m",
            "--playlist",
            default="Beets",
            help="name of the playlist to be added in Plex",
        )
        playlistimport_cmd.parser.add_option(
            "-u",
            "--url",
            default="",
            help="playlist URL to be imported in Plex",
        )
        playlistimport_cmd.parser.add_option(
            "-l",
            "--listenbrainz",
            action="store_true",
            help="use ListenBrainz as input option",
        )

        def func_playlist_import(lib, opts, args):
            self._plex_import_playlist(opts.playlist, opts.url, opts.listenbrainz)

        playlistimport_cmd.func = func_playlist_import

        # plexplaylist2collection command
        plexplaylist2collection_cmd = ui.Subcommand("plexplaylist2collection")

        plexplaylist2collection_cmd.parser.add_option(
            "-m",
            "--playlist",
            default="Beets",
            help="name of the playlist to be converted",
        )

        def func_playlist2collection(lib, opts, args):
            self._plex_playlist_to_collection(opts.playlist)

        plexplaylist2collection_cmd.func = func_playlist2collection

        # plexsearchimport command
        searchimport_cmd = ui.Subcommand(
            "plexsearchimport",
            help="import playlist in to Plex based on Youtube search",
        )
        searchimport_cmd.parser.add_option(
            "-m",
            "--playlist",
            default="Beets",
            help="name of the playlist to be added in Plex",
        )
        searchimport_cmd.parser.add_option(
            "-s",
            "--search",
            default="",
            help="Create playlist based on Youtube search in Plex",
        )
        searchimport_cmd.parser.add_option(
            "-l", "--limit", default=10, help="Number of tracks"
        )

        def func_search_import(lib, opts, args):
            self._plex_import_search(opts.playlist, opts.search, opts.limit)

        searchimport_cmd.func = func_search_import

        # plexplaylistclear command
        playlistclear_cmd = ui.Subcommand(
            "plexplaylistclear", help="clear Plex playlist"
        )

        playlistclear_cmd.parser.add_option(
            "-m",
            "--playlist",
            default="",
            help="name of the Plex playlist to be cleared",
        )

        def func_playlist_clear(lib, opts, args):
            self._plex_clear_playlist(opts.playlist)

        playlistclear_cmd.func = func_playlist_clear

        # plexcollage command
        collage_cmd = ui.Subcommand(
            "plexcollage", help="create album collage based on Plex history"
        )

        collage_cmd.parser.add_option(
            "-i", "--interval", default=7, help="days to look back for history"
        )
        collage_cmd.parser.add_option(
            "-g", "--grid", default=3, help="dimension of the collage grid"
        )

        def func_collage(lib, opts, args):
            self._plex_collage(opts.interval, opts.grid)

        collage_cmd.func = func_collage

        # plexsonic command
        sonicsage_cmd = ui.Subcommand(
            "plexsonic", help="create ChatGPT-based playlists"
        )

        sonicsage_cmd.parser.add_option(
            "-n", "--number", default=10, help="number of song recommendations"
        )
        sonicsage_cmd.parser.add_option(
            "-p", "--prompt", default="", help="describe what you want to hear"
        )
        sonicsage_cmd.parser.add_option(
            "-m",
            "--playlist",
            default="SonicSage",
            help="name of the playlist to be added in Plex",
        )
        sonicsage_cmd.parser.add_option(
            "-c",
            "--clear",
            dest="clear",
            default=False,
            help="Clear playlist if not empty",
        )

        def func_sonic(lib, opts, args):
            self._plex_sonicsage(opts.number, opts.prompt, opts.playlist, opts.clear)

        sonicsage_cmd.func = func_sonic

        # plex2spotify command
        plex2spotify_cmd = ui.Subcommand(
            "plex2spotify", help="Transfer Plex playlist to Spotify"
        )

        plex2spotify_cmd.parser.add_option(
            "-m",
            "--playlist",
            default="beets",
            help="name of the playlist to be added in Spotify",
        )

        def func_plex2spotify(lib, opts, args):
            self._plex2spotify(lib, opts.playlist)

        plex2spotify_cmd.func = func_plex2spotify

        # Replace the "dailydiscovery" command with "plex_smartplaylists" command:
        plex_smartplaylists_cmd = ui.Subcommand(
            "plex_smartplaylists",
            help="Generate system-defined or custom smart playlists",
        )

        # Add import-failed option and log-file option
        plex_smartplaylists_cmd.parser.add_option(
            "-i",
            "--import-failed",
            action="store_true",
            default=False,
            help="import previously failed tracks from log files using manual search",
        )
        plex_smartplaylists_cmd.parser.add_option(
            "-l",
            "--log-file",
            default=None,
            help="specific log file to process (default: process all logs)",
        )

        def func_plex_smartplaylists(lib, opts, args):
            if opts.import_failed:
                total_imported, total_failed = self.process_import_logs(lib, opts.log_file)
                self._log.info(
                    "Manual import complete - Successfully imported: {}, Failed: {}",
                    total_imported, total_failed
                )
                return

            # Retrieve playlists from config
            playlists_config = config["plexsync"]["playlists"]["items"].get(list)
            if not playlists_config:
                self._log.warning(
                    "No playlists defined in config['plexsync']['playlists']['items']. Skipping."
                )
                return

            # Process all playlists at once
            plex_smartplaylists(self, lib, playlists_config)

        plex_smartplaylists_cmd.func = func_plex_smartplaylists

        # Finally, register the new command instead of the old daily_discovery_cmd
        return [
            plexupdate_cmd,
            sync_cmd,
            playlistadd_cmd,
            playlistrem_cmd,
            syncrecent_cmd,
            playlistimport_cmd,
            playlistclear_cmd,
            collage_cmd,
            sonicsage_cmd,
            searchimport_cmd,
            plexplaylist2collection_cmd,
            plex2spotify_cmd,
            plex_smartplaylists_cmd,
        ]

    def _plexupdate(self):
        """Update Plex music library."""
        try:
            self.music.update()
            self._log.info("Update started.")
        except exceptions.PlexApiException:
            self._log.warning("{} Update failed", self.config["plex"]["library_name"])

    def _fetch_plex_info(self, items, write, force):
        """Obtain track information from Plex."""
        items_len = len(items)
        with ThreadPoolExecutor() as executor:
            for index, item in enumerate(items, start=1):
                executor.submit(
                    self._process_item, index, item, write, force, items_len
                )

    def _process_item(self, index, item, write, force, items_len):
        self._log.info("Processing {}/{} tracks - {} ", index, items_len, item)
        if not force and "plex_userrating" in item:
            self._log.debug("Plex rating already present for: {}", item)
            return
        plex_track = self.search_plex_track(item)
        if plex_track is None:
            self._log.info("No track found for: {}", item)
            return
        item.plex_guid = plex_track.guid
        item.plex_ratingkey = plex_track.ratingKey
        item.plex_userrating = plex_track.userRating
        item.plex_skipcount = plex_track.skipCount
        item.plex_viewcount = plex_track.viewCount
        item.plex_lastviewedat = plex_track.lastViewedAt
        item.plex_lastratedat = plex_track.lastRatedAt
        item.plex_updated = time.time()
        item.store()
        if write:
            item.try_write()

    def search_plex_track(self, item):
        """Fetch the Plex track key."""
        tracks = self.music.searchTracks(
            **{"album.title": item.album, "track.title": item.title}, limit=50
        )
        if len(tracks) == 1:
            return tracks[0]
        elif len(tracks) > 1:
            for track in tracks:
                if track.parentTitle == item.album and track.title == item.title:
                    return track
        else:
            self._log.debug("Track {} not found in Plex library", item)
            return None

    def sort_plex_playlist(self, playlist_name, sort_field):
        """Sort a Plex playlist by a given field."""

        # Get the playlist
        playlist = self.plex.playlist(playlist_name)

        # Get the items in the playlist
        items = playlist.items()

        # Sort the items based on the sort_field
        sorted_items = sorted(
            items,
            key=lambda x: (
                getattr(x, sort_field).timestamp()
                if getattr(x, sort_field) is not None
                else 0
            ),
            reverse=True,  # Sort most recent first
        )

        # Remove all items from the playlist
        playlist.removeItems(items)

        # Add the sorted items back to the playlist
        for item in sorted_items:
            playlist.addItems(item)

    def _update_recently_played(self, lib, days=7):
        """Update recently played track info using plex_lookup."""
        tracks = self.music.search(
            filters={"track.lastViewedAt>>": f"{days}d"}, libtype="track"
        )
        self._log.info("Updating information for {} tracks", len(tracks))

        # Build lookup once for all tracks
        plex_lookup = self.build_plex_lookup(lib)

        with lib.transaction():
            for track in tracks:
                beets_item = plex_lookup.get(track.ratingKey)
                if not beets_item:
                    self._log.debug("Track {} not found in beets", track.ratingKey)
                    continue

                self._log.info("Updating information for {}", beets_item)
                try:
                    beets_item.plex_userrating = track.userRating
                    beets_item.plex_skipcount = track.skipCount
                    beets_item.plex_viewcount = track.viewCount
                    beets_item.plex_lastviewedat = (
                        track.lastViewedAt.timestamp()
                        if track.lastViewedAt
                        else None
                    )
                    beets_item.plex_lastratedat = (
                        track.lastRatedAt.timestamp() if track.lastRatedAt else None
                    )
                    beets_item.plex_updated = time.time()
                    beets_item.store()
                    beets_item.try_write()
                except exceptions.NotFound:
                    self._log.debug("Track not found in Plex: {}", beets_item)
                    continue

    def _cache_result(self, cache_key, result, cleaned_metadata=None):
        """Helper method to safely cache search results."""
        if not cache_key:
            return

        try:
            ratingKey = result.ratingKey if hasattr(result, "ratingKey") else result
            self.cache.set(cache_key, ratingKey, cleaned_metadata)
        except Exception as e:
            self._log.error("Failed to cache result: {}", e)

    def _handle_manual_search(self, sorted_tracks, song):
        """Helper function to handle manual search."""
        source_title = song.get("title", "")
        source_album = song.get("album", "Unknown")  # Changed from None to "Unknown"
        source_artist = song.get("artist", "")

        # Use beets UI formatting for the query header
        print_(ui.colorize('text_highlight', '\nChoose candidates for: ') +
               ui.colorize('text_highlight_minor', f"{source_album} - {source_title} - {source_artist}"))

        # Format and display the matches
        for i, (track, score) in enumerate(sorted_tracks, start=1):
            track_artist = getattr(track, 'originalTitle', None) or track.artist().title

            # Use beets' similarity detection for highlighting
            def highlight_matches(source, target):
                """Highlight exact matching parts between source and target strings."""
                if source is None or target is None:
                    return target or "Unknown"

                # Modified approach that's more precise with word boundaries
                # First check for whole word matches
                source_words = source.lower().split() if source else []
                target_words = target.lower().split() if target else []

                # If source and target are identical (case-insensitive), highlight the whole thing
                if source and target and source.lower() == target.lower():
                    return ui.colorize('text_success', target)

                # Process each target word individually for precise highlighting
                highlighted_words = []
                for i, target_word in enumerate(target_words):
                    word_matched = False
                    clean_target_word = re.sub(r'[^\w]', '', target_word.lower())

                    for source_word in source_words:
                        clean_source_word = re.sub(r'[^\w]', '', source_word.lower())
                        # Only match on actual words, not substrings within words
                        if (clean_source_word == clean_target_word or
                            get_fuzzy_score(clean_source_word, clean_target_word) > 0.8):
                            # Use the original formatting from target
                            highlighted_words.append(ui.colorize('text_success', target.split()[i]))
                            word_matched = True
                            break

                    if not word_matched:
                        highlighted_words.append(target.split()[i])

                return ' '.join(highlighted_words)

            # Highlight matching parts
            highlighted_title = highlight_matches(source_title, track.title)
            highlighted_album = highlight_matches(source_album, track.parentTitle)
            highlighted_artist = highlight_matches(source_artist, track_artist)

            # Color code the score
            score_color = get_color_for_score(score)

            # Format the line with matching and index colors
            print_(
                f"{i}. {highlighted_album} - {highlighted_title} - "
                f"{highlighted_artist} (Match: {ui.colorize(score_color, f'{score:.2f}')})"
            )

        # Show options footer
        print_(ui.colorize('text_highlight', '\nActions:'))
        print_(ui.colorize('text', '  #: Select match by number'))
        print_(
            f"  {ui.colorize('action', 'a')}{ui.colorize('text', ': Abort')}   "
            f"{ui.colorize('action', 's')}{ui.colorize('text', ': Skip')}   "
            f"{ui.colorize('action', 'e')}{ui.colorize('text', ': Enter manual search')}\n"
        )

        sel = ui.input_options(
            ("aBort", "Skip", "Enter"),
            numrange=(1, len(sorted_tracks)),
            default=1
        )

        if sel in ("b", "B"):
            return None
        elif sel in ("s", "S"):
            self._log.debug("User skipped, storing negative cache result.")
            self._cache_result(song, None)
            return None
        elif sel in ("e", "E"):
            return self.manual_track_search(song)

        selected_track = sorted_tracks[sel - 1][0] if sel > 0 else None
        if selected_track:
            final_key = self.cache._make_cache_key(song)
            self._log.debug("Storing manual selection in cache for key: {} ratingKey: {}",
                            final_key, selected_track.ratingKey)
            self._cache_result(final_key, selected_track.ratingKey, cleaned_metadata=song)
        return selected_track

    def manual_track_search(self, original_song=None):
        """Manually search for a track in the Plex library."""
        print_(ui.colorize('text_highlight', '\nManual Search'))
        print_(ui.colorize('text', 'Enter search criteria (empty to skip):'))

        title = input_(ui.colorize('text_highlight_minor', 'Title: ')).strip()
        album = input_(ui.colorize('text_highlight_minor', 'Album: ')).strip()
        artist = input_(ui.colorize('text_highlight_minor', 'Artist: ')).strip()

        # Log the search parameters for debugging
        self._log.debug("Searching with title='{}', album='{}', artist='{}'", title, album, artist)

        try:
            # Try different search strategies in order
            tracks = []

            # Strategy 1: If we have an album name from a movie soundtrack, search by album first
            if album and any(x in album.lower() for x in ['movie', 'soundtrack', 'original']):
                tracks = self.music.searchTracks(**{"album.title": album}, limit=100)
                self._log.debug("Album-first search found {} tracks", len(tracks))

            # Strategy 2: If first strategy didn't work or wasn't applicable, try combined search
            if not tracks and album and title:
                tracks = self.music.searchTracks(
                    **{"album.title": album, "track.title": title},
                    limit=100
                )
                self._log.debug("Combined album-title search found {} tracks", len(tracks))

            # Strategy 3: Try album-only search if no tracks found yet
            if not tracks and album:
                tracks = self.music.searchTracks(**{"album.title": album}, limit=100)
                self._log.debug("Album-only search found {} tracks", len(tracks))

            # Strategy 4: Try title-only search if still no tracks
            if not tracks and title:
                tracks = self.music.searchTracks(**{"track.title": title}, limit=100)
                self._log.debug("Title-only search found {} tracks", len(tracks))

            if not tracks and artist:
                # Strategy 5: Try artist-only search as last resort
                tracks = self.music.searchTracks(**{"artist.title": artist}, limit=100)
                self._log.debug("Artist-only search found {} tracks", len(tracks))

            if not tracks:
                self._log.info("No matching tracks found")
                return None

            # Filter results with more sophisticated matching
            filtered_tracks = []
            for track in tracks:
                track_artist = getattr(track, 'originalTitle', None) or track.artist().title
                track_album = track.parentTitle
                track_title = track.title

                # Debug log each track being considered
                self._log.debug("Considering track: {} - {} - {}", track_album, track_title, track_artist)

                # More sophisticated matching thresholds
                title_match = not title or get_fuzzy_score(title.lower(), track_title.lower()) > 0.4
                album_match = not album or get_fuzzy_score(album.lower(), track_album.lower()) > 0.4
                artist_match = not artist or get_fuzzy_score(artist.lower(), track_artist.lower()) > 0.4

                # Only include if it matches all provided criteria
                if title_match and album_match and artist_match:
                    # Calculate overall match score
                    score = 0.0
                    count = 0

                    if title:
                        score += get_fuzzy_score(title.lower(), track_title.lower())
                        count += 1
                    if album:
                        score += get_fuzzy_score(album.lower(), track_album.lower())
                        count += 1
                    if artist:
                        score += get_fuzzy_score(artist.lower(), track_artist.lower())
                        count += 1

                    # Calculate average score if we have any criteria
                    avg_score = score / count if count > 0 else 0.0

                    filtered_tracks.append((track, avg_score))

            # Sort by score, highest first
            sorted_tracks = sorted(filtered_tracks, key=lambda x: x[1], reverse=True)

            if not sorted_tracks:
                self._log.info("No matching tracks found after filtering")
                return None

            # Present the top matches to the user
            return self._handle_manual_search(sorted_tracks, original_song)

        except Exception as e:
            self._log.error("Error during manual search: {}", e)
            return None

    def search_plex_song(self, song, manual_search=False):
        """Search for a song in the Plex library.

        Args:
            song: Dictionary with song metadata (title, artist, album)
            manual_search: Whether to enable manual search if automatic fails

        Returns:
            Plex track object or None if not found
        """
        # Check if we have a cached result for this song
        cache_key = self.cache._make_cache_key(song)
        cached_result = self.cache.get(cache_key)

        if cached_result is not None:
            self._log.debug("Using cached result for {}", song)
            if cached_result == -1:  # Negative cache
                return None

            try:
                return self.plex.fetchItem(cached_result)
            except exceptions.NotFound:
                self._log.debug("Cached item not found in Plex, will search again")
                # Continue with search since cached item wasn't found

        # Extract song metadata
        title = song.get("title", "")
        album = song.get("album", "")
        artist = song.get("artist", "")

        if not title or not artist:
            self._log.debug("Missing required metadata: title={}, artist={}", title, artist)
            return None

        # Try to clean up the metadata using LLM if enabled
        cleaned_metadata = None
        if self.search_llm:
            try:
                cleaned_metadata = search_track_info(self.search_llm, song)
                if cleaned_metadata:
                    self._log.debug("LLM cleaned metadata: {}", cleaned_metadata)
                    # Use cleaned metadata for search
                    title = cleaned_metadata.get("title", title)
                    album = cleaned_metadata.get("album", album)
                    artist = cleaned_metadata.get("artist", artist)
            except Exception as e:
                self._log.error("Error using LLM for search cleaning: {}", e)

        # Try different search strategies
        track = None

        # Strategy 1: Direct search with album and title
        if album:
            try:
                tracks = self.music.searchTracks(
                    **{"album.title": album, "track.title": title},
                    limit=10
                )
                if tracks:
                    # Calculate distances and find best match
                    track_distances = []
                    for t in tracks:
                        distance = plex_track_distance(t, title, album, artist)
                        track_distances.append((t, distance))

                    # Sort by distance (lower is better)
                    track_distances.sort(key=lambda x: x[1])
                    best_match = track_distances[0]

                    # If distance is below threshold, use this track
                    if best_match[1] < 0.4:  # Threshold for good match
                        track = best_match[0]
                        self._log.debug("Found direct match: {} (distance: {})",
                                      track.title, best_match[1])
            except Exception as e:
                self._log.debug("Error in direct search: {}", e)

        # Strategy 2: Title-only search if direct search failed
        if not track:
            try:
                tracks = self.music.searchTracks(**{"track.title": title}, limit=20)
                if tracks:
                    # Calculate distances and find best match
                    track_distances = []
                    for t in tracks:
                        distance = plex_track_distance(t, title, album, artist)
                        track_distances.append((t, distance))

                    # Sort by distance (lower is better)
                    track_distances.sort(key=lambda x: x[1])
                    best_match = track_distances[0]

                    # If distance is below threshold, use this track
                    if best_match[1] < 0.3:  # Stricter threshold for title-only
                        track = best_match[0]
                        self._log.debug("Found title match: {} (distance: {})",
                                      track.title, best_match[1])
            except Exception as e:
                self._log.debug("Error in title search: {}", e)

        # If automatic search failed and manual search is enabled, try manual search
        if not track and manual_search:
            self._log.info("Automatic search failed, trying manual search")

            # Get all potential matches for manual selection
            all_tracks = []

            # Try different search strategies to gather candidates
            try:
                # Title search
                title_tracks = self.music.searchTracks(**{"track.title": title}, limit=10)
                all_tracks.extend(title_tracks)

                # Artist search
                artist_tracks = self.music.searchTracks(**{"artist.title": artist}, limit=10)
                all_tracks.extend(artist_tracks)

                # Album search if available
                if album:
                    album_tracks = self.music.searchTracks(**{"album.title": album}, limit=10)
                    all_tracks.extend(album_tracks)

                # Remove duplicates
                unique_tracks = {}
                for t in all_tracks:
                    unique_tracks[t.ratingKey] = t

                # Calculate distances for all unique tracks
                track_distances = []
                for t in unique_tracks.values():
                    distance = plex_track_distance(t, title, album, artist)
                    # Convert distance to a score (1.0 - distance, higher is better)
                    score = max(0.0, 1.0 - distance)
                    track_distances.append((t, score))

                # Sort by score (higher is better)
                track_distances.sort(key=lambda x: x[1], reverse=True)

                # Take top matches for manual selection
                top_matches = track_distances[:10]

                # Let user select from matches
                track = self._handle_manual_search(top_matches, song)

            except Exception as e:
                self._log.error("Error in manual search: {}", e)

        # Cache the result
        if track:
            self._cache_result(cache_key, track, cleaned_metadata)
        else:
            # Cache negative result
            self.cache.set(cache_key, -1, cleaned_metadata)

        return track

    def build_plex_lookup(self, lib):
        """Build a lookup dictionary from Plex ratingKey to beets Item."""
        plex_lookup = {}

        # Get all items with plex_ratingkey
        items = lib.items('plex_ratingkey:?')

        for item in items:
            plex_lookup[item.plex_ratingkey] = item

        self._log.debug("Built Plex lookup with {} items", len(plex_lookup))
        return plex_lookup

    def setup_llm(self):
        """Set up LLM client for search cleaning."""
        try:
            from beetsplug.llm import setup_llm_client
            self.llm_client = setup_llm_client(self._log)
        except Exception as e:
            self._log.error("Failed to set up LLM client: {}", e)
            self.llm_client = None

    def _plex_collage(self, interval, grid):
        """Create a collage of album art from recently played tracks."""
        from PIL import Image
        import numpy as np
        import io
        import requests

        # Get recently played tracks
        tracks = self.music.search(
            filters={"track.lastViewedAt>>": f"{interval}d"},
            libtype="track"
        )

        if not tracks:
            self._log.warning("No tracks found in the last {} days", interval)
            return

        # Get unique albums
        albums = {}
        for track in tracks:
            album_key = track.parentRatingKey
            if album_key not in albums:
                albums[album_key] = {
                    'album': track.album(),
                    'play_count': 1
                }
            else:
                albums[album_key]['play_count'] += 1

        # Sort albums by play count
        sorted_albums = sorted(
            albums.values(),
            key=lambda x: x['play_count'],
            reverse=True
        )

        # Take top N albums based on grid size
        grid_size = int(grid)
        top_albums = sorted_albums[:grid_size * grid_size]

        if len(top_albums) < grid_size * grid_size:
            self._log.warning(
                "Not enough albums for {}x{} grid, using {} albums",
                grid_size, grid_size, len(top_albums)
            )

        # Download album art and create collage
        album_images = []
        for album_data in top_albums:
            album = album_data['album']
            try:
                # Get album art URL
                art_url = album.thumb
                if not art_url.startswith('http'):
                    art_url = self.plex.url(art_url)

                # Add token to URL
                if '?' in art_url:
                    art_url += f"&X-Plex-Token={self.plex._token}"
                else:
                    art_url += f"?X-Plex-Token={self.plex._token}"

                # Download image
                response = requests.get(art_url)
                img = Image.open(io.BytesIO(response.content))

                # Resize to consistent size
                img = img.resize((300, 300))
                album_images.append(img)

            except Exception as e:
                self._log.error("Error downloading album art for {}: {}", album.title, e)

        if not album_images:
            self._log.error("Failed to download any album art")
            return

        # Create collage
        collage_width = grid_size * 300
        collage_height = grid_size * 300
        collage = Image.new('RGB', (collage_width, collage_height))

        # Place images in grid
        for i, img in enumerate(album_images):
            row = i // grid_size
            col = i % grid_size
            collage.paste(img, (col * 300, row * 300))

        # Save collage
        collage.save('collage.png')
        self._log.info("Collage saved to collage.png")

    def _plex_sonicsage(self, number, prompt, playlist, clear):
        """Create a playlist using ChatGPT recommendations."""
        if not self.llm_client:
            self._log.error("LLM client not available. Cannot use SonicSage.")
            return

        from beetsplug.llm import get_song_recommendations

        # Get song recommendations
        self._log.info("Getting song recommendations for: {}", prompt)
        songs = get_song_recommendations(self.llm_client, prompt, int(number))

        if not songs:
            self._log.error("Failed to get song recommendations")
            return

        # Search for songs in Plex
        self._log.info("Searching for {} recommended songs in Plex", len(songs))
        found_tracks = []

        for song in songs:
            self._log.info("Searching for: {} - {} - {}",
                         song.title, song.artist, song.album)

            # Create song dict for search
            song_dict = {
                "title": song.title,
                "artist": song.artist,
                "album": song.album
            }

            # Search in Plex
            track = self.search_plex_song(song_dict, manual_search=True)

            if track:
                self._log.info("Found match: {}", track.title)
                found_tracks.append(track)
            else:
                self._log.warning("No match found for: {} - {}",
                                song.title, song.artist)

        if not found_tracks:
            self._log.error("No matching tracks found in Plex")
            return

        # Clear playlist if requested
        if clear:
            try:
                self._plex_clear_playlist(playlist)
                self._log.info("Cleared existing playlist: {}", playlist)
            except Exception:
                self._log.debug("No existing playlist to clear: {}", playlist)

        # Add tracks to playlist
        self._plex_add_playlist_item(found_tracks, playlist)
        self._log.info(
            "Created playlist '{}' with {} tracks",
            playlist, len(found_tracks)
        )
