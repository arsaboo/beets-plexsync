import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta

from plexapi.audio import Track
from plexapi.server import PlexServer
from plexapi.video import Video

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
        self.plugin = plugin_instance
        logger.debug('Initializing cache at: {}', db_path)
        self._initialize_db()
        self._initialize_spotify_cache()  # Initialize without migration

    def _initialize_db(self):
        """Initialize the SQLite database."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Create the table if it doesn't exist
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS cache (
                        query TEXT PRIMARY KEY,
                        plex_ratingkey INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                # Add indexes
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_created_at ON cache(created_at)
                ''')
                conn.commit()
                logger.debug('Cache database initialized successfully')

                # Check if cleaned_query column exists, if not, add it
                cursor.execute("PRAGMA table_info(cache)")
                columns = [col[1] for col in cursor.fetchall()]
                if 'cleaned_query' not in columns:
                    cursor.execute('''
                        ALTER TABLE cache ADD COLUMN cleaned_query TEXT
                    ''')
                    conn.commit()
                    logger.debug('Added cleaned_query column to cache table')

                # Cleanup old entries on startup
                self._cleanup_expired()
        except Exception as e:
            logger.error('Failed to initialize cache database: {}', e)
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
                for table_type in ['api', 'web', 'tracks']:
                    table_name = f'spotify_{table_type}_cache'
                    if table_name not in existing_tables:
                        cursor.execute(f'''
                            CREATE TABLE {table_name} (
                                playlist_id TEXT PRIMARY KEY,
                                data TEXT,
                                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                            )
                        ''')
                        cursor.execute(f'''
                            CREATE INDEX IF NOT EXISTS idx_{table_name}_created
                            ON {table_name}(created_at)
                        ''')
                        logger.debug('Created new {} table', table_name)

                conn.commit()
                logger.debug('Spotify cache tables verified')

        except Exception as e:
            logger.error('Failed to initialize Spotify cache tables: {}', e)
            raise

    def clear_expired_spotify_cache(self):
        """Clear expired Spotify cache entries with randomized expiration."""
        try:
            import random
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Get all entries
                for table_type in ['api', 'web', 'tracks']:
                    table_name = f'spotify_{table_type}_cache'
                    cursor.execute(
                        f'SELECT playlist_id, created_at FROM {table_name}'
                    )
                    rows = cursor.fetchall()

                    for playlist_id, created_at in rows:
                        if created_at:
                            # Generate random expiration between 36 and 60 hours
                            expiry_hours = random.uniform(36, 60)
                            created_dt = datetime.fromisoformat(created_at)
                            expiry = created_dt + timedelta(hours=expiry_hours)

                            # Check if expired
                            if datetime.now() > expiry:
                                cursor.execute(
                                    f'DELETE FROM {table_name} WHERE playlist_id = ?',
                                    (playlist_id,)
                                )
                                if cursor.rowcount:
                                    logger.debug(
                                        'Cleaned expired entry from {} (age: {:.1f}h)',
                                        table_name,
                                        expiry_hours
                                    )

                conn.commit()
        except Exception as e:
            logger.error('Failed to clear expired Spotify cache: {}', e)

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

    def normalize_text(self, text):
        """Normalize text for consistent cache keys."""
        if not text:
            return ""
        # Convert to lowercase
        text = text.lower()
        # Remove featuring artists
        text = re.sub(r'\s*[\(\[]?(?:feat\.?|ft\.?|featuring)\s+[^\]\)]+[\]\)]?\s*$', '', text)
        # Remove any remaining parentheses or brackets at the end
        text = re.sub(r'\s*[\(\[][^\]\)]*[\]\)]\s*$', '', text)
        # Remove extra whitespace
        text = ' '.join(text.split())
        return text

    def _make_cache_key(self, query_data):
        """Create a consistent cache key regardless of input type."""
        if isinstance(query_data, str):
            return query_data
        elif isinstance(query_data, dict):
            # Normalize and clean the key fields
            key_data = {
                "title": self.normalize_text(query_data.get("title", "")),
                "artist": self.normalize_text(query_data.get("artist", "")),
                "album": self.normalize_text(query_data.get("album", ""))
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
                # Normalize the query for lookup
                cache_key = self._make_cache_key(query)

                # Try exact match first
                cursor.execute(
                    'SELECT plex_ratingkey, cleaned_query FROM cache WHERE query = ?',
                    (json.dumps(query) if isinstance(query, dict) else query,)
                )
                row = cursor.fetchone()

                if not row:
                    # Try normalized match if exact match fails
                    cursor.execute(
                        'SELECT plex_ratingkey, cleaned_query FROM cache WHERE query = ?',
                        (cache_key,)
                    )
                    row = cursor.fetchone()

                if row:
                    plex_ratingkey, cleaned_metadata_json = row
                    cleaned_metadata = json.loads(cleaned_metadata_json) if cleaned_metadata_json else None

                    logger.debug('Cache hit for query: {} (rating_key: {}, cleaned: {})',
                               self._sanitize_query_for_log(cache_key),
                               plex_ratingkey,
                               cleaned_metadata)
                    return (plex_ratingkey, cleaned_metadata)

                logger.debug('Cache miss for query: {}',
                            self._sanitize_query_for_log(cache_key))
                return None

        except Exception as e:
            logger.error('Cache lookup failed: {}', str(e))
            return None

    def set(self, query, plex_ratingkey, cleaned_metadata=None):
        """Store both original and cleaned metadata in cache."""
        try:
            def datetime_handler(obj):
                if isinstance(obj, datetime):
                    return obj.isoformat()
                raise TypeError(f'Object of type {type(obj)} is not JSON serializable')

            # Store the original query as-is
            cache_key = json.dumps(query, default=datetime_handler) if isinstance(query, dict) else query
            if not cache_key:
                logger.debug('Skipping cache for empty query')
                return None

            # Also store normalized version
            normalized_key = self._make_cache_key(query)

            # Use -1 for negative cache entries (when plex_ratingkey is None)
            rating_key = -1 if plex_ratingkey is None else int(plex_ratingkey)

            # Store cleaned metadata as JSON string
            cleaned_json = json.dumps(cleaned_metadata, default=datetime_handler) if cleaned_metadata else None

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                # Store both original and normalized versions
                for key in [cache_key, normalized_key]:
                    cursor.execute(
                        'REPLACE INTO cache (query, plex_ratingkey, cleaned_query) VALUES (?, ?, ?)',
                        (str(key), rating_key, cleaned_json)
                    )
                conn.commit()
                logger.debug('Cached result for query: "{}" (rating_key: {}, cleaned: {})',
                           self._sanitize_query_for_log(cache_key),
                           rating_key,
                           cleaned_metadata)
        except Exception as e:
            logger.error('Cache storage failed for query "{}": {}',
                        self._sanitize_query_for_log(query), str(e))
            return None

    def get_spotify_cache(self, playlist_id, cache_type='api'):
        """Get cached Spotify data."""
        try:
            # Clear expired entries first
            self.clear_expired_spotify_cache()

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                table_name = f'spotify_{cache_type}_cache'

                cursor.execute(
                    f'SELECT data FROM {table_name} WHERE playlist_id = ?',
                    (playlist_id,)
                )
                row = cursor.fetchone()

                if row:
                    logger.debug('Cache hit for Spotify {} cache: {}',
                               cache_type, playlist_id)
                    return json.loads(row[0])

                logger.debug('Cache miss for Spotify {} cache: {}',
                           cache_type, playlist_id)
                return None

        except Exception as e:
            logger.error('Spotify cache lookup failed: {}', e)
            return None

    def set_spotify_cache(self, playlist_id, data, cache_type='api'):
        """Store Spotify data in cache."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                table_name = f'spotify_{cache_type}_cache'

                # Convert datetime objects to ISO format strings
                def datetime_handler(obj):
                    if isinstance(obj, datetime):
                        return obj.isoformat()
                    return str(obj)

                # Store data as JSON string
                json_data = json.dumps(data, default=datetime_handler)

                cursor.execute(
                    f'REPLACE INTO {table_name} (playlist_id, data) VALUES (?, ?)',
                    (playlist_id, json_data)
                )
                conn.commit()
                logger.debug('Cached Spotify {} data for playlist: {}',
                           cache_type, playlist_id)

        except Exception as e:
            logger.error('Spotify cache storage failed: {}', e)

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