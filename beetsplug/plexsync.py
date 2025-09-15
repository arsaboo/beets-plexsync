"""Update and sync Plex music library.

Plex users enter the Plex Token to enable updating.
Put something like the following in your config.yaml to configure:
    plex:
        host: localhost
        token: token
"""

import logging
import os
import asyncio
import random
import re
import time
import json
import spotipy
import numpy as np
import confuse
import enlighten
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

import dateutil.parser
import requests
from beets import config, ui
from beets.dbcore import types
from beets.dbcore.query import MatchQuery
from beets.dbcore.types import DateType
from beets.library import Item  # Added Item to import
from beets.plugins import BeetsPlugin
from beets.ui import input_, print_
from beets.autotag.distance import Distance
from bs4 import BeautifulSoup
from jiosaavn import JioSaavn
from openai import OpenAI
from plexapi import exceptions
from plexapi.server import PlexServer
from pydantic import BaseModel, Field
from requests.exceptions import ConnectionError, ContentDecodingError
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth

from beetsplug.caching import Cache
from beetsplug.llm import search_track_info, Song, SongRecommendations
from beetsplug.matching import clean_string, plex_track_distance, get_fuzzy_score
from beetsplug.provider_gaana import import_gaana_playlist
from beetsplug.provider_tidal import import_tidal_playlist
from beetsplug.provider_youtube import import_yt_playlist, import_yt_search
from beetsplug.provider_apple import import_apple_playlist
from beetsplug.provider_jiosaavn import import_jiosaavn_playlist
from beetsplug.provider_m3u8 import import_m3u8_playlist
from beetsplug.provider_post import import_post_playlist
from beetsplug.helpers import parse_title, clean_album_name, get_config_value, highlight_matches
from beetsplug import plex_ops
from beetsplug import spotify_provider
from beetsplug import collage as collage_mod
from beetsplug import smartplaylists as sp_mod


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

        # Set up the logger
        self._log = logging.getLogger('beets.plexsync')

        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 0.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
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
                    "model": "qwen2.5:latest",  # Override model for search
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

    def authenticate_spotify(self):
        spotify_provider.authenticate(self)

    def process_spotify_track(self, track):
        return spotify_provider.process_spotify_track(track, self._log)

    def import_spotify_playlist(self, playlist_id):
        return spotify_provider.import_spotify_playlist(self, playlist_id)

    def import_apple_playlist(self, url):
        """Import Apple Music playlist with caching."""
        return import_apple_playlist(url, self.cache, self.headers)
    def import_jiosaavn_playlist(self, url):
        """Import JioSaavn playlist with caching."""
        return import_jiosaavn_playlist(url, self.cache)

    def get_playlist_id(self, url):
        return spotify_provider.get_playlist_id(url)

    def get_playlist_tracks(self, playlist_id):
        return spotify_provider.get_playlist_tracks(self, playlist_id)

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
            items = lib.items(args)
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
            self._plex2spotify(lib, opts.playlist, args)

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
        # Add --only option to filter playlists by id
        plex_smartplaylists_cmd.parser.add_option(
            "-o",
            "--only",
            dest="only",
            default=None,
            help="comma-separated list of playlist IDs to update (e.g. daily_discovery,forgotten_gems)",
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

            # If --only is specified, filter playlists by id
            if opts.only:
                only_ids = [x.strip() for x in opts.only.split(",") if x.strip()]
                playlists_config = [p for p in playlists_config if p.get("id") in only_ids]
                self._log.info("Filtered playlists to process: {}", only_ids)

            # Process all playlists at once
            self._plex_smartplaylists(lib, playlists_config)

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
        ]    # Using helper functions from helpers.py instead of class methods


    def find_closest_match(self, song, tracks):
        """Find best matching tracks using enhanced string similarity with context-aware weights."""
        matches = []

        # Create a temporary beets Item for comparison with null safety
        temp_item = Item()
        temp_item.title = (song.get('title') or '').strip() if song.get('title') is not None else ''
        temp_item.artist = (song.get('artist') or '').strip() if song.get('artist') is not None else ''
        temp_item.album = (song.get('album') or '').strip() if song.get('album') is not None else ''

        for track in tracks:
            score, dist = plex_track_distance(temp_item, track)
            matches.append((track, score))

            # Debug logging - simpler format with positional args
            self._log.debug("Track: {} - {}, Score: {:.3f}",
                          track.parentTitle, track.title, score)

        # Sort by score descending
        matches.sort(key=lambda x: x[1], reverse=True)
        return matches

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
        plex_ops.sort_plex_playlist(self.plex, playlist_name, sort_field, self._log)

    def _plex_add_playlist_item(self, items, playlist):
        """Add items to Plex playlist."""
        plex_ops.plex_add_playlist_item(self.plex, items, playlist, self._log)

    def _plex_playlist_to_collection(self, playlist):
        """Convert a Plex playlist to a Plex collection."""
        plex_ops.plex_playlist_to_collection(self.music, playlist, self._log)

    def _plex_remove_playlist_item(self, items, playlist):
        """Remove items from Plex playlist."""
        plex_ops.plex_remove_playlist_item(self.plex, items, playlist, self._log)

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

    def _handle_manual_search(self, sorted_tracks, song, original_query=None):
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

            # Highlight matching parts
            highlighted_title = highlight_matches(source_title, track.title)
            highlighted_album = highlight_matches(source_album, track.parentTitle)
            highlighted_artist = highlight_matches(source_artist, track_artist)

            # Color code the score
            if score >= 0.8:
                score_color = 'text_success'    # High match
            elif score >= 0.5:
                score_color = 'text_warning'    # Medium match
            else:
                score_color = 'text_error'      # Low match

            # Format the line with matching and index colors
            print_(
                f"{i}. {highlighted_album} - {highlighted_title} - "
                f"{highlighted_artist} (Match: {ui.colorize(score_color, f'{score:.2f}')})"
            )        # Show options footer
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
            query_to_neg_cache = None
            if original_query is not None and original_query.get('title') and original_query.get('title').strip():
                query_to_neg_cache = original_query
            elif song.get('title') and song.get('title').strip(): # 'song' is the current search terms
                query_to_neg_cache = song

            if query_to_neg_cache:
                self._cache_result(query_to_neg_cache, None)
            else:
                self._log.debug("No suitable query to store negative cache against for skip.")
            return None
        elif sel in ("e", "E"):
            return self.manual_track_search(original_query if original_query is not None else song)

        selected_track = sorted_tracks[sel - 1][0] if sel > 0 else None
        if selected_track:
            # Cache the result for the current song query using proper cache key
            current_cache_key = self.cache._make_cache_key(song)
            self._cache_result(current_cache_key, selected_track)
            self._log.debug("Cached result for current song query: {}", song)

            # ALWAYS cache for the original query that led to this manual search
            if original_query is not None and original_query != song:
                original_cache_key = self.cache._make_cache_key(original_query)
                self._log.debug("Also caching result for original query key: {}", original_query)
                self._cache_result(original_cache_key, selected_track)

            return selected_track

    def manual_track_search(self, original_query=None):
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

                # Handle multiple artists better
                artist_match = True
                if artist:
                    track_artists = set(a.strip().lower() for a in track_artist.split(','))
                    search_artists = set(a.strip().lower() for a in artist.split(','))

                    # Calculate artist match score using intersection
                    common_artists = track_artists.intersection(search_artists)
                    total_artists = track_artists.union(search_artists)
                    artist_score = len(common_artists) / len(total_artists) if total_artists else 0

                    # Consider it a match if we have at least 30% artist overlap
                    artist_match = artist_score >= 0.3

                # Enhanced matching criteria:
                # 1. Perfect album match (for soundtracks)
                perfect_album = album and track_album and album.lower() == track_album.lower()
                # 2. Strong title match
                strong_title = title and get_fuzzy_score(title.lower(), track_title.lower()) > 0.8
                # 3. Standard criteria
                standard_match = (title_match and album_match and artist_match)

                if perfect_album or strong_title or standard_match:
                    filtered_tracks.append(track)
                    self._log.debug(
                        "Matched: {} - {} - {} (Perfect album: {}, Strong title: {}, Standard: {})",
                        track_album, track_title, track_artist,
                        perfect_album, strong_title, standard_match
                    )

            if not filtered_tracks:
                self._log.info("No matching tracks found after filtering")
                return None

            # Create song_dict for match scoring
            song_dict = {
                "title": title if title else "",
                "album": album if album else "",
                "artist": artist if artist else "",
            }

            # Sort matches by relevance (removed is_soundtrack parameter)
            sorted_tracks = self.find_closest_match(song_dict, filtered_tracks)

            # Use beets UI formatting for the query header
            print_(ui.colorize('text_highlight', '\nChoose candidates for: ') +
                   ui.colorize('text_highlight_minor', f"{album} - {title} - {artist}"))

            # Format and display the matches
            for i, (track, score) in enumerate(sorted_tracks, start=1):
                track_artist = getattr(track, 'originalTitle', None) or track.artist().title

                # Highlight matching parts
                highlighted_title = highlight_matches(title, track.title)
                highlighted_album = highlight_matches(album, track.parentTitle)
                highlighted_artist = highlight_matches(artist, track_artist)

                # Color code the score
                if score >= 0.8:
                    score_color = 'text_success'    # High match
                elif score >= 0.5:
                    score_color = 'text_warning'    # Medium match
                else:
                    score_color = 'text_error'      # Low match

                # Format the line with matching and index colors
                print_(
                    f"{ui.colorize('action', str(i))}. {highlighted_album} - {highlighted_title} - "
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
                self._log.debug("User skipped in manual_track_search, storing negative cache result.")
                query_to_neg_cache = None
                if original_query is not None and original_query.get('title') and original_query.get('title').strip():
                    query_to_neg_cache = original_query
                elif song_dict.get('title') and song_dict.get('title').strip(): # song_dict is from the manual text input
                    query_to_neg_cache = song_dict

                if query_to_neg_cache:
                    self._cache_result(query_to_neg_cache, None)
                else:
                    self._log.debug("No suitable query to store negative cache against for skip in manual_track_search.")
                return None
            elif sel in ("e", "E"):
                return self.manual_track_search(original_query)

            selected_track = sorted_tracks[sel - 1][0] if sel > 0 else None
            if selected_track:
                # Determine the primary query to cache against
                query_to_cache = None
                if original_query is not None and original_query.get('title') and original_query.get('title').strip():
                    query_to_cache = original_query
                    self._log.debug("Using original_query for caching: {}", original_query)
                elif song_dict.get('title') and song_dict.get('title').strip(): # song_dict is from the manual text input
                    query_to_cache = song_dict
                    self._log.debug("Using current song_dict for caching (original_query was not suitable): {}", song_dict)

                if query_to_cache:
                    self._cache_result(query_to_cache, selected_track)
                else:
                    self._log.debug("No suitable query to cache the selected track against.")

                # Always update the original key if it differs from the manual/LLM-cleaned query
                if original_query is not None and song_dict != original_query:
                    self._log.debug("Also caching result for original query key: {}", original_query)
                    self._cache_result(original_query, selected_track)
            return selected_track
        except Exception as e:
            self._log.error("Error during manual search: {}", e)
            return None

    def search_plex_song(self, song, manual_search=None, llm_attempted=False):
        """Fetch the Plex track key with fallback options."""
        if manual_search is None:
            manual_search = config["plexsync"]["manual_search"].get(bool)

        # Debug the cache key generation
        cache_key = self.cache._make_cache_key(song)
        self._log.debug("Generated cache key: '{}' for song: {}", cache_key, song)

        # Check cache first - this is the key fix
        cached_result = self.cache.get(cache_key)

        # Add debug info about cache lookup
        if cached_result is not None:
            self._log.debug("Cache HIT for key: '{}' -> result: {}", cache_key, cached_result)
        else:
            self._log.debug("Cache MISS for key: '{}'", cache_key)
            # Debug what keys exist in cache for this song
            self.cache.debug_cache_keys(song)

        if cached_result is not None:
            if isinstance(cached_result, tuple):
                rating_key, cleaned_metadata = cached_result
                # Handle negative cache (skipped tracks)
                if rating_key == -1 or rating_key is None:
                    # If we have cleaned metadata from LLM and this is the first attempt, try that
                    if cleaned_metadata and not llm_attempted:
                        self._log.debug("Using cached cleaned metadata: {}", cleaned_metadata)
                        # Retry with cleaned metadata, but avoid nested prompts here
                        result = self.search_plex_song(cleaned_metadata, False, llm_attempted=True)

                        # If LLM search succeeds, update the original cache
                        if result is not None:
                            self._log.debug("Cached cleaned metadata search succeeded, updating original cache: {}", song)
                            self._cache_result(cache_key, result)
                            return result
                        # If LLM search also fails, respect the original negative cache
                        else:
                            self._log.debug("Cached cleaned metadata search also failed, respecting original skip for: {}", song)
                            return None
                    # Return None for definitively skipped tracks (no cleaned metadata or already tried LLM)
                    self._log.debug("Found cached skip result for: {}", song)
                    return None

                # Handle positive cache (matched tracks)
                try:
                    if rating_key:
                        cached_track = self.music.fetchItem(rating_key)
                        self._log.debug("Found cached match for: {} -> {}", song, cached_track.title)
                        return cached_track
                except Exception as e:
                    self._log.debug("Failed to fetch cached item {}: {}", rating_key, e)
                    # If cached item not found in Plex, remove it from cache and continue
                    self.cache.set(cache_key, None)
            else:  # Legacy cache entry
                if cached_result == -1 or cached_result is None:
                    self._log.debug("Found legacy cached skip result for: {}", song)
                    return None
                try:
                    if cached_result:
                        cached_track = self.music.fetchItem(cached_result)
                        self._log.debug("Found legacy cached match for: {} -> {}", song, cached_track.title)
                        return cached_track
                except Exception as e:
                    self._log.debug("Failed to fetch legacy cached item {}: {}", cached_result, e)
                    # If cached item not found in Plex, remove it from cache and continue
                    self.cache.set(cache_key, None)

        # Note: when llm_attempted is True (searching cleaned metadata), we still
        # run the regular search strategies below. The LLM branch will not repeat
        # because it is guarded by `not llm_attempted`.

        # Try regular search with multiple strategies
        # Ensure song["artist"] is not None before splitting
        tracks = []
        search_strategies_tried = []

        try:
            if song["artist"] is None:
                song["artist"] = ""

            # Strategy 1: Album + Title search (existing)
            if song["album"] is not None and song["album"] != "":
                search_strategies_tried.append("album_title")
                tracks = self.music.searchTracks(
                    **{"album.title": song["album"], "track.title": song["title"]}, limit=50
                )
                self._log.debug("Strategy 1 (Album+Title): Found {} tracks", len(tracks))

            # Strategy 2: Title-only search if album search failed
            if len(tracks) == 0:
                search_strategies_tried.append("title_only")
                tracks = self.music.searchTracks(**{"track.title": song["title"]}, limit=50)
                self._log.debug("Strategy 2 (Title-only): Found {} tracks", len(tracks))

            # Strategy 3: Simplified title search if still no matches
            if len(tracks) == 0:
                search_strategies_tried.append("simplified_title")
                simplified_title = clean_string(song["title"])
                tracks = self.music.searchTracks(**{"track.title": simplified_title}, limit=50)
                self._log.debug("Strategy 3 (Simplified title): Found {} tracks", len(tracks))

            # Strategy 4: Artist + Title search
            if len(tracks) == 0 and song["artist"] and song["title"]:
                search_strategies_tried.append("artist_title")
                tracks = self.music.searchTracks(
                    **{"artist.title": song["artist"], "track.title": song["title"]}, limit=50
                )
                self._log.debug("Strategy 4 (Artist+Title): Found {} tracks", len(tracks))

            # Strategy 5: Album-only search
            if len(tracks) == 0 and song["album"]:
                search_strategies_tried.append("album_only")
                tracks = self.music.searchTracks(**{"album.title": song["album"]}, limit=100)
                self._log.debug("Strategy 5 (Album-only): Found {} tracks", len(tracks))

            # Strategy 6: Soundtrack-aware search (if applicable)
            if len(tracks) == 0 and song["album"] == "" and " - from " in song["title"].lower():
                search_strategies_tried.append("soundtrack_aware")
                # Extract movie name from "Song - From Movie" format
                import re
                soundtrack_pattern = re.compile(r'.*\s*-\s*from\s*"([^"]+)"', re.IGNORECASE)
                match = soundtrack_pattern.search(song["title"])
                if match:
                    movie_name = match.group(1)
                    tracks = self.music.searchTracks(**{"album.title": movie_name}, limit=50)
                    self._log.debug("Strategy 6 (Soundtrack-aware): Found {} tracks for movie '{}'", len(tracks), movie_name)

        except Exception as e:
            self._log.debug(
                "Error during multi-strategy search for {} - {}. Error: {}",
                song.get("album", ""),
                song.get("title", ""),
                e,
            )
            return None

        # Process search results
        if len(tracks) == 1:
            result = tracks[0]
            self._cache_result(cache_key, result)
            return result
        elif len(tracks) > 1:
            sorted_tracks = self.find_closest_match(song, tracks)
            self._log.debug("Found {} tracks for {} using strategies: {}", len(sorted_tracks), song["title"], ", ".join(search_strategies_tried))

            # Try manual search first if enabled and we have matches
            if manual_search and len(sorted_tracks) > 0:
                result = self._handle_manual_search(sorted_tracks, song, original_query=song)
                # Cache the result for the original query if manual search succeeded
                if result is not None:
                    self._cache_result(cache_key, result)
                return result

            # Otherwise try automatic matching with improved threshold
            best_match = sorted_tracks[0]
            if best_match[1] >= 0.7:  # Lower threshold since we have more strategies
                self._cache_result(cache_key, best_match[0])
                return best_match[0]
            else:
                self._log.debug("Best match score {} below threshold for: {}", best_match[1], song["title"])

        # Try LLM cleaning if enabled and not already attempted
        cleaned_metadata_for_negative = None
        if not llm_attempted and self.search_llm and config["plexsync"]["use_llm_search"].get(bool):
            search_query = f"{song['title']} by {song['artist']}"
            if song.get('album'):
                search_query += f" from {song['album']}"

            self._log.debug("Attempting LLM cleanup for: {} using strategies: {}", search_query, ", ".join(search_strategies_tried))
            cleaned_metadata = search_track_info(search_query)
            if cleaned_metadata:
                # Use original value if LLM returns None for a field
                cleaned_title = cleaned_metadata.get("title")
                cleaned_album = cleaned_metadata.get("album")
                cleaned_artist = cleaned_metadata.get("artist")

                cleaned_song = {
                    "title": cleaned_title if cleaned_title is not None else song["title"],
                    "album": cleaned_album if cleaned_album is not None else song.get("album"),
                    "artist": cleaned_artist if cleaned_artist is not None else song.get("artist")
                }
                self._log.debug("Using LLM cleaned metadata: {}", cleaned_song)

                # Try search with cleaned metadata (avoid nested prompt here)
                result = self.search_plex_song(cleaned_song, False, llm_attempted=True)

                # If we found a match using LLM-cleaned metadata, cache it for the original query
                if result is not None:
                    self._log.debug("LLM-cleaned search succeeded, caching for original query: {}", song)
                    self._cache_result(cache_key, result)
                    return result
                else:
                    # Preserve cleaned metadata for a final negative cache if needed,
                    # but do not return yet so we can offer manual search below.
                    cleaned_metadata_for_negative = cleaned_song

        # Final fallback: try manual search if enabled
        if manual_search:
            self._log.info(
                "\nTrack {} - {} - {} not found in Plex (tried strategies: {})",
                song.get("album", "Unknown"),
                song.get("artist", "Unknown"),
                song["title"],
                ", ".join(search_strategies_tried) if search_strategies_tried else "none"
            )
            if ui.input_yn(ui.colorize('text_highlight', "\nSearch manually?") + " (Y/n)"):
                result = self.manual_track_search(song)
                # If manual search succeeds, cache it for the original query
                if result is not None:
                    self._log.debug("Manual search succeeded, caching for original query: {}", song)
                    self._cache_result(cache_key, result)
                    return result

        # Store negative result if nothing found
        self._log.debug("All search strategies failed for: {} (tried: {})", song, ", ".join(search_strategies_tried) if search_strategies_tried else "none")
        # If LLM provided cleaned metadata earlier, include it with negative cache
        if cleaned_metadata_for_negative is not None:
            self._cache_result(cache_key, None, cleaned_metadata_for_negative)
        else:
            self._cache_result(cache_key, None)
        return None

    def _process_matches(self, tracks, song, manual_search):
        """Helper function to process multiple track matches."""
        artist = song["artist"].split(",")[0]
        sorted_tracks = self.find_closest_match(song, tracks)
        self._log.debug("Found {} tracks for {}", len(sorted_tracks), song["title"])

        if manual_search and len(sorted_tracks) > 0:
            return self._handle_manual_search(sorted_tracks, song)

        result = None
        for track, score in sorted_tracks:
            if track.originalTitle is not None:
                plex_artist = track.originalTitle
            else:
                plex_artist = track.artist().title
            if artist in plex_artist:
                result = track
                break

        if result is not None:
            cache_key = json.dumps(song)
            self.cache.set(cache_key, result.ratingKey)
        return result

    def _plex_import_playlist(self, playlist, playlist_url=None, listenbrainz=False):
        """Import playlist into Plex."""
        if listenbrainz:
            try:
                from beetsplug.listenbrainz import ListenBrainzPlugin
            except ModuleNotFoundError:
                self._log.error("ListenBrainz plugin not installed")
                return
            try:
                lb = ListenBrainzPlugin()
            except Exception as e:
                self._log.error(
                    "Unable to initialize ListenBrainz plugin. Error: {}", e
                )
                return
            # there are 2 playlists to be imported. 1. Weekly jams 2. Weekly exploration
            # get the weekly jams playlist
            self._log.info("Importing weekly jams playlist")
            weekly_jams = lb.get_weekly_jams()
            self._log.info("Importing {} songs from Weekly Jams", len(weekly_jams))
            self.add_songs_to_plex("Weekly Jams", weekly_jams, config["plexsync"]["manual_search"].get(bool))

            self._log.info("Importing weekly exploration playlist")
            weekly_exploration = lb.get_weekly_exploration()
            self._log.info(
                "Importing {} songs from Weekly Exploration", len(weekly_exploration)
            )
            self.add_songs_to_plex("Weekly Exploration", weekly_exploration, config["plexsync"]["manual_search"].get(bool))
        else:
            if playlist_url is None or (
                "http://" not in playlist_url and "https://" not in playlist_url
            ):
                raise ui.UserError("Playlist URL not provided")
            if "apple" in playlist_url:
                songs = self.import_apple_playlist(playlist_url)
            elif "jiosaavn" in playlist_url:
                songs = self.import_jiosaavn_playlist(playlist_url)
            elif "gaana.com" in playlist_url:
                songs = self.import_gaana_playlist(playlist_url)
            elif "spotify" in playlist_url:
                songs = self.import_spotify_playlist(self.get_playlist_id(playlist_url))
            elif "youtube" in playlist_url:
                songs = self.import_yt_playlist(playlist_url)
            elif "tidal" in playlist_url:
                songs = self.import_tidal_playlist(playlist_url)
            else:
                songs = []
                self._log.error("Playlist URL not supported")
            self._log.info("Importing {} songs from {}", len(songs), playlist_url)
            self.add_songs_to_plex(playlist, songs, config["plexsync"]["manual_search"].get(bool))

    def add_songs_to_plex(self, playlist, songs, manual_search):
        """Add songs to a Plex playlist.

        Args:
            playlist: Name of the playlist
            songs: List of songs to add
            manual_search: Whether to enable manual search for matches
        """
        song_list = []
        if songs:
            for song in songs:
                found = self.search_plex_song(song, manual_search)
                if found is not None:
                    song_list.append(found)

        if not song_list:
            self._log.warning("No songs found to add to playlist {}", playlist)
            return

        self._plex_add_playlist_item(song_list, playlist)

    def _plex_import_search(self, playlist, search, limit=10):
        """Import search results into Plex."""
        self._log.info("Searching for {}", search)
        songs = self.import_yt_search(search, limit)
        song_list = []
        if songs:
            for song in songs:
                found = self.search_plex_song(song)
                if found is not None:
                    song_list.append(found)
        self._plex_add_playlist_item(song_list, playlist)

    def _plex_clear_playlist(self, playlist):
        """Clear Plex playlist."""
        plex_ops.plex_clear_playlist(self.plex, playlist)

    def _plex_collage(self, interval, grid):
        return collage_mod.plex_collage(self, interval, grid)

    def create_collage(self, list_image_urls, dimension):
        return collage_mod.create_collage(list_image_urls, dimension, self._log)

    def _plex_most_played_albums(self, tracks, interval):
        from datetime import datetime, timedelta

        now = datetime.now()
        frm_dt = now - timedelta(days=interval)

        # Build a map of album ratingKey -> album object from the provided tracks
        album_map = {}
        for t in tracks:
            try:
                alb = t.album()
                alb_key = getattr(t, "parentRatingKey", None) or getattr(alb, "ratingKey", None)
                if alb_key and alb_key not in album_map:
                    album_map[str(alb_key)] = alb
            except Exception:
                continue

        # Preferred method: use server-level history filtered by date & section
        album_data = {}
        used_server_history = False
        try:
            section_id = int(getattr(self.music, "key", 0)) or None
            history_entries = self.plex.history(
                mindate=frm_dt,
                librarySectionID=section_id,
                maxresults=None,
            )
            used_server_history = True
            self._log.debug("Using server history for section {} since {} ({} entries)", section_id, frm_dt.strftime('%Y-%m-%d'), len(history_entries))

            skipped_entries = 0
            for h in history_entries:
                try:
                    viewed_at = getattr(h, "viewedAt", None)

                    album_key = getattr(h, "parentRatingKey", None)
                    if album_key:
                        album_key = str(album_key)
                    else:
                        album_key = None

                    album_obj = album_map.get(album_key) if album_key else None

                    if album_obj is None:
                        # First try to fetch album directly via parentRatingKey
                        if album_key is not None:
                            try:
                                album_obj = self.plex.fetchItem(int(album_key))
                                album_map[album_key] = album_obj
                            except Exception:
                                album_obj = None

                    if album_obj is None:
                        # Fallback: fetch the track from history and then resolve its album
                        try:
                            track_key = getattr(h, "ratingKey", None)
                            if track_key is not None:
                                trk = self.plex.fetchItem(int(track_key))
                                album_obj = trk.album()
                                # Derive a stable album key from the album object
                                derived_key = str(getattr(album_obj, "ratingKey", album_obj.title))
                                album_key = derived_key
                                if album_key not in album_map:
                                    album_map[album_key] = album_obj
                        except Exception:
                            album_obj = None

                    if album_obj is None or album_key is None:
                        skipped_entries += 1
                        continue

                    data = album_data.setdefault(album_key, {"album": album_obj, "count": 0, "last_played": None})
                    data["count"] += 1
                    if viewed_at and (data["last_played"] is None or viewed_at > data["last_played"]):
                        data["last_played"] = viewed_at
                except Exception:
                    skipped_entries += 1
                    continue
            if skipped_entries:
                self._log.debug("Skipped {} history entries that could not resolve to an album", skipped_entries)
        except Exception as e:
            # Fallback: per-track history (older approach). Some Plex setups or plexapi versions
            # may not support the server.history call with these filters.
            self._log.debug("Falling back to per-track history due to error: {}", e)
            for track in tracks:
                try:
                    history = track.history(mindate=frm_dt)
                    count = len(history)

                    track_last_played = track.lastViewedAt
                    history_last_played = max((h.viewedAt for h in history if getattr(h, "viewedAt", None) is not None), default=None)
                    last_played = max(filter(None, [track_last_played, history_last_played]), default=None)

                    album_obj = track.album()
                    album_key = getattr(track, "parentRatingKey", None) or getattr(album_obj, "ratingKey", None) or album_obj.title
                    key = str(album_key)

                    if key not in album_data:
                        album_data[key] = {"album": album_obj, "count": count, "last_played": last_played}
                    else:
                        album_data[key]["count"] += count
                        if last_played and (album_data[key]["last_played"] is None or last_played > album_data[key]["last_played"]):
                            album_data[key]["last_played"] = last_played
                except Exception as ex:
                    self._log.debug("Error processing track history for {}: {}", getattr(track, 'title', 'unknown'), ex)
                    continue

        # Sort and build result
        albums_list = [(data["album"], data["count"], data["last_played"]) for data in album_data.values()]
        sorted_albums = sorted(albums_list, key=lambda x: (-x[1], -(x[2].timestamp() if x[2] else 0)))

        result = []
        for album, count, last_played in sorted_albums:
            if count > 0:
                try:
                    album.count = count
                    album.last_played_date = last_played
                    result.append(album)
                    self._log.debug(
                        "{} played {} times, last played on {}",
                        getattr(album, 'title', 'Unknown Album'),
                        count,
                        (last_played.strftime("%Y-%m-%d %H:%M:%S") if last_played else "Never"),
                    )
                except Exception:
                    # In case album is a lightweight object missing attributes
                    continue

        return result

    def _plex_sonicsage(self, number, prompt, playlist, clear):
        """Generate song recommendations using LLM based on a given prompt."""
        if self.llm_client is None:
            self._log.error("No LLM configured correctly")
            return
        if prompt == "":
            self._log.error("Prompt not provided")
            return

        recommendations = self.get_llm_recommendations(number, prompt)
        if recommendations is None:
            return

        song_list = []
        for song in recommendations.songs:
            song_dict = {
                "title": song.title.strip(),
                "album": song.album.strip(),
                "artist": song.artist.strip(),
                "year": int(song.year) if song.year.isdigit() else None,
            }
            song_list.append(song_dict)

        self._log.debug(
            "{} songs to be added in Plex library: {}", len(song_list), song_list
        )
        matched_songs = []
        for song in song_list:
            found = self.search_plex_song(song)
            if found is not None:
                matched_songs.append(found)
        self._log.debug("Songs matched in Plex library: {}", matched_songs)
        if clear:
            try:
                self._plex_clear_playlist(playlist)
            except exceptions.NotFound:
                self._log.debug(f"Unable to clear playlist {playlist}")
        try:
            self._plex_add_playlist_item(matched_songs, playlist)
        except Exception as e:
            self._log.error("Unable to add songs to playlist. Error: {}", e)

    def setup_llm(self):
        """Setup LLM client using OpenAI-compatible API."""

        try:
            client_args = {
                "api_key": config["llm"]["api_key"].get(),
            }

            base_url = config["llm"]["base_url"].get()
            if (base_url):
                client_args["base_url"] = base_url

            self.llm_client = OpenAI(**client_args)
        except Exception as e:
            self._log.error("Unable to connect to LLM service. Error: {}", e)
            return

    def get_llm_recommendations(self, number, prompt):
        """Get song recommendations from LLM service."""
        model = config["llm"]["model"].get()
        num_songs = int(number)
        sys_prompt = f"""
        You are a music recommender. You will reply with {num_songs} song
        recommendations in a JSON format. Only reply with the JSON object,
        no need to send anything else. Include title, artist, album, and
        year in the JSON response. Use the JSON format:
        {{
            "songs": [
                {{
                    "title": "Title of song 1",
                    "artist": "Artist of Song 1",
                    "album": "Album of Song 1",
                    "year": "Year of release"
                }}
            ]
        }}
        """
        messages = [{"role": "system", "content": sys_prompt}]
        messages.append({"role": "user", "content": prompt})
        try:
            self._log.info("Sending request to LLM service")
            chat = self.llm_client.chat.completions.create(
                model=model, messages=messages, temperature=0.7
            )
        except Exception as e:
            self._log.error("Unable to connect to LLM service. Error: {}", e)
            return
        reply = chat.choices[0].message.content
        tokens = chat.usage.total_tokens
        self._log.debug("LLM service used {} tokens and replied: {}", tokens, reply)
        return self.extract_json(reply)

    def extract_json(self, jsonString):
        """Extract and parse JSON from a string using Pydantic."""
        try:
            json_data = re.search(r"\{.*\}", jsonString, re.DOTALL).group()
            return SongRecommendations.model_validate_json(json_data)
        except Exception as e:
            self._log.error("Unable to parse JSON. Error: {}", e)
            return None

    def import_yt_playlist(self, url):
        """Import YouTube playlist with caching."""
        return import_yt_playlist(url, self.cache)

    def import_yt_search(self, query, limit):
        """Import YouTube search results."""
        return import_yt_search(query, limit, self.cache)

    def import_tidal_playlist(self, url):
        """Import Tidal playlist with caching."""
        return import_tidal_playlist(url, self.cache)

    def import_gaana_playlist(self, url):
        """Import Gaana playlist with caching."""
        return import_gaana_playlist(url, self.cache)

    def _plex2spotify(self, lib, playlist, query_args=None):
        """Transfer Plex playlist to Spotify using plex_lookup with optional query filtering."""
        self.authenticate_spotify()
        plex_playlist = self.plex.playlist(playlist)
        plex_playlist_items = plex_playlist.items()
        self._log.debug("Total items in Plex playlist: {}", len(plex_playlist_items))

        # Build lookup once for all tracks
        plex_lookup = self.build_plex_lookup(lib)

        # Process tracks in order and maintain the original Plex playlist order
        spotify_tracks = []

        # If query args are provided, filter the beets items first
        if query_args:
            # Get all beets items that match the query
            query_items = lib.items(query_args)
            query_rating_keys = {item.plex_ratingkey for item in query_items if hasattr(item, 'plex_ratingkey')}
            self._log.info("Query matched {} beets items, filtering playlist accordingly", len(query_rating_keys))
        else:
            query_rating_keys = None

        for item in plex_playlist_items:
            self._log.debug("Processing {}", item.ratingKey)

            beets_item = plex_lookup.get(item.ratingKey)
            if not beets_item:
                self._log.debug(
                    "Library not synced. Item not found in Beets: {} - {}",
                    item.parentTitle,
                    item.title
                )
                continue

            # Apply query filter if provided
            if query_rating_keys is not None and item.ratingKey not in query_rating_keys:
                self._log.debug(
                    "Item filtered out by query: {} - {} - {}",
                    beets_item.artist,
                    beets_item.album,
                    beets_item.title
                )
                continue

            self._log.debug("Beets item: {}", beets_item)

            spotify_track_id = None

            # First try to get existing spotify track ID from beets
            try:
                spotify_track_id = beets_item.spotify_track_id
                self._log.debug("Spotify track id in beets: {}", spotify_track_id)

                # Verify the track is available and playable on Spotify
                if spotify_track_id:
                    try:
                        track_info = self.sp.track(spotify_track_id)
                        # Strict availability check: must be playable and not restricted
                        if (
                            not track_info
                            or not track_info.get('is_playable', True)
                            or track_info.get('restrictions', {}).get('reason') == 'unavailable'
                            or not track_info.get('available_markets')
                        ):
                            self._log.debug("Track {} is not playable or not available, searching for alternatives", spotify_track_id)
                            spotify_track_id = None
                    except Exception as e:
                        self._log.debug("Error checking track availability {}: {}", spotify_track_id, e)
                        spotify_track_id = None

            except Exception:
                spotify_track_id = None
                self._log.debug("Spotify track_id not found in beets")

            # If no valid track ID, search for it
            if not spotify_track_id:
                spotify_track_id = self._search_spotify_track(beets_item)

            if spotify_track_id:
                spotify_tracks.append(spotify_track_id)
            else:
                self._log.info("No playable Spotify match found for {}", beets_item)

        if query_args:
            self._log.info("Found {} Spotify tracks matching query in Plex playlist order", len(spotify_tracks))
        else:
            self._log.debug("Found {} Spotify tracks in Plex playlist order", len(spotify_tracks))

        self.add_tracks_to_spotify_playlist(playlist, spotify_tracks)

    def _search_spotify_track(self, beets_item):
        return spotify_provider.search_spotify_track(self, beets_item)

    def add_tracks_to_spotify_playlist(self, playlist_name, track_uris):
        return spotify_provider.add_tracks_to_spotify_playlist(self, playlist_name, track_uris)



    def get_preferred_attributes(self):
        return sp_mod.get_preferred_attributes(self)

    def build_plex_lookup(self, lib):
        return sp_mod.build_plex_lookup(self, lib)

    def calculate_rating_score(self, rating):
        """Calculate score based on rating (60% weight)."""
        if not rating or rating <= 0:
            return 0
        score_map = {
            10: 100,
            9: 80,
            8: 60,
            7: 40,
            6: 20
        }
        return score_map.get(int(rating), 0) * 0.6

    def calculate_last_played_score(self, last_played):
        """Calculate score based on last played date (20% weight)."""
        if not last_played:
            return 100 * 0.2  # Never played gets max score

        days_since_played = (datetime.now() - datetime.fromtimestamp(last_played)).days

        if days_since_played > 180:  # 6 months
            return 80 * 0.2
        elif days_since_played > 90:  # 3 months
            return 60 * 0.2
        elif days_since_played > 30:  # 1 month
            return 40 * 0.2
        else:
            return 20 * 0.2

    def calculate_play_count_score(self, play_count):
        """Calculate score based on play count (20% weight)."""
        if not play_count or play_count < 0:
            play_count = 0

        if (play_count <= 2):
            return 100 * 0.2
        elif (play_count <= 5):
            return 80 * 0.2
        elif (play_count <= 10):
            return 60 * 0.2
        elif (play_count <= 20):
            return 40 * 0.2
        else:
            return 20 * 0.2

    def calculate_track_score(self, track, base_time=None, tracks_context=None):
        from beetsplug import smartplaylists as sp_mod
        return sp_mod.calculate_track_score(self, track, base_time, tracks_context)

    def select_tracks_weighted(self, tracks, num_tracks):
        from beetsplug import smartplaylists as sp_mod
        return sp_mod.select_tracks_weighted(self, tracks, num_tracks)

    def calculate_playlist_proportions(self, max_tracks, discovery_ratio):
        from beetsplug import smartplaylists as sp_mod
        return sp_mod.calculate_playlist_proportions(self, max_tracks, discovery_ratio)

    def validate_filter_config(self, filter_config):
        # Delegated to smartplaylists sidecar
        return sp_mod.validate_filter_config(self, filter_config)

    def _apply_exclusion_filters(self, tracks, exclude_config):
        # Delegated to smartplaylists sidecar
        return sp_mod._apply_exclusion_filters(self, tracks, exclude_config)

    def _apply_inclusion_filters(self, tracks, include_config):
        # Delegated to smartplaylists sidecar
        return sp_mod._apply_inclusion_filters(self, tracks, include_config)

    def apply_playlist_filters(self, tracks, filter_config):
        # Delegated to smartplaylists sidecar
        return sp_mod.apply_playlist_filters(self, tracks, filter_config)

    def generate_daily_discovery(self, lib, dd_config, plex_lookup, preferred_genres, similar_tracks):
        return sp_mod.generate_daily_discovery(self, lib, dd_config, plex_lookup, preferred_genres, similar_tracks)

    # get_filtered_library_tracks is no longer needed; logic is in smartplaylists

    def generate_forgotten_gems(self, lib, ug_config, plex_lookup, preferred_genres, similar_tracks):
        # Delegated to smartplaylists sidecar
        return sp_mod.generate_forgotten_gems(self, lib, ug_config, plex_lookup, preferred_genres, similar_tracks)

    def generate_recent_hits(self, lib, rh_config, plex_lookup, preferred_genres, similar_tracks):
        # Delegated to smartplaylists sidecar
        return sp_mod.generate_recent_hits(self, lib, rh_config, plex_lookup, preferred_genres, similar_tracks)

    def import_m3u8_playlist(self, filepath):
        """Import M3U8 playlist with caching."""
        return import_m3u8_playlist(filepath, self.cache)

    def import_post_playlist(self, source_config):
        """Import playlist from a POST request endpoint with caching."""
        return import_post_playlist(source_config, self.cache)

    def generate_imported_playlist(self, lib, playlist_config, plex_lookup=None):
        """Generate a playlist by importing from external sources (delegated)."""
        return sp_mod.generate_imported_playlist(self, lib, playlist_config, plex_lookup)

    def process_import_logs(self, lib, specific_log=None):
        """Process import logs in config directory and attempt manual import.

        Args:
            lib: The beets library instance
            specific_log: Optional filename to process only one log file

        Returns:
            tuple: (total_imported, total_failed)
        """
        total_imported = 0
        total_failed = 0

        def parse_track_info(line):
            """Helper function to parse track info from log line."""
            try:
                _, track_info = line.split("Not found:", 1)
                # First try to find the Unknown album marker as a separator
                parts = track_info.split(" - Unknown - ")
                if len(parts) == 2:
                    artist = parts[0].strip()
                    title = parts[1].strip()
                    album = "Unknown"
                else:
                    # Fallback to traditional parsing if no "Unknown" found
                    parts = track_info.strip().split(" - ")
                    if len(parts) >= 3:
                        artist = parts[0]
                        album = parts[1]
                        # Join remaining parts as title (may contain dashes)
                        title = " - ".join(parts[2:])
                    else:
                        return None

                # Use clean_string instead of clean_title
                title = clean_string(title)

                # Clean up artist (handle cases like "Vishal - Shekhar")
                artist = re.sub(r'\s+-\s+', ' & ', artist)

                if artist != "Unknown" and title:
                    return {
                        "artist": artist.strip(),
                        "album": album.strip() if album != "Unknown" else None,
                        "title": title.strip()
                    }
            except ValueError:
                pass
            return None

        if specific_log:
            # Process single log file
            log_path = Path(self.config_dir) / specific_log
            if not log_path.exists():
                self._log.error("Log file not found: {}", specific_log)
                return total_imported, total_failed
            log_files = [log_path]
        else:
            # Process all log files
            log_files = Path(self.config_dir).glob("*_import.log")

        for log_file in log_files:
            playlist_name = log_file.stem.replace("_import", "").replace("_", " ").title()
            self._log.info("Processing failed imports for playlist: {}", playlist_name)

            # Read the entire log file
            with open(log_file, 'r', encoding='utf-8') as f:
                log_content = f.readlines()

            tracks_to_import = []
            track_lines_to_remove = set()
            in_not_found_section = False
            header_lines = []
            summary_lines = []
            not_found_start = -1

            # First pass: collect tracks and identify sections
            for i, line in enumerate(log_content):
                if "Tracks not found in Plex library:" in line:
                    in_not_found_section = True
                    not_found_start = i
                    continue
                elif "Import Summary:" in line:
                    in_not_found_section = False
                    summary_lines = log_content[i:]
                    break

                if i < not_found_start:
                    header_lines.append(line)
                elif in_not_found_section and line.startswith("Not found:"):
                    track_info = parse_track_info(line)
                    if track_info:
                        track_info["line_num"] = i
                        tracks_to_import.append(track_info)

            if tracks_to_import:
                self._log.info("Attempting to manually import {} tracks for {}",
                             len(tracks_to_import), playlist_name)

                matched_tracks = []
                for track in tracks_to_import:
                    found = self.search_plex_song(track, manual_search=True)
                    if found:
                        matched_tracks.append(found)
                        track_lines_to_remove.add(track["line_num"])
                        total_imported += 1
                    else:
                        total_failed += 1

                if matched_tracks:
                    self._plex_add_playlist_item(matched_tracks, playlist_name)
                    self._log.info("Added {} tracks to playlist {}",
                                 len(matched_tracks), playlist_name)
                    # Update the log file
                    remaining_not_found = [
                        line for i, line in enumerate(log_content)
                        if i not in track_lines_to_remove
                    ]

                    # Update summary
                    new_summary = []
                    for line in summary_lines:
                        if "Tracks not found in Plex" in line:
                            remaining_not_found_count = len([
                                l for l in remaining_not_found
                                if l.startswith("Not found:")
                            ])
                            new_summary.append(f"Tracks not found in Plex: {remaining_not_found_count}\n")
                        else:
                            new_summary.append(line)

                    # Write updated log file
                    with open(log_file, 'w', encoding='utf-8') as f:
                        f.writelines(header_lines)
                        f.write("Tracks not found in Plex library:\n")
                        f.writelines(l for l in remaining_not_found if l.startswith("Not found:"))
                        f.write("\nUpdated at: {}\n".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        f.writelines(new_summary)

        return total_imported, total_failed

    def _plex_smartplaylists(self, lib, playlists_config):
        """Process all playlists at once with a single lookup dictionary."""
        # Build lookup once for all playlists
        self._log.info("Building Plex lookup dictionary...")
        plex_lookup = self.build_plex_lookup(lib)
        self._log.debug("Found {} tracks in lookup dictionary", len(plex_lookup))

        # Get preferred attributes once if needed for smart playlists
        preferred_genres = None
        similar_tracks = None
        if any(p.get("id") in ["daily_discovery", "forgotten_gems"] for p in playlists_config):
            preferred_genres, similar_tracks = self.get_preferred_attributes()
            self._log.debug("Using preferred genres: {}", preferred_genres)
            self._log.debug("Processing {} pre-filtered similar tracks", len(similar_tracks))

        # Process each playlist
        for p in playlists_config:
            playlist_type = p.get("type", "smart")
            playlist_id = p.get("id")
            playlist_name = p.get("name", "Unnamed playlist")

            if (playlist_type == "imported"):
                sp_mod.generate_imported_playlist(self, lib, p, plex_lookup)
            elif playlist_id in ["daily_discovery", "forgotten_gems", "recent_hits"]:
                if playlist_id == "daily_discovery":
                    sp_mod.generate_daily_discovery(self, lib, p, plex_lookup, preferred_genres, similar_tracks)
                elif playlist_id == "forgotten_gems":
                    sp_mod.generate_forgotten_gems(self, lib, p, plex_lookup, preferred_genres, similar_tracks)
                else:  # recent_hits
                    sp_mod.generate_recent_hits(self, lib, p, plex_lookup, preferred_genres, similar_tracks)
            else:
                self._log.warning(
                    "Unrecognized playlist configuration '{}' - type: '{}', id: '{}'. "
                    "Valid types are 'imported' or 'smart'. "
                    "Valid smart playlist IDs are 'daily_discovery', 'forgotten_gems', and 'recent_hits'.",
                    playlist_name, playlist_type, playlist_id
                )

    def shutdown(self, lib):
        """Clean up when plugin is disabled."""
        if self.loop and not self.loop.is_closed():
            self.close()

    def album_for_id(self, album_id):
        """Metadata plugin interface method - PlexSync doesn't provide album metadata."""
        return None

    def track_distance(self, item, info):
        """Metadata plugin interface method - PlexSync doesn't provide track distance."""
        return Distance()
