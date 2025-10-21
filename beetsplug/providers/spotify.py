"""Spotify provider helpers extracted from plexsync.

These functions operate on the plugin instance to keep behavior identical.
They do not change cache key formats or returned structures.
"""

import os
import re
import json
from typing import Any, Dict, List, Optional
from collections import Counter

import dateutil.parser
import requests
from bs4 import BeautifulSoup
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.exceptions import SpotifyOauthError

from beets import config
from beetsplug.utils.helpers import parse_title, clean_album_name


def _clear_cached_token(plugin) -> None:
    """Remove any cached Spotify token file."""
    handler = getattr(plugin, "auth_manager", None)
    if handler is None:
        return

    cache_handler = handler.cache_handler
    if hasattr(cache_handler, "delete_cached_token"):
        cache_handler.delete_cached_token()
        return

    cache_path = getattr(cache_handler, "cache_path", None) or getattr(
        plugin, "plexsync_token", None
    )
    if not cache_path:
        return

    try:
        os.remove(cache_path)
        plugin._log.debug("Deleted Spotify cache file {}", cache_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        plugin._log.debug("Failed to delete Spotify cache file {}: {}", cache_path, exc)


def authenticate(plugin) -> None:
    """Authenticate Spotify, storing `sp` on the plugin identical to before."""
    ID = config["spotify"]["client_id"].get()
    SECRET = config["spotify"]["client_secret"].get()
    redirect_uri = "http://127.0.0.1/"
    scope = (
        "user-read-private user-read-email playlist-modify-public "
        "playlist-modify-private playlist-read-private"
    )

    plugin.auth_manager = SpotifyOAuth(
        client_id=ID,
        client_secret=SECRET,
        redirect_uri=redirect_uri,
        scope=scope,
        open_browser=False,
        cache_path=plugin.plexsync_token,
    )
    try:
        plugin.token_info = plugin.auth_manager.get_cached_token()
    except SpotifyOauthError as exc:
        plugin._log.debug("Failed to load cached Spotify token: {}", exc)
        _clear_cached_token(plugin)
        plugin.token_info = None

    if not plugin.token_info:
        plugin.token_info = plugin.auth_manager.get_access_token(as_dict=True)
    else:
        try:
            need_token = plugin.auth_manager.is_token_expired(plugin.token_info)
        except (SpotifyOauthError, KeyError, TypeError) as exc:
            plugin._log.debug("Cached Spotify token missing metadata: {}", exc)
            need_token = True

        if need_token:
            try:
                plugin.token_info = plugin.auth_manager.refresh_access_token(
                    plugin.token_info["refresh_token"]
                )
            except SpotifyOauthError as exc:
                message = str(exc).lower()
                if "invalid_grant" in message:
                    plugin._log.info(
                        "Spotify refresh token revoked; requesting new authorization."
                    )
                    _clear_cached_token(plugin)
                    plugin.token_info = plugin.auth_manager.get_access_token(
                        as_dict=True
                    )
                else:
                    raise

    plugin.sp = spotipy.Spotify(auth=plugin.token_info.get("access_token"))


def process_spotify_track(track: Dict[str, Any], logger) -> Optional[Dict[str, Any]]:
    """Process a single Spotify track into a standardized dict."""
    try:
        if ('From "' in track['name']) or ("From &quot" in track['name']):
            title_orig = track['name'].replace("&quot;", '"')
            title, album = parse_title(title_orig)
        else:
            title = track['name']
            album = clean_album_name(track['album']['name'])

        try:
            year = track['album'].get('release_date')
            if year:
                year = dateutil.parser.parse(year, ignoretz=True)
        except (ValueError, KeyError, AttributeError):
            year = None

        artist = track['artists'][0]['name'] if track['artists'] else "Unknown"

        return {
            "title": title.strip(),
            "album": album.strip(),
            "artist": artist.strip(),
            "year": year
        }
    except Exception as e:
        logger.debug("Error processing Spotify track: {}", e)
        return None


def get_playlist_id(url: str) -> str:
    parts = url.split("/")
    index = parts.index("playlist")
    return parts[index + 1]


def get_playlist_tracks(plugin, playlist_id: str) -> List[Dict[str, Any]]:
    """Return list of track items for a Spotify playlist (all pages)."""
    try:
        tracks_response = plugin.sp.playlist_items(
            playlist_id, additional_types=["track"]
        )
        tracks = tracks_response["items"]
        while tracks_response["next"]:
            tracks_response = plugin.sp.next(tracks_response)
            tracks.extend(tracks_response["items"])
        return tracks
    except spotipy.exceptions.SpotifyException as e:
        plugin._log.error("Failed to fetch playlist: {} - {}", playlist_id, str(e))
        return []


def import_spotify_playlist(plugin, playlist_id: str) -> List[Dict[str, Any]]:
    """Import a Spotify playlist using API first, then fallback to scraping."""
    song_list: List[Dict[str, Any]] = []

    cached_tracks = plugin.cache.get_playlist_cache(playlist_id, 'spotify_tracks')
    if cached_tracks:
        plugin._log.info("Using cached track list for Spotify playlist {}", playlist_id)
        return cached_tracks

    try:
        cached_api_data = plugin.cache.get_playlist_cache(playlist_id, 'spotify_api')
        if cached_api_data:
            songs = cached_api_data
        else:
            authenticate(plugin)
            songs = get_playlist_tracks(plugin, playlist_id)
            if songs:
                plugin.cache.set_playlist_cache(playlist_id, 'spotify_api', songs)

        if songs:
            for song in songs:
                track_data = process_spotify_track(song["track"], plugin._log)
                if track_data:
                    song_list.append(track_data)

            if song_list:
                plugin._log.info("Successfully imported {} tracks via Spotify API", len(song_list))
                plugin.cache.set_playlist_cache(playlist_id, 'spotify_tracks', song_list)
                return song_list

    except Exception as e:
        plugin._log.warning("Spotify API import failed: {}. Falling back to scraping.", e)

    cached_web_data = plugin.cache.get_playlist_cache(playlist_id, 'spotify_web')
    if cached_web_data:
        return cached_web_data

    try:
        playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"
        response = requests.get(playlist_url, headers=plugin.headers)
        if response.status_code != 200:
            plugin._log.error("Failed to fetch playlist page: {}", response.status_code)
            return song_list

        soup = BeautifulSoup(response.text, "html.parser")

        meta_script = None
        for script in soup.find_all("script"):
            if script.string and "Spotify.Entity" in str(script.string):
                meta_script = script
                break

        if meta_script:
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
                        try:
                            if 'release_date' in track_data.get('album', {}):
                                year = track_data['album']['release_date']
                                if year:
                                    year = dateutil.parser.parse(year, ignoretz=True)
                                    song_dict['year'] = year
                        except Exception:
                            pass
                        song_list.append(song_dict)
                else:
                    # Fallback: try to find track links
                    for link in soup.find_all('a', href=True):
                        href = link['href']
                        if '/track/' in href:
                            track_id = href.split('/track/')[-1].split('?')[0]
                            track_url = f"https://open.spotify.com/track/{track_id}"
                            try:
                                track_page = requests.get(track_url, headers=plugin.headers)
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
                                plugin._log.debug("Error processing track {}: {}", track_url, e)

        if song_list:
            plugin._log.info("Successfully scraped {} tracks from Spotify playlist", len(song_list))
            plugin.cache.set_playlist_cache(playlist_id, 'spotify_web', song_list)
            return song_list

    except Exception as e:
        plugin._log.error("Error scraping Spotify playlist: {}", e)
        return song_list

    return song_list


def _fuzzy_score(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def search_spotify_track(plugin, beets_item) -> Optional[str]:
    """Search for a track on Spotify with fallback strategies."""
    search_strategies = [
        lambda: f"track:{beets_item.title} album:{beets_item.album} artist:{beets_item.artist}",
        lambda: f"track:{beets_item.title} album:{beets_item.album}",
        lambda: f"track:{beets_item.title} artist:{beets_item.artist}",
        lambda: f'"{beets_item.title}" "{beets_item.artist}"',
        lambda: f"{beets_item.title} {beets_item.artist}",
    ]

    for i, strategy in enumerate(search_strategies, 1):
        try:
            query = strategy()
            plugin._log.debug("Spotify search strategy {}: {}", i, query)

            spotify_search_results = plugin.sp.search(
                q=query,
                limit=10,
                type="track",
            )

            if spotify_search_results["tracks"]["items"]:
                for track in spotify_search_results["tracks"]["items"]:
                    if track.get('is_playable', True):
                        track_title = track['name'].lower()
                        original_title = beets_item.title.lower()
                        track_artist = track['artists'][0]['name'].lower()
                        original_artist = beets_item.artist.lower()

                        title_match = (original_title in track_title or
                                       track_title in original_title or
                                       _fuzzy_score(original_title, track_title) > 0.6)
                        artist_match = (original_artist in track_artist or
                                        track_artist in original_artist or
                                        _fuzzy_score(original_artist, track_artist) > 0.6)

                        if title_match and artist_match:
                            plugin._log.debug("Found playable match: {} - {} (strategy {})",
                                              track['name'], track['artists'][0]['name'], i)
                            return track['id']
                        elif i >= 5:
                            if title_match or artist_match:
                                plugin._log.debug("Found loose match: {} - {} (strategy {})",
                                                  track['name'], track['artists'][0]['name'], i)
                                return track['id']

                plugin._log.debug("Found {} results but no good matches for strategy {}",
                                   len(spotify_search_results["tracks"]["items"]), i)
            else:
                plugin._log.debug("No results for strategy {}", i)

        except Exception as e:
            plugin._log.debug("Error in search strategy {}: {}", i, e)
            continue

    return None


def add_tracks_to_spotify_playlist(plugin, playlist_name: str, track_uris: List[str]) -> None:
    """Sync new tracks to top, keep existing order below.

    - Adds only the tracks missing from the current playlist at position 0
      while preserving their relative order.
    - Removes tracks that are no longer present in the target (optional cleanup).
    - Uses non-overlapping 100-size chunks to avoid duplication.
    """
    user_id = plugin.sp.current_user()["id"]
    playlists = plugin.sp.user_playlists(user_id)
    playlist_id = None
    for playlist in playlists["items"]:
        if playlist["name"].lower() == playlist_name.lower():
            playlist_id = playlist["id"]
            break
    if not playlist_id:
        playlist = plugin.sp.user_playlist_create(
            user_id, playlist_name, public=False
        )
        playlist_id = playlist["id"]
        plugin._log.debug(
            f"Playlist {playlist_name} created with id {playlist_id}"
        )

    # Normalize target IDs preserving order and duplicates
    target_track_ids: List[str] = [
        uri.replace("spotify:track:", "") if isinstance(uri, str) and uri.startswith("spotify:track:") else uri
        for uri in track_uris
        if uri
    ]

    # Fetch current playlist (ordered)
    playlist_tracks = get_playlist_tracks(plugin, playlist_id)
    current_track_ids: List[str] = [
        t["track"]["id"] for t in playlist_tracks if t.get("track") and t["track"].get("id")
    ]

    # Fast path: exact match (order and counts)
    if current_track_ids == target_track_ids:
        plugin._log.debug("Playlist is already in sync - no changes needed")
        return

    # Remove tracks that are not in target at all
    current_counts = Counter(current_track_ids)
    target_counts = Counter(target_track_ids)
    obsolete_ids = [tid for tid in current_counts.keys() if tid not in target_counts]
    if obsolete_ids:
        for i in range(0, len(obsolete_ids), 100):
            chunk = obsolete_ids[i:i+100]
            plugin.sp.user_playlist_remove_all_occurrences_of_tracks(
                user_id, playlist_id, chunk
            )
        plugin._log.debug(f"Removed {len(obsolete_ids)} obsolete tracks from playlist {playlist_id}")

    # Compute which tracks are missing (multiset difference), preserving order
    remaining_counts = Counter(
        {tid: min(current_counts.get(tid, 0), target_counts.get(tid, 0)) for tid in set(current_counts) | set(target_counts)}
    )
    new_track_ids: List[str] = []
    temp_counts = Counter(remaining_counts)
    for tid in target_track_ids:
        if temp_counts.get(tid, 0) > 0:
            temp_counts[tid] -= 1
        else:
            new_track_ids.append(tid)

    plugin._log.debug(
        f"Current={len(current_track_ids)} Target={len(target_track_ids)} New={len(new_track_ids)} Removed={len(obsolete_ids)}"
    )

    # Add new tracks at top, preserving their order via reverse chunking
    if new_track_ids:
        n = len(new_track_ids)
        idx = n
        while idx > 0:
            start = max(0, idx - 100)
            chunk = new_track_ids[start:idx]
            plugin.sp.user_playlist_add_tracks(user_id, playlist_id, chunk, position=0)
            idx -= 100
        plugin._log.debug(
            f"Added {len(new_track_ids)} new tracks to top of playlist {playlist_id}"
        )

    # Note: We are intentionally not reordering existing tracks to keep their
    # relative order and "Date added" intact, per requirement.
