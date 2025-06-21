"""
Utility functions for interacting with a Plex server.
"""
import time
import logging
from plexapi import exceptions

# Placeholder for PlexSync class methods that will be moved here
# We'll need to adjust how `self._log` and `self.plex` are accessed,
# possibly by passing them as arguments or instantiating/accessing them differently.

_log = logging.getLogger('beets.plexsync.plex_utils')

def plexupdate(plex_instance, library_name):
    """Update Plex music library."""
    try:
        music_library = plex_instance.library.section(library_name)
        music_library.update()
        _log.info("Plex library update started.")
    except exceptions.PlexApiException as e:
        _log.warning(f"Plex library '{library_name}' update failed: {e}")
    except Exception as e:
        _log.error(f"An unexpected error occurred during Plex library update: {e}")

def fetch_plex_info(plex_instance, music_library, items, write, force, process_item_func):
    """Obtain track information from Plex."""
    from concurrent.futures import ThreadPoolExecutor
    items_len = len(items)
    with ThreadPoolExecutor() as executor:
        for index, item in enumerate(items, start=1):
            executor.submit(
                process_item_func, plex_instance, music_library, index, item, write, force, items_len
            )

def process_item_for_plex_info(plex_instance, music_library, index, item, write, force, items_len, search_plex_track_func):
    """Helper function to process a single item for fetch_plex_info."""
    # This function is called by fetch_plex_info for each item.
    # It needs access to _log from the main PlexSync class or have its own logger.
    # For now, assume _log is accessible or passed.
    _log.info(f"Processing {index}/{items_len} tracks - {item}")
    if not force and "plex_userrating" in item:
        _log.debug(f"Plex rating already present for: {item}")
        return
    plex_track = search_plex_track_func(music_library, item) # Pass music_library
    if plex_track is None:
        _log.info(f"No track found for: {item}")
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

def search_plex_track(music_library, item):
    """Fetch the Plex track key from a given music library."""
    # This function needs access to _log from the main PlexSync class or have its own logger.
    try:
        tracks = music_library.searchTracks(
            **{"album.title": item.album, "track.title": item.title}
        )
        if not tracks: # Try artist and title if album search fails
            tracks = music_library.searchTracks(
                **{"artist.title": item.artist, "track.title": item.title}
            )

        if len(tracks) == 1:
            return tracks[0]
        elif len(tracks) > 1:
            # Prefer exact match on album and title
            for track in tracks:
                if track.parentTitle == item.album and track.title == item.title:
                    return track
            # Fallback: if album is not well defined, try artist and title
            for track in tracks:
                if track.grandparentTitle == item.artist and track.title == item.title: # grandparentTitle is often artist for tracks
                    return track
            _log.debug(f"Multiple tracks found for {item}, returning the first one as best guess.")
            return tracks[0] # As a last resort, return the first match.
        else:
            _log.debug(f"Track {item} not found in Plex library")
            return None
    except Exception as e:
        _log.error(f"Error searching Plex track for {item}: {e}")
        return None


def sort_plex_playlist(plex_instance, playlist_name, sort_field="lastViewedAt"):
    """Sort a Plex playlist by a given field."""
    try:
        playlist = plex_instance.playlist(playlist_name)
        if not playlist:
            _log.warning(f"Playlist '{playlist_name}' not found for sorting.")
            return

        items = playlist.items()
        if not items:
            _log.info(f"Playlist '{playlist_name}' is empty, no sorting needed.")
            return

        # Sort the items based on the sort_field
        # Handle cases where sort_field might be None
        sorted_items = sorted(
            items,
            key=lambda x: (
                getattr(x, sort_field).timestamp()
                if getattr(x, sort_field) is not None
                else 0 # Default for None or missing attribute
            ),
            reverse=True,  # Sort most recent first
        )

        # Remove all items from the playlist
        playlist.removeItems(items)
        # Add the sorted items back to the playlist
        playlist.addItems(sorted_items)
        _log.info(f"Playlist '{playlist_name}' sorted by '{sort_field}'.")
    except exceptions.NotFound:
        _log.error(f"Playlist '{playlist_name}' not found for sorting.")
    except Exception as e:
        _log.error(f"Error sorting playlist '{playlist_name}': {e}")


def plex_add_playlist_item(plex_instance, items, playlist_name, sort_playlist=True, sort_field="lastViewedAt"):
    """Add items to Plex playlist."""
    if not items:
        _log.warning(f"No items to add to playlist {playlist_name}")
        return

    plex_set = set()
    try:
        plst = plex_instance.playlist(playlist_name)
        playlist_set = set(plst.items()) if plst else set()
    except exceptions.NotFound:
        plst = None
        playlist_set = set()

    for item in items:
        try:
            # Check for both plex_ratingkey and ratingKey (for beets items vs plex items)
            rating_key_attr = getattr(item, 'plex_ratingkey', None) or getattr(item, 'ratingKey', None)
            if rating_key_attr:
                plex_item = plex_instance.fetchItem(rating_key_attr)
                if plex_item:
                    plex_set.add(plex_item)
                else:
                    _log.warning(f"Could not fetch item with key {rating_key_attr} from Plex.")
            else:
                _log.warning(f"Item {item} does not have plex_ratingkey or ratingKey attribute.")
        except (exceptions.NotFound, AttributeError) as e:
            _log.warning(f"Item {item} (or its key) not found in Plex library or attribute missing. Error: {e}")
            continue
        except Exception as e: # Catch any other Plex API errors
            _log.error(f"Error fetching item {item} from Plex: {e}")
            continue

    to_add = list(plex_set - playlist_set) # Convert to list for addItems
    if not to_add:
        _log.info(f"No new tracks to add to '{playlist_name}' playlist.")
        return

    _log.info(f"Adding {len(to_add)} tracks to '{playlist_name}' playlist.")
    if plst is None:
        _log.info(f"Playlist '{playlist_name}' will be created.")
        try:
            plst = plex_instance.createPlaylist(playlist_name, items=to_add)
        except Exception as e:
            _log.error(f"Failed to create playlist '{playlist_name}' and add items: {e}")
            return # Stop if playlist creation fails
    else:
        try:
            plst.addItems(items=to_add)
        except exceptions.BadRequest as e:
            _log.error(f"Error adding items to '{playlist_name}' playlist. Error: {e}")
        except Exception as e:
            _log.error(f"Unexpected error adding items to '{playlist_name}': {e}")

    if sort_playlist and plst: # Ensure plst is not None
        try:
            sort_plex_playlist(plex_instance, playlist_name, sort_field)
        except Exception as e:
            _log.error(f"Failed to sort playlist '{playlist_name}' after adding items: {e}")


def plex_playlist_to_collection(plex_instance, music_library, playlist_name):
    """Convert a Plex playlist to a Plex collection."""
    try:
        plst = plex_instance.playlist(playlist_name)
        if not plst:
            _log.error(f"Playlist '{playlist_name}' not found.")
            return
        playlist_items = set(plst.items())
    except exceptions.NotFound:
        _log.error(f"Playlist '{playlist_name}' not found.")
        return
    except Exception as e:
        _log.error(f"Error fetching playlist '{playlist_name}': {e}")
        return

    if not playlist_items:
        _log.info(f"Playlist '{playlist_name}' is empty. No collection will be created/updated.")
        return

    try:
        col = music_library.collection(playlist_name)
        collection_items = set(col.items()) if col else set()
    except exceptions.NotFound:
        col = None
        collection_items = set()
    except Exception as e:
        _log.error(f"Error fetching collection '{playlist_name}': {e}")
        return # Cannot proceed if fetching collection fails

    to_add = list(playlist_items - collection_items) # Convert to list for addItems/createCollection

    if not to_add:
        _log.info(f"No new items from playlist '{playlist_name}' to add to collection '{playlist_name}'.")
        return

    _log.info(f"Adding {len(to_add)} tracks to '{playlist_name}' collection.")
    if col is None:
        _log.info(f"Collection '{playlist_name}' will be created.")
        try:
            music_library.createCollection(playlist_name, items=to_add)
        except Exception as e:
            _log.error(f"Failed to create collection '{playlist_name}': {e}")
    else:
        try:
            col.addItems(items=to_add)
        except exceptions.BadRequest as e:
            _log.error(f"Error adding items to '{playlist_name}' collection. Error: {e}")
        except Exception as e:
            _log.error(f"Unexpected error adding items to collection '{playlist_name}': {e}")


def plex_remove_playlist_item(plex_instance, items, playlist_name):
    """Remove items from Plex playlist."""
    plex_set_to_remove = set()
    try:
        plst = plex_instance.playlist(playlist_name)
        if not plst:
            _log.error(f"Playlist '{playlist_name}' not found for removing items.")
            return
        playlist_items = set(plst.items())
    except exceptions.NotFound:
        _log.error(f"Playlist '{playlist_name}' not found.")
        return
    except Exception as e:
        _log.error(f"Error fetching playlist '{playlist_name}': {e}")
        return

    if not playlist_items:
        _log.info(f"Playlist '{playlist_name}' is already empty.")
        return

    for item in items: # These are typically beets items
        try:
            # We need the Plex item representation to remove it
            # Assuming item.plex_ratingkey holds the key
            rating_key = getattr(item, 'plex_ratingkey', None)
            if rating_key:
                plex_item_to_remove = plex_instance.fetchItem(rating_key)
                if plex_item_to_remove:
                    plex_set_to_remove.add(plex_item_to_remove)
            else:
                # If no rating key, try to find it by searching (less efficient)
                # This part might be slow and is a fallback.
                # Consider if direct key lookup is always possible for items passed here.
                _log.debug(f"Item {item} has no plex_ratingkey. Searching in Plex to find it for removal.")
                # search_results = search_plex_track(plex_instance.library.section("Music"), item) # Assuming music_library access
                # if search_results:
                #    plex_set_to_remove.add(search_results)
                # else:
                _log.warning(f"Could not find Plex item corresponding to {item} for removal without a rating key.")
        except (exceptions.NotFound, AttributeError):
            _log.warning(f"Item {item} (or its key) not found in Plex library for removal.")
            continue
        except Exception as e:
            _log.error(f"Error processing item {item} for removal: {e}")

    to_remove_actually_in_playlist = list(plex_set_to_remove.intersection(playlist_items))

    if not to_remove_actually_in_playlist:
        _log.info(f"No specified items found in playlist '{playlist_name}' to remove.")
        return

    _log.info(f"Removing {len(to_remove_actually_in_playlist)} tracks from '{playlist_name}' playlist.")
    try:
        plst.removeItems(items=to_remove_actually_in_playlist)
    except Exception as e:
        _log.error(f"Error removing items from playlist '{playlist_name}': {e}")


def update_recently_played(plex_instance, music_library, beets_lib, days=7, build_plex_lookup_func=None):
    """Update recently played track info using plex_lookup."""
    # This function needs access to _log from the main PlexSync class or have its own logger.
    # Also needs build_plex_lookup function.
    if build_plex_lookup_func is None:
        _log.error("build_plex_lookup_func not provided to update_recently_played.")
        return

    try:
        tracks = music_library.search(
            filters={"track.lastViewedAt>>": f"{days}d"}, libtype="track"
        )
        _log.info(f"Updating information for {len(tracks)} recently played tracks (last {days} days).")

        if not tracks:
            _log.info("No recently played tracks found to update.")
            return

        plex_lookup = build_plex_lookup_func(beets_lib)

        with beets_lib.transaction():
            updated_count = 0
            for track in tracks:
                beets_item = plex_lookup.get(track.ratingKey)
                if not beets_item:
                    _log.debug(f"Plex track with ratingKey {track.ratingKey} ({track.title}) not found in beets library via lookup.")
                    continue

                _log.info(f"Updating beets item: {beets_item} with info from Plex track: {track.title}")
                try:
                    changed = False
                    if getattr(beets_item, 'plex_userrating', None) != track.userRating:
                        beets_item.plex_userrating = track.userRating
                        changed = True
                    if getattr(beets_item, 'plex_skipcount', 0) != track.skipCount:
                        beets_item.plex_skipcount = track.skipCount
                        changed = True
                    if getattr(beets_item, 'plex_viewcount', 0) != track.viewCount:
                        beets_item.plex_viewcount = track.viewCount
                        changed = True

                    new_lastviewedat = track.lastViewedAt.timestamp() if track.lastViewedAt else None
                    if getattr(beets_item, 'plex_lastviewedat', None) != new_lastviewedat:
                         # Convert beets_item.plex_lastviewedat to timestamp if it's a datetime object
                        current_lastviewedat_ts = beets_item.plex_lastviewedat
                        if hasattr(current_lastviewedat_ts, 'timestamp'): # Check if it's a datetime object
                            current_lastviewedat_ts = current_lastviewedat_ts.timestamp()

                        if current_lastviewedat_ts != new_lastviewedat:
                            beets_item.plex_lastviewedat = new_lastviewedat
                            changed = True


                    new_lastratedat = track.lastRatedAt.timestamp() if track.lastRatedAt else None
                    if getattr(beets_item, 'plex_lastratedat', None) != new_lastratedat:
                        beets_item.plex_lastratedat = new_lastratedat
                        changed = True

                    if changed:
                        beets_item.plex_updated = time.time()
                        beets_item.store()
                        # item.try_write() # Consider if writing tags is desired here
                        updated_count +=1
                        _log.debug(f"Updated beets item: {beets_item}")
                    else:
                        _log.debug(f"No changes for beets item: {beets_item}")

                except exceptions.NotFound: # Should not happen if track is from plex search
                    _log.debug(f"Track not found in Plex during update: {beets_item}")
                    continue
                except Exception as e:
                    _log.error(f"Error updating beets item {beets_item} from Plex track {track.title}: {e}")
            _log.info(f"Finished updating recently played tracks. {updated_count} items updated in beets.")
    except Exception as e:
        _log.error(f"Error in update_recently_played: {e}")


def plex_clear_playlist(plex_instance, playlist_name):
    """Clear Plex playlist."""
    # This function needs access to _log from the main PlexSync class or have its own logger.
    try:
        plist = plex_instance.playlist(playlist_name)
        if not plist:
            _log.warning(f"Playlist '{playlist_name}' not found, cannot clear.")
            return

        tracks = plist.items()
        if not tracks:
            _log.info(f"Playlist '{playlist_name}' is already empty.")
            return

        _log.info(f"Clearing {len(tracks)} tracks from playlist '{playlist_name}'.")
        plist.removeItems(tracks) # removeItems can take a list of items
        _log.info(f"Playlist '{playlist_name}' cleared.")
    except exceptions.NotFound:
        _log.warning(f"Playlist '{playlist_name}' not found, cannot clear.")
    except Exception as e:
        _log.error(f"Error clearing playlist '{playlist_name}': {e}")


def plex_collage(plex_instance, music_library, config_dir, interval, grid, plex_most_played_albums_func, create_collage_func):
    """Create a collage of most played albums."""
    import os
    # This function needs access to _log, plex_most_played_albums, and create_collage
    # from the main PlexSync class or have them passed/defined.

    interval = int(interval)
    grid = int(grid)

    _log.info(f"Creating collage of most played albums in the last {interval} days with a {grid}x{grid} grid.")

    # Get recently played tracks to determine most played albums
    # This search might be broad; plex_most_played_albums_func will do the heavy lifting of counting.
    try:
        tracks = music_library.search(
            filters={"track.lastViewedAt>>": f"{interval}d"}, # Tracks viewed in the interval
            sort="viewCount:desc", # Sort by overall view count as a hint
            libtype="track",
        )
    except Exception as e:
        _log.error(f"Failed to search for recently played tracks for collage: {e}")
        return

    if not tracks:
        _log.warning("No tracks found in the specified interval for collage.")
        # return # Don't return yet, plex_most_played_albums_func might still find something if its logic is different

    sorted_albums = plex_most_played_albums_func(plex_instance, music_library, tracks, interval) # Pass plex_instance and music_library
    max_albums = grid * grid
    top_albums = sorted_albums[:max_albums]

    if not top_albums:
        _log.error("No albums found with play history in the specified time period for collage.")
        return

    album_art_urls = []
    for album in top_albums:
        # Ensure album object has thumbUrl and it's not None or empty
        if hasattr(album, 'thumbUrl') and album.thumbUrl:
            # Construct full URL if thumbUrl is relative
            art_url = album.thumbUrl
            if not art_url.startswith('http'):
                art_url = plex_instance.url(art_url, includeToken=True)
            album_art_urls.append(art_url)
            _log.debug(f"Added album art for: {album.title} (played {getattr(album, 'count', 'N/A')} times), URL: {art_url}")
        else:
            _log.debug(f"Album {album.title} has no thumbUrl.")


    if not album_art_urls:
        _log.error("No album artwork found for the top played albums.")
        return

    if len(album_art_urls) < max_albums:
        _log.warning(f"Found only {len(album_art_urls)} album arts, collage will have empty spots.")


    try:
        collage_image = create_collage_func(album_art_urls, grid, plex_instance) # Pass plex_instance for tokenized URLs
        output_path = os.path.join(config_dir, "collage.png") # Ensure config_dir is correct path
        collage_image.save(output_path, "PNG", quality=95)
        _log.info(f"Collage saved to: {output_path}")
    except Exception as e:
        _log.error(f"Failed to create or save collage: {e}")
        import traceback
        _log.error(traceback.format_exc())


def plex_most_played_albums(plex_instance, music_library, tracks_in_interval, interval_days):
    """
    Determines the most played albums in a given interval.
    Relies on track history for accurate play counts within the interval.
    """
    from datetime import datetime, timedelta
    # This function needs access to _log from the main PlexSync class or have its own logger.

    now = datetime.now()
    frm_dt = now - timedelta(days=interval_days)
    album_play_data = {} # Store data as {album_key: {'album_obj': obj, 'count': num, 'last_played_ts': timestamp}}

    _log.info(f"Calculating most played albums from {len(tracks_in_interval)} tracks in the last {interval_days} days.")

    processed_tracks = 0
    for track in tracks_in_interval:
        processed_tracks += 1
        if processed_tracks % 100 == 0:
            _log.debug(f"Processed {processed_tracks}/{len(tracks_in_interval)} tracks for album play counts...")
        try:
            # Fetch history for this specific track within the date range
            history = track.history(mindate=frm_dt)
            play_count_in_interval = len(history)

            if play_count_in_interval > 0:
                album = track.album() # Get the full album object
                if not album: # Should not happen if track has album
                    _log.debug(f"Track {track.title} has no album object, skipping.")
                    continue

                album_key = album.ratingKey # Use ratingKey to uniquely identify album

                if album_key not in album_play_data:
                    album_play_data[album_key] = {
                        "album_obj": album,
                        "count": 0,
                        "last_played_ts": 0 # Store as timestamp for easier sorting
                    }

                album_play_data[album_key]["count"] += play_count_in_interval

                # Determine the latest play timestamp for this album from its tracks in the interval
                current_album_last_play = album_play_data[album_key]["last_played_ts"]
                for play_event in history:
                    if play_event.viewedAt:
                        play_event_ts = play_event.viewedAt.timestamp()
                        if play_event_ts > current_album_last_play:
                            current_album_last_play = play_event_ts
                album_play_data[album_key]["last_played_ts"] = current_album_last_play

        except exceptions.NotFound:
            _log.debug(f"Track {track.title} (or its album/history) not found while processing for collage. Skipping.")
        except Exception as e:
            _log.debug(f"Error processing track history for '{track.title}': {e}")
            continue

    if not album_play_data:
        _log.info("No albums found with play counts in the interval.")
        return []

    # Convert to list of tuples for sorting: (album_obj, count, last_played_ts)
    albums_list_for_sorting = [
        (data["album_obj"], data["count"], data["last_played_ts"])
        for data in album_play_data.values() if data["count"] > 0 # Only include albums played in interval
    ]

    # Sort by count (descending), then by last played timestamp (descending)
    sorted_albums_tuples = sorted(
        albums_list_for_sorting,
        key=lambda x: (-x[1], -x[2]) # Negative for descending sort
    )

    result_albums = []
    for album_obj, count, last_played_ts in sorted_albums_tuples:
        album_obj.count = count # Attach count for logging/use in collage function
        album_obj.last_played_date = datetime.fromtimestamp(last_played_ts) if last_played_ts > 0 else None
        result_albums.append(album_obj)
        _log.info(
            f"Album: {album_obj.title}, Plays in interval: {count}, Last played: "
            f"{album_obj.last_played_date.strftime('%Y-%m-%d %H:%M:%S') if album_obj.last_played_date else 'N/A'}"
        )

    return result_albums


def create_collage_image(list_image_urls, dimension, plex_instance=None):
    """Create a square collage from a list of image urls.
       Accepts plex_instance to correctly fetch tokenized URLs if needed.
    """
    from io import BytesIO
    import requests # Make sure requests is imported
    from PIL import Image, ImageDraw, ImageFont # For placeholder text

    thumbnail_size = 300  # Size of each album art
    grid_size = thumbnail_size * dimension
    actual_images_to_place = min(len(list_image_urls), dimension * dimension)

    _log.info(f"Creating {dimension}x{dimension} collage with {actual_images_to_place} images.")

    # Create the base image
    grid_image = Image.new("RGB", (grid_size, grid_size), "black")
    draw = ImageDraw.Draw(grid_image)
    try:
        # Attempt to load a simple font for placeholder text
        font = ImageFont.truetype("arial.ttf", 18) # Adjust font and size as needed
    except IOError:
        font = ImageFont.load_default()
        _log.warning("Arial font not found, using default font for placeholders.")


    for index in range(dimension * dimension): # Iterate up to total grid cells
        x_pos = thumbnail_size * (index % dimension)
        y_pos = thumbnail_size * (index // dimension)

        if index < len(list_image_urls):
            url = list_image_urls[index]
            try:
                # If plex_instance is provided and URL is relative, make it absolute with token
                if plex_instance and not url.startswith('http'):
                    full_url = plex_instance.url(url, includeToken=True)
                else:
                    full_url = url

                _log.debug(f"Fetching image {index + 1}/{len(list_image_urls)}: {full_url}")
                response = requests.get(full_url, timeout=20, stream=True) # Increased timeout, stream
                response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

                img = Image.open(BytesIO(response.content))

                if img.mode != "RGB":
                    img = img.convert("RGB")

                # Resize maintaining aspect ratio, fitting within thumbnail_size box
                img.thumbnail((thumbnail_size, thumbnail_size), Image.Resampling.LANCZOS)

                # Calculate pasting position to center the image if it's smaller than thumbnail_size
                paste_x = x_pos + (thumbnail_size - img.width) // 2
                paste_y = y_pos + (thumbnail_size - img.height) // 2

                grid_image.paste(img, (paste_x, paste_y))
                img.close()

            except requests.exceptions.RequestException as e:
                _log.warning(f"Failed to fetch image {url}: {e}. Using placeholder.")
                draw.rectangle([x_pos, y_pos, x_pos + thumbnail_size, y_pos + thumbnail_size], fill="gray")
                draw.text((x_pos + 10, y_pos + 10), f"Error\n{url.split('/')[-1][:20]}...", fill="white", font=font)
            except IOError as e: # PIL Image processing errors
                _log.warning(f"Failed to process image {url}: {e}. Using placeholder.")
                draw.rectangle([x_pos, y_pos, x_pos + thumbnail_size, y_pos + thumbnail_size], fill="darkgray")
                draw.text((x_pos + 10, y_pos + 10), f"ProcessErr\n{url.split('/')[-1][:20]}...", fill="white", font=font)
            except Exception as e:
                _log.error(f"Unexpected error with image {url}: {e}. Using placeholder.")
                draw.rectangle([x_pos, y_pos, x_pos + thumbnail_size, y_pos + thumbnail_size], fill="dimgray")
                draw.text((x_pos + 10, y_pos + 10), f"OtherErr\n{url.split('/')[-1][:20]}...", fill="white", font=font)
        else:
            # Fill empty spots if not enough images
            _log.debug(f"No image for grid cell {index + 1}, filling with placeholder.")
            draw.rectangle([x_pos, y_pos, x_pos + thumbnail_size, y_pos + thumbnail_size], fill="dimgray")
            draw.text((x_pos + thumbnail_size//2 - 20 , y_pos + thumbnail_size//2 - 10), "Empty", fill="lightgray", font=font)


    return grid_image
