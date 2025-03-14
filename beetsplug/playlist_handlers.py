"""Playlist operations and import functions for PlexSync plugin."""

import json
import re
from datetime import datetime
from pathlib import Path

import dateutil.parser
import requests
import spotipy
from beets import ui
from bs4 import BeautifulSoup
from plexapi import exceptions
from spotipy.oauth2 import SpotifyOAuth
from jiosaavn import JioSaavn

from beetsplug.utils import parse_title, clean_album_name, clean_title


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


def process_spotify_track(self, track):
    """Process a single Spotify track into a standardized format."""
    try:
        # Find and store the song title
        if ('From "' in track['name']) or ("From &quot" in track['name']):
            title_orig = track['name'].replace("&quot;", '"')
            title, album = parse_title(title_orig)
        else:
            title = track['name']
            album = clean_album_name(track['album']['name'])

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

saavn = JioSaavn()

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
                    title, album = parse_title(title_orig)
                else:
                    title = song["title"]
                    album = clean_album_name(song["more_info"]["album"])

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
            for line in f:
                line = line.strip()
                if not line or line.startswith('#EXTM3U'):
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
                elif line.startswith('#EXTALB:'):
                    # Extract album info - fix the strip() typo here
                    current_song['album'] = line[8:].strip()
                elif not line.startswith('#'):
                    # This is a file path line - finalize the song entry
                    if current_song and all(k in current_song for k in ['title', 'artist']):
                        # If no album was specified, use None
                        if 'album' not in current_song:
                            current_song['album'] = None
                        song_list.append(current_song)
                        current_song = {}

        if song_list:
            self.cache.set_playlist_cache(playlist_id, 'm3u8', song_list)
            self._log.info("Cached {} tracks from M3U8 playlist", len(song_list))

        return song_list

    except Exception as e:
        self._log.error("Error importing M3U8 playlist {}: {}", filepath, e)
        return []


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

    plex_add_playlist_item(self, song_list, playlist)


def plex_add_playlist_item(plugin, items, playlist):
    """Add items to Plex playlist."""
    if not items:
        plugin._log.warning("No items to add to playlist {}", playlist)
        return

    plex_set = set()
    try:
        plst = plugin.plex.playlist(playlist)
        playlist_set = set(plst.items())
    except exceptions.NotFound:
        plst = None
        playlist_set = set()
    for item in items:
        try:
            # Check for both plex_ratingkey and ratingKey
            rating_key = getattr(item, 'plex_ratingkey', None) or getattr(item, 'ratingKey', None)
            if rating_key:
                plex_set.add(plugin.plex.fetchItem(rating_key))
            else:
                plugin._log.warning("{} does not have plex_ratingkey or ratingKey attribute. Item details: {}", item, vars(item))
        except (exceptions.NotFound, AttributeError) as e:
            plugin._log.warning("{} not found in Plex library. Error: {}", item, e)
            continue
    to_add = plex_set - playlist_set
    plugin._log.info("Adding {} tracks to {} playlist", len(to_add), playlist)
    if plst is None:
        plugin._log.info("{} playlist will be created", playlist)
        plugin.plex.createPlaylist(playlist, items=list(to_add))
    else:
        try:
            plst.addItems(items=list(to_add))
        except exceptions.BadRequest as e:
            plugin._log.error(
                "Error adding items {} to {} playlist. Error: {}",
                items,
                playlist,
                e,
            )
    plugin.sort_plex_playlist(playlist, "lastViewedAt")


def plex_remove_playlist_item(plugin, items, playlist):
    """Remove items from Plex playlist."""
    plex_set = set()
    try:
        plst = plugin.plex.playlist(playlist)
        playlist_set = set(plst.items())
    except exceptions.NotFound:
        plugin._log.error("{} playlist not found", playlist)
        return
    for item in items:
        try:
            plex_set.add(plugin.plex.fetchItem(item.plex_ratingkey))
        except (
            exceptions.NotFound,
            AttributeError,
            requests.exceptions.ContentDecodingError,
            requests.exceptions.ConnectionError,
        ) as e:
            plugin._log.warning("{} not found in Plex library. Error: {}", item, e)
            continue
    to_remove = plex_set.intersection(playlist_set)
    plugin._log.info("Removing {} tracks from {} playlist", len(to_remove), playlist)
    plst.removeItems(items=list(to_remove))


def plex_clear_playlist(plugin, playlist):
    """Clear Plex playlist."""
    # Get the playlist
    plist = plugin.plex.playlist(playlist)
    # Get a list of all the tracks in the playlist
    tracks = plist.items()
    # Loop through each track
    for track in tracks:
        # Remove the track from the playlist
        plist.removeItems(track)


def plex_playlist_to_collection(plugin, playlist):
    """Convert a Plex playlist to a Plex collection."""
    try:
        plst = plugin.music.playlist(playlist)
        playlist_set = set(plst.items())
    except exceptions.NotFound:
        plugin._log.error("{} playlist not found", playlist)
        return
    try:
        col = plugin.music.collection(playlist)
        collection_set = set(col.items())
    except exceptions.NotFound:
        col = None
        collection_set = set()
    to_add = playlist_set - collection_set
    plugin._log.info("Adding {} tracks to {} collection", len(to_add), playlist)
    if col is None:
        plugin._log.info("{} collection will be created", playlist)
        plugin.music.createCollection(playlist, items=list(to_add))
    else:
        try:
            col.addItems(items=list(to_add))
        except exceptions.BadRequest as e:
            plugin._log.error(
                "Error adding items to {} collection. Error: {}",
                playlist,
                e,
            )


def plex_import_playlist(plugin, playlist, playlist_url=None, listenbrainz=False):
    """Import playlist into Plex."""
    if listenbrainz:
        try:
            from beetsplug.listenbrainz import ListenBrainzPlugin
        except ModuleNotFoundError:
            plugin._log.error("ListenBrainz plugin not installed")
            return
        try:
            lb = ListenBrainzPlugin()
        except Exception as e:
            plugin._log.error(
                "Unable to initialize ListenBrainz plugin. Error: {}", e
            )
            return
        # there are 2 playlists to be imported. 1. Weekly jams 2. Weekly exploration
        # get the weekly jams playlist
        plugin._log.info("Importing weekly jams playlist")
        weekly_jams = lb.get_weekly_jams()
        plugin._log.info("Importing {} songs from Weekly Jams", len(weekly_jams))
        plugin.add_songs_to_plex("Weekly Jams", weekly_jams, plugin.config["plexsync"]["manual_search"].get(bool))

        plugin._log.info("Importing weekly exploration playlist")
        weekly_exploration = lb.get_weekly_exploration()
        plugin._log.info(
            "Importing {} songs from Weekly Exploration", len(weekly_exploration)
        )
        plugin.add_songs_to_plex("Weekly Exploration", weekly_exploration, plugin.config["plexsync"]["manual_search"].get(bool))
    else:
        if playlist_url is None or (
            "http://" not in playlist_url and "https://" not in playlist_url
        ):
            raise ui.UserError("Playlist URL not provided")
        if "apple" in playlist_url:
            songs = plugin.import_apple_playlist(playlist_url)
        elif "jiosaavn" in playlist_url:
            songs = plugin.import_jiosaavn_playlist(playlist_url)
        elif "gaana.com" in playlist_url:
            songs = plugin.import_gaana_playlist(playlist_url)
        elif "spotify" in playlist_url:
            songs = plugin.import_spotify_playlist(plugin.get_playlist_id(playlist_url))
        elif "youtube" in playlist_url:
            songs = plugin.import_yt_playlist(playlist_url)
        elif "tidal" in playlist_url:
            songs = plugin.import_tidal_playlist(playlist_url)
        else:
            songs = []
            plugin._log.error("Playlist URL not supported")
        plugin._log.info("Importing {} songs from {}", len(songs), playlist_url)
        plugin.add_songs_to_plex(playlist, songs, plugin.config["plexsync"]["manual_search"].get(bool))


def plex_import_search(plugin, playlist, search, limit=10):
    """Import search results into Plex."""
    plugin._log.info("Searching for {}", search)
    songs = plugin.import_yt_search(search, limit)
    song_list = []
    if songs:
        for song in songs:
            found = plugin.search_plex_song(song)
            if found is not None:
                song_list.append(found)
    plugin.plex_add_playlist_item(song_list, playlist)


def plex2spotify(self, lib, playlist):
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
    """Add tracks to a Spotify playlist."""
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


def get_playlist_id(self, url):
    """Extract playlist ID from URL."""
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


def authenticate_spotify(self):
    """Authenticate with Spotify API."""
    ID = self.config["spotify"]["client_id"].get()
    SECRET = self.config["spotify"]["client_secret"].get()
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
                self.plex_add_playlist_item(matched_tracks, playlist_name)
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
