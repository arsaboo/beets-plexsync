import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta

from plexapi.audio import Track
from plexapi.server import PlexServer
from plexapi.video import Video
from xml.etree.ElementTree import Element

logger = logging.getLogger("beets")


class PlexJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder for Plex objects."""

    def default(self, obj):
        if obj is None:
            return None
        if isinstance(obj, (Track, Video)):
            try:
                encoded = {
                    "_type": obj.__class__.__name__,
                    "plex_ratingkey": getattr(obj, "ratingKey", None),
                    "title": getattr(obj, "title", ""),
                    "parentTitle": getattr(obj, "parentTitle", ""),
                    "originalTitle": getattr(obj, "originalTitle", ""),
                    "userRating": getattr(obj, "userRating", None),
                    "viewCount": getattr(obj, "viewCount", 0),
                    "lastViewedAt": (
                        obj.lastViewedAt.isoformat()
                        if getattr(obj, "lastViewedAt", None)
                        else None
                    ),
                }
                logger.debug("Encoded Plex object: {} -> {}", obj.title, encoded)
                return encoded
            except AttributeError as e:
                logger.error("Failed to encode Plex object: {}", e)
                return None
        elif isinstance(obj, datetime):
            return obj.isoformat()
        elif isinstance(obj, PlexServer):
            logger.debug("Skipping PlexServer object serialization")
            return None
        elif isinstance(obj, Element):
            return str(obj)
        return super().default(obj)


class Cache:
    def __init__(self, db_path, plugin_instance):
        self.db_path = db_path
        self.plugin = plugin_instance
        logger.debug("Initializing cache at: {}", db_path)
        self._initialize_db()
        self._initialize_spotify_cache()

    def _initialize_db(self):
        """Initialize the SQLite database."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Create the tables if they don't exist
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cache (
                        query TEXT PRIMARY KEY,
                        plex_ratingkey INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """
                )

                # Add indexes
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_created_at ON cache(created_at)
                """
                )

                # Create playlist cache table
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS playlist_cache (
                        playlist_id TEXT,
                        source TEXT,
                        data TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (playlist_id, source)
                    )
                """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_playlist_cache_created
                    ON playlist_cache(created_at)
                """
                )

                conn.commit()
                logger.debug("Cache database initialized successfully")

                # Check if cleaned_query column exists
                cursor.execute("PRAGMA table_info(cache)")
                columns = [col[1] for col in cursor.fetchall()]
                if "cleaned_query" not in columns:
                    cursor.execute("ALTER TABLE cache ADD COLUMN cleaned_query TEXT")
                    conn.commit()
                    logger.debug("Added cleaned_query column to cache table")

                # Cleanup old entries on startup
                self._cleanup_expired()

        except Exception as e:
            logger.error("Failed to initialize cache database: {}", e)
            raise

    def _initialize_spotify_cache(self):
        """Initialize Spotify-specific cache tables."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Check if tables exist
                existing_tables = set()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                for row in cursor.fetchall():
                    existing_tables.add(row[0])

                # Create tables only if they don't exist
                for table_type in ["api", "web", "tracks"]:
                    table_name = f"spotify_{table_type}_cache"
                    if table_name not in existing_tables:
                        cursor.execute(
                            f"""
                            CREATE TABLE {table_name} (
                                playlist_id TEXT PRIMARY KEY,
                                data TEXT,
                                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                            )
                        """
                        )
                        cursor.execute(
                            f"""
                            CREATE INDEX IF NOT EXISTS idx_{table_name}_created
                            ON {table_name}(created_at)
                        """
                        )
                        logger.debug("Created new {} table", table_name)

                conn.commit()
                logger.debug("Spotify cache tables verified")

        except Exception as e:
            logger.error("Failed to initialize Spotify cache tables: {}", e)
            raise

    def clear_expired_playlist_cache(self, max_age_hours=168, source=None):
        """Clear expired playlist cache entries with smart expiration.

        Automatically applies randomized expiration (60-200 hours) for Spotify sources
        to prevent thundering herd issues. Uses fixed TTL for other sources.

        Args:
            max_age_hours: Fixed TTL for non-Spotify sources (default: 7 days)
            source: Optional source filter (e.g., "spotify_tracks", "youtube")
        """
        try:
            import random

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Build query based on source filter
                if source:
                    query = "SELECT playlist_id, source, created_at FROM playlist_cache WHERE source = ?"
                    cursor.execute(query, (source,))
                else:
                    query = "SELECT playlist_id, source, created_at FROM playlist_cache"
                    cursor.execute(query)

                rows = cursor.fetchall()
                deleted_count = 0

                for playlist_id, source_name, created_at_str in rows:
                    created_at = datetime.fromisoformat(created_at_str)

                    # Use randomized expiry for Spotify to prevent thundering herd
                    if source_name and 'spotify' in source_name.lower():
                        random_hours = random.randint(60, 200)
                        expiry = created_at + timedelta(hours=random_hours)
                    else:
                        # Fixed expiry for other sources
                        expiry = created_at + timedelta(hours=max_age_hours)

                    # Delete if expired
                    if datetime.now() > expiry:
                        cursor.execute(
                            "DELETE FROM playlist_cache WHERE playlist_id = ? AND source = ?",
                            (playlist_id, source_name),
                        )
                        deleted_count += 1

                if deleted_count:
                    logger.debug("Cleaned {} expired playlist cache entries", deleted_count)

                conn.commit()
        except Exception as e:
            logger.error("Failed to clear expired playlist cache: {}", e)

    def _cleanup_expired(self, days=7):
        """Remove negative cache entries older than specified days."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                expiry = datetime.now() - timedelta(days=days)
                cursor.execute(
                    "DELETE FROM cache WHERE plex_ratingkey = -1 AND created_at < ?",
                    (expiry.isoformat(),),
                )
                if cursor.rowcount:
                    logger.debug(
                        "Cleaned up {} expired negative cache entries", cursor.rowcount
                    )
                conn.commit()
        except Exception as e:
            logger.error("Failed to cleanup expired cache entries: {}", e)

    def _sanitize_query_for_log(self, query):
        """Sanitize query for logging."""
        try:
            return str(query)
        except Exception:
            return "<unserializable query>"

    def normalize_text(self, text):
        """Normalize text for consistent cache keys."""
        if not text:
            return ""
        # Convert to lowercase
        text = text.lower()
        # Remove featuring artists
        text = re.sub(
            r"\s*[\(\[]?(?:feat\.?|ft\.?|featuring)\s+[^\]\)]+[\]\)]?\s*", "", text
        )
        # Remove any parentheses or brackets and their contents
        text = re.sub(r"\s*[\(\[][^\]\)]*[\]\)]\s*", "", text)
        # Remove extra whitespace
        text = " ".join(text.split())
        return text


    def _make_cache_key(self, query_data):
        """Create a consistent cache key regardless of input type."""
        if isinstance(query_data, str):
            return query_data
        elif isinstance(query_data, dict):
            # Normalize and clean the key fields - keep the original 3-part format
            normalized_title = self.normalize_text(query_data.get("title", ""))
            normalized_artist = self.normalize_text(query_data.get("artist", ""))
            normalized_album = self.normalize_text(query_data.get("album", ""))

            # Create a pipe-separated key with title|artist|album
            key_str = f"{normalized_title}|{normalized_artist}|{normalized_album}"
            return key_str
        return str(query_data)

    def _verify_track_exists(self, plex_ratingkey, query):
        """Verify track exists in Plex."""
        try:
            # First try direct lookup
            self.plugin.music.fetchItem(plex_ratingkey)
            return True
        except Exception:
            return False

    def get(self, query):
        """Retrieve cached result for a given query."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Generate cache key
                cache_key = self._make_cache_key(query)

                # Try exact match first
                cursor.execute(
                    'SELECT plex_ratingkey, cleaned_query FROM cache WHERE query = ?',
                    (cache_key,)
                )
                row = cursor.fetchone()

                if row:
                    plex_ratingkey, cleaned_metadata_json = row
                    cleaned_metadata = json.loads(cleaned_metadata_json) if cleaned_metadata_json else None
                    return (plex_ratingkey, cleaned_metadata)

                # If no exact match, try flexible matching for new pipe format only
                # This handles cases where album names might have slight variations
                if isinstance(query, dict):
                    normalized_title = self.normalize_text(query.get("title", ""))
                    normalized_artist = self.normalize_text(query.get("artist", ""))

                    # Look for entries with same title and artist (new pipe format only)
                    cursor.execute(
                        '''SELECT plex_ratingkey, cleaned_query, query
                           FROM cache
                           WHERE query LIKE ? AND query LIKE '%|%' ''',
                        (f'{normalized_title}|{normalized_artist}|%',)
                    )

                    for row in cursor.fetchall():
                        plex_ratingkey, cleaned_metadata_json, cached_query = row
                        cleaned_metadata = json.loads(cleaned_metadata_json) if cleaned_metadata_json else None
                        logger.debug('Found flexible match: "{}" -> rating_key: {}', cached_query, plex_ratingkey)
                        return (plex_ratingkey, cleaned_metadata)

                return None
        except Exception as e:
            logger.error('Cache lookup failed: {}', str(e))
            return None

    def set(self, query, plex_ratingkey, cleaned_metadata=None):
        """Store result in cache."""
        try:
            def datetime_handler(obj):
                if isinstance(obj, datetime):
                    return obj.isoformat()
                raise TypeError(f'Object of type {type(obj)} is not JSON serializable')

            rating_key = -1 if plex_ratingkey is None else int(plex_ratingkey)
            cleaned_json = json.dumps(cleaned_metadata, default=datetime_handler) if cleaned_metadata else None

            # Generate cache key using the same method as get()
            cache_key = self._make_cache_key(query)

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'REPLACE INTO cache (query, plex_ratingkey, cleaned_query) VALUES (?, ?, ?)',
                    (cache_key, rating_key, cleaned_json)
                )
                conn.commit()
                logger.debug('Cached result: "{}" -> rating_key: {}', cache_key, rating_key)

        except Exception as e:
            logger.error('Cache storage failed for query "{}": {}',
                        self._sanitize_query_for_log(query), str(e))
            return None

    def get_playlist_cache(self, playlist_id, source):
        """Get cached playlist data for any source."""
        try:
            # Clear expired entries first
            self.clear_expired_playlist_cache()

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT data FROM playlist_cache WHERE playlist_id = ? AND source = ?",
                    (playlist_id, source),
                )
                row = cursor.fetchone()

                if row:
                    logger.debug("Cache hit for {} playlist: {}", source, playlist_id)
                    return json.loads(row[0])

                logger.debug("Cache miss for {} playlist: {}", source, playlist_id)
                return None

        except Exception as e:
            logger.error("{} playlist cache lookup failed: {}", source, e)
            return None

    def set_playlist_cache(self, playlist_id, source, data):
        """Store playlist data in cache for any source."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Convert datetime objects to ISO format strings
                def datetime_handler(obj):
                    if isinstance(obj, datetime):
                        return obj.isoformat()
                    return str(obj)

                # Store data as JSON string
                json_data = json.dumps(data, default=datetime_handler)

                cursor.execute(
                    "REPLACE INTO playlist_cache (playlist_id, source, data) VALUES (?, ?, ?)",
                    (playlist_id, source, json_data),
                )
                conn.commit()
                logger.debug("Cached {} playlist data for: {}", source, playlist_id)

        except Exception as e:
            logger.error("{} playlist cache storage failed: {}", source, e)

    # Legacy methods for backward compatibility
    def get_spotify_cache(self, playlist_id, cache_type="api"):
        """Legacy method - redirects to generic get_playlist_cache."""
        return self.get_playlist_cache(playlist_id, f"spotify_{cache_type}")

    def set_spotify_cache(self, playlist_id, data, cache_type="api"):
        """Legacy method - redirects to generic set_playlist_cache."""
        return self.set_playlist_cache(playlist_id, f"spotify_{cache_type}", data)

    def clear(self):
        """Clear all cached entries."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM cache")
                count_before = cursor.fetchone()[0]

                cursor.execute("DELETE FROM cache")
                conn.commit()

                logger.info("Cleared {} entries from cache", count_before)
        except Exception as e:
            logger.error("Failed to clear cache: {}", e)

    def clear_negative_cache_entries(self, pattern=None):
        """Clear negative cache entries, optionally matching a pattern."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                if pattern:
                    # Clear specific pattern
                    cursor.execute(
                        "DELETE FROM cache WHERE plex_ratingkey = -1 AND query LIKE ?",
                        (f"%{pattern}%",)
                    )
                    logger.debug("Cleared {} negative cache entries matching pattern: {}",
                               cursor.rowcount, pattern)
                else:
                    # Clear all negative entries
                    cursor.execute("DELETE FROM cache WHERE plex_ratingkey = -1")
                    logger.debug("Cleared {} negative cache entries", cursor.rowcount)

                conn.commit()
                return cursor.rowcount
        except Exception as e:
            logger.error("Failed to clear negative cache entries: {}", e)
            return 0

    def clear_old_format_entries(self):
        """Clear all old format cache entries (JSON and list formats)."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Delete entries that don't use the new pipe format
                cursor.execute(
                    "DELETE FROM cache WHERE query NOT LIKE '%|%' OR query LIKE '{%' OR query LIKE '[%'"
                )
                cleared_count = cursor.rowcount

                if cleared_count > 0:
                    logger.info("Cleared {} old format cache entries", cleared_count)

                conn.commit()
                return cleared_count
        except Exception as e:
            logger.error("Failed to clear old format cache entries: {}", e)
            return 0

