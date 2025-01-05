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
from typing import List

import confuse
import dateutil.parser
import numpy as np  # Add numpy import
import openai
import requests
import spotipy
from beets import config, ui
from beets.dbcore import types
from beets.dbcore.query import MatchQuery
from beets.library import DateType
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

        self.config_dir = config.config_dir()
        self.llm_client = None

        # Initialize minimal genre vocabulary for fallback encoding
        self.genre_vocabulary = [
            'rock', 'pop', 'electronic', 'hip hop',
            'classical', 'jazz', 'metal', 'indie'
        ]

        # Initialize genre embeddings as None (will be loaded on demand)
        self.genre_embeddings = None

        # Call the setup methods
        try:
            self.setup_llm()
        except Exception as e:
            self._log.error("Failed to set up LLM client: {}", e)
            self.llm_client = None

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
                "discovery_ratio": 70,  # Percentage of highly rated tracks (0-100)
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

    def import_spotify_playlist(self, playlist_id):
        """This function returns a list of tracks in a Spotify playlist."""
        self.authenticate_spotify()
        songs = self.get_playlist_tracks(playlist_id)
        song_list = []
        for song in songs:
            # Find and store the song title
            if ('From "' in song["track"]["name"]) or (
                "From &quot" in song["track"]["name"]
            ):
                title_orig = song["track"]["name"].replace("&quot;", '"')
                title, album = self.parse_title(title_orig)
            else:
                title = song["track"]["name"]
                album = self.clean_album_name(song["track"]["album"]["name"])
            try:
                year = dateutil.parser.parse(
                    song["track"]["album"]["release_date"], ignoretz=True
                )
            except ValueError:
                year = None
            # Find and store the song artist
            artist = song["track"]["artists"][0]["name"]
            # Create a dictionary with the song information
            song_dict = {
                "title": title.strip(),
                "album": album.strip(),
                "artist": artist.strip(),
                "year": year,
            }
            # Append the dictionary to the list of songs
            song_list.append(song_dict)
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

        def func_plex_smartplaylists(lib, opts, args):
            # Retrieve playlists from config
            playlists_config = config["plexsync"]["playlists"]["items"].get(list)
            if not playlists_config:
                self._log.warning(
                    "No playlists defined in config['plexsync']['playlists']['items']. Skipping."
                )
                return

            # Build lookup dictionary once
            self._log.info("Building Plex lookup dictionary...")
            plex_lookup = self.build_plex_lookup(lib)
            self._log.debug("Found {} tracks in lookup dictionary", len(plex_lookup))

            for p in playlists_config:
                playlist_id = p.get("id")
                if playlist_id == "daily_discovery":
                    self.generate_daily_discovery(lib, p, plex_lookup)
                elif playlist_id == "unheard_gems":
                    self.generate_unheard_gems(lib, p, plex_lookup)
                elif playlist_id == "unrated_gems":
                    self.generate_unrated_gems(lib, p, plex_lookup)

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

    def import_jiosaavn_playlist(self, playlist_url):
        data = asyncio.run(
            self.saavn.get_playlist_songs(playlist_url, page=1, limit=100)
        )
        songs = data["data"]["list"]
        song_list = []
        for song in songs:
            # Find and store the song title
            if ('From "' in song["title"]) or ("From &quot" in song["title"]):
                title_orig = song["title"].replace("&quot;", '"')
                title, album = self.parse_title(title_orig)
            else:
                title = song["title"]
                album = self.clean_album_name(song["more_info"]["album"])
            year = song["year"]
            # Find and store the song artist
            try:
                artist = song["more_info"]["artistMap"]["primary_artists"][0]["name"]
            except KeyError:
                continue
            # Find and store the song duration
            # duration = song.find("div", class_="songs-list-row__length").text.strip()
            # Create a dictionary with the song information
            song_dict = {
                "title": title.strip(),
                "album": album.strip(),
                "artist": artist.strip(),
                "year": year,
            }
            # Append the dictionary to the list of songs
            song_list.append(song_dict)
        return song_list

    # Define a function that takes a title string and a list of tuples as input
    def find_closest_match(self, title, lst):
        # Initialize an empty list to store the matches and their scores
        matches = []
        # Loop through each tuple in the list
        for t in lst:
            # Use the SequenceMatcher class to compare the title with the
            # first element of the tuple
            # The ratio method returns a score between 0 and 1 indicating how
            # similar the two strings are based on the Levenshtein distance
            score = difflib.SequenceMatcher(None, title, t.title).ratio()
            # Append the tuple and the score to the matches list
            matches.append((t, score))
        # Sort the matches list by the score in descending order
        matches.sort(key=lambda x: x[1], reverse=True)
        # Return only the first element of each tuple in the matches
        # list as a new list
        return [m[0] for m in matches]

    def import_apple_playlist(self, url):
        import json

        # Send a GET request to the URL and get the HTML content
        response = requests.get(url)
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
        songs = data[0]["data"]["sections"][1]["items"]

        # Create an empty list to store the songs
        song_list = []
        # Loop through each song element
        for song in songs:
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
        return song_list

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
            **{"album.title": item.album, "track.title": item.title}
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
        plex_set = set()
        try:
            plst = self.plex.playlist(playlist)
            playlist_set = set(plst.items())
        except exceptions.NotFound:
            plst = None
            playlist_set = set()
        for item in items:
            try:
                plex_set.add(self.plex.fetchItem(item.plex_ratingkey))
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
        """Fetch the Plex track key."""
        tracks = self.music.search(
            filters={"track.lastViewedAt>>": f"{days}d"}, libtype="track"
        )
        self._log.info("Updating information for {} tracks", len(tracks))
        with lib.transaction():
            for track in tracks:
                query = MatchQuery("plex_ratingkey", track.ratingKey, fast=False)
                items = lib.items(query)
                if not items:
                    self._log.debug("{} | track not found", query)
                    continue
                elif len(items) == 1:
                    self._log.info("Updating information for {} ", items[0])
                    try:
                        items[0].plex_userrating = track.userRating
                        items[0].plex_skipcount = track.skipCount
                        items[0].plex_viewcount = track.viewCount
                        items[0].plex_lastviewedat = (
                            track.lastViewedAt.timestamp()
                            if track.lastViewedAt
                            else None
                        )
                        items[0].plex_lastratedat = (
                            track.lastRatedAt.timestamp() if track.lastRatedAt else None
                        )
                        items[0].plex_updated = time.time()
                        items[0].store()
                        items[0].try_write()
                    except exceptions.NotFound:
                        self._log.debug("{} | track not found", items[0])
                        continue
                else:
                    self._log.debug("Please sync Plex library again")
                    continue

    def search_plex_song(self, song, manual_search=False):
        """Fetch the Plex track key."""

        if 'From "' in song["title"] or '[From "' in song["title"]:
            song["title"], song["album"] = self.parse_title(song["title"])
        try:
            if song["album"] is None:
                tracks = self.music.searchTracks(**{"track.title": song["title"]})
            else:
                tracks = self.music.searchTracks(
                    **{"album.title": song["album"], "track.title": song["title"]}
                )
                if len(tracks) == 0:
                    song["title"] = re.sub(r"\(.*\)", "", song["title"]).strip()
                    tracks = self.music.searchTracks(**{"track.title": song["title"]})
        except Exception as e:
            self._log.debug(
                "Error searching for {} - {}. Error: {}",
                song["album"],
                song["title"],
                e,
            )
            return None
        artist = song["artist"].split(",")[0]
        if len(tracks) == 1:
            return tracks[0]
        elif len(tracks) > 1:
            sorted_tracks = self.find_closest_match(song["title"], tracks)
            self._log.debug("Found {} tracks for {}", len(sorted_tracks), song["title"])
            if manual_search and len(sorted_tracks) > 0:
                print_(
                    f'Choose candidates for {song["album"]} '
                    f'- {song["title"]} - {song["artist"]}:'
                )
                for i, track in enumerate(sorted_tracks, start=1):
                    print_(
                        f"{i}. {track.parentTitle} - {track.title} - "
                        f"{track.artist().title}"
                    )
                sel = ui.input_options(
                    ("aBort", "Skip"), numrange=(1, len(sorted_tracks)), default=1
                )
                if sel in ("b", "B", "s", "S"):
                    return None
                return sorted_tracks[sel - 1] if sel > 0 else None
            for track in sorted_tracks:
                if track.originalTitle is not None:
                    plex_artist = track.originalTitle
                else:
                    plex_artist = track.artist().title
                if artist in plex_artist:
                    return track
        else:
            if config["plexsync"]["manual_search"] and not manual_search:
                self._log.info(
                    "Track {} - {} - {} not found in Plex",
                    song["album"],
                    song["artist"],
                    song["title"],
                )
                if ui.input_yn("Search manually? (Y/n)"):
                    self.manual_track_search()
            else:
                self._log.info(
                    "Track {} - {} - {} not found in Plex",
                    song["album"],
                    song["artist"],
                    song["title"],
                )
            return None

    def manual_track_search(self):
        """Manually search for a track in the Plex library.

        Prompts the user to enter the title, album, and artist of the track
        they want to search for.
        Calls the `search_plex_song` method with the provided information and
        sets the `manual_search` flag to True.
        """
        song_dict = {}
        title = input_("Title:").strip()
        album = input_("Album:").strip()
        artist = input_("Artist:").strip()
        song_dict = {
            "title": title.strip(),
            "album": album.strip(),
            "artist": artist.strip(),
        }
        self.search_plex_song(song_dict, manual_search=True)

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
            self.add_songs_to_plex("Weekly Jams", weekly_jams)

            self._log.info("Importing weekly exploration playlist")
            weekly_exploration = lb.get_weekly_exploration()
            self._log.info(
                "Importing {} songs from Weekly Exploration", len(weekly_exploration)
            )
            self.add_songs_to_plex("Weekly Exploration", weekly_exploration)
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
            self.add_songs_to_plex(playlist, songs)

    def add_songs_to_plex(self, playlist, songs):
        song_list = []
        if songs:
            for song in songs:
                if self.search_plex_song(song) is not None:
                    found = self.search_plex_song(song)
                    song_dict = {
                        "title": found.title,
                        "album": found.parentTitle,
                        "plex_ratingkey": found.ratingKey,
                    }
                    song_list.append(self.dotdict(song_dict))
        self._plex_add_playlist_item(song_list, playlist)

    def _plex_import_search(self, playlist, search, limit=10):
        """Import search results into Plex."""
        self._log.info("Searching for {}", search)
        songs = self.import_yt_search(search, limit)
        song_list = []
        if songs:
            for song in songs:
                if self.search_plex_song(song) is not None:
                    found = self.search_plex_song(song)
                    song_dict = {
                        "title": found.title,
                        "album": found.parentTitle,
                        "plex_ratingkey": found.ratingKey,
                    }
                    song_list.append(self.dotdict(song_dict))
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
            if self.search_plex_song(song) is not None:
                found = self.search_plex_song(song)
                match_dict = {
                    "title": found.title,
                    "album": found.parentTitle,
                    "plex_ratingkey": found.ratingKey,
                }
                self._log.debug("Song matched in Plex library: {}", match_dict)
                matched_songs.append(self.dotdict(match_dict))
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
            if base_url:
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
        return ytp.import_youtube_playlist(url)

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
        try:
            from beetsplug.tidal import TidalPlugin
        except ModuleNotFoundError:
            self._log.error("Tidal plugin not installed")
            return
        try:
            tidal = TidalPlugin()
        except Exception as e:
            self._log.error("Unable to initialize Tidal plugin. Error: {}", e)
            return
        return tidal.import_tidal_playlist(url)

    def import_gaana_playlist(self, url):
        try:
            from beetsplug.gaana import GaanaPlugin
        except ModuleNotFoundError:
            self._log.error(
                "Gaana plugin not installed. \
                            See https://github.com/arsaboo/beets-gaana"
            )
            return
        try:
            gaana = GaanaPlugin()
        except Exception as e:
            self._log.error("Unable to initialize Gaana plugin. Error: {}", e)
            return
        return gaana.import_gaana_playlist(url)

    def _plex2spotify(self, lib, playlist):
        self.authenticate_spotify()
        plex_playlist = self.plex.playlist(playlist)
        plex_playlist_items = plex_playlist.items()
        self._log.debug(f"Plex playlist items: {plex_playlist_items}")
        spotify_tracks = []
        for item in plex_playlist_items:
            self._log.debug(f"Processing {item.ratingKey}")
            with lib.transaction():
                query = MatchQuery("plex_ratingkey", item.ratingKey, fast=False)
                items = lib.items(query)
                if not items:
                    self._log.debug(
                        f"Item not found in Beets "
                        f"{item.ratingKey}: {item.parentTitle} - "
                        f"{item.title}"
                    )
                    continue
                beets_item = items[0]
                self._log.debug(f"Beets item: {beets_item}")
                try:
                    spotify_track_id = beets_item.spotify_track_id
                    self._log.debug(
                        f"Spotify track id in beets: " f"{spotify_track_id}"
                    )
                except Exception:
                    spotify_track_id = None
                    self._log.debug("Spotify track_id not found in beets")
                if not spotify_track_id:
                    self._log.debug(
                        f"Searching for {beets_item.title} "
                        f"{beets_item.album} in Spotify"
                    )
                    spotify_search_results = self.sp.search(
                        q=f"track:{beets_item.title} album:{beets_item.album}",
                        limit=1,
                        type="track",
                    )
                    if not spotify_search_results["tracks"]["items"]:
                        self._log.info(f"Spotify match not found for " f"{beets_item}")
                        continue
                    spotify_track_id = spotify_search_results["tracks"]["items"][0][
                        "id"
                    ]
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

        # Get tracks to exclude (played in last exclusion_days)
        exclusion_date = datetime.now() - timedelta(days=exclusion_days)
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

    def generate_daily_discovery(self, lib, dd_config, plex_lookup):
        """Generate a Daily Discovery playlist with plex_smartplaylists command."""
        playlist_name = dd_config.get("name", "Daily Discovery")
        self._log.info("Generating {} playlist", playlist_name)

        # Setup and configuration
        preferred_genres, similar_tracks = self.get_preferred_attributes()
        self._log.debug(f"Using preferred genres: {preferred_genres}")
        self._log.debug(f"Processing {len(similar_tracks)} pre-filtered similar tracks")

        if (
            "playlists" in config["plexsync"]
            and "defaults" in config["plexsync"]["playlists"]
        ):
            defaults_cfg = config["plexsync"]["playlists"]["defaults"].get({})
        else:
            defaults_cfg = {}

        max_tracks = self.get_config_value(dd_config, defaults_cfg, "max_tracks", 20)
        discovery_ratio = self.get_config_value(
            dd_config, defaults_cfg, "discovery_ratio", 70
        )

        # Use lookup dictionary instead of individual queries
        matched_tracks = []
        for plex_track in similar_tracks:
            try:
                beets_item = plex_lookup.get(plex_track.ratingKey)
                if beets_item and float(getattr(beets_item, "plex_userrating", 0)) > 3:
                    matched_tracks.append(beets_item)
                    self._log.debug(
                        "Matched: {} - {} (Rating: {})",
                        beets_item.artist,
                        beets_item.title,
                        getattr(beets_item, "plex_userrating", 0),
                    )
            except Exception as e:
                self._log.debug("Error processing track {}: {}", plex_track.title, e)
                continue

        self._log.info("Found {} tracks matching criteria", len(matched_tracks))

        # Replace the sorting and selection block with:
        import random

        # Get the discovery ratio from config (default 70%)
        discovery_ratio = max(0, min(100, discovery_ratio)) / 100.0

        # Calculate how many tracks of each type we want
        rated_tracks_count = int(max_tracks * discovery_ratio)
        discovery_tracks_count = max_tracks - rated_tracks_count

        # Split tracks into rated and unrated
        rated_tracks = []
        unrated_tracks = []
        for track in matched_tracks:
            rating = float(getattr(track, "plex_userrating", 0))
            if rating > 3:
                rated_tracks.append(track)
            elif rating == 0:  # Only truly unrated tracks
                unrated_tracks.append(track)

        # Sort rated tracks by rating and popularity
        rated_tracks = sorted(
            rated_tracks,
            key=lambda x: (
                float(getattr(x, "plex_userrating", 0)),
                int(getattr(x, "spotify_track_popularity", 0)),
            ),
            reverse=True,
        )

        # Sort unrated tracks by popularity
        unrated_tracks = sorted(
            unrated_tracks,
            key=lambda x: int(getattr(x, "spotify_track_popularity", 0)),
            reverse=True,
        )

        # Select tracks
        selected_rated = rated_tracks[:rated_tracks_count]
        selected_unrated = unrated_tracks[:discovery_tracks_count]

        # If we don't have enough unrated tracks, fill with rated ones
        if len(selected_unrated) < discovery_tracks_count:
            additional_rated = rated_tracks[
                rated_tracks_count : rated_tracks_count
                + (discovery_tracks_count - len(selected_unrated))
            ]
            selected_rated.extend(additional_rated)

        # Combine and shuffle
        selected_tracks = selected_rated + selected_unrated
        random.shuffle(selected_tracks)

        if not selected_tracks:
            self._log.warning("No tracks matched criteria for Daily Discovery playlist")
            return

        # Clear existing playlist only right before adding new tracks
        try:
            self._plex_clear_playlist(playlist_name)
            self._log.info("Cleared existing Daily Discovery playlist")
        except exceptions.NotFound:
            self._log.debug("No existing Daily Discovery playlist found")

        # Create playlist
        self._plex_add_playlist_item(selected_tracks, playlist_name)

        self._log.info(
            "Successfully updated {} playlist with {} tracks",
            playlist_name,
            len(selected_tracks),
        )

    def generate_unheard_gems(self, lib, ug_config, plex_lookup):
        """Generate an Unheard Gems playlist with tracks matching user taste but low play count."""
        playlist_name = ug_config.get("name", "Unheard Gems")
        self._log.info("Generating {} playlist", playlist_name)

        # Get preferred genres from user's listening history
        preferred_genres, _ = self.get_preferred_attributes()
        self._log.debug(f"Using preferred genres: {preferred_genres}")

        # Get configuration
        if (
            "playlists" in config["plexsync"]
            and "defaults" in config["plexsync"]["playlists"]
        ):
            defaults_cfg = config["plexsync"]["playlists"]["defaults"].get({})
        else:
            defaults_cfg = {}

        max_tracks = self.get_config_value(ug_config, defaults_cfg, "max_tracks", 20)
        max_plays = self.get_config_value(ug_config, defaults_cfg, "max_plays", 2)

        # Build filters for unplayed/barely played tracks
        filters = {
            "track.viewCount<<=": max_plays,  # Tracks played max_plays times or less
            "track.userRating!=": 1,  # Exclude 1-star rated tracks
            "track.userRating!=": 2,  # Exclude 2-star rated tracks
        }

        # Find tracks with matching genres but low play count
        unheard_tracks = []
        tracks = self.music.searchTracks(**filters)

        for track in tracks:
            try:
                # Check if track genres match user preferences
                track_genres = {str(g.tag).lower() for g in track.genres}
                if any(genre in track_genres for genre in preferred_genres):
                    beets_item = plex_lookup.get(track.ratingKey)
                    if beets_item:
                        unheard_tracks.append(beets_item)
                        self._log.debug(
                            "Found unheard gem: {} - {} (Plays: {})",
                            beets_item.artist,
                            beets_item.title,
                            track.viewCount,
                        )
            except Exception as e:
                self._log.debug("Error processing track {}: {}", track.title, e)
                continue

        # Sort by popularity if available
        unheard_tracks.sort(
            key=lambda x: int(getattr(x, "spotify_track_popularity", 0)), reverse=True
        )

        # Select tracks
        selected_tracks = unheard_tracks[:max_tracks]

        if not selected_tracks:
            self._log.warning("No tracks matched criteria for Unheard Gems playlist")
            return

        # Clear existing playlist
        try:
            self._plex_clear_playlist(playlist_name)
            self._log.info("Cleared existing Unheard Gems playlist")
        except exceptions.NotFound:
            self._log.debug("No existing Unheard Gems playlist found")

        # Create playlist
        self._plex_add_playlist_item(selected_tracks, playlist_name)

        self._log.info(
            "Successfully updated {} playlist with {} tracks",
            playlist_name,
            len(selected_tracks),
        )

    def generate_unrated_gems(self, lib, ug_config, plex_lookup):
        """Generate an Unrated Gems playlist using hybrid recommendations."""
        playlist_name = ug_config.get("name", "Unrated Gems")
        self._log.info("Generating {} playlist", playlist_name)

        # Get configuration
        if "playlists" in config["plexsync"] and "defaults" in config["plexsync"]["playlists"]:
            defaults_cfg = config["plexsync"]["playlists"]["defaults"].get({})
        else:
            defaults_cfg = {}

        max_tracks = self.get_config_value(ug_config, defaults_cfg, "max_tracks", 20)

        # 1. Analyze user preferences from rated tracks (lower rating threshold to 4)
        rated_tracks = []
        for item in lib.items():
            if hasattr(item, "plex_userrating") and float(item.plex_userrating) >= 4:
                rated_tracks.append(item)

        if not rated_tracks:
            self._log.warning("No rated tracks found for building user preferences")
            return

        # 2. Build user preference profile
        preferences = self._build_user_preferences(rated_tracks)

        # 3. Find candidate unrated tracks
        candidates = []
        for item in lib.items():
            if (not hasattr(item, "plex_userrating") or
                item.plex_userrating == 0 or
                item.plex_userrating is None):
                candidates.append(item)

        self._log.debug("Found {} candidate tracks", len(candidates))

        # 4. Score all candidates
        scored_tracks = []
        for track in candidates:
            score = self._calculate_track_score(track, preferences)
            scored_tracks.append((track, score))
            self._log.debug(
                "Track scored {:.2f}: {} - {}",
                score,
                track.artist,
                track.title
            )

        # 5. Sort by score and additional criteria
        scored_tracks.sort(
            key=lambda x: (
                x[1],  # Primary sort by score
                int(getattr(x[0], "spotify_track_popularity", 0)),  # Secondary sort by popularity
                getattr(x[0], "year", 0)  # Tertiary sort by year
            ),
            reverse=True
        )

        # 6. Select top tracks while ensuring artist diversity
        selected_tracks = []
        artist_limit = max(3, max_tracks // 5)  # Allow up to 3 tracks per artist or 20% of max_tracks
        artist_count = {}

        for track, score in scored_tracks:
            artist = track.artist
            if artist_count.get(artist, 0) < artist_limit:
                selected_tracks.append(track)
                artist_count[artist] = artist_count.get(artist, 0) + 1
                self._log.debug(
                    "Selected track: {} - {} (Score: {:.2f})",
                    track.artist,
                    track.title,
                    score
                )

            if len(selected_tracks) >= max_tracks:
                break

        if not selected_tracks:
            self._log.warning("No suitable unrated tracks found")
            return

        self._log.info("Selected {} tracks for playlist", len(selected_tracks))

        # 7. Update playlist
        try:
            self._plex_clear_playlist(playlist_name)
            self._log.info("Cleared existing Unrated Gems playlist")
        except exceptions.NotFound:
            self._log.debug("No existing Unrated Gems playlist found")

        self._plex_add_playlist_item(selected_tracks, playlist_name)
        self._log.info(
            "Successfully updated {} playlist with {} tracks",
            playlist_name,
            len(selected_tracks)
        )

    def _build_user_preferences(self, rated_tracks):
        """Build user preference profile from rated tracks."""
        preferences = {
            "genres": {},
            "moods": {},
            "artist_gender": {"male": 0, "female": 0},
            "audio_features": {
                "bpm": [],
                "danceability": [],
                "loudness": [],
            },
        }

        for track in rated_tracks:
            # Genre preferences
            if hasattr(track, "genre"):
                genres = track.genre.split(";")
                for genre in genres:
                    preferences["genres"][genre.strip()] = (
                        preferences["genres"].get(genre.strip(), 0) + 1
                    )

            # Mood preferences
            mood_attributes = [
                "mood_acoustic",
                "mood_aggressive",
                "mood_electronic",
                "mood_happy",
                "mood_sad",
                "mood_party",
                "mood_relaxed",
            ]
            for attr in mood_attributes:
                if hasattr(track, attr):
                    preferences["moods"][attr] = preferences["moods"].get(attr, [])
                    preferences["moods"][attr].append(float(getattr(track, attr, 0)))

            # Artist gender preferences
            if hasattr(track, "is_male") and track.is_male:
                preferences["artist_gender"]["male"] += 1
            if hasattr(track, "is_female") and track.is_female:
                preferences["artist_gender"]["female"] += 1

            # Audio features
            if hasattr(track, "bpm"):
                preferences["audio_features"]["bpm"].append(float(track.bpm))
            if hasattr(track, "danceability"):
                preferences["audio_features"]["danceability"].append(
                    float(track.danceability)
                )
            if hasattr(track, "average_loudness"):
                preferences["audio_features"]["loudness"].append(
                    float(track.average_loudness)
                )

        # Normalize preferences
        self._normalize_preferences(preferences)
        return preferences

    def _normalize_preferences(self, preferences):
        """Normalize preference values."""
        # Normalize genres
        total_genres = sum(preferences["genres"].values())
        if total_genres > 0:
            for genre in preferences["genres"]:
                preferences["genres"][genre] /= total_genres

        # Calculate averages for moods and audio features
        for mood in preferences["moods"]:
            if preferences["moods"][mood]:
                preferences["moods"][mood] = sum(preferences["moods"][mood]) / len(
                    preferences["moods"][mood]
                )

        for feature in preferences["audio_features"]:
            if preferences["audio_features"][feature]:
                preferences["audio_features"][feature] = {
                    "mean": sum(preferences["audio_features"][feature])
                    / len(preferences["audio_features"][feature]),
                    "std": self._calculate_std(preferences["audio_features"][feature]),
                }

    def _calculate_std(self, values):
        """Calculate standard deviation of a list of numbers."""
        if not values:
            return 0.0

        mean = sum(values) / len(values)
        squared_diff_sum = sum((x - mean) ** 2 for x in values)
        variance = squared_diff_sum / len(values)
        return variance ** 0.5

    def _calculate_track_score(self, track, preferences):
        """Calculate similarity score using collaborative filtering and weighted learning."""
        # Initialize feature vectors
        track_features = self._extract_track_features(track)
        if not track_features:
            return 0.0

        # Get learned weights from user preferences
        weights = self._calculate_feature_weights(preferences)

        # Calculate weighted cosine similarity
        similarity_score = self._weighted_cosine_similarity(
            track_features,
            preferences["feature_vector"],
            weights
        )

        # Apply temporal decay to favor more recent preferences
        if hasattr(track, "added"):
            temporal_weight = self._calculate_temporal_weight(track.added)
            similarity_score *= temporal_weight

        # Normalize to 0-1 range
        return max(0.0, min(1.0, similarity_score))

    def _extract_track_features(self, track):
        """Extract and normalize feature vector from track."""
        features = {}

        # Audio features (normalize to 0-1 range)
        audio_features = {
            'bpm': (0, 200),  # Most songs under 200 BPM
            'beats_count': (0, 1000),  # Normalize beat count
            'average_loudness': (-60, 0),  # Typical loudness range in dB
            'danceability': (0, 1)  # Already normalized
        }

        for feature, (min_val, max_val) in audio_features.items():
            if hasattr(track, feature):
                value = float(getattr(track, feature))
                if feature == 'average_loudness':
                    # Normalize loudness from dB range to 0-1
                    features[feature] = (value - min_val) / (max_val - min_val)
                else:
                    features[feature] = max(0.0, min(1.0, value / max_val))

        # Boolean features
        binary_features = [
            'danceable', 'is_voice', 'is_instrumental'
        ]

        for feature in binary_features:
            if hasattr(track, feature):
                features[feature] = 1.0 if getattr(track, feature) else 0.0

        # Mood features (assumed to be already normalized 0-1)
        mood_features = [
            'mood_acoustic', 'mood_aggressive', 'mood_electronic',
            'mood_happy', 'mood_sad', 'mood_party', 'mood_relaxed'
        ]

        for feature in mood_features:
            if hasattr(track, feature):
                features[feature] = float(getattr(track, feature))

        # MIREX mood clusters (one-hot encoding)
        mirex_clusters = [
            'mood_mirex_cluster_1', 'mood_mirex_cluster_2',
            'mood_mirex_cluster_3', 'mood_mirex_cluster_4',
            'mood_mirex_cluster_5'
        ]

        for cluster in mirex_clusters:
            if hasattr(track, cluster):
                features[cluster] = float(getattr(track, cluster))

        # Gender features
        if hasattr(track, 'is_male'):
            features['is_male'] = float(track.is_male)
        if hasattr(track, 'is_female'):
            features['is_female'] = float(track.is_female)

        # Genre features (using rosamerica classification)
        if hasattr(track, 'genre_rosamerica'):
            genres = str(track.genre_rosamerica).split(';')
            features['genre_vector'] = self._encode_genres(genres)

        # Voice/Instrumental classification (convert to binary)
        if hasattr(track, 'voice_instrumental'):
            # Convert categorical to binary (1.0 for 'voice', 0.0 for 'instrumental')
            features['voice_instrumental'] = 1.0 if track.voice_instrumental == 'voice' else 0.0

        return features

    def _calculate_feature_weights(self, preferences):
        """Calculate feature importance weights using user preference history."""
        weights = {}

        # Updated base weights for our feature categories
        base_weights = {
            'audio': 0.25,      # Audio features (bpm, loudness, etc.)
            'mood': 0.30,       # Mood and MIREX clusters
            'genre': 0.25,      # Genre classifications
            'metadata': 0.20    # Gender, voice/instrumental, etc.
        }

        # Adjust weights based on user preference consistency
        if preferences.get('rating_history'):
            # Calculate preference consistency scores
            audio_consistency = self._calculate_consistency(
                preferences['rating_history'], 'audio'
            )
            mood_consistency = self._calculate_consistency(
                preferences['rating_history'], 'mood'
            )
            genre_consistency = self._calculate_consistency(
                preferences['rating_history'], 'genre'
            )
            metadata_consistency = self._calculate_consistency(
                preferences['rating_history'], 'metadata'
            )

            # Normalize consistency scores
            total_consistency = (audio_consistency + mood_consistency +
                               genre_consistency + metadata_consistency)

            if total_consistency > 0:
                weights['audio'] = base_weights['audio'] * (audio_consistency / total_consistency)
                weights['mood'] = base_weights['mood'] * (mood_consistency / total_consistency)
                weights['genre'] = base_weights['genre'] * (genre_consistency / total_consistency)
                weights['metadata'] = base_weights['metadata'] * (metadata_consistency / total_consistency)
        else:
            weights = base_weights

        return weights

    def _weighted_cosine_similarity(self, vec1, vec2, weights):
        """Calculate weighted cosine similarity between two feature vectors."""
        if not vec1 or not vec2:
            return 0.0

        numerator = 0.0
        norm1 = 0.0
        norm2 = 0.0

        # Calculate weighted dot product and norms
        for feature in vec1:
            if feature in vec2 and feature in weights:
                weight = weights.get(feature, 1.0)
                numerator += weight * vec1[feature] * vec2[feature]
                norm1 += weight * vec1[feature] * vec1[feature]
                norm2 += weight * vec2[feature] * vec2[feature]

        # Avoid division by zero
        if norm1 == 0.0 or norm2 == 0.0:
            return 0.0

        return numerator / ((norm1 * norm2) ** 0.5)

    def _calculate_temporal_weight(self, timestamp):
        """Calculate temporal weight to favor more recent preferences."""
        if not timestamp:
            return 1.0

        # Convert timestamp to datetime if needed
        if isinstance(timestamp, (int, float)):
            timestamp = datetime.fromtimestamp(timestamp)

        # Calculate days since the track was added
        days_old = (datetime.now() - timestamp).days

        # Use a half-life decay function
        half_life = 365  # Adjust this value to control decay rate
        temporal_weight = 2 ** (-days_old / half_life)

        return temporal_weight

    def _calculate_consistency(self, history, feature_type):
        """Calculate consistency score for a particular feature type."""
        if not history:
            return 1.0

        # Group ratings by feature values
        feature_ratings = {}
        for entry in history:
            feature_val = entry.get(feature_type)
            rating = entry.get('rating')
            if feature_val and rating:
                if feature_val not in feature_ratings:
                    feature_ratings[feature_val] = []
                feature_ratings[feature_val].append(rating)

        # Calculate rating variance for each feature value
        variances = []
        for ratings in feature_ratings.values():
            if len(ratings) > 1:
                mean = sum(ratings) / len(ratings)
                variance = sum((r - mean) ** 2 for r in ratings) / len(ratings)
                variances.append(variance)

        # Return inverse of average variance (higher consistency = lower variance)
        if variances:
            avg_variance = sum(variances) / len(variances)
            return 1.0 / (1.0 + avg_variance)
        return 1.0

    def _encode_genres(self, genres):
        """Encode genres using pre-trained embeddings or one-hot encoding."""
        # If using pre-trained embeddings (recommended)
        if hasattr(self, 'genre_embeddings') and self.genre_embeddings is not None:
            try:
                genre_vec = np.zeros(self.genre_embeddings.vector_size)
                count = 0
                for genre in genres:
                    if genre in self.genre_embeddings:
                        genre_vec += self.genre_embeddings[genre]
                        count += 1
                return genre_vec / count if count > 0 else genre_vec
            except (AttributeError, TypeError):
                # Fall through to one-hot encoding if embeddings fail
                pass

        # Fallback to one-hot encoding
        genre_vec = np.zeros(len(self.genre_vocabulary))
        for genre in genres:
            if genre in self.genre_vocabulary:
                idx = self.genre_vocabulary.index(genre)
                genre_vec[idx] = 1
        return genre_vec
