import logging
import time
from concurrent.futures import ThreadPoolExecutor

from plexapi.server import PlexServer
from plexapi import exceptions
from beets import config, ui
from beets.library import Item
from requests.exceptions import ConnectionError, ContentDecodingError

from beetsplug.matching import plex_track_distance
from beetsplug.llm import search_track_info

log = logging.getLogger('beets.plexsync.plex_tool')

class PlexTool:
    def __init__(self, cache):
        self.cache = cache
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

    def update_library(self):
        """Update Plex music library."""
        try:
            self.music.update()
            log.info("Update started.")
        except exceptions.PlexApiException:
            log.warning("{} Update failed", self.config["plex"]["library_name"])

    def fetch_plex_info(self, items, write, force):
        """Obtain track information from Plex."""
        items_len = len(items)
        with ThreadPoolExecutor() as executor:
            for index, item in enumerate(items, start=1):
                executor.submit(
                    self._process_item, index, item, write, force, items_len
                )

    def _process_item(self, index, item, write, force, items_len):
        log.info("Processing {}/{} tracks - {} ", index, items_len, item)
        if not force and "plex_userrating" in item:
            log.debug("Plex rating already present for: {}", item)
            return
        plex_track = self.search_plex_track(item)
        if plex_track is None:
            log.info("No track found for: {}", item)
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
            log.debug("Track {} not found in Plex library", item)
            return None

    def sort_playlist(self, playlist_name, sort_field):
        """Sort a Plex playlist by a given field."""
        playlist = self.plex.playlist(playlist_name)
        items = playlist.items()
        sorted_items = sorted(
            items,
            key=lambda x: (
                getattr(x, sort_field).timestamp()
                if getattr(x, sort_field) is not None
                else 0
            ),
            reverse=True,
        )
        playlist.removeItems(items)
        for item in sorted_items:
            playlist.addItems(item)

    def add_to_playlist(self, items, playlist_name):
        """Add items to Plex playlist."""
        if not items:
            log.warning("No items to add to playlist {}", playlist_name)
            return

        plex_set = set()
        try:
            plst = self.plex.playlist(playlist_name)
            playlist_set = set(plst.items())
        except exceptions.NotFound:
            plst = None
            playlist_set = set()
        for item in items:
            try:
                rating_key = getattr(item, 'plex_ratingkey', None) or getattr(item, 'ratingKey', None)
                if rating_key:
                    plex_set.add(self.plex.fetchItem(rating_key))
                else:
                    log.warning("{} does not have plex_ratingkey or ratingKey attribute. Item details: {}", item, vars(item))
            except (exceptions.NotFound, AttributeError) as e:
                log.warning("{} not found in Plex library. Error: {}", item, e)
                continue
        to_add = plex_set - playlist_set
        log.info("Adding {} tracks to {} playlist", len(to_add), playlist_name)
        if plst is None:
            log.info("{} playlist will be created", playlist_name)
            self.plex.createPlaylist(playlist_name, items=list(to_add))
        else:
            try:
                plst.addItems(items=list(to_add))
            except exceptions.BadRequest as e:
                log.error(
                    "Error adding items {} to {} playlist. Error: {}",
                    items,
                    playlist_name,
                    e,
                )
        self.sort_playlist(playlist_name, "lastViewedAt")

    def playlist_to_collection(self, playlist_name):
        """Convert a Plex playlist to a Plex collection."""
        try:
            plst = self.music.playlist(playlist_name)
            playlist_set = set(plst.items())
        except exceptions.NotFound:
            log.error("{} playlist not found", playlist_name)
            return
        try:
            col = self.music.collection(playlist_name)
            collection_set = set(col.items())
        except exceptions.NotFound:
            col = None
            collection_set = set()
        to_add = playlist_set - collection_set
        log.info("Adding {} tracks to {} collection", len(to_add), playlist_name)
        if col is None:
            log.info("{} collection will be created", playlist_name)
            self.music.createCollection(playlist_name, items=list(to_add))
        else:
            try:
                col.addItems(items=list(to_add))
            except exceptions.BadRequest as e:
                log.error(
                    "Error adding items {} to {} collection. Error: {}",
                    items,
                    playlist_name,
                    e,
                )

    def remove_from_playlist(self, items, playlist_name):
        """Remove items from Plex playlist."""
        plex_set = set()
        try:
            plst = self.plex.playlist(playlist_name)
            playlist_set = set(plst.items())
        except exceptions.NotFound:
            log.error("{} playlist not found", playlist_name)
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
                log.warning("{} not found in Plex library. Error: {}", item, e)
                continue
        to_remove = plex_set.intersection(playlist_set)
        log.info("Removing {} tracks from {} playlist", len(to_remove), playlist_name)
        plst.removeItems(items=list(to_remove))

    def update_recently_played(self, lib, days=7):
        """Update recently played track info using plex_lookup."""
        tracks = self.music.search(
            filters={"track.lastViewedAt>>": f"{days}d"}, libtype="track"
        )
        log.info("Updating information for {} tracks", len(tracks))

        plex_lookup = self.build_plex_lookup(lib)

        with lib.transaction():
            for track in tracks:
                beets_item = plex_lookup.get(track.ratingKey)
                if not beets_item:
                    log.debug("Track {} not found in beets", track.ratingKey)
                    continue

                log.info("Updating information for {}", beets_item)
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
                    log.debug("Track not found in Plex: {}", beets_item)
                    continue

    def build_plex_lookup(self, lib):
        """Build a lookup dictionary mapping Plex rating keys to beets items."""
        log.debug("Building lookup dictionary for Plex rating keys")
        plex_lookup = {}
        for item in lib.items():
            if hasattr(item, "plex_ratingkey"):
                plex_lookup[item.plex_ratingkey] = item
        return plex_lookup

    def find_closest_match(self, song, tracks):
        """Find best matching tracks using string similarity with dynamic weights."""
        matches = []
        config = {
            'weights': {
                'title': 0.45,
                'artist': 0.35,
                'album': 0.20,
            }
        }
        temp_item = Item()
        temp_item.title = song.get('title', '').strip()
        temp_item.artist = song.get('artist', '').strip()
        temp_item.album = song.get('album', '').strip() if song.get('album') else ''

        for track in tracks:
            score, dist = plex_track_distance(temp_item, track, config)
            matches.append((track, score))
            log.debug("Track: {} - {}, Score: {:.3f}",
                          track.parentTitle, track.title, score)
        matches.sort(key=lambda x: x[1], reverse=True)
        return matches

    def search_song(self, song, manual_search=None, llm_attempted=False):
        """Fetch the Plex track key with fallback options."""
        if manual_search is None:
            manual_search = config["plexsync"]["manual_search"].get(bool)
        cache_key = self.cache._make_cache_key(song)
        cached_result = self.cache.get(cache_key)
        if cached_result is not None:
            if isinstance(cached_result, tuple):
                rating_key, cleaned_metadata = cached_result
                if rating_key == -1 or rating_key is None:
                    if cleaned_metadata and not llm_attempted:
                        log.debug("Using cached cleaned metadata: {}", cleaned_metadata)
                        result = self.search_song(cleaned_metadata, manual_search, llm_attempted=True)
                        if result is not None:
                            log.debug("Cached cleaned metadata search succeeded, updating original cache: {}", song)
                            self.cache.set(cache_key, result.ratingKey)
                        return result
                    return None
                try:
                    if rating_key:
                        return self.music.fetchItem(rating_key)
                except Exception as e:
                    log.debug("Failed to fetch cached item {}: {}", rating_key, e)
            else:
                if cached_result == -1 or cached_result is None:
                    return None
                try:
                    if cached_result:
                        return self.music.fetchItem(cached_result)
                except Exception as e:
                    log.debug("Failed to fetch cached item {}: {}", cached_result, e)
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
                    song["title"] = clean_string(song["title"])
                    tracks = self.music.searchTracks(**{"track.title": song["title"]}, limit=50)
            if song["title"] is None or song["title"] == "" and song["album"] and song["artist"]:
                tracks = self.music.searchTracks(
                    **{"album.title": song["album"], "artist.title": song["artist"]}, limit=50
                )
        except Exception as e:
            log.debug(
                "Error searching for {} - {}. Error: {}",
                song["album"],
                song["title"],
                e,
            )
            return None
        if len(tracks) == 1:
            result = tracks[0]
            self.cache.set(cache_key, result.ratingKey)
            return result
        elif len(tracks) > 1:
            sorted_tracks = self.find_closest_match(song, tracks)
            log.debug("Found {} tracks for {}", len(sorted_tracks), song["title"])
            if manual_search and len(sorted_tracks) > 0:
                return self._handle_manual_search(sorted_tracks, song, original_query=song)
            best_match = sorted_tracks[0]
            if best_match[1] >= 0.8:
                self.cache.set(cache_key, best_match[0].ratingKey)
                return best_match[0]
        if not llm_attempted and config["plexsync"]["use_llm_search"].get(bool):
            from beetsplug.tools.music_search import MusicSearch
            music_search = MusicSearch()
            search_query = f"{song['title']} by {song['artist']}"
            if song.get('album'):
                search_query += f" from {song['album']}"
            cleaned_metadata = music_search.run(search_query)
            if cleaned_metadata:
                cleaned_title = cleaned_metadata.get("title")
                cleaned_album = cleaned_metadata.get("album")
                cleaned_artist = cleaned_metadata.get("artist")
                cleaned_song = {
                    "title": cleaned_title if cleaned_title is not None else song["title"],
                    "album": cleaned_album if cleaned_album is not None else song.get("album"),
                    "artist": cleaned_artist if cleaned_artist is not None else song.get("artist")
                }
                log.debug("Using LLM cleaned metadata: {}", cleaned_song)
                self.cache.set(cache_key, (None, cleaned_song))
                result = self.search_song(cleaned_song, manual_search, llm_attempted=True)
                if result is not None:
                    log.debug("LLM-cleaned search succeeded, also caching for original query: {}", song)
                    self.cache.set(cache_key, result.ratingKey)
                return result
        if manual_search:
            log.info(
                "\nTrack {} - {} - {} not found in Plex".format(
                song.get("album", "Unknown"),
                song.get("artist", "Unknown"),
                song["title"])
            )
            if ui.input_yn(ui.colorize('text_highlight', "\nSearch manually?") + " (Y/n)"):
                result = self.manual_track_search(song)
                if result is not None:
                    log.debug("Manual search succeeded, caching for original query: {}", song)
                    self.cache.set(cache_key, result.ratingKey)
                return result
        self.cache.set(cache_key, -1)
        return None
