import sqlite3
import json
import os
import logging
from datetime import datetime
from plexapi.video import Video
from plexapi.audio import Track
from plexapi.server import PlexServer

# Initialize logger with plexsync prefix
logger = logging.getLogger('beets.plexsync')

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
                conn.commit()
                logger.debug('Cache database initialized successfully')
        except Exception as e:
            logger.error('Failed to initialize cache database: {}', e)
            raise

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
                cursor.execute('SELECT plex_ratingkey FROM cache WHERE query = ?', (query,))
                row = cursor.fetchone()
                if row:
                    logger.debug('Cache hit for query: {}', self._sanitize_query_for_log(query))
                    return row[0]
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