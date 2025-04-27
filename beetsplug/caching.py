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

                # Create the tables if they don't exist
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

                # Create playlist cache table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS playlist_cache (
                        playlist_id TEXT,
                        source TEXT,
                        data TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (playlist_id, source)
                    )
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_playlist_cache_created
                    ON playlist_cache(created_at)
                ''')

                conn.commit()
                logger.debug('Cache database initialized successfully')

                # Check if cleaned_query column exists
                cursor.execute("PRAGMA table_info(cache)")
                columns = [col[1] for col in cursor.fetchall()]
                if 'cleaned_query' not in columns:
                    cursor.execute('ALTER TABLE cache ADD COLUMN cleaned_query TEXT')
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
                            expiry_hours = random.uniform(60, 200)
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

    def clear_expired_playlist_cache(self, max_age_hours=72):
        """Clear expired playlist cache entries."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                expiry = datetime.now() - timedelta(hours=max_age_hours)

                # Delete expired entries
                cursor.execute(
                    'DELETE FROM playlist_cache WHERE created_at < ?',
                    (expiry.isoformat(),)
                )
                if cursor.rowcount:
                    logger.debug('Cleaned {} expired playlist cache entries', cursor.rowcount)
                conn.commit()
        except Exception as e:
            logger.error('Failed to clear expired playlist cache: {}', e)

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
        # Remove year markers like [1977]
        text = re.sub(r'\s*\[\d{4}\]\s*$', '', text)
        # Remove featuring artists
        text = re.sub(r'\s*[\(\[]?(?:feat\.?|ft\.?|featuring)\s+[^\]\)]+[\]\)]?\s*$', '', text)
        # Remove any remaining parentheses or brackets at the end
        text = re.sub(r'\s*[\(\[][^\]\)]*[\]\)]\s*$', '', text)
        # Remove extra whitespace
        text = ' '.join(text.split())
        return text

    def _make_cache_key(self, query_data):
        """Create a consistent cache key regardless of input type."""
        logger.debug('_make_cache_key input: {}', query_data)
        if isinstance(query_data, str):
            return query_data
        elif isinstance(query_data, dict):
            # Normalize and clean the key fields
            key_data = {
                "title": self.normalize_text(query_data.get("title", "")),
                "artist": self.normalize_text(query_data.get("artist", "")),
                "album": self.normalize_text(query_data.get("album", ""))
            }
            # Create a consistent string representation without using sorted()
            # which was causing title and artist to be swapped
            key_str = json.dumps([
                ["album", key_data["album"]],
                ["artist", key_data["artist"]],
                ["title", key_data["title"]]
            ])
            logger.debug('_make_cache_key output: {}', key_str)
            return key_str
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

                # For debugging
                original_query_key = json.dumps(query) if isinstance(query, dict) else query
                logger.debug('Looking up with original key: {}', original_query_key)

                # Normalize the query for lookup
                cache_key = self._make_cache_key(query)
                logger.debug('Looking up with normalized key: {}', cache_key)

                # Try exact match first
                cursor.execute(
                    'SELECT plex_ratingkey, cleaned_query FROM cache WHERE query = ?',
                    (original_query_key,)
                )
                row = cursor.fetchone()

                if not row:
                    # Try normalized match if exact match fails
                    cursor.execute(
                        'SELECT plex_ratingkey, cleaned_query FROM cache WHERE query = ?',
                        (cache_key,)
                    )
                    row = cursor.fetchone()

                    # If still not found and this is a dict with artist and title, try with swapped values
                    if not row and isinstance(query, dict) and 'artist' in query and 'title' in query:
                        # Try with artist and title swapped
                        swapped_query = {
                            'title': query.get('artist', ''),
                            'artist': query.get('title', ''),
                            'album': query.get('album', '')
                        }
                        swapped_key = self._make_cache_key(swapped_query)

                        # Debug the swapped attempt
                        logger.debug('Trying with swapped artist/title: {}', swapped_key)

                        cursor.execute(
                            'SELECT plex_ratingkey, cleaned_query FROM cache WHERE query = ?',
                            (swapped_key,)
                        )
                        row = cursor.fetchone()

                        if row:
                            logger.debug('Cache hit with swapped artist/title')

                if row:
                    plex_ratingkey, cleaned_metadata_json = row
                    cleaned_metadata = json.loads(cleaned_metadata_json) if cleaned_metadata_json else None

                    logger.debug('Cache hit for query: {} (rating_key: {}, cleaned: {})',
                            self._sanitize_query_for_log(cache_key),
                            plex_ratingkey,
                            cleaned_metadata)
                    return (plex_ratingkey, cleaned_metadata)

                # Additional attempt: Try with un-normalized keys but compared in a normalized way
                cursor.execute('SELECT query, plex_ratingkey, cleaned_query FROM cache')
                all_keys = cursor.fetchall()

                for stored_key, stored_rating_key, stored_cleaned in all_keys:
                    try:
                        # Check if the stored key can be parsed as JSON
                        if stored_key.startswith('[') or stored_key.startswith('{'):
                            stored_dict = json.loads(stored_key)
                            # If both are dicts, compare normalized versions
                            if isinstance(stored_dict, list) and isinstance(query, dict):
                                # Convert the stored list format [["album", "value"], ...] to dict
                                stored_as_dict = {}
                                for k, v in stored_dict:
                                    stored_as_dict[k] = v

                                # Compare normalized versions
                                if (self.normalize_text(stored_as_dict.get("title", "")) == self.normalize_text(query.get("title", "")) and
                                    self.normalize_text(stored_as_dict.get("artist", "")) == self.normalize_text(query.get("artist", "")) and
                                    self.normalize_text(stored_as_dict.get("album", "")) == self.normalize_text(query.get("album", ""))):

                                    logger.debug('Cache hit with deep normalized comparison')
                                    cleaned_metadata = json.loads(stored_cleaned) if stored_cleaned else None
                                    return (stored_rating_key, cleaned_metadata)
                    except:
                        # Skip any parsing errors
                        pass

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

                # NEW PART: Also store the cleaned metadata as its own entry if it exists
                if cleaned_metadata:
                    # Create a cache key for the cleaned metadata
                    cleaned_key = self._make_cache_key(cleaned_metadata)
                    cursor.execute(
                        'REPLACE INTO cache (query, plex_ratingkey, cleaned_query) VALUES (?, ?, ?)',
                        (str(cleaned_key), rating_key, None)
                    )
                    logger.debug('Also cached cleaned metadata with key: {}', cleaned_key)

                conn.commit()
                logger.debug('Cached result for query: "{}" (rating_key: {}, cleaned: {})',
                        self._sanitize_query_for_log(cache_key),
                        rating_key,
                        cleaned_metadata)
        except Exception as e:
            logger.error('Cache storage failed for query "{}": {}',
                        self._sanitize_query_for_log(query), str(e))
            return None

    def get_playlist_cache(self, playlist_id, source):
        """Get cached playlist data for any source.

        Args:
            playlist_id: Unique identifier for the playlist
            source: Source platform (e.g., 'spotify', 'apple', 'jiosaavn')
        """
        try:
            # Clear expired entries first
            self.clear_expired_playlist_cache()

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'SELECT data FROM playlist_cache WHERE playlist_id = ? AND source = ?',
                    (playlist_id, source)
                )
                row = cursor.fetchone()

                if row:
                    logger.debug('Cache hit for {} playlist: {}',
                               source, playlist_id)
                    return json.loads(row[0])

                logger.debug('Cache miss for {} playlist: {}',
                           source, playlist_id)
                return None

        except Exception as e:
            logger.error('{} playlist cache lookup failed: {}', source, e)
            return None

    def set_playlist_cache(self, playlist_id, source, data):
        """Store playlist data in cache for any source.

        Args:
            playlist_id: Unique identifier for the playlist
            source: Source platform (e.g., 'spotify', 'apple', 'jiosaavn')
            data: Playlist data to cache
        """
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
                    'REPLACE INTO playlist_cache (playlist_id, source, data) VALUES (?, ?, ?)',
                    (playlist_id, source, json_data)
                )
                conn.commit()
                logger.debug('Cached {} playlist data for: {}',
                           source, playlist_id)

        except Exception as e:
            logger.error('{} playlist cache storage failed: {}', source, e)

    # Remove these methods as they're replaced by the generic versions above
    def get_spotify_cache(self, playlist_id, cache_type='api'):
        """Legacy method - redirects to generic get_playlist_cache."""
        return self.get_playlist_cache(playlist_id, f'spotify_{cache_type}')

    def set_spotify_cache(self, playlist_id, data, cache_type='api'):
        """Legacy method - redirects to generic set_playlist_cache."""
        return self.set_playlist_cache(playlist_id, f'spotify_{cache_type}', data)

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