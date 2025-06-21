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
import re
import difflib
import json
import spotipy
import numpy as np
import confuse
import enlighten
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

import dateutil.parser
import requests
from beets import config, ui
from beets.dbcore import types
from beets.dbcore.query import MatchQuery
from beets.library import DateType, Item
from beets.plugins import BeetsPlugin
from beets.ui import input_, print_
from requests.exceptions import ConnectionError, ContentDecodingError
from plexapi import exceptions
from plexapi.server import PlexServer
from pydantic import BaseModel, Field
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth

from beetsplug.caching import Cache
from beetsplug.llm import search_track_info
from beetsplug.matching import clean_string, plex_track_distance
from beetsplug.provider_gaana import import_gaana_playlist
from beetsplug.provider_tidal import import_tidal_playlist
from beetsplug.provider_youtube import import_yt_playlist, import_yt_search
from beetsplug.provider_apple import import_apple_playlist
from beetsplug.provider_jiosaavn import import_jiosaavn_playlist
from beetsplug.provider_m3u8 import import_m3u8_playlist
from beetsplug.provider_post import import_post_playlist
from beetsplug.helpers import parse_title, clean_album_name
from beetsplug import plex_utils
from beetsplug import spotify_utils
from beetsplug import llm_utils
from beetsplug import playlist_importers
from beetsplug import smart_playlists
# Song and SongRecommendations models are now in llm_utils.py,
# but PlexSync might still use them for type hinting or if other methods return them.
# For now, let's assume they can be imported from llm_utils if needed elsewhere,
# or defined in a central models.py if used by more than just LLM stuff.
# Keeping them here for now if PlexSync methods other than LLM ones use them.


class Song(BaseModel): # Keep if used by non-LLM parts of PlexSync
    title: str
    artist: str
    album: str
    year: str = Field(description="Year of release")


class SongRecommendations(BaseModel): # Keep if used by non-LLM parts of PlexSync
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

    # authenticate_spotify, process_spotify_track, import_spotify_playlist,
    # get_playlist_id (for spotify), get_playlist_tracks (for spotify)
    # have been moved to spotify_utils.py and are called from there or directly.
    # Calls in methods like _plex_import_playlist and generate_imported_playlist
    # for spotify functionality will use self.import_spotify_playlist or self.get_playlist_id as wrappers.

    # Wrapper for authenticate_spotify for internal use if needed by other methods in this class
    def _ensure_spotify_authenticated(self):
        if not hasattr(self, 'sp') or not self.sp:
            spotify_utils.authenticate_spotify_for_plugin(
                plugin_instance=self,
                client_id=config["spotify"]["client_id"].get(),
                client_secret=config["spotify"]["client_secret"].get(),
                redirect_uri="http://localhost/",
                scope=(
                    "user-read-private user-read-email playlist-modify-public "
                    "playlist-modify-private playlist-read-private"
                ),
                token_cache_path=self.plexsync_token
            )
            if not self.sp: # Still not authenticated
                 self._log.error("Spotify authentication required but failed.")
                 raise ConnectionError("Failed to authenticate with Spotify.")


    # Wrapper for import_spotify_playlist
    def import_spotify_playlist(self, playlist_id_str): # Renamed playlist_id to playlist_id_str
        self._ensure_spotify_authenticated()
        return spotify_utils.import_spotify_playlist_with_fallback(
            sp_instance=self.sp,
            playlist_id=playlist_id_str, # Use renamed var
            cache_instance=self.cache,
            http_headers=self.headers
        )

    # Wrapper for get_playlist_id (specifically for Spotify URLs)
    def get_spotify_playlist_id_from_url(self, url):
        return spotify_utils.get_spotify_playlist_id_from_url(url)

    # Wrapper for get_spotify_playlist_tracks (Spotify) - kept if other parts of PlexSync need it directly.
    def get_spotify_playlist_tracks(self, playlist_id_str):
        self._ensure_spotify_authenticated()
        return spotify_utils.get_spotify_playlist_tracks_api(self.sp, playlist_id_str)

    # Direct provider import methods are removed.
    # Calls will go through _plex_import_playlist or generate_imported_playlist,
    # which will utilize the playlist_importers module.

    def listen_for_db_change(self, lib, model):
        """Listens for beets db change and register the update for the end."""
        self.register_listener("cli_exit", self._plexupdate)

    def commands(self):
        """Add beet UI commands to interact with Plex."""
        plexupdate_cmd = ui.Subcommand(
            "plexupdate", help=f"Update {self.data_source} library"
        )

        def func(lib, opts, args):
            # self._plexupdate() becomes:
            plex_utils.plexupdate(self.plex, config["plex"]["library_name"].get())

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
            # self._fetch_plex_info becomes:
            plex_utils.fetch_plex_info(
                self.plex, self.music, items, ui.should_write(), opts.force_refetch,
                self._process_item_for_plex_info_wrapper # Pass the wrapper
            )

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
            # self._plex_add_playlist_item becomes:
            plex_utils.plex_add_playlist_item(self.plex, items, opts.playlist)

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
            # self._plex_remove_playlist_item becomes:
            plex_utils.plex_remove_playlist_item(self.plex, items, opts.playlist)

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
            # self._update_recently_played becomes:
            plex_utils.update_recently_played(self.plex, self.music, lib, int(opts.days), self.build_plex_lookup)

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
            # self._plex_playlist_to_collection becomes:
            plex_utils.plex_playlist_to_collection(self.plex, self.music, opts.playlist)

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
            # self._plex_clear_playlist becomes:
            plex_utils.plex_clear_playlist(self.plex, opts.playlist)

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
            # self._plex_collage becomes:
            plex_utils.plex_collage(
                self.plex,
                self.music,
                self.config_dir,
                opts.interval,
                opts.grid,
                plex_utils.plex_most_played_albums, # Pass the utility function directly
                plex_utils.create_collage_image     # Pass the utility function directly
            )

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


    def get_fuzzy_score(self, str1, str2):
        """Calculate fuzzy match score between two strings."""
        if not str1 or not str2:
            return 0
        return difflib.SequenceMatcher(None, str1.lower(), str2.lower()).ratio()

    def clean_text_for_matching(self, text):
        """Clean text for better fuzzy matching.

        Args:
            text: Text to clean

        Returns:
            str: Cleaned text
        """
        if not text:
            return ""
        # Convert to lowercase
        text = text.lower()
        # Remove parentheses and contents
        text = re.sub(r'\([^)]*\)', '', text)
        # Remove brackets and contents
        text = re.sub(r'\[[^\]]*\]', '', text)
        # Remove soundtrack mentions
        text = re.sub(r'(?i)original\s+(?:motion\s+picture\s+)?soundtrack', '', text)
        # Remove special chars and extra spaces
        text = re.sub(r'[^\w\s]', ' ', text)
        # Normalize whitespace
        text = ' '.join(text.split())
        return text

    def calculate_string_similarity(self, source, target):
        """Calculate similarity score between two strings.

        Args:
            source: Source string to match
            target: Target string to match against

        Returns:
            float: Similarity score between 0-1
        """
        if not source or not target:
            return 0.0

        source = source.lower().strip()
        target = target.lower().strip()

        # Exact match
        if source == target:
            return 1.0

        # Source is substring of target or vice versa
        if source in target or target in source:
            shorter = min(len(source), len(target))
            longer = max(len(source), len(target))
            return 0.9 * (shorter / longer)

        # Calculate Levenshtein distance
        distance = difflib.SequenceMatcher(None, source, target).ratio()
        return distance

    def calculate_artist_similarity(self, source_artists, target_artists):
        """Calculate similarity between artist sets with partial matching.

        Args:
            source_artists: List/set of source artist names
            target_artists: List/set of target artist names

        Returns:
            float: Similarity score between 0-1
        """
        def normalize_artist(artist):
            """Normalize artist name for comparison."""
            if not artist:
                return ""
            artist = artist.lower()
            # Replace common separators
            artist = re.sub(r'\s*[&,]\s*', ' and ', artist)
            # Remove featuring
            artist = re.sub(r'\s*(?:feat\.?|ft\.?|featuring)\s*.*$', '', artist)
            # Remove special chars
            artist = re.sub(r'[^\w\s]', '', artist)
            return artist.strip()

        def get_artist_parts(artist):
            """Split artist name into words for partial matching."""
            return set(normalize_artist(artist).split())

        # Normalize and filter out empty/None artists
        source = [normalize_artist(a) for a in source_artists if a and a.lower() != "unknown"]
        target = [normalize_artist(a) for a in target_artists if a and a.lower() != "unknown"]

        if not source or not target:
            return 0.0

        # Calculate exact matches first
        exact_matches = len(set(source).intersection(target))
        if exact_matches:
            # Weight by proportion of exact matches
            return exact_matches / max(len(source), len(target))

        # If no exact matches, try partial word matching
        source_parts = set().union(*(get_artist_parts(a) for a in source))
        target_parts = set().union(*(get_artist_parts(a) for a in target))

        if not source_parts or not target_parts:
            return 0.0

        # Calculate Jaccard similarity of word parts
        intersection = len(source_parts.intersection(target_parts))
        union = len(source_parts.union(target_parts))

        # Reduce score for partial matches
        return 0.8 * (intersection / union if union > 0 else 0)

    def ensure_float(value):
        """Safely convert a numeric or list of numerics to a float."""
        if isinstance(value, list):
            return float(sum(value) / len(value)) if value else 0.0
        return float(value)

    def find_closest_match(self, song, tracks):
        """Find best matching tracks using string similarity with dynamic weights."""
        matches = []

        # Default config with only title, artist, and album weights
        config_match = { # Renamed to avoid conflict with beets.config
            'weights': {
                'title': 0.45,      # Title most important
                'artist': 0.35,     # Artist next
                'album': 0.20,      # Album title
            }
        }

        # Create a temporary beets Item for comparison
        temp_item = Item()
        temp_item.title = song.get('title', '').strip()
        temp_item.artist = song.get('artist', '').strip()
        temp_item.album = song.get('album', '').strip() if song.get('album') else ''

        for track in tracks:
            score, dist = plex_track_distance(temp_item, track, config_match) # Use renamed config_match
            matches.append((track, score))

            # Debug logging - simpler format with positional args
            self._log.debug("Track: {} - {}, Score: {:.3f}",
                          track.parentTitle, track.title, score)

        # Sort by score descending
        matches.sort(key=lambda x: x[1], reverse=True)
        return matches

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
                for i_word, target_word_val in enumerate(target_words): # renamed i to i_word
                    word_matched = False
                    clean_target_word = re.sub(r'[^\w]', '', target_word_val.lower())

                    for source_word in source_words:
                        clean_source_word = re.sub(r'[^\w]', '', source_word.lower())
                        # Only match on actual words, not substrings within words
                        if (clean_source_word == clean_target_word or
                            self.get_fuzzy_score(clean_source_word, clean_target_word) > 0.8):
                            # Use the original formatting from target
                            highlighted_words.append(ui.colorize('text_success', target.split()[i_word]))
                            word_matched = True
                            break

                    if not word_matched:
                        highlighted_words.append(target.split()[i_word])

                return ' '.join(highlighted_words)

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
            # Cache the result for the current song query
            self._cache_result(song, selected_track)
            self._log.debug("Cached result for current song query: {}", song)

            # ALWAYS cache for the original query that led to this manual search
            if original_query is not None and original_query != song:
                self._log.debug("Also caching result for original query key: {}", original_query)
                self._cache_result(original_query, selected_track)

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
                title_match = not title or self.get_fuzzy_score(title.lower(), track_title.lower()) > 0.4
                album_match = not album or self.get_fuzzy_score(album.lower(), track_album.lower()) > 0.4

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
                strong_title = title and self.get_fuzzy_score(title.lower(), track_title.lower()) > 0.8
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
            sorted_tracks_manual = self.find_closest_match(song_dict, filtered_tracks) # Renamed to avoid conflict

            # Use beets UI formatting for the query header
            print_(ui.colorize('text_highlight', '\nChoose candidates for: ') +
                   ui.colorize('text_highlight_minor', f"{album} - {title} - {artist}"))

            # Format and display the matches
            for i, (track_match, score) in enumerate(sorted_tracks_manual, start=1): # Use renamed var
                track_artist_match = getattr(track_match, 'originalTitle', None) or track_match.artist().title # Use renamed var

                # Use beets' similarity detection for highlighting
                def highlight_matches_manual(source, target): # Renamed to avoid conflict
                    """Highlight exact matching parts between source and target strings."""
                    if source is None or target is None:
                        return target or "Unknown"

                    # Split both strings into words while preserving spaces
                    source_words = source.replace(',', ' ,').split()
                    target_words = target.replace(',', ' ,').split()

                    # Process each target word
                    highlighted_words = []
                    for target_word in target_words:
                        word_matched = False
                        for source_word in source_words:
                            if self.get_fuzzy_score(source_word.lower(), target_word.lower()) > 0.8:
                                highlighted_words.append(ui.colorize('added_highlight', target_word))
                                word_matched = True
                                break
                        if not word_matched:
                            highlighted_words.append(target_word)

                    return ' '.join(highlighted_words)

                # Highlight matching parts
                highlighted_title = highlight_matches_manual(title, track_match.title) # Use renamed var
                highlighted_album = highlight_matches_manual(album, track_match.parentTitle) # Use renamed var
                highlighted_artist = highlight_matches_manual(artist, track_artist_match) # Use renamed var

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
                numrange=(1, len(sorted_tracks_manual)), # Use renamed var
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

            selected_track = sorted_tracks_manual[sel - 1][0] if sel > 0 else None # Use renamed var
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
            manual_search = config["plexsync"]["manual_search"].get(bool)        # Check cache first
        cache_key = self.cache._make_cache_key(song)
        cached_result = self.cache.get(cache_key)
        if cached_result is not None:
            if isinstance(cached_result, tuple):
                rating_key, cleaned_metadata = cached_result
                if rating_key == -1 or rating_key is None:  # Handle both None and -1
                    if cleaned_metadata and not llm_attempted:
                        self._log.debug("Using cached cleaned metadata: {}", cleaned_metadata)
                        result = self.search_plex_song(cleaned_metadata, manual_search, llm_attempted=True)

                        # If we found a match using cached cleaned metadata, update the original cache entry
                        if result is not None:
                            self._log.debug("Cached cleaned metadata search succeeded, updating original cache: {}", song)
                            self._cache_result(cache_key, result)

                        return result
                    return None  # Return None if we have a negative cache result
                try:
                    if rating_key:  # Only try to fetch if we have a valid rating key
                        return self.music.fetchItem(rating_key)
                except Exception as e:
                    self._log.debug("Failed to fetch cached item {}: {}", rating_key, e)
            else:  # Legacy cache entry
                if cached_result == -1 or cached_result is None:
                    return None
                try:
                    if cached_result:  # Only try to fetch if we have a valid rating key
                        return self.music.fetchItem(cached_result)
                except Exception as e:
                    self._log.debug("Failed to fetch cached item {}: {}", cached_result, e)

        # Try regular search
        # Ensure song["artist"] is not None before splitting
        try:
            if song["artist"] is None:
                song["artist"] = ""
            if song["album"] is None:
                tracks = self.music.searchTracks(**{"track.title": song["title"]}, limit=50)
            else:
                tracks = self.music.searchTracks(
                    **{"album.title": song["album"], "track.title": song["title"]}, limit=50
                )
                if len(tracks) == 0:
                    # Try with simplified title (no parentheses)
                    song["title"] = clean_string(song["title"])
                    tracks = self.music.searchTracks(**{"track.title": song["title"]}, limit=50)
            if song["title"] is None or song["title"] == "" and song["album"] and song["artist"]:
                tracks = self.music.searchTracks(
                    **{"album.title": song["album"], "artist.title": song["artist"]}, limit=50
                )
        except Exception as e:
            self._log.debug(
                "Error searching for {} - {}. Error: {}",
                song["album"],
                song["title"],
                e,
            )
            return None

        # Process search results
        if len(tracks) == 1:
            result = tracks[0]
            self._cache_result(cache_key, result)
            return result
        elif len(tracks) > 1:
            sorted_tracks_plex = self.find_closest_match(song, tracks) # Renamed
            self._log.debug("Found {} tracks for {}", len(sorted_tracks_plex), song["title"]) # Use renamed

            # Try manual search first if enabled and we have matches
            if manual_search and len(sorted_tracks_plex) > 0: # Use renamed
                return self._handle_manual_search(sorted_tracks_plex, song, original_query=song) # Use renamed

            # Otherwise try automatic matching with improved threshold
            best_match = sorted_tracks_plex[0] # Use renamed
            if best_match[1] >= 0.8:  # Require 80% match score for automatic matching
                self._cache_result(cache_key, best_match[0])
                return best_match[0]

        # Try LLM cleaning if enabled and not already attempted
        if not llm_attempted and self.search_llm and config["plexsync"]["use_llm_search"].get(bool):
            search_query = f"{song['title']} by {song['artist']}"
            if song.get('album'):
                search_query += f" from {song['album']}"

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
                self._log.debug("Using LLM cleaned metadata: {}", cleaned_song)                # Cache the original query with cleaned metadata
                self._cache_result(cache_key, None, cleaned_song)

                # Try search with cleaned metadata
                result = self.search_plex_song(cleaned_song, manual_search, llm_attempted=True)

                # If we found a match using LLM-cleaned metadata, also cache it for the original query
                if result is not None:
                    self._log.debug("LLM-cleaned search succeeded, also caching for original query: {}", song)
                    self._cache_result(cache_key, result)

                return result        # Final fallback: try manual search if enabled
        if manual_search:
            self._log.info(
                "\nTrack {} - {} - {} not found in Plex".format(
                song.get("album", "Unknown"),
                song.get("artist", "Unknown"),
                song["title"])
            )
            if ui.input_yn(ui.colorize('text_highlight', "\nSearch manually?") + " (Y/n)"):
                result = self.manual_track_search(song)
                # If manual search succeeds, cache it for the original query
                if result is not None:
                    self._log.debug("Manual search succeeded, caching for original query: {}", song)
                    self._cache_result(cache_key, result)
                return result

        # Store negative result if nothing found
        self._cache_result(cache_key, None)
        return None

    def _process_matches(self, tracks, song, manual_search):
        """Helper function to process multiple track matches."""
        artist = song["artist"].split(",")[0]
        sorted_tracks_proc = self.find_closest_match(song, tracks) # Renamed
        self._log.debug("Found {} tracks for {}", len(sorted_tracks_proc), song["title"]) # Use renamed

        if manual_search and len(sorted_tracks_proc) > 0: # Use renamed
            return self._handle_manual_search(sorted_tracks_proc, song) # Use renamed

        result = None
        for track, score in sorted_tracks_proc: # Use renamed
            if track.originalTitle is not None:
                plex_artist = track.originalTitle
            else:
                plex_artist = track.artist().title
            if artist in plex_artist:
                result = track
                break

        if result is not None:
            cache_key = json.dumps(song) # This might be too simple for dicts; consider sorted tuple of items
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
            songs = []
            # Determine if playlist_url is a URL or a file path for M3U8
            if playlist_url and (playlist_url.lower().startswith("http://") or playlist_url.lower().startswith("https://")):
                # It's a URL, use the URL importer
                # Ensure Spotify is authenticated if it's a Spotify URL
                if "spotify" in playlist_url.lower():
                    self._ensure_spotify_authenticated() # Make sure self.sp is available

                songs = playlist_importers.import_playlist_from_url(
                    playlist_url,
                    self.cache,
                    self.headers,
                    getattr(self, 'sp', None) # Pass authenticated Spotify instance if available
                )
            elif playlist_url and playlist_url.lower().endswith('.m3u8'):
                # It's an M3U8 file path
                songs = playlist_importers.import_from_m3u8_file(playlist_url, self.cache, self.config_dir)
            else:
                if not playlist_url:
                    raise ui.UserError("Playlist URL or file path not provided.")
                else:
                    # Fallback for unrecognized format or if it's a non-URL, non-M3U8 string.
                    # Try treating as a URL as a last resort if it wasn't caught by http/https check.
                    self._log.warning("Unclear playlist source format for '{}'. Attempting as URL.", playlist_url)
                    songs = playlist_importers.import_playlist_from_url(
                        playlist_url, self.cache, self.headers, getattr(self, 'sp', None)
                    )
                    if not songs: # If still no songs, then it's truly unsupported or invalid
                         raise ui.UserError(f"Unsupported or invalid playlist URL/file: {playlist_url}")

            if songs:
                self._log.info("Importing {} songs from source: {}", len(songs), playlist_url)
                self.add_songs_to_plex(playlist, songs, config["plexsync"]["manual_search"].get(bool))
            else:
                # Log this case, but might not be an error if playlist was empty or URL invalid (handled by importer)
                self._log.warning("No songs were imported from source: {}. Playlist in Plex will not be updated with new tracks from this source.", playlist_url)

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

        plex_utils.plex_add_playlist_item(self.plex, song_list, playlist) # Call util function

    def _plex_import_search(self, playlist, search, limit=10):
        """Import search results into Plex using the new importer."""
        self._log.info("Importing from YouTube search: '{}' into playlist '{}'", search, playlist)
        songs_from_search = playlist_importers.import_from_youtube_search(search, int(limit), self.cache)

        song_list_to_add_in_plex = []
        if songs_from_search:
            for song_data in songs_from_search:
                manual_search_for_import = config["plexsync"]["manual_search"].get(bool)
                found_plex_track = self.search_plex_song(song_data, manual_search=manual_search_for_import)
                if found_plex_track:
                    song_list_to_add_in_plex.append(found_plex_track)

        if song_list_to_add_in_plex:
            plex_utils.plex_add_playlist_item(self.plex, song_list_to_add_in_plex, playlist)
            self._log.info("Added {} tracks from YouTube search '{}' to playlist '{}'", len(song_list_to_add_in_plex), search, playlist)
        else:
            self._log.warning("No tracks found or matched from YouTube search '{}' to add to playlist '{}'", search, playlist)

    def _plex_clear_playlist(self, playlist):
        plex_utils.plex_clear_playlist(self.plex, playlist) # Call util function

    def _plex_sonicsage(self, number, prompt, playlist_name_sonic, clear_playlist_sonic): # Renamed playlist, clear
        """Generate song recommendations using LLM based on a given prompt."""
        if not self.llm_client: # Check if client is initialized by setup_llm_wrapper
            self._log.error("LLM client not configured or failed to initialize. Cannot get recommendations.")
            return
        if not prompt:
            self._log.error("Prompt not provided for SonicSage.")
            return

        # Call the utility function for recommendations
        recommendations = llm_utils.get_llm_song_recommendations(
            llm_client=self.llm_client,
            model_name=config["llm"]["model"].get(),
            num_songs=int(number),
            user_prompt=prompt
        )

        if not recommendations or not recommendations.songs:
            self._log.warning("No recommendations received from LLM or recommendations list is empty.")
            return

        song_list_to_match = []
        for rec_song in recommendations.songs:
            # Ensure year is an int if possible, else None
            year_val = None
            if rec_song.year and isinstance(rec_song.year, str) and rec_song.year.isdigit():
                year_val = int(rec_song.year)
            elif isinstance(rec_song.year, int): # If it's already an int
                year_val = rec_song.year

            song_dict = {
                "title": rec_song.title.strip() if rec_song.title else "",
                "album": rec_song.album.strip() if rec_song.album else "", # Handle None album
                "artist": rec_song.artist.strip() if rec_song.artist else "",
                "year": year_val,
            }
            song_list_to_match.append(song_dict)

        self._log.debug(
            "{} songs recommended by LLM to be matched in Plex: {}", len(song_list_to_match), song_list_to_match
        )

        matched_plex_songs = []
        for song_item_llm in song_list_to_match: # Renamed song_item
            # Use manual_search setting from config for searching these new tracks
            manual_search_sonic = config["plexsync"]["manual_search"].get(bool)
            found_plex_track = self.search_plex_song(song_item_llm, manual_search=manual_search_sonic)
            if found_plex_track:
                matched_plex_songs.append(found_plex_track)

        self._log.debug("Songs matched in Plex library: {}", matched_plex_songs)

        if clear_playlist_sonic: # Use renamed var
            try:
                plex_utils.plex_clear_playlist(self.plex, playlist_name_sonic) # Use renamed var
            except exceptions.NotFound:
                self._log.debug(f"Unable to clear playlist {playlist_name_sonic} (not found).") # Use renamed var

        if matched_plex_songs:
            try:
                plex_utils.plex_add_playlist_item(self.plex, matched_plex_songs, playlist_name_sonic) # Use renamed var
                self._log.info(f"Added {len(matched_plex_songs)} LLM recommended songs to playlist '{playlist_name_sonic}'.")
            except Exception as e:
                self._log.error(f"Unable to add LLM recommended songs to playlist '{playlist_name_sonic}'. Error: {e}")
        else:
            self._log.info(f"No LLM recommended songs were matched in Plex to add to playlist '{playlist_name_sonic}'.")


    def setup_llm(self): # Renamed from setup_llm_client to match original call
        """Setup LLM client using OpenAI-compatible API."""
        # This now acts as a wrapper to call the utility.
        # The llm_client is stored on self.
        self.llm_client = llm_utils.setup_llm_client(
            api_key=config["llm"]["api_key"].get(),
            base_url=config["llm"]["base_url"].get() # Pass base_url which might be empty
        )
        if not self.llm_client:
            self._log.error("LLM Client setup failed. LLM functionalities might not work.")

        # Setup search_llm if configured separately (original logic from __init__)
        # This part assumes search_llm might use a different configuration or client.
        # If search_llm is the same as llm_client, this can be simplified.
        if config["plexsync"]["use_llm_search"].get(bool):
            search_provider = config["llm"]["search"]["provider"].get(str)
            if search_provider == "ollama": # Assuming this means the custom search_track_info via POST
                # The original search_track_info in beetsplug.llm does its own requests.post
                # So, self.search_llm here might just be a flag or a simple client if needed for that.
                # For now, if it's 'ollama' for search, we rely on the existing `search_track_info`
                # which is imported from `beetsplug.llm` and doesn't use `self.search_llm` client directly.
                # So, self.search_llm could be set to self.llm_client if the generic client is also
                # to be used for some search functions, or None if search is entirely separate.
                # Based on current `search_track_info` in `beetsplug.llm`, it doesn't use an OpenAI client.
                # So, setting self.search_llm = self.llm_client might be misleading if search_track_info is called.
                # Let's stick to the original logic: self.search_llm = self.llm_client implies the main client is used for search tasks.
                # If a separate client for search is desired via llm_utils:
                # self.search_llm = llm_utils.setup_llm_client(
                # api_key=config["llm"]["search"]["api_key"].get() or config["llm"]["api_key"].get(),
                # base_url=config["llm"]["search"]["base_url"].get()
                # )
                # For now, assume if use_llm_search is true, the main llm_client is also the search_llm client.
                self.search_llm = self.llm_client
            else: # If provider is not ollama, assume it uses the main llm_client
                 self.search_llm = self.llm_client

            if not self.search_llm:
                 self._log.warning("Search LLM client setup failed or not configured for separate search.")


    # get_llm_recommendations and extract_json are now part of llm_utils.get_llm_song_recommendations
    # import_yt_playlist, import_yt_search, import_tidal_playlist, import_gaana_playlist
    # are now handled by playlist_importers.py.
    # These direct methods can be removed.

    def _plex2spotify(self, lib, playlist_name_to_transfer):
        self._ensure_spotify_authenticated() # Use the wrapper
        if not self.sp: # Check again after ensure_authenticated
            self._log.error("Spotify authentication failed. Cannot proceed with plex2spotify.")
            return

        try:
            plex_playlist_obj = self.plex.playlist(playlist_name_to_transfer)
            if not plex_playlist_obj:
                self._log.error(f"Plex playlist '{playlist_name_to_transfer}' not found.")
                return
            plex_items = plex_playlist_obj.items()
        except exceptions.NotFound:
            self._log.error(f"Plex playlist '{playlist_name_to_transfer}' not found.")
            return
        except Exception as e:
            self._log.error(f"Error fetching Plex playlist '{playlist_name_to_transfer}': {e}")
            return

        spotify_user_id = self.sp.current_user()["id"]

        spotify_utils.transfer_plex_playlist_to_spotify(
            plex_playlist_items=plex_items,
            beets_lib=lib,
            sp_instance=self.sp,
            spotify_user_id=spotify_user_id,
            target_playlist_name=playlist_name_to_transfer,
            plex_lookup_func=self.build_plex_lookup,
            search_llm_instance=self.search_llm
        )

    # add_tracks_to_spotify_playlist is fully moved to spotify_utils and called via transfer_plex_playlist_to_spotify

    def get_config_value(self, item_cfg, defaults_cfg, key, code_default): # Remains in PlexSync
        if key in item_cfg: val = item_cfg[key]; return val.get() if hasattr(val, "get") else val
        if key in defaults_cfg: val = defaults_cfg[key]; return val.get() if hasattr(val, "get") else val
        return code_default

    def build_plex_lookup(self, lib): # Remains in PlexSync
        """Build a lookup dictionary mapping Plex rating keys to beets items.

        Args:
            lib: The beets library instance

        Returns:
            dict: A dictionary mapping Plex rating keys to their corresponding beets items
        """
        self._log.debug("Building lookup dictionary for Plex rating keys")
        plex_lookup = {}
        for item in lib.items():
            if hasattr(item, "plex_ratingkey"):
                plex_lookup[item.plex_ratingkey] = item
        return plex_lookup

    # Smart playlist generation logic (get_preferred_attributes, scoring, selection, filtering, generate_daily_discovery, get_filtered_library_tracks, generate_forgotten_gems)
    # has been moved to smart_playlists.py.
    # The main _plex_smartplaylists method will be updated to call functions from smart_playlists.py.

    def generate_imported_playlist(self, lib, playlist_config, plex_lookup=None):
        """Generate a playlist by importing from external sources."""
        playlist_name = playlist_config.get("name", "Imported Playlist")
        sources_conf = playlist_config.get("sources", []) # Renamed sources
        max_tracks = playlist_config.get("max_tracks", None)

        # Create log file path in beets config directory
        log_file = os.path.join(self.config_dir, f"{playlist_name.lower().replace(' ', '_')}_import.log")

        # Clear/create the log file
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(f"Import log for playlist: {playlist_name}\n")
            f.write(f"Import started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("-" * 80 + "\n\n")

        # Get config options with defaults
        if (
            "playlists" in config["plexsync"]
            and "defaults" in config["plexsync"]["playlists"]
        ):
            defaults_cfg = config["plexsync"]["playlists"]["defaults"].get({})
        else:
            defaults_cfg = {}

        manual_search_conf = self.get_config_value( # Renamed manual_search
            playlist_config, defaults_cfg, "manual_search", config["plexsync"]["manual_search"].get(bool)
        )
        clear_playlist_conf = self.get_config_value( # Renamed clear_playlist
            playlist_config, defaults_cfg, "clear_playlist", False
        )

        if not sources_conf: # Use renamed var
            self._log.warning("No sources defined for imported playlist {}", playlist_name)
            return

        self._log.info("Generating imported playlist {} from {} sources", playlist_name, len(sources_conf)) # Use renamed var

        # Import tracks from all sources
        all_tracks = []
        not_found_count = 0

        for source_item in sources_conf: # Renamed source, Use renamed var
            try:
                self._log.info("Importing from source: {}", source_item) # Use renamed var
                if isinstance(source_item, str):  # Handle string sources (URLs and file paths) # Use renamed var
                    if source_item.lower().endswith('.m3u8'): # Use renamed var
                        # Check if path is absolute, if not make it relative to config dir
                        if not os.path.isabs(source_item): # Use renamed var
                            source_item_path = os.path.join(self.config_dir, source_item) # Renamed source to source_item_path
                        else:
                            source_item_path = source_item # Use renamed var
                        tracks_imported = self.import_m3u8_playlist(source_item_path) # Renamed tracks, Use renamed var
                    elif "spotify" in source_item: # Use renamed var
                        spotify_playlist_id_imp = self.get_spotify_playlist_id_from_url(source_item) # Use renamed var
                        if spotify_playlist_id_imp:
                            tracks_imported = self.import_spotify_playlist(spotify_playlist_id_imp) # Renamed tracks
                        else:
                            tracks_imported = []
                            self._log.error(f"Could not extract Spotify playlist ID from URL: {source_item}")
                    elif "jiosaavn" in source_item: # Use renamed var
                        tracks_imported = self.import_jiosaavn_playlist(source_item) # Renamed tracks, Use renamed var
                    elif "apple" in source_item: # Use renamed var
                        tracks_imported = self.import_apple_playlist(source_item) # Renamed tracks, Use renamed var
                    elif "gaana" in source_item: # Use renamed var
                        tracks_imported = self.import_gaana_playlist(source_item) # Renamed tracks, Use renamed var
                    elif "youtube" in source_item: # Use renamed var
                        tracks_imported = self.import_yt_playlist(source_item) # Renamed tracks, Use renamed var
                    elif "tidal" in source_item: # Use renamed var
                        tracks_imported = self.import_tidal_playlist(source_item) # Renamed tracks, Use renamed var
                    else:
                        self._log.warning("Unsupported source: {}", source_item) # Use renamed var
                        continue
                elif isinstance(source_item, dict) and source_item.get("type") == "post": # Use renamed var
                    tracks_imported = self.import_post_playlist(source_item) # Renamed tracks, Use renamed var
                else:
                    self._log.warning("Invalid source format: {}", source_item) # Use renamed var
                    continue

                if tracks_imported: # Use renamed var
                    all_tracks.extend(tracks_imported) # Use renamed var

            except Exception as e:
                self._log.error("Error importing from {}: {}", source_item, e) # Use renamed var
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(f"Error importing from source {source_item}: {str(e)}\n") # Use renamed var
                continue

        if not all_tracks:
            self._log.warning("No tracks found from any source for playlist {}", playlist_name)
            return

        # Initialize enlighten manager and progress bar for all tracks
        manager = enlighten.get_manager()
        progress_bar = manager.counter(
            total=len(all_tracks),
            desc=f"Processing tracks for {playlist_name}",
            unit="tracks"
        )

        # Process tracks through Plex first
        matched_songs = []
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write("\nTracks not found in Plex library:\n")
            f.write("-" * 80 + "\n")

        for track_match_plex in all_tracks: # Renamed track
            found = self.search_plex_song(track_match_plex, manual_search_conf) # Use renamed var, Use renamed var
            if found:
                # Just use Plex rating directly
                plex_rating = float(getattr(found, "userRating", 0) or 0)

                if plex_rating == 0 or plex_rating > 2:  # Include unrated or rating > 2
                    matched_songs.append(found)
                    self._log.debug(
                        "Matched in Plex: {} - {} - {} (Rating: {})",
                        track_match_plex.get('artist', 'Unknown'), # Use renamed var
                        track_match_plex.get('parentTitle', 'Unknown'), # Use renamed var
                        track_match_plex.get('title', 'Unknown'), # Use renamed var
                        plex_rating
                    )
                else:
                    with open(log_file, 'a', encoding='utf-8') as f:
                        f.write(f"Low rated ({plex_rating}): {track_match_plex.get('artist', 'Unknown')} - {track_match_plex.get('parentTitle', 'Unknown')} - {track_match_plex.get('title', 'Unknown')}\n") # Use renamed var
            else:
                not_found_count += 1
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(f"Not found: {track_match_plex.get('artist', 'Unknown')} - {track_match_plex.get('parentTitle', 'Unknown')} - {track_match_plex.get('title', 'Unknown')}\n") # Use renamed var

            # Update progress bar
            progress_bar.update()

        # Complete and close progress bar
        progress_bar.close()
        manager.stop()

        # Get filters from config and apply them
        filters_imported = playlist_config.get("filters", {}) # Renamed filters
        if filters_imported: # Use renamed var
            self._log.debug("Applying filters to {} matched tracks...", len(matched_songs))

            # Convert Plex tracks to beets items first
            beets_items = []

            # Use provided lookup dictionary or build new one if not provided
            if plex_lookup is None:
                self._log.debug("Building Plex lookup dictionary...")
                plex_lookup = self.build_plex_lookup(lib)

            for track_to_beets in matched_songs: # Renamed track
                try:
                    beets_item = plex_lookup.get(track_to_beets.ratingKey) # Use renamed var
                    if beets_item:
                        beets_items.append(beets_item)
                except Exception as e:
                    self._log.debug("Error finding beets item for {}: {}", track_to_beets.title, e) # Use renamed var
                    continue

            # Now apply filters to beets items
            filtered_items = []
            for item_filter_imp in beets_items: # Renamed item
                include_item = True

                if 'exclude' in filters_imported: # Use renamed var
                    if 'years' in filters_imported['exclude']: # Use renamed var
                        years_config = filters_imported['exclude']['years'] # Use renamed var
                        if 'after' in years_config and item_filter_imp.year: # Use renamed var
                            if item_filter_imp.year > years_config['after']: # Use renamed var
                                include_item = False
                                self._log.debug("Excluding {} (year {} > {})",
                                    item_filter_imp.title, item_filter_imp.year, years_config['after']) # Use renamed var
                        if 'before' in years_config and item_filter_imp.year: # Use renamed var
                            if item_filter_imp.year < years_config['before']: # Use renamed var
                                include_item = False
                                self._log.debug("Excluding {} (year {} < {})",
                                    item_filter_imp.title, item_filter_imp.year, years_config['before']) # Use renamed var

                if include_item:
                    filtered_items.append(item_filter_imp) # Use renamed var

            self._log.debug("After filtering: {} tracks remain", len(filtered_items))
            matched_songs = filtered_items

        # Deduplicate based on ratingKey for Plex Track objects and plex_ratingkey for beets items
        seen = set()
        unique_matched = []
        for song_unique_imp in matched_songs: # Renamed song
            # Try both ratingKey (Plex Track) and plex_ratingkey (beets Item)
            rating_key = (
                getattr(song_unique_imp, 'ratingKey', None)  # For Plex Track objects, Use renamed var
                or getattr(song_unique_imp, 'plex_ratingkey', None)  # For beets Items, Use renamed var
            )
            if rating_key and rating_key not in seen:
                seen.add(rating_key)
                unique_matched.append(song_unique_imp) # Use renamed var
        # Apply track limit if specified
        if max_tracks:
            unique_matched = unique_matched[:max_tracks]

        # Write summary at the end of log file
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"\nImport Summary:\n")
            f.write("-" * 80 + "\n")
            f.write(f"Total tracks from sources: {len(all_tracks)}\n")
            f.write(f"Tracks not found in Plex: {not_found_count}\n")
            f.write(f"Tracks matched and added: {len(matched_songs)}\n") # Should be len(unique_matched) here?
            f.write(f"\nImport completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

        self._log.info(
            "Found {} unique tracks after filtering (see {} for details)",
            len(unique_matched), log_file
        )

        # Create or update playlist based on clear_playlist setting
        if clear_playlist_conf: # Use renamed var
            try:
                plex_utils.plex_clear_playlist(self.plex, playlist_name)
                self._log.info("Cleared existing playlist {}", playlist_name)
            except exceptions.NotFound:
                self._log.debug("No existing playlist {} found", playlist_name)

        if unique_matched:
            plex_utils.plex_add_playlist_item(self.plex, unique_matched, playlist_name)
            self._log.info(
                "Successfully created playlist {} with {} tracks",
                playlist_name,
                len(unique_matched)
            )
        else:
            self._log.warning("No tracks remaining after filtering for {}", playlist_name)

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

        def parse_track_info_log(line): # Renamed parse_track_info
            """Helper function to parse track info from log line."""
            try:
                _, track_info_str = line.split("Not found:", 1) # Renamed track_info
                # First try to find the Unknown album marker as a separator
                parts = track_info_str.split(" - Unknown - ") # Use renamed var
                if len(parts) == 2:
                    artist = parts[0].strip()
                    title = parts[1].strip()
                    album = "Unknown"
                else:
                    # Fallback to traditional parsing if no "Unknown" found
                    parts = track_info_str.strip().split(" - ") # Use renamed var
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

        for log_file_item in log_files: # Renamed log_file
            playlist_name = log_file_item.stem.replace("_import", "").replace("_", " ").title() # Use renamed var
            self._log.info("Processing failed imports for playlist: {}", playlist_name)

            # Read the entire log file
            with open(log_file_item, 'r', encoding='utf-8') as f: # Use renamed var
                log_content = f.readlines()

            tracks_to_import = []
            track_lines_to_remove = set()
            in_not_found_section = False
            header_lines = []
            summary_lines = []
            not_found_start = -1

            # First pass: collect tracks and identify sections
            for i_log, line_log in enumerate(log_content): # Renamed i, line
                if "Tracks not found in Plex library:" in line_log: # Use renamed var
                    in_not_found_section = True
                    not_found_start = i_log # Use renamed var
                    continue
                elif "Import Summary:" in line_log: # Use renamed var
                    in_not_found_section = False
                    summary_lines = log_content[i_log:] # Use renamed var
                    break

                if i_log < not_found_start: # Use renamed var
                    header_lines.append(line_log) # Use renamed var
                elif in_not_found_section and line_log.startswith("Not found:"): # Use renamed var
                    track_info_parsed = parse_track_info_log(line_log) # Renamed, Use renamed var
                    if track_info_parsed: # Use renamed var
                        track_info_parsed["line_num"] = i_log # Use renamed var, Use renamed var
                        tracks_to_import.append(track_info_parsed) # Use renamed var

            if tracks_to_import:
                self._log.info("Attempting to manually import {} tracks for {}",
                             len(tracks_to_import), playlist_name)

                matched_tracks_log = [] # Renamed
                for track_log_item in tracks_to_import: # Renamed track
                    found = self.search_plex_song(track_log_item, manual_search=True) # Use renamed var
                    if found:
                        matched_tracks_log.append(found) # Use renamed var
                        track_lines_to_remove.add(track_log_item["line_num"]) # Use renamed var
                        total_imported += 1
                    else:
                        total_failed += 1

                if matched_tracks_log: # Use renamed var
                    plex_utils.plex_add_playlist_item(self.plex, matched_tracks_log, playlist_name) # Use renamed var
                    self._log.info("Added {} tracks to playlist {}",
                                 len(matched_tracks_log), playlist_name) # Use renamed var
                    # Update the log file
                    remaining_not_found = [
                        line_rem for i_rem, line_rem in enumerate(log_content) # Renamed line, i
                        if i_rem not in track_lines_to_remove # Use renamed var
                    ]

                    # Update summary
                    new_summary = []
                    for line_sum in summary_lines: # Renamed line
                        if "Tracks not found in Plex" in line_sum: # Use renamed var
                            remaining_not_found_count = len([
                                l_rem for l_rem in remaining_not_found # Renamed l
                                if l_rem.startswith("Not found:") # Use renamed var
                            ])
                            new_summary.append(f"Tracks not found in Plex: {remaining_not_found_count}\n")
                        else:
                            new_summary.append(line_sum) # Use renamed var

                    # Write updated log file
                    with open(log_file_item, 'w', encoding='utf-8') as f: # Use renamed var
                        f.writelines(header_lines)
                        f.write("Tracks not found in Plex library:\n")
                        f.writelines(l_fnl for l_fnl in remaining_not_found if l_fnl.startswith("Not found:")) # Renamed l
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
        similar_tracks_smart = None # Renamed similar_tracks
        if any(p.get("id") in ["daily_discovery", "forgotten_gems"] for p in playlists_config):
            from beetsplug.smart_playlists import get_preferred_attributes_from_history
            preferred_genres, similar_tracks_smart = get_preferred_attributes_from_history(self.music, config["plexsync"]["history_days"].get(int), config["plexsync"]["exclusion_days"].get(int))
            self._log.debug("Using preferred genres: {}", preferred_genres)
            self._log.debug("Processing {} pre-filtered similar tracks", len(similar_tracks_smart)) # Use renamed var

        # Process each playlist
        for p_conf in playlists_config: # Renamed p
            playlist_type = p_conf.get("type", "smart") # Use renamed var
            playlist_id_conf = p_conf.get("id") # Renamed playlist_id, Use renamed var
            playlist_name_conf = p_conf.get("name", "Unnamed playlist") # Renamed playlist_name, Use renamed var

            if (playlist_type == "imported"):
                self.generate_imported_playlist(lib, p_conf, plex_lookup)  # Pass plex_lookup, Use renamed var
            elif playlist_id_conf in ["daily_discovery", "forgotten_gems"]: # Use renamed var
                if playlist_id_conf == "daily_discovery": # Use renamed var
                    from beetsplug.smart_playlists import generate_daily_discovery_playlist
                    generate_daily_discovery_playlist(
                        self.music, lib, plex_lookup, preferred_genres, similar_tracks_smart,
                        p_conf, config["plexsync"]["playlists"]["defaults"].get({}),
                        lambda name: plex_utils.plex_clear_playlist(self.plex, name),
                        lambda tracks, name: plex_utils.plex_add_playlist_item(self.plex, tracks, name)
                    )
                else:  # forgotten_gems
                    from beetsplug.smart_playlists import generate_forgotten_gems_playlist
                    generate_forgotten_gems_playlist(
                        self.music, lib, plex_lookup, p_conf, config["plexsync"]["playlists"]["defaults"].get({}),
                        None, similar_tracks_smart,
                        lambda name: plex_utils.plex_clear_playlist(self.plex, name),
                        lambda tracks, name: plex_utils.plex_add_playlist_item(self.plex, tracks, name)
                    )
            else:
                self._log.warning(
                    "Unrecognized playlist configuration '{}' - type: '{}', id: '{}'. "
                    "Valid types are 'imported' or 'smart'. "
                    "Valid smart playlist IDs are 'daily_discovery' and 'forgotten_gems'.",
                    playlist_name_conf, playlist_type, playlist_id_conf # Use renamed vars
                )

    def shutdown(self, lib):
        """Clean up when plugin is disabled."""
        if self.loop and not self.loop.is_closed():
            self.close()