import logging
import re
import json
import spotipy
import dateutil.parser
import requests
from bs4 import BeautifulSoup
from spotipy.oauth2 import SpotifyOAuth
from beets import config
import confuse

from beetsplug.provider_apple import import_apple_playlist
from beetsplug.provider_gaana import import_gaana_playlist
from beetsplug.provider_jiosaavn import import_jiosaavn_playlist
from beetsplug.provider_m3u8 import import_m3u8_playlist
from beetsplug.provider_post import import_post_playlist
from beetsplug.provider_tidal import import_tidal_playlist
from beetsplug.provider_youtube import import_yt_playlist
from beetsplug.caching import Cache
from beetsplug.helpers import parse_title, clean_album_name

log = logging.getLogger('beets.plexsync.playlist_importer')

class PlaylistImporter:
    def __init__(self, cache: Cache):
        self.cache = cache
        self.sp = None
        self.auth_manager = None
        self.plexsync_token = config["plexsync"]["tokenfile"].get(
            confuse.Filename(in_app_dir=True)
        )
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 0.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "DNT": "1",  # Do Not Track Request Header
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
        }

    def run(self, url: str):
        if "apple" in url:
            return import_apple_playlist(url, self.cache)
        elif "jiosaavn" in url:
            return import_jiosaavn_playlist(url, self.cache)
        elif "gaana.com" in url:
            return import_gaana_playlist(url, self.cache)
        elif "spotify" in url:
            playlist_id = self.get_playlist_id(url)
            return self.import_spotify_playlist(playlist_id)
        elif "youtube" in url:
            return import_yt_playlist(url, self.cache)
        elif "tidal" in url:
            return import_tidal_playlist(url, self.cache)
        elif url.lower().endswith('.m3u8'):
            return import_m3u8_playlist(url, self.cache)
        else:
            log.error("Playlist URL not supported")
            return []

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
        try:            # Find and store the song title
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
            log.debug("Error processing Spotify track: {}", e)
            return None

    def import_spotify_playlist(self, playlist_id):
        """Import a Spotify playlist using API first, then fallback to scraping."""
        song_list = []

        # Check cache first
        cached_tracks = self.cache.get_playlist_cache(playlist_id, 'spotify_tracks')
        if (cached_tracks):
            log.info("Using cached track list for Spotify playlist {}", playlist_id)
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
                    log.info("Successfully imported {} tracks via Spotify API", len(song_list))
                    # Cache processed tracks
                    self.cache.set_playlist_cache(playlist_id, 'spotify_tracks', song_list)
                    return song_list

        except Exception as e:
            log.warning("Spotify API import failed: {}. Falling back to scraping.", e)

        # Web scraping fallback with caching
        cached_web_data = self.cache.get_playlist_cache(playlist_id, 'spotify_web')
        if cached_web_data:
            return cached_web_data

        try:
            playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"
            response = requests.get(playlist_url, headers=self.headers)
            if response.status_code != 200:
                log.error("Failed to fetch playlist page: {}", response.status_code)
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
                            log.debug("Error processing track {}: {}", track_url, e)

            if song_list:
                log.info("Successfully scraped {} tracks from Spotify playlist", len(song_list))
                self.cache.set_playlist_cache(playlist_id, 'spotify_web', song_list)
                return song_list

        except Exception as e:
            log.error("Error scraping Spotify playlist: {}", e)
            return song_list

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
            log.error("Failed to fetch playlist: {} - {}", playlist_id, str(e))
            return []
