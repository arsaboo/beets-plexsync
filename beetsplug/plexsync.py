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

import confuse
import dateutil.parser
import requests
import spotipy
from beets import config, ui
from beets.dbcore import types
from beets.dbcore.query import MatchQuery
from beets.library import DateType
from beets.plugins import BeetsPlugin
from bs4 import BeautifulSoup
from jiosaavn import JioSaavn
from plexapi import exceptions
from plexapi.server import PlexServer
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth


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

        self.config_dir = config.config_dir()

        # Adding defaults.
        config['plex'].add({
            'host': 'localhost',
            'port': 32400,
            'token': '',
            'library_name': 'Music',
            'secure': False,
            'ignore_cert_errors': False})

        config['plexsync'].add({
            'tokenfile': 'spotify_plexsync.json'})
        self.plexsync_token = config['plexsync']['tokenfile'].get(
            confuse.Filename(in_app_dir=True)
        )

        config['plex']['token'].redact = True
        baseurl = "http://" + config['plex']['host'].get() + ":" \
            + str(config['plex']['port'].get())
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

    # def setup_spotify(self):
    #     self._log.debug("Setting up Spotify")
    #     ID = config["spotify"]["client_id"].get()
    #     SECRET = config["spotify"]["client_secret"].get()
    #     self.auth_manager = SpotifyClientCredentials(client_id=ID,
    #                                                  client_secret=SECRET)
    #     self.sp = spotipy.Spotify(client_credentials_manager=self.auth_manager)

    def authenticate_spotify_old(self):
        ID = config["spotify"]["client_id"].get()
        SECRET = config["spotify"]["client_secret"].get()
        redirect_uri = "http://localhost/"
        scope = "user-read-private user-read-email"
        # Create a SpotifyOAuth object with your credentials and scope
        self.auth_manager = SpotifyOAuth(client_id=ID,
                                    client_secret=SECRET,
                                    redirect_uri=redirect_uri,
                                    scope=scope, open_browser=False,cache_path=self.plexsync_token)
        # Create a Spotify object with the auth_manager
        self.sp = spotipy.Spotify(auth_manager=self.auth_manager)

    def authenticate_spotify(self):
        ID = config["spotify"]["client_id"].get()
        SECRET = config["spotify"]["client_secret"].get()
        redirect_uri = "http://localhost/"
        scope = "user-read-private user-read-email"

        # Create a SpotifyOAuth object with your credentials and scope
        self.auth_manager = SpotifyOAuth(client_id=ID,
                                    client_secret=SECRET,
                                    redirect_uri=redirect_uri,
                                    scope=scope, open_browser=False,cache_path=self.plexsync_token)
        self.token_info = self.auth_manager.get_cached_token()
        if self.token_info is None:
            self.auth_manager.get_access_token(as_dict=True)
        need_token = self.auth_manager.is_token_expired(self.token_info)
        if need_token:
            new_token = self.auth_manager.refresh_access_token(self.token_info['refresh_token'])
            self.token_info = new_token
        # Create a Spotify object with the auth_manager
        self.sp = spotipy.Spotify(auth=self.token_info.get('access_token'))

    def import_spotify_playlist(self, playlist_id):
        """This function returns a list of tracks in a Spotify playlist."""
        self.authenticate_spotify()
        songs = self.get_playlist_tracks(playlist_id)
        song_list = []
        for song in songs:
            # Find and store the song title
            if (("From \"" in song["track"]["name"]) or \
                ("From &quot" in song["track"]["name"])):
                title_orig = song["track"]["name"].replace("&quot;", "\"")
                title, album = self.parse_title(title_orig)
            else:
                title = song["track"]["name"]
                album = self.clean_album_name(song["track"]["album"]["name"])
            try:
                year = dateutil.parser.parse(song["track"]["album"]["release_date"], ignoretz=True)
            except ValueError:
                year = None
            # Find and store the song artist
            artist = song["track"]["artists"][0]["name"]
            # Create a dictionary with the song information
            song_dict = {"title": title.strip(),
                         "album": album.strip(), "artist": artist.strip(), "year": year}
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
            tracks_response = self.sp.next(tracks_response)
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
                                             help='name of the playlist to be \
                                                 added in Plex')
        playlistimport_cmd.parser.add_option('-u', '--url', default='',
                                             help='playlist URL to be imported in Plex')
        def func_playlist_import(lib, opts, args):
            self._plex_import_playlist(opts.playlist, opts.url)

        playlistimport_cmd.func = func_playlist_import

        # plexplaylistclear command
        playlistclear_cmd = ui.Subcommand('plexplaylistclear',
                                           help="clear Plex playlist")

        playlistclear_cmd.parser.add_option('-m', '--playlist',
                                             default='',
                                             help='name of the Plex playlist \
                                                 to be cleared')
        def func_playlist_clear(lib, opts, args):
            self._plex_clear_playlist(opts.playlist)

        playlistclear_cmd.func = func_playlist_clear

        # plexcollage command
        collage_cmd = ui.Subcommand('plexcollage', help="create album collage \
            based on Plex history")

        collage_cmd.parser.add_option('-i', '--interval', default=7,
                                      help='days to look back for history')
        collage_cmd.parser.add_option('-g', '--grid', default=3,
                                      help='dimension of the collage grid')
        def func_collage(lib, opts, args):
            self._plex_collage(opts.interval, opts.grid)

        collage_cmd.func = func_collage

        return [plexupdate_cmd, sync_cmd, playlistadd_cmd, playlistrem_cmd,
                syncrecent_cmd, playlistimport_cmd, playlistclear_cmd,
                collage_cmd]

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
        album_orig = album_orig.replace("(Original Motion Picture Soundtrack)",
                                        "").replace("- Hindi","").strip()
        if "(From \"" in album_orig:
            album = re.sub(r'^[^"]+"|(?<!^)"[^"]+"|"[^"]+$', '', album_orig)
        elif "[From \"" in album_orig:
            album = re.sub(r'^[^"]+"|(?<!^)"[^"]+"|"[^"]+$', '', album_orig)
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
        data = asyncio.run(self.saavn.get_playlist_songs(playlist_url,
                                                         page=1, limit=100))
        songs = data['data']['list']
        song_list = []
        for song in songs:
            # Find and store the song title
            if (("From \"" in song['title']) or ("From &quot" in song['title'])):
                title_orig = song['title'].replace("&quot;", "\"")
                title, album = self.parse_title(title_orig)
            else:
                title = song['title']
                album = self.clean_album_name(song['more_info']['album'])
            year = song['year']
            # Find and store the song artist
            try:
                artist = song['more_info']['artistMap']['primary_artists'][0]['name']
            except:
                continue
            # Find and store the song duration
            #duration = song.find("div", class_="songs-list-row__length").text.strip()
            # Create a dictionary with the song information
            song_dict = {"title": title.strip(), "album": album.strip(), "artist": artist.strip(), "year": year}
            # Append the dictionary to the list of songs
            song_list.append(song_dict)
        return song_list

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

    def import_gaana_playlist(self, playlist_url):
        # Make a GET request to the playlist url
        response = requests.get(playlist_url)
        # Check if the response is successful
        if response.status_code == 200:
            # Parse the html data from the response
            soup = BeautifulSoup(response.text, "html.parser")
            # Find all the div elements with class "s_c"
            result = soup.find_all("ul", {"class": "_row list_data"})
            # Create an empty list to store the tracks
            tracks = []
            # Loop through each div element
            for div in result:
                div_art = div.find("div", {"class": "_art"})
                artist = div_art.text.strip()
                div_alb = div.find("div", {"class": "_alb"})
                album = div_alb.text.strip()
                span = div.find("span", {"class": "t_over"})
                # Get the text content of the span element
                title_tmp = span.text.strip()
                title_orig = re.sub("^Premium  ", "", title_tmp)
                if "(From \"" in title_orig or "[From \"" in title_orig:
                    title, album = self.parse_title(title_orig)
                else:
                    title = title_orig.strip()
                song_dict = {"title": title.strip(),
                             "album": self.clean_album_name(album.strip()),
                             "artist": artist}
                # Append the title to the tracks list
                tracks.append(song_dict)
            # Return the tracks as a list of strings
            return tracks
        else:
            # Raise an exception if the response is not successful
            raise Exception(f"Gaana website returned status code {response.status_code}")

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
            artist = song.find("div",
                               class_="songs-list-row__by-line").text.strip().replace("\n", "").replace("  ", "")
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
            self._log.info('Track {} - {} not found in Plex',
                           song['album'], song['title'])
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
        elif "gaana.com" in playlist_url:
            songs = self.import_gaana_playlist(playlist_url)
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
        """Get the most played albums from Plex in the last 10 days."""
        self._log.info('Creating collage of most played albums in the last {} '
                       'days', interval)
        tot = int(grid) ** 2
        # Get the most played albums in the last 10 days
        interval2 = str(interval) + 'd'
        albums = self.music.search(filters={'album.lastViewedAt>>': interval2},
                                   sort="viewCount:desc", libtype='album',
                                   maxresults=tot)
        sorted = self._plex_most_played_albums(albums, int(interval))
        # Create a list of album art
        album_art = []
        for album in sorted:
            album_art.append(album.thumbUrl)
        collage = self.create_collage(album_art, int(grid))
        collage.save(os.path.join(self.config_dir, 'collage.png'))

    def _plex_most_played_albums(self, albums, interval):
        from datetime import datetime, timedelta
        now = datetime.now
        for album in albums:
            fromdt = now() - timedelta(days=interval)
            history = album.history(mindate=fromdt)
            album.count = len(history)
        # sort the albums according to the number of times they were played
        sorted_albums = sorted(albums, key=lambda x: x.count, reverse=True)
        return sorted_albums


    def create_collage(self, list_image_urls, dimension):
        """Create a collage from a list of image urls."""
        import math
        from io import BytesIO

        from PIL import Image
        thumbnail_size = 300
        images = []
        for url in list_image_urls:
            try:
                response = requests.get(url)
                img = Image.open(BytesIO(response.content))
                img.resize((thumbnail_size, thumbnail_size))
                images.append(img)
            except Exception:
                self._log.debug('Unable to fetch image from {}', url)
                continue
        # Calculate the size of the grid
        grid_size = thumbnail_size * dimension
        # Create the new image
        grid = Image.new('RGB', size=(grid_size, grid_size))
        # Paste the images into the grid
        for index, image in enumerate(images):
            grid.paste(image,
                       box=(thumbnail_size * (index % dimension),
                            thumbnail_size * math.floor(index / dimension)))
        return grid
