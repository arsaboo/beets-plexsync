"""Update and sync Plex music library.

Plex users enter the Plex Token to enable updating.
Put something like the following in your config.yaml to configure:
    plex:
        host: localhost
        token: token
"""

import asyncio
import difflib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

import confuse
import dateutil.parser
import openai
import requests
import spotipy
from beets import config, ui
from beets.dbcore import types
from beets.dbcore.query import MatchQuery
from beets.library import Item, DateType  # Added Item to import
from beets.plugins import BeetsPlugin
from beets.ui import input_, print_
from bs4 import BeautifulSoup
from jiosaavn import JioSaavn
from openai import OpenAI
from plexapi import exceptions
from plexapi.server import PlexServer
from pydantic import BaseModel, Field
from requests.exceptions import ConnectionError, ContentDecodingError
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth

from beetsplug.caching import Cache
from beetsplug.llm import search_track_info
from beetsplug.matching import plex_track_distance, clean_string
import enlighten  # Add enlighten library import


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
            + ":" + str(config["plex"]["port"].get())
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
        ID = config["spotify"]["client_id"].get()
        SECRET = config["spotify"]["client_secret"].get()
        redirect_uri = "http://localhost/"
        scope = (
            "user-read-private user-read-email playlist-modify-public "
            "playlist-modify-private playlist-read-private"
        )

        # Create a SpotifyOAuth object with your credentials and scope
        self.auth_manager = SpotifyOAuth(
            client_id=ID,
            client_secret=SECRET,
            redirect_uri=redirect_uri,
            scope=scope,
            open_browser=False,
            cache_path=self.plexsync_token,
        )
        self.token_info = self.auth_manager.get_cached_token()
        if self.token_info is None:
            self.auth_manager.get_access_token(as_dict=True)
        need_token = self.auth_manager.is_token_expired(self.token_info)
        if need_token:
            new_token = self.auth_manager.refresh_access_token(
                self.token_info["refresh_token"]
            )
            self.token_info = new_token
        # Create a Spotify object with the auth_manager
        self.sp = spotipy.Spotify(auth=self.token_info.get("access_token"))

    def process_spotify_track(self, track):
        """Process a single Spotify track into a standardized format."""
        try:
            # Find and store the song title
            if ('From "' in track['name']) or ("From &quot" in track['name']):
                title_orig = track['name'].replace("&quot;", '"')
                title, album = self.parse_title(title_orig)
            else:
                title = track['name']
                album = self.clean_album_name(track['album']['name'])

            # Get year if available
            try:
                year = track['album'].get('release_date')
                if year:
                    year = dateutil.parser.parse(year, ignoretz=True)
            except (ValueError, KeyError, AttributeError):
                year = None

            # Get primary artist
            artist = track['artists'][0]['name'] if track['artists'] else "Unknown"

            return {
                "title": title.strip(),
                "album": album.strip(),
                "artist": artist.strip(),
                "year": year
            }
        except Exception as e:
            self._log.debug("Error processing Spotify track: {}", e)
            return None

    def import_spotify_playlist(self, playlist_id):
        """Import a Spotify playlist using API first, then fallback to scraping."""
        song_list = []

        # Check cache first
        cached_tracks = self.cache.get_playlist_cache(playlist_id, 'spotify_tracks')
        if (cached_tracks):
            self._log.info("Using cached track list for Spotify playlist {}", playlist_id)
            return cached_tracks

        # First try the API method
        try:
            # Check API cache
            cached_api_data = self.cache.get_playlist_cache(playlist_id, 'spotify_api')
            if (cached_api_data):
                songs = cached_api_data
            else:
                self.authenticate_spotify()
                songs = self.get_playlist_tracks(playlist_id)
                if (songs):
                    self.cache.set_playlist_cache(playlist_id, 'spotify_api', songs)

            if (songs):
                for song in songs:
                    if (track_data := self.process_spotify_track(song["track"])):
                        song_list.append(track_data)

                if (song_list):
                    self._log.info("Successfully imported {} tracks via Spotify API", len(song_list))
                    # Cache processed tracks
                    self.cache.set_playlist_cache(playlist_id, 'spotify_tracks', song_list)
                    return song_list

        except Exception as e:
            self._log.warning("Spotify API import failed: {}. Falling back to scraping.", e)

        # Web scraping fallback with caching
        cached_web_data = self.cache.get_playlist_cache(playlist_id, 'spotify_web')
        if cached_web_data:
            return cached_web_data

        try:
            playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"
            response = requests.get(playlist_url, headers=self.headers)
            if response.status_code != 200:
                self._log.error("Failed to fetch playlist page: {}", response.status_code)
                return song_list

            soup = BeautifulSoup(response.text, "html.parser")

            # Try to find metadata script
            meta_script = None
            for script in soup.find_all("script"):
                if script.string and "Spotify.Entity" in str(script.string):
                    meta_script = script
                    break

            if meta_script:
                # Extract JSON data
                json_text = re.search(r'Spotify\.Entity = ({.+});', str(meta_script.string))
                if json_text:
                    playlist_data = json.loads(json_text.group(1))
                    if 'tracks' in playlist_data:
                        for track in playlist_data['tracks']['items']:
                            if not track or not track.get('track'):
                                continue

                            track_data = track['track']
                            song_dict = {
                                'title': track_data.get('name', '').strip(),
                                'artist': track_data.get('artists', [{}])[0].get('name', '').strip(),
                                'album': track_data.get('album', {}).get('name', '').strip(),
                                'year': None
                            }

                            # Try to extract year from release date
                            release_date = track_data.get('album', {}).get('release_date')
                            if release_date:
                                try:
                                    song_dict['year'] = int(release_date[:4])
                                except (ValueError, TypeError):
                                    pass

                            song_list.append(song_dict)

            # Fallback to metadata tags if script parsing fails
            if not song_list:
                track_metas = soup.find_all("meta", {"name": "music:song"})
                for meta in track_metas:
                    track_url = meta.get("content", "")
                    if track_url:
                        try:
                            track_id = re.search(r'track/([a-zA-Z0-9]+)', track_url).group(1)
                            track_page = requests.get(
                                f"https://open.spotify.com/track/{track_id}",
                                headers=self.headers
                            )
                            if track_page.status_code == 200:
                                track_soup = BeautifulSoup(track_page.text, 'html.parser')
                                title = track_soup.find('meta', {'property': 'og:title'})
                                description = track_soup.find('meta', {'property': 'og:description'})

                                if title and description:
                                    desc_parts = description['content'].split(' · ')
                                    song_dict = {
                                        'title': title['content'].strip(),
                                        'artist': desc_parts[0].strip() if len(desc_parts) > 0 else '',
                                        'album': desc_parts[1].strip() if len(desc_parts) > 1 else '',
                                        'year': None
                                    }
                                    song_list.append(song_dict)

                        except Exception as e:
                            self._log.debug("Error processing track {}: {}", track_url, e)

            if song_list:
                self._log.info("Successfully scraped {} tracks from Spotify playlist", len(song_list))
                self.cache.set_playlist_cache(playlist_id, 'spotify_web', song_list)
                return song_list

        except Exception as e:
            self._log.error("Error scraping Spotify playlist: {}", e)
            return song_list

        return song_list

    def import_apple_playlist(self, url):
        """Import Apple Music playlist with caching."""
        # Generate cache key from URL
        playlist_id = url.split('/')[-1]

        # Check cache
        cached_data = self.cache.get_playlist_cache(playlist_id, 'apple')
        if (cached_data):
            self._log.info("Using cached Apple Music playlist data")
            return cached_data

        song_list = []

        try:
            # Send a GET request to the URL and get the HTML content
            response = requests.get(url, headers=self.headers)
            content = response.text

            # Create a BeautifulSoup object with the HTML content
            soup = BeautifulSoup(content, "html.parser")
            try:
                data = soup.find("script", id="serialized-server-data").text
            except AttributeError:
                self._log.debug("Error parsing Apple Music playlist")
                return None

            # load the data as a JSON object
            data = json.loads(data)

            # Extract songs from the sections
            try:
                songs = data[0]["data"]["sections"][1]["items"]
            except (KeyError, IndexError) as e:
                self._log.error("Failed to extract songs from Apple Music data: {}", e)
                return None

            # Loop through each song element
            for song in songs:
                try:
                    # Find and store the song title
                    title = song["title"].strip()
                    album = song["tertiaryLinks"][0]["title"]
                    # Find and store the song artist
                    artist = song["subtitleLinks"][0]["title"]
                    # Create a dictionary with the song information
                    song_dict = {
                        "title": title.strip(),
                        "album": album.strip(),
                        "artist": artist.strip(),
                    }
                    # Append the dictionary to the list of songs
                    song_list.append(song_dict)
                except (KeyError, IndexError) as e:
                    self._log.debug("Error processing song {}: {}", song.get("title", "Unknown"), e)
                    continue

            if (song_list):
                self.cache.set_playlist_cache(playlist_id, 'apple', song_list)
                self._log.info("Cached {} tracks from Apple Music playlist", len(song_list))

        except Exception as e:
            self._log.error("Error importing Apple Music playlist: {}", e)
            return []

        return song_list

    def import_jiosaavn_playlist(self, url):
        """Import JioSaavn playlist with caching."""
        playlist_id = url.split('/')[-1]

        # Check cache first
        cached_data = self.cache.get_playlist_cache(playlist_id, 'jiosaavn')
        if (cached_data):
            self._log.info("Using cached JioSaavn playlist data")
            return cached_data

        # Initialize empty song list
        song_list = []

        try:
            loop = self.get_event_loop()

            # Run the async operation and get results
            data = loop.run_until_complete(
                self.saavn.get_playlist_songs(url, page=1, limit=100)
            )

            if not data or "data" not in data or "list" not in data["data"]:
                self._log.error("Invalid response from JioSaavn API")
                return song_list

            songs = data["data"]["list"]

            for song in songs:
                try:
                    # Process song title
                    if ('From "' in song["title"]) or ("From &quot" in song["title"]):
                        title_orig = song["title"].replace("&quot;", '"')
                        title, album = self.parse_title(title_orig)
                    else:
                        title = song["title"]
                        album = self.clean_album_name(song["more_info"]["album"])

                    # Get year if available
                    year = song.get("year", None)

                    # Get primary artist from artistMap
                    try:
                        artist = song["more_info"]["artistMap"]["primary_artists"][0]["name"]
                    except (KeyError, IndexError):
                        # Fallback to first featured artist if primary not found
                        try:
                            artist = song["more_info"]["artistMap"]["featured_artists"][0]["name"]
                        except (KeyError, IndexError):
                            # Skip if no artist found
                            continue

                    # Create song dictionary with cleaned data
                    song_dict = {
                        "title": title.strip(),
                        "album": album.strip(),
                        "artist": artist.strip(),
                        "year": year,
                    }

                    song_list.append(song_dict)
                    self._log.debug("Added song: {} - {}", song_dict["title"], song_dict["artist"])

                except Exception as e:
                    self._log.debug("Error processing JioSaavn song: {}", e)
                    continue

            # Cache successful results
            if song_list:
                self.cache.set_playlist_cache(playlist_id, 'jiosaavn', song_list)
                self._log.info("Cached {} tracks from JioSaavn playlist", len(song_list))

        except Exception as e:
            self._log.error("Error importing JioSaavn playlist: {}", e)

        return song_list

    def get_playlist_id(self, url):
        # split the url by "/"
        parts = url.split("/")
        # find the index of "playlist"
        index = parts.index("playlist")
        # get the next part as the playlist id
        playlist_id = parts[index + 1]
        # return the playlist id
        return playlist_id

    def get_playlist_tracks(self, playlist_id):
        """This function returns a list of tracks in a Spotify playlist.

        Args:
            playlist_id (string): Spotify playlist ID

        Returns:
            list: tracks in a Spotify playlist
        """
        try:
            # Use playlist_items instead of playlist_tracks
            tracks_response = self.sp.playlist_items(
                playlist_id, additional_types=["track"]
            )
            tracks = tracks_response["items"]

            # Fetch remaining tracks if playlist has more than 100 tracks
            while tracks_response["next"]:
                tracks_response = self.sp.next(tracks_response)
                tracks.extend(tracks_response["items"])

            return tracks

        except spotipy.exceptions.SpotifyException as e:
            self._log.error("Failed to fetch playlist: {} - {}", playlist_id, str(e))
            return []

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
        ]

    def parse_title(self, title_orig):
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

    def clean_album_name(self, album_orig):
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

    saavn = JioSaavn()

    # Define a function to get playlist songs by id
    async def get_playlist_songs(playlist_url):
        # Use the async method from saavn
        songs = await saavn.get_playlist_songs(playlist_url)
        # Return a list of songs with details
        return songs

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
        config = {
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
            score, dist = plex_track_distance(temp_item, track, config)
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

    def _plex_add_playlist_item(self, items, playlist):
        """Add items to Plex playlist."""
        if not items:
            self._log.warning("No items to add to playlist {}", playlist)
            return

        plex_set = set()
        try:
            plst = self.plex.playlist(playlist)
            playlist_set = set(plst.items())
        except exceptions.NotFound:
            plst = None
            playlist_set = set()
        for item in items:
            try:
                # Check for both plex_ratingkey and ratingKey
                rating_key = getattr(item, 'plex_ratingkey', None) or getattr(item, 'ratingKey', None)
                if rating_key:
                    plex_set.add(self.plex.fetchItem(rating_key))
                else:
                    self._log.warning("{} does not have plex_ratingkey or ratingKey attribute. Item details: {}", item, vars(item))
            except (exceptions.NotFound, AttributeError) as e:
                self._log.warning("{} not found in Plex library. Error: {}", item, e)
                continue
        to_add = plex_set - playlist_set
        self._log.info("Adding {} tracks to {} playlist", len(to_add), playlist)
        if plst is None:
            self._log.info("{} playlist will be created", playlist)
            self.plex.createPlaylist(playlist, items=list(to_add))
        else:
            try:
                plst.addItems(items=list(to_add))
            except exceptions.BadRequest as e:
                self._log.error(
                    "Error adding items {} to {} playlist. Error: {}",
                    items,
                    playlist,
                    e,
                )
        self.sort_plex_playlist(playlist, "lastViewedAt")

    def _plex_playlist_to_collection(self, playlist):
        """Convert a Plex playlist to a Plex collection."""
        try:
            plst = self.music.playlist(playlist)
            playlist_set = set(plst.items())
        except exceptions.NotFound:
            self._log.error("{} playlist not found", playlist)
            return
        try:
            col = self.music.collection(playlist)
            collection_set = set(col.items())
        except exceptions.NotFound:
            col = None
            collection_set = set()
        to_add = playlist_set - collection_set
        self._log.info("Adding {} tracks to {} collection", len(to_add), playlist)
        if col is None:
            self._log.info("{} collection will be created", playlist)
            self.music.createCollection(playlist, items=list(to_add))
        else:
            try:
                col.addItems(items=list(to_add))
            except exceptions.BadRequest as e:
                self._log.error(
                    "Error adding items {} to {} collection. Error: {}",
                    items,
                    playlist,
                    e,
                )

    def _plex_remove_playlist_item(self, items, playlist):
        """Remove items from Plex playlist."""
        plex_set = set()
        try:
            plst = self.plex.playlist(playlist)
            playlist_set = set(plst.items())
        except exceptions.NotFound:
            self._log.error("{} playlist not found", playlist)
            return
        for item in items:
            try:
                plex_set.add(self.plex.fetchItem(item.plex_ratingkey))
            except (
                exceptions.NotFound,
                AttributeError,
                ContentDecodingError,
                ConnectionError,
            ) as e:
                self._log.warning("{} not found in Plex library. Error: {}", item, e)
                continue
        to_remove = plex_set.intersection(playlist_set)
        self._log.info("Removing {} tracks from {} playlist", len(to_remove), playlist)
        plst.removeItems(items=list(to_remove))

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
                            self.get_fuzzy_score(clean_source_word, clean_target_word) > 0.8):
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
            sorted_tracks = self.find_closest_match(song_dict, filtered_tracks)

            # Use beets UI formatting for the query header
            print_(ui.colorize('text_highlight', '\nChoose candidates for: ') +
                   ui.colorize('text_highlight_minor', f"{album} - {title} - {artist}"))

            # Format and display the matches
            for i, (track, score) in enumerate(sorted_tracks, start=1):
                track_artist = getattr(track, 'originalTitle', None) or track.artist().title

                # Use beets' similarity detection for highlighting
                def highlight_matches(source, target):
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
                f"  {ui.colorize('action', 's')}{ui.colorize('text', ': Skip')}   "
                f"  {ui.colorize('action', 'e')}{ui.colorize('text', ': Enter manual search')}\n"
            )

            sel = ui.input_options(
                ("aBort", "Skip", "Enter"),
                numrange=(1, len(sorted_tracks)),
                default=1
            )

            if sel in ("b", "B"):
                return None
            elif sel in ("s", "S"):
                self._cache_result(song_dict, None)
                return None
            elif sel in ("e", "E"):
                return self.manual_track_search(song_dict)

            selected_track = sorted_tracks[sel - 1][0] if sel > 0 else None

            if selected_track and original_song:
                self._cache_result(self.cache._make_cache_key(original_song), selected_track)

            return selected_track

        except Exception as e:
            self._log.error("Error during manual search: {}", e)
            return None

    def search_plex_song(self, song, manual_search=None, llm_attempted=False):
        """Fetch the Plex track key with fallback options."""
        if manual_search is None:
            manual_search = config["plexsync"]["manual_search"].get(bool)

        # Check cache first
        cache_key = self.cache._make_cache_key(song)
        cached_result = self.cache.get(cache_key)
        if cached_result is not None:
            if isinstance(cached_result, tuple):
                rating_key, cleaned_metadata = cached_result
                if rating_key == -1 or rating_key is None:  # Handle both None and -1
                    if cleaned_metadata and not llm_attempted:
                        self._log.debug("Using cached cleaned metadata: {}", cleaned_metadata)
                        return self.search_plex_song(cleaned_metadata, manual_search, llm_attempted=True)
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
        artist = song["artist"].split(",")[0]
        try:
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
            sorted_tracks = self.find_closest_match(song, tracks)
            self._log.debug("Found {} tracks for {}", len(sorted_tracks), song["title"])

            # Try manual search first if enabled and we have matches
            if manual_search and len(sorted_tracks) > 0:
                manual_result = self._handle_manual_search(sorted_tracks, song)
                if manual_result:
                    return manual_result
                # If user skipped, the negative cache was already stored, return None
                return None

            # Otherwise try automatic matching with improved threshold
            best_match = sorted_tracks[0]
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
                cleaned_song = {
                    "title": cleaned_metadata.get("title", song["title"]),
                    "album": cleaned_metadata.get("album", song["album"]),
                    "artist": cleaned_metadata.get("artist", song["artist"])
                }
                self._log.debug("Using LLM cleaned metadata: {}", cleaned_song)

                # Cache the original query with cleaned metadata
                self._cache_result(cache_key, None, cleaned_song)

                # Try search with cleaned metadata
                return self.search_plex_song(cleaned_song, manual_search, llm_attempted=True)

        # Final fallback: try manual search if enabled
        if manual_search:
            self._log.info(
                "\nTrack {} - {} - {} not found in Plex".format(
                song.get("album", "Unknown"),
                song.get("artist", "Unknown"),
                song["title"])
            )
            if ui.input_yn(ui.colorize('text_highlight', "\nSearch manually?") + " (Y/n)"):
                return self.manual_track_search(song)

        # Store negative result if nothing found
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
        # Get the playlist
        plist = self.plex.playlist(playlist)
        # Get a list of all the tracks in the playlist
        tracks = plist.items()
        # Loop through each track
        for track in tracks:
            # Remove the track from the playlist
            plist.removeItems(track)

    def _plex_collage(self, interval, grid):
        """Create a collage of most played albums.

        Args:
            interval (int): Number of days to look back
            grid (int): Grid dimension (e.g., 3 for 3x3, 4 for 4x4)
        """
        # Convert input parameters to integers
        interval = int(interval)
        grid = int(grid)

        self._log.info(
            "Creating collage of most played albums in the last {} days", interval
        )

        # Get recently played tracks
        tracks = self.music.search(
            filters={"track.lastViewedAt>>": f"{interval}d"},
            sort="viewCount:desc",
            libtype="track",
        )

        # Get sorted albums and limit to grid*grid
        max_albums = grid * grid
        sorted_albums = self._plex_most_played_albums(tracks, interval)[:max_albums]

        if not sorted_albums:
            self._log.error("No albums found in the specified time period")
            return

        # Create a list of album art URLs
        album_art_urls = []
        for album in sorted_albums:
            if hasattr(album, "thumbUrl") and album.thumbUrl:
                album_art_urls.append(album.thumbUrl)
                self._log.debug(
                    "Added album art for: {} (played {} times)",
                    album.title,
                    album.count,
                )

        if not album_art_urls:
            self._log.error("No album artwork found")
            return

        try:
            collage = self.create_collage(album_art_urls, grid)
            output_path = os.path.join(self.config_dir, "collage.png")
            collage.save(output_path, "PNG", quality=95)
            self._log.info("Collage saved to: {}", output_path)
        except Exception as e:
            self._log.error("Failed to create collage: {}", e)

    def create_collage(self, list_image_urls, dimension):
        """Create a square collage from a list of image urls.

        Args:
            list_image_urls (list): List of image URLs
            dimension (int): Grid dimension (e.g., 3 for 3x3)

        Returns:
            PIL.Image: The generated collage image
        """
        from io import BytesIO

        from PIL import Image

        thumbnail_size = 300  # Size of each album art
        grid_size = thumbnail_size * dimension

        # Create the base image
        grid = Image.new("RGB", (grid_size, grid_size), "black")

        for index, url in enumerate(list_image_urls):
            if index >= dimension * dimension:
                break

            try:
                # Download and process image
                response = requests.get(url, timeout=10)
                img = Image.open(BytesIO(response.content))

                # Convert to RGB if necessary
                if img.mode != "RGB":
                    img = img.convert("RGB")

                # Resize maintaining aspect ratio
                img.thumbnail(
                    (thumbnail_size, thumbnail_size), Image.Resampling.LANCZOS
                )

                # Calculate position
                x = thumbnail_size * (index % dimension)
                y = thumbnail_size * (index // dimension)
                grid.paste(img, (x, y))

                # Clean up
                img.close()

            except Exception as e:
                self._log.debug("Failed to process image {}: {}", url, e)
                continue

        return grid

    def _plex_most_played_albums(self, tracks, interval):
        from datetime import datetime, timedelta

        now = datetime.now()
        frm_dt = now - timedelta(days=interval)
        album_data = {}

        for track in tracks:
            try:
                history = track.history(mindate=frm_dt)
                count = len(history)

                # Get last played date from track directly if available
                track_last_played = track.lastViewedAt

                # If track has history entries, get the most recent one
                if history:
                    history_last_played = max(
                        (h.viewedAt for h in history if h.viewedAt is not None),
                        default=None,
                    )
                    # Use the more recent of track.lastViewedAt and history
                    last_played = max(
                        filter(None, [track_last_played, history_last_played]),
                        default=None,
                    )
                else:
                    last_played = track_last_played

                if track.parentTitle not in album_data:
                    album_data[track.parentTitle] = {
                        "album": track.album(),
                        "count": count,
                        "last_played": last_played,
                    }
                else:
                    album_data[track.parentTitle]["count"] += count
                    if last_played and (
                        not album_data[track.parentTitle]["last_played"]
                        or last_played > album_data[track.parentTitle]["last_played"]
                    ):
                        album_data[track.parentTitle]["last_played"] = last_played

            except Exception as e:
                self._log.debug(
                    "Error processing track history for {}: {}", track.title, e
                )
                continue

        # Convert to sortable list and sort
        albums_list = [
            (data["album"], data["count"], data["last_played"])
            for data in album_data.values()
        ]

        # Sort by count (descending) and last played (most recent first)
        sorted_albums = sorted(
            albums_list, key=lambda x: (-x[1], -(x[2].timestamp() if x[2] else 0))
        )

        # Extract just the album objects and add attributes
        result = []
        for album, count, last_played in sorted_albums:
            if count > 0:  # Only include albums that have been played
                album.count = count
                album.last_played_date = last_played
                result.append(album)
                self._log.info(
                    "{} played {} times, last played on {}",
                    album.title,
                    count,
                    (
                        last_played.strftime("%Y-%m-%d %H:%M:%S")
                        if last_played
                        else "Never"
                    ),
                )

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
        # Generate cache key from URL
        playlist_id = url.split('list=')[-1].split('&')[0]  # Extract playlist ID from URL

        # Check cache
        cached_data = self.cache.get_playlist_cache(playlist_id, 'youtube')
        if (cached_data):
            self._log.info("Using cached YouTube playlist data")
            return cached_data

        try:
            from beetsplug.youtube import YouTubePlugin
        except ModuleNotFoundError:
            self._log.error("YouTube plugin not installed")
            return None

        try:
            ytp = YouTubePlugin()
            song_list = ytp.import_youtube_playlist(url)

            # Cache successful results
            if song_list:
                self.cache.set_playlist_cache(playlist_id, 'youtube', song_list)
                self._log.info("Cached {} tracks from YouTube playlist", len(song_list))

            return song_list
        except Exception as e:
            self._log.error("Unable to initialize YouTube plugin. Error: {}", e)
            return None

    def import_yt_search(self, query, limit):
        try:
            from beetsplug.youtube import YouTubePlugin
        except ModuleNotFoundError:
            self._log.error("YouTube plugin not installed")
            return
        try:
            ytp = YouTubePlugin()
        except Exception as e:
            self._log.error("Unable to initialize YouTube plugin. Error: {}", e)
            return
        return ytp.import_youtube_search(query, limit)

    def import_tidal_playlist(self, url):
        """Import Tidal playlist with caching."""
        # Generate cache key from URL
        playlist_id = url.split('/')[-1]

        # Check cache
        cached_data = self.cache.get_playlist_cache(playlist_id, 'tidal')
        if (cached_data):
            self._log.info("Using cached Tidal playlist data")
            return cached_data

        try:
            from beetsplug.tidal import TidalPlugin
        except ModuleNotFoundError:
            self._log.error("Tidal plugin not installed")
            return None

        try:
            tidal = TidalPlugin()
            song_list = tidal.import_tidal_playlist(url)

            # Cache successful results
            if song_list:
                self.cache.set_playlist_cache(playlist_id, 'tidal', song_list)
                self._log.info("Cached {} tracks from Tidal playlist", len(song_list))

            return song_list
        except Exception as e:
            self._log.error("Unable to initialize Tidal plugin. Error: {}", e)
            return None

    def import_gaana_playlist(self, url):
        """Import Gaana playlist with caching."""
        # Generate cache key from URL
        playlist_id = url.split('/')[-1]

        # Check cache
        cached_data = self.cache.get_playlist_cache(playlist_id, 'gaana')
        if (cached_data):
            self._log.info("Using cached Gaana playlist data")
            return cached_data

        try:
            from beetsplug.gaana import GaanaPlugin
        except ModuleNotFoundError:
            self._log.error(
                "Gaana plugin not installed. \
                            See https://github.com/arsaboo/beets-gaana"
            )
            return None

        try:
            gaana = GaanaPlugin()
        except Exception as e:
            self._log.error("Unable to initialize Gaana plugin. Error: {}", e)
            return None

        # Get songs from Gaana
        song_list = gaana.import_gaana_playlist(url)

        # Cache successful results
        if song_list:
            self.cache.set_playlist_cache(playlist_id, 'gaana', song_list)
            self._log.info("Cached {} tracks from Gaana playlist", len(song_list))

        return song_list

    def import_m3u8_playlist(self, filepath):
        """Import M3U8 playlist with caching."""
        # Generate cache key from file path
        playlist_id = str(Path(filepath).stem)

        # Check cache
        cached_data = self.cache.get_playlist_cache(playlist_id, 'm3u8')
        if (cached_data):
            self._log.info("Using cached M3U8 playlist data")
            return cached_data

        song_list = []
        current_song = {}

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()

                i = 0
                while i < len(lines):
                    line = lines[i].strip()

                    if not line or line.startswith('#EXTM3U'):
                        i += 1
                        continue

                    if line.startswith('#EXTINF:'):
                        # Extract artist - title from the EXTINF line
                        meta = line.split(',', 1)[1]
                        if ' - ' in meta:
                            artist, title = meta.split(' - ', 1)
                            current_song = {
                                'artist': artist.strip(),
                                'title': title.strip(),
                                'album': None  # Will be set by EXTALB
                            }

                            # Debug logging for this entry
                            self._log.debug("Parsing M3U8 entry - artist='{}', title='{}'",
                                         current_song['artist'], current_song['title'])

                            # Check for EXTALB on next line
                            if i + 1 < len(lines) and lines[i+1].strip().startswith('#EXTALB:'):
                                i += 1
                                album_line = lines[i].strip()
                                current_song['album'] = album_line[8:].strip()
                                self._log.debug("  with album='{}'", current_song['album'])

                            # Check for file path on next line (should not start with #)
                            if i + 1 < len(lines) and not lines[i+1].strip().startswith('#'):
                                i += 1
                                # This is the file path - finalize song entry
                                if current_song and all(k in current_song for k in ['title', 'artist']):
                                    # Final debug log before adding to list
                                    self._log.debug("Added M3U8 track: {}", current_song)
                                    song_list.append(current_song.copy())
                                    current_song = {}
                    i += 1

            if song_list:
                self.cache.set_playlist_cache(playlist_id, 'm3u8', song_list)
                self._log.info("Cached {} tracks from M3U8 playlist", len(song_list))

            return song_list

        except Exception as e:
            self._log.error("Error importing M3U8 playlist {}: {}", filepath, e)
            return []

    def _plex2spotify(self, lib, playlist):
        """Transfer Plex playlist to Spotify using plex_lookup."""
        self.authenticate_spotify()
        plex_playlist = self.plex.playlist(playlist)
        plex_playlist_items = plex_playlist.items()
        self._log.debug("Plex playlist items: {}", plex_playlist_items)

        # Build lookup once for all tracks
        plex_lookup = self.build_plex_lookup(lib)

        spotify_tracks = []
        for item in plex_playlist_items:
            self._log.debug("Processing {}", item.ratingKey)

            beets_item = plex_lookup.get(item.ratingKey)
            if not beets_item:
                self._log.debug(
                    "Item not found in Beets: {} - {}",
                    item.parentTitle,
                    item.title
                )
                continue

            self._log.debug("Beets item: {}", beets_item)

            try:
                spotify_track_id = beets_item.spotify_track_id
                self._log.debug("Spotify track id in beets: {}", spotify_track_id)
            except Exception:
                spotify_track_id = None
                self._log.debug("Spotify track_id not found in beets")

            if not spotify_track_id:
                self._log.debug(
                    "Searching for {} {} in Spotify",
                    beets_item.title,
                    beets_item.album
                )
                spotify_search_results = self.sp.search(
                    q=f"track:{beets_item.title} album:{beets_item.album}",
                    limit=1,
                    type="track",
                )
                if not spotify_search_results["tracks"]["items"]:
                    self._log.info("Spotify match not found for {}", beets_item)
                    continue
                spotify_track_id = spotify_search_results["tracks"]["items"][0]["id"]

            spotify_tracks.append(spotify_track_id)

        self.add_tracks_to_spotify_playlist(playlist, spotify_tracks)

    def add_tracks_to_spotify_playlist(self, playlist_name, track_uris):
        user_id = self.sp.current_user()["id"]
        playlists = self.sp.user_playlists(user_id)
        playlist_id = None
        for playlist in playlists["items"]:
            if playlist["name"].lower() == playlist_name.lower():
                playlist_id = playlist["id"]
                break
        if not playlist_id:
            playlist = self.sp.user_playlist_create(
                user_id, playlist_name, public=False
            )
            playlist_id = playlist["id"]
            self._log.debug(
                f"Playlist {playlist_name} created with id " f"{playlist_id}"
            )
        playlist_tracks = self.get_playlist_tracks(playlist_id)
        # get the tracks in the playlist
        uris = [
            track["track"]["uri"].replace("spotify:track:", "")
            for track in playlist_tracks
        ]
        track_uris = list(set(track_uris) - set(uris))
        self._log.debug(f"Tracks to be added: {track_uris}")
        if len(track_uris) > 0:
            for i in range(0, len(track_uris), 100):
                chunk = track_uris[i : i + 100]
                self.sp.user_playlist_add_tracks(user_id, playlist_id, chunk)
            self._log.debug(
                f"Added {len(track_uris)} tracks to playlist " f"{playlist_id}"
            )
        else:
            self._log.debug("No tracks to add to playlist")

    def get_config_value(self, item_cfg, defaults_cfg, key, code_default):
        if key in item_cfg:
            val = item_cfg[key]
            return val.get() if hasattr(val, "get") else val
        elif key in defaults_cfg:
            val = defaults_cfg[key]
            return val.get() if hasattr(val, "get") else val
        else:
            return code_default

    def get_preferred_attributes(self):
        """Determine preferred genres and similar tracks based on user listening habits."""
        # Get history period from config
        if (
            "playlists" in config["plexsync"]
            and "defaults" in config["plexsync"]["playlists"]
        ):
            defaults_cfg = config["plexsync"]["playlists"]["defaults"].get({})
        else:
            defaults_cfg = {}

        history_days = self.get_config_value(
            config["plexsync"], defaults_cfg, "history_days", 15
        )
        exclusion_days = self.get_config_value(
            config["plexsync"], defaults_cfg, "exclusion_days", 30
        )

        # Fetch tracks played in the configured period
        tracks = self.music.search(
            filters={"track.lastViewedAt>>": f"{history_days}d"}, libtype="track"
        )

        # Track genre counts and similar tracks
        genre_counts = {}
        similar_tracks = set()

        recently_played = set(
            track.ratingKey
            for track in self.music.search(
                filters={"track.lastViewedAt>>": f"{exclusion_days}d"}, libtype="track"
            )
        )

        for track in tracks:
            # Count genres
            track_genres = set()
            for genre in track.genres:
                if genre:
                    genre_str = str(genre.tag).lower()
                    genre_counts[genre_str] = genre_counts.get(genre_str, 0) + 1
                    track_genres.add(genre_str)

            # Get sonically similar tracks
            try:
                sonic_matches = track.sonicallySimilar()
                # Filter sonic matches
                for match in sonic_matches:
                    # Check rating - include unrated (-1) and highly rated (>=4) tracks
                    rating = getattr(
                        match, "userRating", -1
                    )  # Default to -1 if attribute doesn't exist
                    if (
                        match.ratingKey not in recently_played  # Not recently played
                        and any(
                            g.tag.lower() in track_genres for g in match.genres
                        )  # Genre match
                        and (rating is None or rating == -1 or rating >= 4)
                    ):  # Rating criteria including None
                        similar_tracks.add(match)
            except Exception as e:
                self._log.debug(
                    "Error getting similar tracks for {}: {}", track.title, e
                )

        # Sort genres by count
        sorted_genres = sorted(genre_counts, key=genre_counts.get, reverse=True)[:5]
        self._log.debug("Top genres: {}", sorted_genres)
        self._log.debug("Found {} similar tracks after filtering", len(similar_tracks))

        return sorted_genres, list(similar_tracks)

    def build_plex_lookup(self, lib):
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
        """Calculate comprehensive score for a track using standardized variables."""
        import numpy as np
        from scipy import stats

        if base_time is None:
            base_time = datetime.now()

        # Get raw values with better defaults for never played/rated tracks
        rating = float(getattr(track, 'plex_userrating', 0))
        last_played = getattr(track, 'plex_lastviewedat', None)
        popularity = float(getattr(track, 'spotify_track_popularity', 0))
        release_year = getattr(track, 'year', None)

        # Convert release year to age
        if release_year:
            try:
                release_year = int(release_year)
                age = base_time.year - release_year
            except ValueError:
                age = 0  # Default to 0 if year is invalid
        else:
            age = 0  # Default to 0 if year is missing

        # For never played tracks, use exponential random distribution
        if last_played is None:
            # Use exponential distribution with mean=365 days
            days_since_played = np.random.exponential(365)
        else:
            days = (base_time - datetime.fromtimestamp(last_played)).days
            # Use exponential decay instead of hard cap
            days_since_played = min(days, 1095)  # Cap at 3 years

        # If we have context tracks, calculate means and stds
        if tracks_context:
            # Get values for all tracks
            all_ratings = [float(getattr(t, 'plex_userrating', 0)) for t in tracks_context]
            all_days = [
                (base_time - datetime.fromtimestamp(getattr(t, 'plex_lastviewedat', base_time - timedelta(days=365)))).days
                for t in tracks_context
            ]
            all_popularity = [float(getattr(t, 'spotify_track_popularity', 0)) for t in tracks_context]
            all_ages = [base_time.year - int(getattr(t, 'year', base_time.year)) for t in tracks_context]

            # Calculate means and stds
            rating_mean, rating_std = np.mean(all_ratings), np.std(all_ratings) or 1
            days_mean, days_std = np.mean(all_days), np.std(all_days) or 1
            popularity_mean, popularity_std = np.mean(all_popularity), np.std(all_popularity) or 1
            age_mean, age_std = np.mean(all_ages), np.std(all_ages) or 1
        else:
            # Use better population estimates
            rating_mean, rating_std = 5, 2.5        # Ratings 0-10
            days_mean, days_std = 365, 180         # ~1 year mean, 6 months std
            popularity_mean, popularity_std = 30, 20  # Spotify popularity 0-100, adjusted mean
            age_mean, age_std = 30, 10              # Age mean 10 years, std 5 years

        # Calculate z-scores with bounds
        z_rating = (rating - rating_mean) / rating_std if rating > 0 else -2.0
        z_recency = -(days_since_played - days_mean) / days_std  # Negative because fewer days = more recent
        z_popularity = (popularity - popularity_mean) / popularity_std
        z_age = -(age - age_mean) / age_std  # Negative because fewer years = more recent

        # Bound z-scores to avoid extreme values
        z_rating = np.clip(z_rating, -3, 3)
        z_recency = np.clip(z_recency, -3, 3)
        z_popularity = np.clip(z_popularity, -3, 3)
        z_age = np.clip(z_age, -3, 3)

        # Determine if track is rated
        is_rated = rating > 0

        # Apply weights based on rating status
        if is_rated:
            # For rated tracks: rating=50%, recency=10%, popularity=10%, age=20%
            weighted_score = (z_rating * 0.5) + (z_recency * 0.1) + (z_popularity * 0.1) + (z_age * 0.2)
        else:
            # For unrated tracks: popularity=50%, recency=20%, age=30%
            weighted_score = (z_recency * 0.2) + (z_popularity * 0.5) + (z_age * 0.3)

        # Convert to 0-100 scale using modified percentile calculation
        # Use a steeper sigmoid curve by multiplying weighted_score by 1.5
        final_score = stats.norm.cdf(weighted_score * 1.5) * 100

        # Add very small gaussian noise (reduced from 2 to 0.5) for minor variety
        noise = np.random.normal(0, 0.5)
        final_score = final_score + noise

        # Apply a minimum threshold of 50 for unrated tracks to ensure quality
        if not is_rated and final_score < 50:
            final_score = 50 + (final_score / 2)  # Scale lower scores up but keep relative ordering

        # Debug logging
        self._log.debug(
            "Score components for {}: rating={:.2f} (z={:.2f}), days={:.0f} (z={:.2f}), "
            "popularity={:.2f} (z={:.2f}), age={:.0f} (z={:.2f}), final={:.2f}",
            track.title,
            rating,
            z_rating,
            days_since_played,
            z_recency,
            popularity,
            z_popularity,
            age,
            z_age,
            final_score
        )

        return max(0, min(100, final_score))  # Clamp between 0 and 100

    def select_tracks_weighted(self, tracks, num_tracks):
        """Select tracks using weighted probability based on scores."""
        import numpy as np

        if not tracks:
            return []

        # Calculate scores for all tracks
        base_time = datetime.now()
        track_scores = [(track, self.calculate_track_score(track, base_time)) for track in tracks]

        # Convert scores to probabilities using softmax
        scores = np.array([score for _, score in track_scores])
        probabilities = np.exp(scores / 10) / sum(np.exp(scores / 10))  # Temperature=10 to control randomness

        # Select tracks based on probabilities
        selected_indices = np.random.choice(
            len(tracks),
            size=min(num_tracks, len(tracks)),
            replace=False,
            p=probabilities
        )

        selected_tracks = [tracks[i] for i in selected_indices]

        # Log selection details for debugging
        for i, track in enumerate(selected_tracks):
            score = track_scores[selected_indices[i]][1]
            self._log.debug(
                "Selected: {} - {} (Score: {:.2f}, Rating: {}, Plays: {})",
                track.album,
                track.title,
                score,
                getattr(track, 'plex_userrating', 0),
                getattr(track, 'plex_viewcount', 0)
            )

        return selected_tracks

    def calculate_playlist_proportions(self, max_tracks, discovery_ratio):
        """Calculate number of rated vs unrated tracks based on discovery ratio.

        Args:
            max_tracks: Total number of tracks desired
            discovery_ratio: Percentage of unrated/discovery tracks desired (0-100)

        Returns:
            tuple: (unrated_tracks_count, rated_tracks_count)
        """
        unrated_tracks_count = min(int(max_tracks * (discovery_ratio / 100)), max_tracks)
        rated_tracks_count = max_tracks - unrated_tracks_count
        return unrated_tracks_count, rated_tracks_count

    def validate_filter_config(self, filter_config):
        """Validate the filter configuration structure and values.

        Args:
            filter_config: Dictionary containing filter configuration

        Returns:
            tuple: (is_valid: bool, error_message: str)
        """
        if not isinstance(filter_config, dict):
            return False, "Filter configuration must be a dictionary"

        # Check exclude/include sections if they exist
        for section in ['exclude', 'include']:
            if section in filter_config:
                if not isinstance(filter_config[section], dict):
                    return False, f"{section} section must be a dictionary"

                section_config = filter_config[section]

                # Validate genres if present
                if 'genres' in section_config:
                    if not isinstance(section_config['genres'], list):
                        return False, f"{section}.genres must be a list"

                # Validate years if present
                if 'years' in section_config:
                    years = section_config['years']
                    if not isinstance(years, dict):
                        return False, f"{section}.years must be a dictionary"

                    # Check year values
                    if 'before' in years and not isinstance(years['before'], int):
                        return False, f"{section}.years.before must be an integer"
                    if 'after' in years and not isinstance(years['after'], int):
                        return False, f"{section}.years.after must be an integer"
                    if 'between' in years:
                        if not isinstance(years['between'], list) or len(years['between']) != 2:
                            return False, f"{section}.years.between must be a list of two integers"
                        if not all(isinstance(y, int) for y in years['between']):
                            return False, f"{section}.years.between values must be integers"

        # Validate min_rating if present
        if 'min_rating' in filter_config:
            if not isinstance(filter_config['min_rating'], (int, float)):
                return False, "min_rating must be a number"
            if not 0 <= filter_config['min_rating'] <= 10:
                return False, "min_rating must be between 0 and 10"

        return True, ""

    def _apply_exclusion_filters(self, tracks, exclude_config):
        """Apply exclusion filters to tracks."""
        filtered_tracks = tracks[:]
        original_count = len(filtered_tracks)

        # Filter by genres
        if 'genres' in exclude_config:
            exclude_genres = [g.lower() for g in exclude_config['genres']]
            filtered_tracks = [
                track for track in filtered_tracks
                if hasattr(track, 'genres') and not any(
                    g.tag.lower() in exclude_genres
                    for g in track.genres
                )
            ]
            self._log.debug(
                "Genre exclusion filter: {} -> {} tracks",
                exclude_genres,
                len(filtered_tracks)
            )

        # Filter by years
        if 'years' in exclude_config:
            years_config = exclude_config['years']

            if 'before' in years_config:
                year_before = years_config['before']
                filtered_tracks = [
                    track for track in filtered_tracks
                    if not hasattr(track, 'year') or
                    track.year is None or
                    track.year >= year_before
                ]
                self._log.debug(
                    "Year before {} filter: {} tracks",
                    year_before,
                    len(filtered_tracks)
                )

            if 'after' in years_config:
                year_after = years_config['after']
                filtered_tracks = [
                    track for track in filtered_tracks
                    if not hasattr(track, 'year') or
                    track.year is None or
                    track.year <= year_after
                ]
                self._log.debug(
                    "Year after {} filter: {} tracks",
                    year_after,
                    len(filtered_tracks)
                )

        self._log.debug(
            "Exclusion filters removed {} tracks",
            original_count - len(filtered_tracks)
        )
        return filtered_tracks

    def _apply_inclusion_filters(self, tracks, include_config):
        """Apply inclusion filters to tracks."""
        filtered_tracks = tracks[:]
        original_count = len(filtered_tracks)

        # Filter by genres
        if 'genres' in include_config:
            include_genres = [g.lower() for g in include_config['genres']]
            filtered_tracks = [
                track for track in filtered_tracks
                if hasattr(track, 'genres') and any(
                    g.tag.lower() in include_genres
                    for g in track.genres
                )
            ]
            self._log.debug(
                "Genre inclusion filter: {} -> {} tracks",
                include_genres,
                len(filtered_tracks)
            )

        # Filter by years
        if 'years' in include_config:
            years_config = include_config['years']

            if 'between' in years_config:
                start_year, end_year = years_config['between']
                filtered_tracks = [
                    track for track in filtered_tracks
                    if hasattr(track, 'year') and
                    track.year is not None and
                    start_year <= track.year <= end_year
                ]
                self._log.debug(
                    "Year between {}-{} filter: {} tracks",
                    start_year,
                    end_year,
                    len(filtered_tracks)
                )

        self._log.debug(
            "Inclusion filters removed {} tracks",
            original_count - len(filtered_tracks)
        )
        return filtered_tracks

    def apply_playlist_filters(self, tracks, filter_config):
        """Apply configured filters to a list of tracks.

        Args:
            tracks: List of tracks to filter
            filter_config: Dictionary containing filter configuration

        Returns:
            list: Filtered track list
        """
        if not tracks:
            return tracks

        # Validate filter configuration
        is_valid, error = self.validate_filter_config(filter_config)
        if not is_valid:
            self._log.error("Invalid filter configuration: {}", error)
            return tracks

        self._log.debug("Applying filters to {} tracks", len(tracks))
        filtered_tracks = tracks[:]

        # Apply exclusion filters first
        if 'exclude' in filter_config:
            self._log.debug("Applying exclusion filters...")
            filtered_tracks = self._apply_exclusion_filters(filtered_tracks, filter_config['exclude'])

        # Then apply inclusion filters
        if 'include' in filter_config:
            self._log.debug("Applying inclusion filters...")
            filtered_tracks = self._apply_inclusion_filters(filtered_tracks, filter_config['include'])

        # Apply rating filter if specified, but preserve unrated tracks
        if 'min_rating' in filter_config:
            min_rating = filter_config['min_rating']
            original_count = len(filtered_tracks)

            # Separate unrated and rated tracks
            unrated_tracks = [
                track for track in filtered_tracks
                if not hasattr(track, 'userRating') or
                track.userRating is None or
                float(track.userRating or 0) == 0
            ]

            rated_tracks = [
                track for track in filtered_tracks
                if hasattr(track, 'userRating') and
                track.userRating is not None and
                float(track.userRating or 0) >= min_rating
            ]

            filtered_tracks = rated_tracks + unrated_tracks

            self._log.debug(
                "Rating filter (>= {}): {} -> {} tracks ({} rated, {} unrated)",
                min_rating,
                original_count,
                len(filtered_tracks),
                len(rated_tracks),
                len(unrated_tracks)
            )

        self._log.debug(
            "Filter application complete: {} -> {} tracks",
            len(tracks),
            len(filtered_tracks)
        )
        return filtered_tracks

    def generate_daily_discovery(self, lib, dd_config, plex_lookup, preferred_genres, similar_tracks):
        """Generate Daily Discovery playlist with improved track selection."""
        playlist_name = dd_config.get("name", "Daily Discovery")
        self._log.info("Generating {} playlist", playlist_name)

        # Get base configuration
        if "playlists" in config["plexsync"] and "defaults" in config["plexsync"]["playlists"]:
            defaults_cfg = config["plexsync"]["playlists"]["defaults"].get({})
        else:
            defaults_cfg = {}

        max_tracks = self.get_config_value(dd_config, defaults_cfg, "max_tracks", 20)
        discovery_ratio = self.get_config_value(dd_config, defaults_cfg, "discovery_ratio", 30)

        # Use lookup dictionary to convert similar tracks to beets items first
        matched_tracks = []
        for plex_track in similar_tracks:
            try:
                beets_item = plex_lookup.get(plex_track.ratingKey)
                if beets_item:
                    matched_tracks.append(plex_track)  # Keep Plex track object for filtering
            except Exception as e:
                self._log.debug("Error processing track {}: {}", plex_track.title, e)
                continue

        self._log.debug("Found {} initial tracks", len(matched_tracks))

        # Get filters from config
        filters = dd_config.get("filters", {})

        # Apply filters to matched tracks
        if filters:
            self._log.debug("Applying filters to {} tracks...", len(matched_tracks))
            filtered_tracks = self.apply_playlist_filters(matched_tracks, filters)
            self._log.debug("After filtering: {} tracks", len(filtered_tracks))
        else:
            filtered_tracks = matched_tracks

        self._log.debug("Processing {} filtered tracks", len(filtered_tracks))

        # Now convert filtered Plex tracks to beets items for final processing
        final_tracks = []
        for track in filtered_tracks:
            try:
                beets_item = plex_lookup.get(track.ratingKey)
                if beets_item:
                    final_tracks.append(beets_item)
            except Exception as e:
                self._log.debug("Error converting track {}: {}", track.title, e)

        self._log.debug("Found {} tracks matching all criteria", len(final_tracks))

        # Split tracks into rated and unrated
        rated_tracks = []
        unrated_tracks = []
        for track in final_tracks:
            rating = float(getattr(track, 'plex_userrating', 0))
            if rating > 0:  # Include all rated tracks
                rated_tracks.append(track)
            else:  # Only truly unrated tracks
                unrated_tracks.append(track)

        self._log.debug("Split into {} rated and {} unrated tracks",
                       len(rated_tracks), len(unrated_tracks))

        # Calculate proportions
        unrated_tracks_count, rated_tracks_count = self.calculate_playlist_proportions(
            max_tracks, discovery_ratio
        )

        # Select tracks using weighted probability
        selected_rated = self.select_tracks_weighted(rated_tracks, rated_tracks_count)
        selected_unrated = self.select_tracks_weighted(unrated_tracks, unrated_tracks_count)

        # If we don't have enough unrated tracks, fill with rated ones
        if len(selected_unrated) < unrated_tracks_count:
            additional_count = min(
                unrated_tracks_count - len(selected_unrated),
                max_tracks - len(selected_rated) - len(selected_unrated)
            )
            remaining_rated = [t for t in rated_tracks if t not in selected_rated]
            additional_rated = self.select_tracks_weighted(remaining_rated, additional_count)
            selected_rated.extend(additional_rated)

        # Combine and shuffle
        selected_tracks = selected_rated + selected_unrated
        if len(selected_tracks) > max_tracks:
            selected_tracks = selected_tracks[:max_tracks]

        import random
        random.shuffle(selected_tracks)

        self._log.info(
            "Selected {} rated tracks and {} unrated tracks",
            len(selected_rated),
            len(selected_unrated)
        )

        if not selected_tracks:
            self._log.warning("No tracks matched criteria for Daily Discovery playlist")
            return

        # Create/update playlist
        try:
            self._plex_clear_playlist(playlist_name)
            self._log.info("Cleared existing Daily Discovery playlist")
        except exceptions.NotFound:
            self._log.debug("No existing Daily Discovery playlist found")

        self._plex_add_playlist_item(selected_tracks, playlist_name)

        self._log.info(
            "Successfully updated {} playlist with {} tracks",
            playlist_name,
            len(selected_tracks)
        )

    def get_filtered_library_tracks(self, preferred_genres, config_filters, exclusion_days=30):
        """Get filtered library tracks using Plex's advanced filters in a single query."""
        try:
            # Build advanced filters structure
            advanced_filters = {'and': []}

            # Handle genre filters
            include_genres = []
            exclude_genres = []

            # Add genres from preferred_genres if no specific inclusion filters
            if preferred_genres:
                include_genres.extend(preferred_genres)

            # Add configured genres
            if config_filters:
                if 'include' in config_filters and 'genres' in config_filters['include']:
                    include_genres.extend(g.lower() for g in config_filters['include']['genres'])
                if 'exclude' in config_filters and 'genres' in config_filters['exclude']:
                    exclude_genres.extend(g.lower() for g in config_filters['exclude']['genres'])

            # Add genre conditions - using OR for inclusions
            if include_genres:
                include_genres = list(set(include_genres))  # Remove duplicates
                advanced_filters['and'].append({
                    'or': [{'genre': genre} for genre in include_genres]
                })

            # Use AND for exclusions with genre! operator
            if exclude_genres:
                exclude_genres = list(set(exclude_genres))  # Remove duplicates
                advanced_filters['and'].append({'genre!': exclude_genres})

            # Handle year filters
            if config_filters:
                if 'include' in config_filters and 'years' in config_filters['include']:
                    years_config = config_filters['include']['years']
                    if 'between' in years_config:
                        start_year, end_year = years_config['between']
                        advanced_filters['and'].append({
                            'and': [
                                {'year>>': start_year},
                                {'year<<': end_year}
                            ]
                        })

                if 'exclude' in config_filters and 'years' in config_filters['exclude']:
                    years_config = config_filters['exclude']['years']
                    if 'before' in years_config:
                        advanced_filters['and'].append({'year>>': years_config['before']})
                    if 'after' in years_config:
                        advanced_filters['and'].append({'year<<': years_config['after']})

            # Handle rating filter
            if config_filters and 'min_rating' in config_filters:
                advanced_filters['and'].append({
                    'or': [
                        {'userRating': 0},  # Unrated
                        {'userRating>>': config_filters['min_rating']}  # Above minimum
                    ]
                })

            # Handle recent plays exclusion
            if exclusion_days > 0:
                advanced_filters['and'].append({'lastViewedAt<<': f"-{exclusion_days}d"})

            self._log.debug("Using advanced filters: {}", advanced_filters)

            # Use searchTracks with advanced filters
            tracks = self.music.searchTracks(filters=advanced_filters)

            self._log.debug(
                "Found {} tracks matching all criteria in a single query",
                len(tracks)
            )

            return tracks

        except Exception as e:
            self._log.error("Error searching with advanced filters: {}. Filter: {}", e, advanced_filters)
            return []

    def generate_forgotten_gems(self, lib, ug_config, plex_lookup, preferred_genres, similar_tracks):
        """Generate a Forgotten Gems playlist with improved discovery."""
        playlist_name = ug_config.get("name", "Forgotten Gems")
        self._log.info("Generating {} playlist", playlist_name)

        # Get configuration
        if "playlists" in config["plexsync"] and "defaults" in config["plexsync"]["playlists"]:
            defaults_cfg = config["plexsync"]["playlists"]["defaults"].get({})
        else:
            defaults_cfg = {}

        max_tracks = self.get_config_value(ug_config, defaults_cfg, "max_tracks", 20)
        discovery_ratio = self.get_config_value(ug_config, defaults_cfg, "discovery_ratio", 30)
        exclusion_days = self.get_config_value(ug_config, defaults_cfg, "exclusion_days", 30)

        # Get filters from config
        filters = ug_config.get("filters", {})

        # If no genres configured in filters, use preferred_genres
        if not filters:
            filters = {'include': {'genres': preferred_genres}}
        elif 'include' not in filters or 'genres' not in filters['include']:
            if 'include' not in filters:
                filters['include'] = {}
            filters['include']['genres'] = preferred_genres
            self._log.debug("Using preferred genres as no genres configured: {}", preferred_genres)

        # Get initial track pool using configured or preferred genres and filters
        self._log.info("Searching library with filters...")
        all_library_tracks = self.get_filtered_library_tracks(
            [], # No need to pass preferred_genres since they're now in filters if needed
            filters,
            exclusion_days
        )

        # Add similar tracks if they match the filter criteria
        if len(similar_tracks) > 0:
            filtered_similar = self.apply_playlist_filters(similar_tracks, filters)

            # Add filtered similar tracks if not already included
            seen_keys = set(track.ratingKey for track in all_library_tracks)
            for track in filtered_similar:
                if track.ratingKey not in seen_keys:
                    all_library_tracks.append(track)
                    seen_keys.add(track.ratingKey)

            self._log.debug(
                "Combined {} library tracks with {} filtered similar tracks",
                len(all_library_tracks), len(filtered_similar)
            )

        # Convert to beets items
        final_tracks = []
        for track in all_library_tracks:
            try:
                beets_item = plex_lookup.get(track.ratingKey)
                if beets_item:
                    final_tracks.append(beets_item)
            except Exception as e:
                self._log.debug("Error converting track {}: {}", track.title, e)

        self._log.debug("Converted {} tracks to beets items", len(final_tracks)

        # Split tracks into rated and unrated
        rated_tracks = []
        unrated_tracks = []
        for track in final_tracks:
            rating = float(getattr(track, 'plex_userrating', 0))
            if rating > 0:  # Include all rated tracks
                rated_tracks.append(track)
            else:  # Only truly unrated tracks
                unrated_tracks.append(track)

        self._log.debug("Split into {} rated and {} unrated tracks", len(rated_tracks), len(unrated_tracks))

        # Calculate proportions
        unrated_tracks_count, rated_tracks_count = self.calculate_playlist_proportions(max_tracks, discovery_ratio)

        # Select tracks using weighted probability
        selected_rated = self.select_tracks_weighted(rated_tracks, rated_tracks_count)
        selected_unrated = self.select_tracks_weighted(unrated_tracks, unrated_tracks_count)

        # If we don't have enough unrated tracks, fill with rated ones
        if len(selected_unrated) < unrated_tracks_count:
            additional_count = min(
                unrated_tracks_count - len(selected_unrated),
                max_tracks - len(selected_rated) - len(selected_unrated)
            )
            remaining_rated = [t for t in rated_tracks if t not in selected_rated]
            additional_rated = self.select_tracks_weighted(remaining_rated, additional_count)
            selected_rated.extend(additional_rated)

        # Combine and shuffle
        selected_tracks = selected_rated + selected_unrated
        if len(selected_tracks) > max_tracks:
            selected_tracks = selected_tracks[:max_tracks]

        import random
        random.shuffle(selected_tracks)

        self._log.info("Selected {} rated tracks and {} unrated tracks", len(selected_rated), len(selected_unrated))

        if not selected_tracks:
            self._log.warning("No tracks matched criteria for Forgotten Gems playlist")
            return

        # Create/update playlist
        try:
            self._plex_clear_playlist(playlist_name)
            self._log.info("Cleared existing Forgotten Gems playlist")
        except exceptions.NotFound:
            self._log.debug("No existing Forgotten Gems playlist found")

        self._plex_add_playlist_item(selected_tracks, playlist_name)

        self._log.info("Successfully updated {} playlist with {} tracks", playlist_name, len(selected_tracks))

    def import_post_playlist(self, source_config):
        """Import playlist from a POST request endpoint with caching."""
        # Generate cache key from URL in payload
        playlist_url = source_config.get("payload", {}).get("playlist_url")
        if not playlist_url:
            self._log.error("No playlist_url provided in POST request payload")
            return []

        playlist_id = playlist_url.split('/')[-1]

        # Check cache
        cached_data = self.cache.get_playlist_cache(playlist_id, 'post')
        if (cached_data):
            self._log.info("Using cached POST request playlist data")
            return cached_data

        server_url = source_config.get("server_url")
        if not server_url:
            self._log.error("No server_url provided for POST request")
            return []

        headers = source_config.get("headers", {})
        payload = source_config.get("payload", {})

        try:
            response = requests.post(server_url, headers=headers, json=payload)
            response.raise_for_status()  # Raise exception for non-200 status codes

            data = response.json()
            if not isinstance(data, dict) or "song_list" not in data:
                self._log.error("Invalid response format. Expected 'song_list' in JSON response")
                return []

            # Convert response to our standard format
            song_list = []
            for song in data["song_list"]:
                song_dict = {
                    "title": song.get("title", "").strip(),
                    "artist": song.get("artist", "").strip(),
                    "album": song.get("album", "").strip() if song.get("album") else None,
                }
                # Add year if available
                if "year" in song and song["year"]:
                    try:
                        year = int(song["year"])
                        song_dict["year"] = year
                    except (ValueError, TypeError):
                        pass

                if song_dict["title"] and song_dict["artist"]:  # Only add if we have minimum required fields
                    song_list.append(song_dict)

            # Cache successful results
            if song_list:
                self.cache.set_playlist_cache(playlist_id, 'post', song_list)
                self._log.info("Cached {} tracks from POST request playlist", len(song_list))

            return song_list

        except requests.exceptions.RequestException as e:
            self._log.error("Error making POST request: {}", e)
            return []
        except ValueError as e:
            self._log.error("Error parsing JSON response: {}", e)
            return []
        except Exception as e:
            self._log.error("Unexpected error during POST request: {}", e)
            return []

    def generate_imported_playlist(self, lib, playlist_config, plex_lookup=None):
        """Generate a playlist by importing from external sources."""
        playlist_name = playlist_config.get("name", "Imported Playlist")
        sources = playlist_config.get("sources", [])
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

        manual_search = self.get_config_value(
            playlist_config, defaults_cfg, "manual_search", config["plexsync"]["manual_search"].get(bool)
        )
        clear_playlist = self.get_config_value(
            playlist_config, defaults_cfg, "clear_playlist", False
        )

        if not sources:
            self._log.warning("No sources defined for imported playlist {}", playlist_name)
            return

        self._log.info("Generating imported playlist {} from {} sources", playlist_name, len(sources))

        # Import tracks from all sources
        all_tracks = []
        not_found_count = 0

        for source in sources:
            try:
                self._log.info("Importing from source: {}", source)
                if isinstance(source, str):  # Handle string sources (URLs and file paths)
                    if source.lower().endswith('.m3u8'):
                        # Check if path is absolute, if not make it relative to config dir
                        if not os.path.isabs(source):
                            source = os.path.join(self.config_dir, source)
                        tracks = self.import_m3u8_playlist(source)
                    elif "spotify" in source:
                        tracks = self.import_spotify_playlist(self.get_playlist_id(source))
                    elif "jiosaavn" in source:
                        tracks = self.import_jiosaavn_playlist(source)
                    elif "apple" in source:
                        tracks = self.import_apple_playlist(source)
                    elif "gaana" in source:
                        tracks = self.import_gaana_playlist(source)
                    elif "youtube" in source:
                        tracks = self.import_yt_playlist(source)
                    elif "tidal" in source:
                        tracks = self.import_tidal_playlist(source)
                    else:
                        self._log.warning("Unsupported source: {}", source)
                        continue
                elif isinstance(source, dict) and source.get("type") == "post":
                    tracks = self.import_post_playlist(source)
                else:
                    self._log.warning("Invalid source format: {}", source)
                    continue

                if tracks:
                    all_tracks.extend(tracks)

            except Exception as e:
                self._log.error("Error importing from {}: {}", source, e)
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(f"Error importing from source {source}: {str(e)}\n")
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

        for track in all_tracks:
            found = self.search_plex_song(track, manual_search)
            if found:
                # Just use Plex rating directly
                plex_rating = float(getattr(found, "userRating", 0) or 0)

                if plex_rating == 0 or plex_rating > 2:  # Include unrated or rating > 2
                    matched_songs.append(found)
                    self._log.debug(
                        "Matched in Plex: {} - {} - {} (Rating: {})",
                        track.get('artist', 'Unknown'),
                        track.get('parentTitle', 'Unknown'),
                        track.get('title', 'Unknown'),
                        plex_rating
                    )
                else:
                    with open(log_file, 'a', encoding='utf-8') as f:
                        f.write(f"Low rated ({plex_rating}): {track.get('artist', 'Unknown')} - {track.get('parentTitle', 'Unknown')} - {track.get('title', 'Unknown')}\n")
            else:
                not_found_count += 1
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(f"Not found: {track.get('artist', 'Unknown')} - {track.get('parentTitle', 'Unknown')} - {track.get('title', 'Unknown')}\n")

# Update progress bar
            progress_bar.update()

        # Complete and close progress bar
        progress_bar.close()
        manager.stop()

        # Get filters from config and apply them
        filters = playlist_config.get("filters", {})
        if filters:
            self._log.debug("Applying filters to {} matched tracks...", len(matched_songs))

            # Convert Plex tracks to beets items first
            beets_items = []

            # Use provided lookup dictionary or build new one if not provided
            if plex_lookup is None:
                self._log.debug("Building Plex lookup dictionary...")
                plex_lookup = self.build_plex_lookup(lib)

            for track in matched_songs:
                try:
                    beets_item = plex_lookup.get(track.ratingKey)
                    if beets_item:
                        beets_items.append(beets_item)
                except Exception as e:
                    self._log.debug("Error finding beets item for {}: {}", track.title, e)
                    continue

            # Now apply filters to beets items
            filtered_items = []
            for item in beets_items:
                include_item = True

                if 'exclude' in filters:
                    if 'years' in filters['exclude']:
                        years_config = filters['exclude']['years']
                        if 'after' in years_config and item.year:
                            if item.year > years_config['after']:
                                include_item = False
                                self._log.debug("Excluding {} (year {} > {})",
                                    item.title, item.year, years_config['after'])
                        if 'before' in years_config and item.year:
                            if item.year < years_config['before']:
                                include_item = False
                                self._log.debug("Excluding {} (year {} < {})",
                                    item.title, item.year, years_config['before'])

                if include_item:
                    filtered_items.append(item)

            self._log.debug("After filtering: {} tracks remain", len(filtered_items))
            matched_songs = filtered_items

        # Deduplicate based on ratingKey for Plex Track objects and plex_ratingkey for beets items
        seen = set()
        unique_matched = []
        for song in matched_songs:
            # Try both ratingKey (Plex Track) and plex_ratingkey (beets Item)
            rating_key = (
                getattr(song, 'ratingKey', None)  # For Plex Track objects
                or getattr(song, 'plex_ratingkey', None)  # For beets Items
            )
            if rating_key and rating_key not in seen:
                seen.add(rating_key)
                unique_matched.append(song)
        # Apply track limit if specified
        if max_tracks:
            unique_matched = unique_matched[:max_tracks]

        # Write summary at the end of log file
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"\nImport Summary:\n")
            f.write("-" * 80 + "\n")
            f.write(f"Total tracks from sources: {len(all_tracks)}\n")
            f.write(f"Tracks not found in Plex: {not_found_count}\n")
            f.write(f"Tracks matched and added: {len(matched_songs)}\n")
            f.write(f"\nImport completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

        self._log.info(
            "Found {} unique tracks after filtering (see {} for details)",
            len(unique_matched), log_file
        )

        # Create or update playlist based on clear_playlist setting
        if clear_playlist:
            try:
                self._plex_clear_playlist(playlist_name)
                self._log.info("Cleared existing playlist {}", playlist_name)
            except exceptions.NotFound:
                self._log.debug("No existing playlist {} found", playlist_name)

        if unique_matched:
            self._plex_add_playlist_item(unique_matched, playlist_name)
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

                # Clean up the title
                title = clean_title(title)

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
                self.generate_imported_playlist(lib, p, plex_lookup)  # Pass plex_lookup
            elif playlist_id in ["daily_discovery", "forgotten_gems"]:
                if playlist_id == "daily_discovery":
                    self.generate_daily_discovery(lib, p, plex_lookup, preferred_genres, similar_tracks)
                else:  # forgotten_gems
                    self.generate_forgotten_gems(lib, p, plex_lookup, preferred_genres, similar_tracks)
            else:
                self._log.warning(
                    "Unrecognized playlist configuration '{}' - type: '{}', id: '{}'. "
                    "Valid types are 'imported' or 'smart'. "
                    "Valid smart playlist IDs are 'daily_discovery' and 'forgotten_gems'.",
                    playlist_name, playlist_type, playlist_id
                )

    def shutdown(self, lib):
        """Clean up when plugin is disabled."""
        if self.loop and not self.loop.is_closed():
            self.close()

def get_color_for_score(score):
    """Get the appropriate color for a given score."""
    if score >= 0.8:
        return 'text_success'    # High match (green)
    elif score >= 0.5:
        return 'text_warning'    # Medium match (yellow)
    else:
        return 'text_error'      # Low match (red)

def clean_title(title):
    """Clean up track title by removing common extras and normalizing format.

    Args:
        title: The title string to clean

    Returns:
        str: Cleaned title string
    """
    # Remove various suffix patterns
    patterns = [
        r'\s*\([^)]*\)\s*$',  # Remove trailing parentheses and contents
        r'\s*\[[^\]]*\]\s*$',  # Remove trailing square brackets and contents
        r'\s*-\s*[^-]*$',      # Remove trailing dash and text
        r'\s*\|[^|]*$',        # Remove trailing pipe and text
        r'\s*feat\.[^,]*',     # Remove "feat." and featured artists
        r'\s*ft\.[^,]*',       # Remove "ft." and featured artists
        r'\s*\d+\s*$',         # Remove trailing numbers
        r'\s+$'                # Remove trailing whitespace
    ]

    cleaned = title
    for pattern in patterns:
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE)

    # Remove redundant spaces
    cleaned = ' '.join(cleaned.split())

    return cleaned
