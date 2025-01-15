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
                    'plex_ratingkey': getattr(obj, 'ratingKey', None),
                    'title': getattr(obj, 'title', ''),
                    'parentTitle': getattr(obj, 'parentTitle', ''),
                    'originalTitle': getattr(obj, 'originalTitle', ''),
                    'userRating': getattr(obj, 'userRating', None),
                    'viewCount': getattr(obj, 'viewCount', 0),
                    'lastViewedAt': obj.lastViewedAt.isoformat() if getattr(obj, 'lastViewedAt', None) else None,
                }
                logger.debug('Encoded Plex object: %s -> %s', obj.title, encoded)
                return encoded
            except AttributeError as e:
                logger.error('Failed to encode Plex object: %s', e)
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
    def __init__(self, db_path):
        self.db_path = db_path
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
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                # Add index on created_at for faster cleanup
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_created_at ON cache(created_at)
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

    def get(self, query):
        """Retrieve cached result for a given query."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'SELECT plex_ratingkey, created_at FROM cache WHERE query = ?',
                    (query,)
                )
                row = cursor.fetchone()
                if row:
                    rating_key, created_at = row
                    if rating_key == -1:  # Negative cache entry
                        created = datetime.fromisoformat(created_at)
                        if datetime.now() - created > timedelta(days=7):
                            # Expired negative entry, remove and return None
                            cursor.execute('DELETE FROM cache WHERE query = ?', (query,))
                            conn.commit()
                            logger.debug('Expired negative cache entry removed for query: {}',
                                       self._sanitize_query_for_log(query))
                            return None
                    else:  # Positive cache entry - verify track still exists
                        try:
                            # This will raise NotFound if track doesn't exist
                            self.music.fetchItem(rating_key)
                        except Exception:
                            # Track no longer exists, remove from cache
                            cursor.execute('DELETE FROM cache WHERE query = ?', (query,))
                            conn.commit()
                            logger.debug('Removed cache entry for deleted track: {}',
                                       self._sanitize_query_for_log(query))
                            return None

                    logger.debug('Cache hit for query: {}', self._sanitize_query_for_log(query))
                    return rating_key
                logger.debug('Cache miss for query: {}', self._sanitize_query_for_log(query))
                return None
        except Exception as e:
            logger.error('Cache lookup failed: {}', str(e))
            return None

    def set(self, query, plex_ratingkey):
        """Store the plex_ratingkey for a given query in the cache."""
        try:
            sanitized_query = self._sanitize_query_for_log(query)

            if not query:
                logger.debug('Skipping cache for empty query')
                return None

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'REPLACE INTO cache (query, plex_ratingkey) VALUES (?, ?)',
                    (query, plex_ratingkey)
                )
                conn.commit()
                logger.debug('Caching result for query: {}', sanitized_query)
        except Exception as e:
            logger.error('Cache storage failed for query {}: {}', sanitized_query, str(e))
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