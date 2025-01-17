import sqlite3
import json
import os
import logging
from datetime import datetime, timedelta
from plexapi.video import Video
from plexapi.audio import Track
from plexapi.server import PlexServer

# Initialize logger with plexsync prefix
logger = logging.getLogger('beets')

class PlexJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder for Plex objects."""
    def default(self, obj):
        if obj is None:
            return None
        if isinstance(obj, (Track, Video)):
            try:
                encoded = {
                    '_type': obj.__class__.__name__,
                    'plex_ratingkey': getattr(obj, 'ratingKey', None),  # Use ratingKey from Plex but encode as plex_ratingkey
                    'title': getattr(obj, 'title', ''),
                    'parentTitle': getattr(obj, 'parentTitle', ''),
                    'originalTitle': getattr(obj, 'originalTitle', ''),
                    'userRating': getattr(obj, 'userRating', None),
                    'viewCount': getattr(obj, 'viewCount', 0),
                    'lastViewedAt': obj.lastViewedAt.isoformat() if getattr(obj, 'lastViewedAt', None) else None,
                }
                logger.debug('Encoded Plex object: {} -> {}', obj.title, encoded)
                return encoded
            except AttributeError as e:
                logger.error('Failed to encode Plex object: {}', e)
                return None
        elif isinstance(obj, datetime):
            return obj.isoformat()
        elif isinstance(obj, PlexServer):
            logger.debug('Skipping PlexServer object serialization')
            return None
        elif isinstance(obj, Element):
            return str(obj)
        return super().default(obj)

class Cache:
    def __init__(self, db_path, plugin_instance):
        self.db_path = db_path
        self.plugin = plugin_instance  # Store reference to plugin instance
        logger.debug('Initializing cache at: {}', db_path)
        self._initialize_db()

    def _initialize_db(self):
        """Initialize the SQLite database."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS cache (
                        query TEXT PRIMARY KEY,
                        plex_ratingkey INTEGER,
                        cleaned_query TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                # Add indexes
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_created_at ON cache(created_at)
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_cleaned_query ON cache(cleaned_query)
                ''')
                conn.commit()
                logger.debug('Cache database initialized successfully')

                # Cleanup old entries on startup
                self._cleanup_expired()
        except Exception as e:
            logger.error('Failed to initialize cache database: {}', e)
            raise

    def _cleanup_expired(self, days=7):
        """Remove negative cache entries older than specified days."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                expiry = datetime.now() - timedelta(days=days)
                cursor.execute(
                    'DELETE FROM cache WHERE plex_ratingkey = -1 AND created_at < ?',
                    (expiry.isoformat(),)
                )
                if cursor.rowcount:
                    logger.debug('Cleaned up {} expired negative cache entries', cursor.rowcount)
                conn.commit()
        except Exception as e:
            logger.error('Failed to cleanup expired cache entries: {}', e)

    def _sanitize_query_for_log(self, query):
        """Sanitize query for logging."""
        try:
            return str(query)
        except Exception:
            return "<unserializable query>"

    def _make_cache_key(self, query_data):
        """Create a consistent cache key regardless of input type."""
        if isinstance(query_data, str):
            return query_data
        elif isinstance(query_data, dict):
            # Only use essential fields for the key
            key_data = {
                "title": (query_data.get("title") or "").strip().lower(),
                "artist": (query_data.get("artist") or "").strip().lower(),
                "album": (query_data.get("album") or "").strip().lower()
            }
            # Sort to ensure consistent order
            return json.dumps(sorted(key_data.items()))
        return str(query_data)

    def _verify_track_exists(self, plex_ratingkey, query):
        """Verify track exists, checking both original and cleaned metadata."""
        try:
            # First try direct lookup
            self.plugin.music.fetchItem(plex_ratingkey)
            return True
        except Exception:
            # If direct lookup fails, check if we have cleaned metadata
            if self.plugin.search_llm and self.plugin.config["plexsync"]["use_llm_search"].get(bool):
                try:
                    # Create search query from available metadata
                    search_query = ""
                    if isinstance(query, dict):
                        parts = []
                        if query.get("title"): parts.append(query["title"])
                        if query.get("artist"): parts.append(f"by {query['artist']}")
                        if query.get("album"): parts.append(f"from {query['album']}")
                        search_query = " ".join(parts)
                    else:
                        search_query = str(query)

                    # Get cleaned metadata
                    cleaned = self.plugin.search_track_info(search_query)
                    if cleaned:
                        # Look for cache entry with cleaned metadata
                        cleaned_key = self._make_cache_key({
                            "title": cleaned.get("title", ""),
                            "album": cleaned.get("album", ""),
                            "artist": cleaned.get("artist", "")
                        })
                        # Check if we have a valid cache entry with cleaned metadata
                        with sqlite3.connect(self.db_path) as conn:
                            cursor = conn.cursor()
                            cursor.execute(
                                'SELECT plex_ratingkey FROM cache WHERE query = ?',
                                (cleaned_key,)
                            )
                            row = cursor.fetchone()
                            if row and row[0] != -1:
                                try:
                                    self.plugin.music.fetchItem(row[0])
                                    return True
                                except Exception:
                                    pass
                except Exception as e:
                    logger.debug('Error checking cleaned metadata: {}', e)
            return False

    def get(self, query):
        """Retrieve cached result for a given query."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cache_key = self._make_cache_key(query)

                # Try original query first
                cursor.execute(
                    'SELECT plex_ratingkey, cleaned_query, created_at FROM cache WHERE query = ?',
                    (cache_key,)
                )
                row = cursor.fetchone()

                if row:
                    plex_ratingkey, cleaned_query, created_at = row
                    if plex_ratingkey == -1:  # Negative cache entry
                        created = datetime.fromisoformat(created_at)
                        if datetime.now() - created > timedelta(days=7):
                            cursor.execute('DELETE FROM cache WHERE query = ?', (cache_key,))
                            conn.commit()
                            logger.debug('Expired negative cache entry removed for query: {}',
                                       self._sanitize_query_for_log(query))
                            return None
                    else:  # Positive cache entry - verify track exists
                        try:
                            self.plugin.music.fetchItem(plex_ratingkey)
                            logger.debug('Cache hit for query: {}', self._sanitize_query_for_log(query))
                            return plex_ratingkey
                        except Exception:
                            # If original fails, try cleaned version if available
                            if cleaned_query:
                                try:
                                    cursor.execute(
                                        'SELECT plex_ratingkey FROM cache WHERE query = ?',
                                        (cleaned_query,)
                                    )
                                    cleaned_row = cursor.fetchone()
                                    if cleaned_row and cleaned_row[0] != -1:
                                        try:
                                            self.plugin.music.fetchItem(cleaned_row[0])
                                            return cleaned_row[0]
                                        except Exception:
                                            pass
                                except Exception:
                                    pass

                            # If both fail, remove entries
                            cursor.execute('DELETE FROM cache WHERE query = ?', (cache_key,))
                            if cleaned_query:
                                cursor.execute('DELETE FROM cache WHERE query = ?', (cleaned_query,))
                            conn.commit()
                            logger.debug('Removed cache entries for deleted track: {}',
                                       self._sanitize_query_for_log(query))
                            return None

                logger.debug('Cache miss for query: {}', self._sanitize_query_for_log(query))
                return None
        except Exception as e:
            logger.error('Cache lookup failed: {}', str(e))
            return None

    def set(self, query, plex_ratingkey, cleaned_metadata=None):
        """Store both original and cleaned metadata in cache."""
        try:
            cache_key = self._make_cache_key(query)
            if not cache_key:
                logger.debug('Skipping cache for empty query')
                return None

            cleaned_key = None
            if cleaned_metadata:
                cleaned_key = self._make_cache_key(cleaned_metadata)

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'REPLACE INTO cache (query, plex_ratingkey, cleaned_query) VALUES (?, ?, ?)',
                    (cache_key, plex_ratingkey, cleaned_key)
                )
                # Also store an entry with the cleaned query if available
                if cleaned_key and cleaned_key != cache_key:
                    cursor.execute(
                        'REPLACE INTO cache (query, plex_ratingkey, cleaned_query) VALUES (?, ?, ?)',
                        (cleaned_key, plex_ratingkey, None)  # No need to store cleaned_query for already cleaned entry
                    )
                conn.commit()
                logger.debug('Cached result for query: {} (cleaned: {})',
                           self._sanitize_query_for_log(cache_key),
                           self._sanitize_query_for_log(cleaned_key) if cleaned_key else "None")
        except Exception as e:
            logger.error('Cache storage failed for query {}: {}',
                        self._sanitize_query_for_log(cache_key), str(e))
            return None

    def clear(self):
        """Clear all cached entries."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT COUNT(*) FROM cache')
                count_before = cursor.fetchone()[0]

                cursor.execute('DELETE FROM cache')
                conn.commit()

                logger.info('Cleared {} entries from cache', count_before)
        except Exception as e:
            logger.error('Failed to clear cache: {}', e)