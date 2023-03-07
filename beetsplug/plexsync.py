"""Update and sync Plex music library.

Plex users enter the Plex Token to enable updating.
Put something like the following in your config.yaml to configure:
    plex:
        host: localhost
        token: token
"""

import difflib
import re
import time

import dateutil.parser
import requests
import spotipy
from beets import config, ui
from beets.dbcore import types
from beets.dbcore.query import MatchQuery
from beets.library import DateType
from beets.plugins import BeetsPlugin
from bs4 import BeautifulSoup
from plexapi import exceptions
from plexapi.server import PlexServer
from spotipy.oauth2 import SpotifyClientCredentials


class PlexSync(BeetsPlugin):
    """Define plexsync class."""
    data_source = 'Plex'

    item_types = {
        'plex_guid': types.STRING,
        'plex_ratingkey': types.INTEGER,
        'plex_userrating': types.FLOAT,
        'plex_skipcount': types.INTEGER,
        'plex_viewcount': types.INTEGER,
        'plex_lastviewedat': DateType(),
        'plex_lastratedat': DateType(),
        'plex_updated': DateType(),
    }

    class dotdict(dict):
        """dot.notation access to dictionary attributes"""
        __getattr__ = dict.get
        __setattr__ = dict.__setitem__
        __delattr__ = dict.__delitem__

    def __init__(self):
        """Initialize plexsync plugin."""
        super().__init__()

        # Adding defaults.
        config['plex'].add({
            'host': 'localhost',
            'port': 32400,
            'token': '',
            'library_name': 'Music',
            'secure': False,
            'ignore_cert_errors': False})

        config['plex']['token'].redact = True
        baseurl = "http://" + config['plex']['host'].get() + ":" \
            + str(config['plex']['port'].get())
        self._log.info(baseurl)
        try:
            self.plex = PlexServer(baseurl,
                                   config['plex']['token'].get())
        except exceptions.Unauthorized:
            raise ui.UserError('Plex authorization failed')
        try:
            self.music = self.plex.library.section(
                config['plex']['library_name'].get())
        except exceptions.NotFound:
            raise ui.UserError(f"{config['plex']['library_name']} \
                library not found")
        self.register_listener('database_change', self.listen_for_db_change)

    def setup_spotify(self):
        print("Setting up Spotify")
        ID = config["spotify"]["client_id"].get()
        SECRET = config["spotify"]["client_secret"].get()
        self.auth_manager = SpotifyClientCredentials(client_id=ID,
                                                     client_secret=SECRET)
        self.sp = spotipy.Spotify(client_credentials_manager=self.auth_manager)

    def import_spotify_playlist(self, playlist_id):
        """This function returns a list of tracks in a Spotify playlist."""
        self.setup_spotify()
        songs = self.get_playlist_tracks(playlist_id)
        song_list = []
        for song in songs:
            # Find and store the song title
            if (("From \"" in song["track"]["name"]) or ("From &quot" in song["track"]["name"])):
                title_orig = song["track"]["name"].replace("&quot;", "\"")
                title, album = self.parse_title(title_orig)
            else:
                title = song["track"]["name"]
                album = self.clean_album_name(song["track"]["album"]["name"])
            year = dateutil.parser.parse(song["track"]["album"]["release_date"], ignoretz=True)
            # Find and store the song artist
            artist = song["track"]["artists"][0]["name"]
            # Find and store the song duration
            #duration = song.find("div", class_="songs-list-row__length").text.strip()
            # Create a dictionary with the song information
            song_dict = {"title": title.strip(), "album": album.strip(), "artist": artist.strip(), "year": year}
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

        tracks_response = self.sp.playlist_tracks(playlist_id)
        tracks = tracks_response["items"]
        while tracks_response["next"]:
            tracks_response = sp.next(tracks_response)
            tracks.extend(tracks_response["items"])
        return tracks

    def listen_for_db_change(self, lib, model):
        """Listens for beets db change and register the update for the end."""
        self.register_listener('cli_exit', self._plexupdate)

    def commands(self):
        """Add beet UI commands to interact with Plex."""
        plexupdate_cmd = ui.Subcommand(
            'plexupdate', help=f'Update {self.data_source} library')

        def func(lib, opts, args):
            self._plexupdate()

        plexupdate_cmd.func = func

        # plexsync command
        sync_cmd = ui.Subcommand('plexsync',
                                 help="fetch track attributes from Plex")
        sync_cmd.parser.add_option(
            '-f', '--force', dest='force_refetch',
            action='store_true', default=False,
            help='re-sync Plex data when already present'
        )

        def func_sync(lib, opts, args):
            items = lib.items(ui.decargs(args))
            self._fetch_plex_info(items, ui.should_write(),
                                  opts.force_refetch)

        sync_cmd.func = func_sync

        # plexplaylistadd command
        playlistadd_cmd = ui.Subcommand('plexplaylistadd',
                                        help="add tracks to Plex playlist")

        playlistadd_cmd.parser.add_option('-m', '--playlist',
                                          default='Beets',
                                          help='add playlist to Plex')

        def func_playlist_add(lib, opts, args):
            items = lib.items(ui.decargs(args))
            self._plex_add_playlist_item(items, opts.playlist)

        playlistadd_cmd.func = func_playlist_add

        # plexplaylistremove command
        playlistrem_cmd = ui.Subcommand('plexplaylistremove',
                                        help="Plex playlist to edit")

        playlistrem_cmd.parser.add_option('-m', '--playlist',
                                          default='Beets',
                                          help='Plex playlist to edit')

        def func_playlist_rem(lib, opts, args):
            items = lib.items(ui.decargs(args))
            self._plex_remove_playlist_item(items, opts.playlist)

        playlistrem_cmd.func = func_playlist_rem

        # plexsyncrecent command - instead of using the plexsync command which
        # can be slow, we can use the plexsyncrecent command to update info
        # for tracks played in the last 7 days.
        syncrecent_cmd = ui.Subcommand('plexsyncrecent',
                                       help="Sync recently played tracks")

        def func_sync_recent(lib, opts, args):
            self._update_recently_played(lib)

        syncrecent_cmd.func = func_sync_recent

        # plexplaylistimport command
        playlistimport_cmd = ui.Subcommand('plexplaylistimport',
                                           help="import playlist in to Plex")

        playlistimport_cmd.parser.add_option('-m', '--playlist',
                                             default='Beets',
                                             help='name of the playlist to be added in Plex')
        playlistimport_cmd.parser.add_option('-u', '--url', default='',
                                             help='playlist URL to be imported in Plex')
        def func_playlist_import(lib, opts, args):
            self._plex_import_playlist(opts.playlist, opts.url)

        playlistimport_cmd.func = func_playlist_import

        return [plexupdate_cmd, sync_cmd, playlistadd_cmd, playlistrem_cmd,
                syncrecent_cmd, playlistimport_cmd]

    def parse_title(self, title_orig):
        if "(From \"" in title_orig:
            title = re.sub(r'\(From.*\)', '', title_orig)
            album = re.sub(r'^[^"]+"|(?<!^)"[^"]+"|"[^"]+$', '', title_orig)
        elif "[From \"" in title_orig:
            title = re.sub(r'\[From.*\]', '', title_orig)
            album = re.sub(r'^[^"]+"|(?<!^)"[^"]+"|"[^"]+$', '', title_orig)
        else:
            title = title_orig
            album = ""
        return title, album

    def clean_album_name(self, album_orig):
        album_orig = album_orig.replace("(Original Motion Picture Soundtrack)", "").strip()
        if "(From \"" in album_orig:
            album = re.sub(r'^[^"]+"|(?<!^)"[^"]+"|"[^"]+$', '', album_orig)
        elif "[From \"" in album_orig:
            album = re.sub(r'^[^"]+"|(?<!^)"[^"]+"|"[^"]+$', '', album_orig)
        else:
            album = album_orig
        return album

    def import_apple_playlist(self, url):
        # Send a GET request to the URL and get the HTML content
        response = requests.get(url)
        content = response.text

        # Create a BeautifulSoup object with the HTML content
        soup = BeautifulSoup(content, "html.parser")

        # Find all the song elements on the page
        songs = soup.find_all("div", class_="songs-list-row")
        # Create an empty list to store the songs
        song_list = []
        # Loop through each song element
        for song in songs:
            # Find and store the song title
            title_orig = song.find("div", class_="songs-list-row__song-name").text.strip()
            title, album = self.parse_title(title_orig)
            # Find and store the song artist
            artist = song.find("div", class_="songs-list-row__by-line").text.strip().replace("\n", "").replace("  ", "")
            # Find and store the song duration
            #duration = song.find("div", class_="songs-list-row__length").text.strip()
            # Create a dictionary with the song information
            song_dict = {"title": title.strip(), "album": album.strip(), "artist": artist.strip()}
            # Append the dictionary to the list of songs
            song_list.append(song_dict)
        return song_list

    def _plexupdate(self):
        """Update Plex music library."""
        try:
            self.music.update()
            self._log.info('Update started.')
        except exceptions.PlexApiException:
            self._log.warning("{} Update failed",
                              self.config['plex']['library_name'])

    def _fetch_plex_info(self, items, write, force):
        """Obtain track information from Plex."""
        for index, item in enumerate(items, start=1):
            self._log.info('Processing {}/{} tracks - {} ',
                           index, len(items), item)
            # If we're not forcing re-downloading for all tracks, check
            # whether the popularity data is already present
            if not force:
                if 'plex_userrating' in item:
                    self._log.debug('Plex rating already present for: {}',
                                    item)
                    continue
            plex_track = self.search_plex_track(item)
            if plex_track is None:
                self._log.info('No track found for: {}', item)
                continue
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
            **{'album.title': item.album, 'track.title': item.title})
        if len(tracks) == 1:
            return tracks[0]
        elif len(tracks) > 1:
            for track in tracks:
                if track.parentTitle == item.album \
                   and track.title == item.title:
                    return track
        else:
            self._log.debug('Track {} not found in Plex library', item)
            return None

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
                self._log.warning('{} not found in Plex library. Error: {}',
                                  item, e)
                continue
        to_add = plex_set - playlist_set
        self._log.info('Adding {} tracks to {} playlist',
                       len(to_add), playlist)
        if plst is None:
            self._log.info('{} playlist will be created', playlist)
            self.plex.createPlaylist(playlist, items=list(to_add))
        else:
            plst.addItems(items=list(to_add))

    def _plex_remove_playlist_item(self, items, playlist):
        """Remove items from Plex playlist."""
        plex_set = set()
        try:
            plst = self.plex.playlist(playlist)
            playlist_set = set(plst.items())
        except exceptions.NotFound:
            self._log.error('{} playlist not found', playlist)
            return
        for item in items:
            try:
                plex_set.add(self.plex.fetchItem(item.plex_ratingkey))
            except (exceptions.NotFound) as e:
                self._log.warning('{} not found in Plex library. Error: {}',
                                  item, e)
                continue
        to_remove = plex_set.intersection(playlist_set)
        self._log.info('Removing {} tracks from {} playlist',
                       len(to_remove), playlist)
        plst.removeItems(items=list(to_remove))

    def _update_recently_played(self, lib):
        """Fetch the Plex track key."""
        tracks = self.music.search(
            filters={'track.lastViewedAt>>': '7d'}, libtype='track')
        self._log.info("Updating information for {} tracks", len(tracks))
        with lib.transaction():
            for track in tracks:
                query = MatchQuery("plex_ratingkey", track.ratingKey,
                                   fast=False)
                items = lib.items(query)
                if not items:
                    self._log.debug("{} | track not found", query)
                    continue
                elif len(items) == 1:
                    self._log.info("Updating information for {} ", items[0])
                    items[0].plex_userrating = track.userRating
                    items[0].plex_skipcount = track.skipCount
                    items[0].plex_viewcount = track.viewCount
                    items[0].plex_lastviewedat = track.lastViewedAt
                    items[0].plex_lastratedat = track.lastRatedAt
                    items[0].plex_updated = time.time()
                    items[0].store()
                    items[0].try_write()
                else:
                    self._log.debug("Please sync Plex library again")
                    continue

    # Define a function that takes a title string and a list of tuples as input
    def find_closest_match(self, title, lst):
        # Initialize an empty list to store the matches and their scores
        matches = []
        # Loop through each tuple in the list
        for t in lst:
            # Use the SequenceMatcher class to compare the title with the first element of the tuple
            # The ratio method returns a score between 0 and 1 indicating how similar the two strings are based on the Levenshtein distance
            score = difflib.SequenceMatcher(None, title, t.title).ratio()
            # Append the tuple and the score to the matches list
            matches.append((t, score))
        # Sort the matches list by the score in descending order
        matches.sort(key=lambda x: x[1], reverse=True)
        # Return only the first element of each tuple in the matches list as a new list
        return [m[0] for m in matches]

    def search_plex_song(self, song):
        """Fetch the Plex track key."""
        if song['album'] == "":
            tracks = self.music.searchTracks(**{'track.title': song['title']})
        else:
            tracks = self.music.searchTracks(**{'album.title': song['album'], 'track.title': song['title']})
        artist = song['artist'].split(",")[0]
        if len(tracks) == 1:
            return tracks[0]
        elif len(tracks) > 1:
            sorted_tracks = self.find_closest_match(song['title'], tracks)
            for track in sorted_tracks:
                if track.originalTitle is not None:
                    plex_artist = track.originalTitle
                else:
                    plex_artist = track.artist().title
                if artist in plex_artist:
                    return track
        else:
            self._log.info('Track {} not found in Plex library', song['title'])
            return None

    def _plex_import_playlist(self, playlist, playlist_url):
        """Import playlist into Plex."""
        if "http://" not in playlist_url and "https://" not in playlist_url:
            raise ui.UserError('Playlist URL not provided')
        self._log.info('Adding tracks from {} into {} playlist',
                       playlist_url, playlist)
        if "apple" in playlist_url:
            songs = self.import_apple_playlist(playlist_url)
        elif "jiosaavn" in playlist_url:
            songs = self.import_jiosaavn_playlist(playlist_url)
        elif "spotify" in playlist_url:
            songs = self.import_spotify_playlist(self.get_playlist_id(playlist_url))
        song_list = []
        for song in songs:
            if self.search_plex_song(song) is not None:
                found = self.search_plex_song(song)
                song_dict = {"title": found.title, "album": found.parentTitle,
                             "plex_ratingkey": found.ratingKey}
                song_list.append(self.dotdict(song_dict))
        self._plex_add_playlist_item(song_list, playlist)
