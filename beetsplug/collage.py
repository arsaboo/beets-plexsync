"""Collage creation helpers extracted from plexsync."""

import os
from datetime import datetime
from io import BytesIO

import requests
from PIL import Image
from plexapi import exceptions


def create_collage(list_image_urls, dimension, logger):
    """Create a square collage from a list of image urls.

    Returns a PIL.Image. Behavior identical to original.
    """
    thumbnail_size = 300
    grid_size = thumbnail_size * dimension
    grid = Image.new("RGB", (grid_size, grid_size), "black")

    for index, url in enumerate(list_image_urls):
        if index >= dimension * dimension:
            break
        try:
            response = requests.get(url, timeout=10)
            img = Image.open(BytesIO(response.content))
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((thumbnail_size, thumbnail_size), Image.Resampling.LANCZOS)
            x = thumbnail_size * (index % dimension)
            y = thumbnail_size * (index // dimension)
            grid.paste(img, (x, y))
            img.close()
        except Exception as e:
            logger.debug("Failed to process image {}: {}", url, e)
            continue
    return grid


def plex_collage(plugin, interval, grid):
    """Create a collage of most played albums and save to config dir."""
    interval = int(interval)
    grid = int(grid)
    plugin._log.info("Creating collage of most played albums in the last {} days", interval)

    tracks = plugin.music.search(
        filters={"track.lastViewedAt>>": f"{interval}d"},
        sort="viewCount:desc",
        libtype="track",
    )

    max_albums = grid * grid
    sorted_albums = plugin._plex_most_played_albums(tracks, interval)[:max_albums]

    if not sorted_albums:
        plugin._log.error("No albums found in the specified time period")
        return

    album_art_urls = []
    for album in sorted_albums:
        if hasattr(album, "thumbUrl") and album.thumbUrl:
            album_art_urls.append(album.thumbUrl)
            plugin._log.debug(
                "Added album art for: {} (played {} times)",
                album.title,
                album.count,
            )

    if not album_art_urls:
        plugin._log.error("No album artwork found")
        return

    try:
        collage = create_collage(album_art_urls, grid, plugin._log)
        output_path = os.path.join(plugin.config_dir, "collage.png")
        collage.save(output_path, "PNG", quality=95)
        plugin._log.info("Collage saved to: {}", output_path)
    except Exception as e:
        plugin._log.error("Failed to create collage: {}", e)

