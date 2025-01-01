"""Update and sync Plex music library.

Plex users enter the Plex Token to enable updating.
Put something like the following in your config.yaml to configure:
    plex:
        host: localhost
        token: token
"""

import asyncio
import difflib
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import List

import confuse
import dateutil.parser
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
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth
from requests.exceptions import ContentDecodingError, ConnectionError
from pydantic import BaseModel, Field
import json


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
            {"tokenfile": "spotify_plexsync.json", "manual_search": False}
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

        # Add daily discovery command
        daily_discovery_cmd = ui.Subcommand(
            "dailydiscovery", help="Generate Daily Discovery playlist"
        )

        def func_daily_discovery(lib, opts, args):
            self.generate_daily_discovery()

        daily_discovery_cmd.func = func_daily_discovery

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
            daily_discovery_cmd,
        ]

    def parse_title(self, title_orig):
        if '(From "' in title_orig:
            title = re.sub(r"\(From.*\)", "", title_orig)
            album = re.sub(r'^[^"]+"|(?<!^)"[^"]+"|"[^"]+$', "", title_orig)
        elif '[From "' in title_orig:
            title = re.sub(r"\[From.*\]", "", title_orig)
            album = re.sub(r'^[^"]+"|(?<!^)"[^"]+"|"[^"]+$', "", title_orig)
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
            album = re.sub(r'^[^"]+"|(?<!^)"[^"]+"|"[^"]+$', "", album_orig)
        elif '[From "' in album_orig:
            album = re.sub(r'^[^"]+"|(?<!^)"[^"]+"|"[^"]+$', "", album_orig)
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

    def generate_daily_discovery(self):
        """Generate a Daily Discovery playlist."""
        self._log.info("Generating Daily Discovery playlist")

        # Define user preferences based on listening habits
        preferred_genres = self.get_preferred_genres()
        preferred_moods = self.get_preferred_moods()
        max_tracks = config["plexsync"].get("max_tracks", 20)

        # Fetch tracks from the library
        tracks = self.music.search(libtype="track")

        # Filter tracks based on user preferences and user rating
        filtered_tracks = [
            track
            for track in tracks
            if track.genre in preferred_genres
            and any(getattr(track, mood) for mood in preferred_moods)
            and (track.plex_userrating or 0) > 3  # Include tracks with user rating > 3
        ]

        # Sort tracks by user rating and Spotify popularity
        sorted_tracks = sorted(
            filtered_tracks,
            key=lambda x: (x.plex_userrating or 0, x.spotify_track_popularity or 0),
            reverse=True,
        )

        # Select tracks for the playlist
        selected_tracks = sorted_tracks[:max_tracks]

        # Create or update the Daily Discovery playlist in Plex
        playlist_name = "Daily Discovery"
        self._plex_add_playlist_item(selected_tracks, playlist_name)

        self._log.info(
            "Daily Discovery playlist generated with {} tracks", len(selected_tracks)
        )

    def get_preferred_genres(self):
        """Determine preferred genres based on user listening habits."""
        # Fetch tracks from the library
        tracks = self.music.search(libtype="track")

        # Count occurrences of each genre
        genre_counts = {}
        for track in tracks:
            genre = track.genre
            if genre:
                genre_counts[genre] = genre_counts.get(genre, 0) + 1

        # Sort genres by count and return the top genres
        sorted_genres = sorted(genre_counts, key=genre_counts.get, reverse=True)
        return sorted_genres[:5]  # Return top 5 genres

    def get_preferred_moods(self):
        """Determine preferred moods based on user listening habits."""
        # Fetch tracks from the library
        tracks = self.music.search(libtype="track")

        # Count occurrences of each mood
        mood_counts = {}
        mood_attributes = [
            "mood_acoustic",
            "mood_aggressive",
            "mood_electronic",
            "mood_happy",
            "mood_sad",
            "mood_party",
            "mood_relaxed",
            "mood_mirex",
        ]
        for track in tracks:
            for mood in mood_attributes:
                if getattr(track, mood, False):
                    mood_counts[mood] = mood_counts.get(mood, 0) + 1

        # Sort moods by count and return the top moods
        sorted_moods = sorted(mood_counts, key=mood_counts.get, reverse=True)
        return sorted_moods[:3]  # Return top 3 moods
