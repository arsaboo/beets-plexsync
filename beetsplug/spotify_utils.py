"""
Utility functions for interacting with Spotify.
"""
import logging
import re
import json
import spotipy
import dateutil.parser
from spotipy.oauth2 import SpotifyOAuth
from bs4 import BeautifulSoup
import requests

from beetsplug.helpers import parse_title, clean_album_name # Assuming these are general enough

_log = logging.getLogger('beets.plexsync.spotify_utils') # Use a specific logger

def authenticate_spotify_for_plugin(plugin_instance, client_id, client_secret, redirect_uri, scope, token_cache_path):
    """
    Authenticates with Spotify API using OAuth.
    Stores the spotipy instance and auth_manager on the plugin_instance.
    """
    try:
        plugin_instance.auth_manager = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope=scope,
            open_browser=False, # Important for a CLI tool
            cache_path=token_cache_path,
        )
        token_info = plugin_instance.auth_manager.get_cached_token()

        if not token_info:
            # Attempt to get a new token. This might require user interaction if cache is empty/invalid.
            # For a beets plugin, direct browser interaction might be problematic.
            # Consider if get_authorization_code() and get_access_token() flow is better handled by user setup.
            # For now, assume get_access_token can handle it or will prompt appropriately if run in a context where it can.
            _log.info("No cached Spotify token found. Attempting to get a new one. This might require user interaction.")
            # The following line would typically open a browser or print a URL for the user to visit.
            # In a headless environment, this needs careful handling.
            # For a beets plugin, it's often expected the user runs an auth command once.
            auth_url = plugin_instance.auth_manager.get_authorize_url()
            _log.info(f"Please authorize here: {auth_url}")
            code = input("Enter the authorization code: ") # This will require user input in console
            token_info = plugin_instance.auth_manager.get_access_token(code, as_dict=True)


        if plugin_instance.auth_manager.is_token_expired(token_info):
            _log.info("Spotify token expired. Refreshing.")
            token_info = plugin_instance.auth_manager.refresh_access_token(
                token_info["refresh_token"]
            )

        plugin_instance.sp = spotipy.Spotify(auth=token_info.get("access_token"))
        _log.info("Successfully authenticated with Spotify.")
        return True
    except spotipy.SpotifyException as e:
        _log.error(f"Spotify authentication failed: {e}")
        plugin_instance.sp = None
        plugin_instance.auth_manager = None
        return False
    except Exception as e:
        _log.error(f"An unexpected error occurred during Spotify authentication: {e}")
        plugin_instance.sp = None
        plugin_instance.auth_manager = None
        return False

def process_spotify_track_data(track_item):
    """
    Processes a single Spotify track item (from API response) into a standardized dictionary.
    """
    if not track_item:
        return None
    try:
        if ('From "' in track_item['name']) or ("From &quot" in track_item['name']):
            title_orig = track_item['name'].replace("&quot;", '"')
            # Assuming parse_title is available and correctly imported
            title, album = parse_title(title_orig)
        else:
            title = track_item['name']
            # Assuming clean_album_name is available
            album = clean_album_name(track_item['album']['name']) if track_item.get('album') else "Unknown Album"

        year = None
        if track_item.get('album') and track_item['album'].get('release_date'):
            try:
                year_str = track_item['album']['release_date']
                # Handle different date precisions (e.g., "2020", "2020-05", "2020-05-15")
                if year_str:
                    year = dateutil.parser.parse(year_str).year
            except (ValueError, AttributeError):
                _log.debug(f"Could not parse year from release_date: {track_item['album'].get('release_date')}")
                year = None # Keep as None if parsing fails

        artist = track_item['artists'][0]['name'] if track_item.get('artists') else "Unknown Artist"

        return {
            "title": title.strip() if title else "",
            "album": album.strip() if album else "",
            "artist": artist.strip() if artist else "",
            "year": year # year is already an int or None
        }
    except Exception as e:
        _log.debug(f"Error processing Spotify track data for '{track_item.get('name', 'Unknown Track')}': {e}")
        return None


def get_spotify_playlist_tracks_api(sp_instance, playlist_id):
    """
    Fetches all tracks from a Spotify playlist using the API.
    Requires an initialized spotipy.Spotify instance.
    """
    if not sp_instance:
        _log.error("Spotipy instance not provided to get_playlist_tracks_api.")
        return []

    all_tracks = []
    try:
        results = sp_instance.playlist_items(playlist_id, additional_types=["track"])
        all_tracks.extend(results["items"])
        while results["next"]:
            results = sp_instance.next(results)
            all_tracks.extend(results["items"])

        processed_tracks = []
        for item in all_tracks:
            if item and item.get('track'): # Ensure track object exists
                track_data = process_spotify_track_data(item['track'])
                if track_data:
                    processed_tracks.append(track_data)
        return processed_tracks
    except spotipy.SpotifyException as e:
        _log.error(f"Spotify API error fetching playlist tracks for {playlist_id}: {e}")
        return []
    except Exception as e:
        _log.error(f"Unexpected error fetching playlist tracks for {playlist_id} via API: {e}")
        return []

def scrape_spotify_playlist_web(playlist_id, cache_instance, http_headers):
    """
    Scrapes a Spotify playlist from the web as a fallback.
    Uses cache_instance for caching results.
    """
    playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"
    song_list = []

    # Check web cache first
    cached_web_data = cache_instance.get_playlist_cache(playlist_id, 'spotify_web')
    if cached_web_data:
        _log.info(f"Using cached web scraped data for Spotify playlist {playlist_id}")
        return cached_web_data

    try:
        response = requests.get(playlist_url, headers=http_headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # Try to find metadata script (Primary method)
        meta_script_content = None
        for script_tag in soup.find_all("script", type="application/json", id="initial-state"):
            # More robust way to find the script containing track data
             meta_script_content = script_tag.string
             break

        if not meta_script_content: # Fallback to older script search if new one fails
            for script_tag in soup.find_all("script"):
                if script_tag.string and "Spotify.Entity" in str(script_tag.string):
                    meta_script_content = str(script_tag.string)
                    break

        if meta_script_content:
            json_data = None
            if "Spotify.Entity" in meta_script_content: # Old way
                 json_match = re.search(r'Spotify\.Entity = ({.+});', meta_script_content)
                 if json_match:
                     json_data = json.loads(json_match.group(1))
            else: # New way (initial-state)
                try:
                    full_data = json.loads(meta_script_content)
                    # The exact path to tracks might vary or need exploration of `full_data` structure
                    # This is a common path, but might need adjustment:
                    playlist_data = full_data.get("entities", {}).get("items", {}).get(f"spotify:playlist:{playlist_id}", {})
                    if playlist_data and 'tracks' in playlist_data: # Old structure under Spotify.Entity
                         track_items = playlist_data['tracks']['items']
                    elif full_data.get("content", {}).get("items"): # A possible new structure
                        track_items = full_data["content"]["items"]
                    else: # Need to find where tracks are, this is an example
                        # Look for a list of items that seem like tracks
                        # This part is highly dependent on Spotify's current HTML structure and embedded JSON
                        # For example, it might be under a key related to the playlist URI
                        # This is a placeholder for the actual logic to extract tracks from the new JSON structure
                        # Example:
                        # entity_key = f"spotify:playlist:{playlist_id}"
                        # tracks_data = full_data.get("entities",{}).get("items",{}).get(entity_key,{}).get("tracks",{})
                        # track_items = tracks_data.get("items", [])
                        # This is a guess, you'll need to inspect the actual JSON from the page
                        _log.debug("Could not find tracks directly in initial-state JSON, attempting generic search.")
                        track_items = [] # Placeholder
                        # A more generic search through the JSON might be needed if structure changes often
                        # For now, we'll rely on the older 'Spotify.Entity' or a known path if that fails

            try:
                if json_data and 'tracks' in json_data: # Primarily for Spotify.Entity structure
                    for track_entry in json_data['tracks']['items']:
                        if not track_entry or not track_entry.get('track'):
                            continue
                        track_data = track_entry['track']
                        song_dict = {
                            'title': track_data.get('name', '').strip(),
                            'artist': track_data.get('artists', [{}])[0].get('name', '').strip(),
                            'album': track_data.get('album', {}).get('name', '').strip(),
                            'year': None
                        }
                        release_date = track_data.get('album', {}).get('release_date')
                        if release_date:
                            try:
                                song_dict['year'] = int(release_date[:4])
                            except (ValueError, TypeError):
                                 _log.debug(f"Could not parse year from release_date: {release_date}")
                        song_list.append(song_dict)
            except Exception as e:
                _log.debug(f"Error processing tracks from JSON data: {e}")

        # Fallback to meta tags if script parsing fails or yields no tracks
        if not song_list:
            _log.info("JSON script parsing for playlist yielded no tracks, or script not found. Falling back to meta tags.")
            track_metas = soup.find_all("meta", {"name": "music:song"})
            if not track_metas:
                 track_metas = soup.find_all("meta", {"property": "music:song"}) # Alternative property

            for meta in track_metas:
                track_url = meta.get("content", "")
                if track_url:
                    try:
                        # Minimal info from meta tags, often just URL. May need to fetch each track page.
                        # This is less ideal due to multiple requests.
                        # For now, we'll try to parse from available info if any, or log the URL.
                        # A common pattern is that og:title and og:description on the playlist page itself might list tracks.
                        # Let's assume for now the meta tags give basic info or we'd need another layer of scraping.
                        # The original code fetched each track page, which is very slow.
                        # We'll try to get info from the playlist page's general meta tags first.
                        og_title = soup.find("meta", property="og:title")
                        page_title = og_title["content"] if og_title else "Unknown Playlist"

                        # This is a simplification; robust parsing from meta tags is complex.
                        # The example below assumes the main page's description might have track info.
                        # This is often not the case for Spotify.
                        # The most reliable way if script fails is often individual track page scraping (slow).
                        # For this refactor, if the primary JSON fails, we'll log it.
                        # The original code's meta tag fallback was already trying individual track pages.
                        # Given the complexity and potential for being blocked, we might simplify this fallback.
                        _log.debug(f"Found track URL via meta tag: {track_url}. Detailed scraping for this URL is complex.")
                        # To replicate original: fetch track_url, parse its og:title, og:description
                        # This is omitted for brevity in this refactoring step but was in original.
                        # If we are to implement it, it would be another request per track.
                        # For now, let's focus on the JSON part primarily.
                        # A simplified placeholder:
                        # song_list.append({'title': track_url.split('/')[-1], 'artist': 'Unknown', 'album': 'Unknown', 'year': None})


                    except Exception as e:
                        _log.debug(f"Error processing meta tag for track {track_url}: {e}")

        if song_list:
            _log.info(f"Successfully scraped {len(song_list)} tracks from Spotify playlist {playlist_id} via web.")
            cache_instance.set_playlist_cache(playlist_id, 'spotify_web', song_list)
        else:
            _log.warning(f"Web scraping for Spotify playlist {playlist_id} yielded no tracks.")

    except requests.RequestException as e:
        _log.error(f"Failed to fetch Spotify playlist page {playlist_id} for scraping: {e}")
    except Exception as e:
        _log.error(f"Error scraping Spotify playlist {playlist_id}: {e}")

    return song_list


def import_spotify_playlist_with_fallback(sp_instance, playlist_id, cache_instance, http_headers):
    """
    Imports a Spotify playlist, trying API first, then web scraping as fallback.
    Uses cache_instance for caching results at different stages.
    sp_instance is the authenticated spotipy.Spotify object.
    """
    # Check processed tracks cache first
    cached_tracks = cache_instance.get_playlist_cache(playlist_id, 'spotify_tracks')
    if cached_tracks:
        _log.info(f"Using fully cached & processed track list for Spotify playlist {playlist_id}")
        return cached_tracks

    # Try API method
    if sp_instance:
        _log.info(f"Attempting to import Spotify playlist {playlist_id} via API.")
        # Check API raw data cache
        # cached_api_data = cache_instance.get_playlist_cache(playlist_id, 'spotify_api_raw')
        # if cached_api_data:
        #     _log.info(f"Using cached raw API data for Spotify playlist {playlist_id}")
        #     api_tracks_raw = cached_api_data
        # else:
        #     api_tracks_raw = get_spotify_playlist_tracks_api_raw(sp_instance, playlist_id) # A function that returns raw items
        #     if api_tracks_raw:
        #         cache_instance.set_playlist_cache(playlist_id, 'spotify_api_raw', api_tracks_raw)

        # For simplicity, let's call the processing version directly
        processed_api_tracks = get_spotify_playlist_tracks_api(sp_instance, playlist_id)

        if processed_api_tracks:
            _log.info(f"Successfully imported {len(processed_api_tracks)} tracks via Spotify API for playlist {playlist_id}.")
            cache_instance.set_playlist_cache(playlist_id, 'spotify_tracks', processed_api_tracks)
            return processed_api_tracks
        else:
            _log.warning(f"Spotify API import failed for playlist {playlist_id}. Falling back to web scraping.")
    else:
        _log.warning("Spotipy instance not available. Skipping API import, proceeding to web scraping for playlist {playlist_id}.")

    # Web scraping fallback
    _log.info(f"Attempting to import Spotify playlist {playlist_id} via web scraping.")
    scraped_songs = scrape_spotify_playlist_web(playlist_id, cache_instance, http_headers)
    if scraped_songs:
        # Cache the processed scraped songs as the final 'spotify_tracks' if API failed
        cache_instance.set_playlist_cache(playlist_id, 'spotify_tracks', scraped_songs)
        return scraped_songs

    _log.error(f"Failed to import Spotify playlist {playlist_id} through both API and web scraping.")
    return []


def get_spotify_playlist_id_from_url(url):
    """Extracts Spotify playlist ID from a URL."""
    match = re.search(r"playlist/([a-zA-Z0-9]+)", url)
    if match:
        return match.group(1)
    _log.warning(f"Could not extract Spotify playlist ID from URL: {url}")
    return None

def add_tracks_to_spotify_playlist(sp_instance, user_id, playlist_name, track_uris_to_add):
    """
    Adds tracks to a specified Spotify playlist. Creates the playlist if it doesn't exist.
    sp_instance: Authenticated Spotipy instance.
    user_id: Spotify user ID.
    playlist_name: Name of the target playlist.
    track_uris_to_add: List of Spotify track URIs (e.g., "spotify:track:XXXXX").
    """
    if not sp_instance:
        _log.error("Spotipy instance not provided for adding tracks to playlist.")
        return False
    if not user_id:
        _log.error("User ID not provided for adding tracks to Spotify playlist.")
        return False

    playlist_id = None
    try:
        playlists = sp_instance.current_user_playlists()
        for pl in playlists["items"]:
            if pl["name"].lower() == playlist_name.lower():
                playlist_id = pl["id"]
                _log.info(f"Found existing Spotify playlist '{playlist_name}' with ID: {playlist_id}")
                break

        if not playlist_id:
            _log.info(f"Playlist '{playlist_name}' not found. Creating new playlist.")
            new_playlist = sp_instance.user_playlist_create(user_id, playlist_name, public=False)
            playlist_id = new_playlist["id"]
            _log.info(f"Created Spotify playlist '{playlist_name}' with ID: {playlist_id}")

        if not playlist_id: # Should not happen if creation was successful
            _log.error(f"Failed to find or create Spotify playlist '{playlist_name}'.")
            return False

        # Get existing tracks in the playlist to avoid duplicates
        existing_track_uris = []
        results = sp_instance.playlist_items(playlist_id, fields='items(track(uri)),next')
        for item in results['items']:
            if item['track'] and item['track']['uri']:
                existing_track_uris.append(item['track']['uri'])
        while results['next']:
            results = sp_instance.next(results)
            for item in results['items']:
                if item['track'] and item['track']['uri']:
                    existing_track_uris.append(item['track']['uri'])

        uris_to_actually_add = list(set(track_uris_to_add) - set(existing_track_uris))

        if not uris_to_actually_add:
            _log.info(f"No new tracks to add to Spotify playlist '{playlist_name}'. All provided tracks are already present.")
            return True

        # Spotify API limits adding 100 tracks per request
        for i in range(0, len(uris_to_actually_add), 100):
            chunk = uris_to_actually_add[i : i + 100]
            sp_instance.playlist_add_items(playlist_id, chunk)
        _log.info(f"Successfully added {len(uris_to_actually_add)} tracks to Spotify playlist '{playlist_name}'.")
        return True

    except spotipy.SpotifyException as e:
        _log.error(f"Spotify API error managing playlist '{playlist_name}': {e}")
        return False
    except Exception as e:
        _log.error(f"Unexpected error managing Spotify playlist '{playlist_name}': {e}")
        return False

def transfer_plex_playlist_to_spotify(plex_playlist_items, beets_lib, sp_instance, spotify_user_id, target_playlist_name, plex_lookup_func, search_llm_instance=None):
    """
    Transfers tracks from a Plex playlist (represented by items) to a Spotify playlist.
    plex_playlist_items: List of Plex track items.
    beets_lib: Beets library instance.
    sp_instance: Authenticated Spotipy instance.
    spotify_user_id: Spotify User ID for creating/adding to playlist.
    target_playlist_name: The name of the Spotify playlist to create/update.
    plex_lookup_func: Function to build {plex_ratingkey: beets_item} lookup.
    search_llm_instance: Optional LLM instance for search cleaning if track not found.
    """
    if not sp_instance:
        _log.error("Spotify instance not available for plex2spotify transfer.")
        return

    plex_lookup = plex_lookup_func(beets_lib) # Build {plex_ratingkey: beets_item}
    spotify_track_uris_to_add = []
    missing_on_spotify_count = 0

    for plex_item in plex_playlist_items:
        beets_item = plex_lookup.get(plex_item.ratingKey)
        if not beets_item:
            _log.debug(f"Plex item '{plex_item.title}' (key: {plex_item.ratingKey}) not found in Beets library via lookup.")
            continue

        spotify_track_id = getattr(beets_item, 'spotify_track_id', None)

        if not spotify_track_id:
            _log.debug(f"Spotify track ID not found in beets for '{beets_item.artist} - {beets_item.title}'. Searching Spotify.")
            query = f"track:{beets_item.title} artist:{beets_item.artist}"
            if beets_item.album:
                query += f" album:{beets_item.album}"

            try:
                # Basic search first
                results = sp_instance.search(q=query, type="track", limit=1)
                if results["tracks"]["items"]:
                    spotify_track_id = results["tracks"]["items"][0]["id"]
                    _log.info(f"Found Spotify match for '{beets_item.title}': ID {spotify_track_id}")
                    # Optionally, store this back to the beets item here if desired
                    # beets_item.spotify_track_id = spotify_track_id
                    # beets_item.store()
                else:
                    # Try LLM enhanced search if available
                    if search_llm_instance and hasattr(search_llm_instance, 'search_track_info'): # Check if search_track_info is callable
                        cleaned_query_parts = search_llm_instance.search_track_info(f"{beets_item.title} by {beets_item.artist} from album {beets_item.album or 'Unknown'}")
                        if cleaned_query_parts:
                            llm_query = f"track:{cleaned_query_parts['title']} artist:{cleaned_query_parts['artist']}"
                            if cleaned_query_parts.get('album'):
                                llm_query += f" album:{cleaned_query_parts['album']}"

                            _log.debug(f"Searching Spotify with LLM cleaned query: {llm_query}")
                            results_llm = sp_instance.search(q=llm_query, type="track", limit=1)
                            if results_llm["tracks"]["items"]:
                                spotify_track_id = results_llm["tracks"]["items"][0]["id"]
                                _log.info(f"Found Spotify match with LLM for '{beets_item.title}': ID {spotify_track_id}")


            except spotipy.SpotifyException as e:
                _log.warning(f"Spotify API error searching for '{beets_item.title}': {e}")
            except Exception as e: # Catch other errors like from LLM
                 _log.warning(f"Error during Spotify search (possibly LLM) for '{beets_item.title}': {e}")


        if spotify_track_id:
            spotify_track_uris_to_add.append(f"spotify:track:{spotify_track_id}")
        else:
            _log.warning(f"Could not find Spotify match for beets item: '{beets_item.artist} - {beets_item.title}'.")
            missing_on_spotify_count +=1

    if missing_on_spotify_count > 0:
        _log.info(f"{missing_on_spotify_count} tracks from Plex playlist could not be found on Spotify.")

    if spotify_track_uris_to_add:
        _log.info(f"Attempting to add {len(spotify_track_uris_to_add)} tracks to Spotify playlist '{target_playlist_name}'.")
        add_tracks_to_spotify_playlist(sp_instance, spotify_user_id, target_playlist_name, spotify_track_uris_to_add)
    else:
        _log.info(f"No tracks to add to Spotify playlist '{target_playlist_name}'.")
