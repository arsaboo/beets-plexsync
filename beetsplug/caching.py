import sqlite3
import json
import os
import logging
from datetime import datetime
from plexapi.video import Video
from plexapi.audio import Track
from plexapi.server import PlexServer

# Initialize logger
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
                    'ratingKey': getattr(obj, 'ratingKey', None),
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
        return super().default(obj)

class Cache:
    def __init__(self, db_path):
        self.db_path = db_path
        logger.debug('Initializing cache at: %s', db_path)
        self._initialize_db()

    def _initialize_db(self):
        """Initialize the SQLite database."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS cache (
                        query TEXT PRIMARY KEY,
                        result TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                conn.commit()
                logger.debug('Cache database initialized successfully')
        except Exception as e:
            logger.error('Failed to initialize cache database: %s', e)
            raise

    def _sanitize_query_for_log(self, query):
        """Sanitize and truncate query for logging."""
        try:
            if isinstance(query, str) and len(query) > 50:
                return query[:47] + "..."
            return str(query)
        except Exception:
            return "<unserializable query>"

    def get(self, query):
        """Retrieve cached result for a given query."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT result, created_at FROM cache WHERE query = ?', (query,))
                row = cursor.fetchone()
                if row:
                    try:
                        result = json.loads(row[0])
                        created_at = row[1]
                        logger.debug('Cache hit for query: {}', self._sanitize_query_for_log(query))
                        return result
                    except json.JSONDecodeError as e:
                        logger.warning('Invalid cache entry found, removing: {}', str(e))
                        cursor.execute('DELETE FROM cache WHERE query = ?', (query,))
                        conn.commit()
                        return None
                logger.debug('Cache miss for query: {}', self._sanitize_query_for_log(query))
                return None
        except Exception as e:
            logger.error('Cache lookup failed: {}', str(e))
            return None

    def set(self, query, result):
        """Store the result for a given query in the cache."""
        try:
            sanitized_query = self._sanitize_query_for_log(query)

            if not query:
                logger.debug('Skipping cache for empty query')
                return None

            # Special handling for None or not found results
            if result is None:
                json_result = json.dumps({'not_found': True})
                logger.debug('Caching negative result for query: {}', sanitized_query)
            else:
                try:
                    json_result = json.dumps(result, cls=PlexJSONEncoder)
                    logger.debug('Caching positive result for query: {}', sanitized_query)
                except TypeError as e:
                    logger.error('Failed to serialize result for query {}: {}', sanitized_query, str(e))
                    return None

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'REPLACE INTO cache (query, result) VALUES (?, ?)',
                    (query, json_result)
                )
                conn.commit()
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

                logger.info('Cleared %d entries from cache', count_before)
        except Exception as e:
            logger.error('Failed to clear cache: %s', e)
