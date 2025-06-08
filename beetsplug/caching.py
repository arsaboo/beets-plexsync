import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta

from plexapi.audio import Track
from plexapi.server import PlexServer
from plexapi.video import Video
from xml.etree.ElementTree import Element

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
        self._initialize_spotify_cache()

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
            # Create a simple pipe-separated key that's more readable and consistent
            key_str = f"{key_data['title']}|{key_data['artist']}|{key_data['album']}"
            logger.debug('_make_cache_key output: {}', key_str)
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
            # Debug: show what's in cache for this query
            self.debug_cache_keys(query)

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Try multiple key formats for backward compatibility
                search_keys = []

                if isinstance(query, dict):
                    # Primary: New simple pipe-separated format
                    simple_key = self._make_cache_key(query)
                    search_keys.append(simple_key)

                    # Backward compatibility: Legacy JSON format
                    def datetime_handler(obj):
                        if isinstance(obj, datetime):
                            return obj.isoformat()
                        raise TypeError(f'Object of type {type(obj)} is not JSON serializable')

                    legacy_key = json.dumps(query, default=datetime_handler)
                    search_keys.append(legacy_key)

                    # Backward compatibility: Old complex JSON array format
                    key_data = {
                        "title": self.normalize_text(query.get("title", "")),
                        "artist": self.normalize_text(query.get("artist", "")),
                        "album": self.normalize_text(query.get("album", ""))
                    }
                    old_format_key = json.dumps([
                        ["album", key_data["album"]],
                        ["artist", key_data["artist"]],
                        ["title", key_data["title"]]
                    ])
                    search_keys.append(old_format_key)                    # Backward compatibility: Try with raw values too (no normalization)
                    raw_key_data = {
                        "title": query.get("title", ""),
                        "artist": query.get("artist", ""),
                        "album": query.get("album", "")
                    }
                    raw_pipe_key = f"{raw_key_data['title']}|{raw_key_data['artist']}|{raw_key_data['album']}"
                    search_keys.append(raw_pipe_key)

                    # Handle None album cases - try variations
                    if query.get("album") is None:
                        # Try with empty string for album
                        alt_query = query.copy()
                        alt_query["album"] = ""
                        search_keys.append(self._make_cache_key(alt_query))

                        # Try with "Unknown" for album (common default)
                        alt_query["album"] = "Unknown"
                        search_keys.append(self._make_cache_key(alt_query))
                          # Try without album field at all - title|artist format
                        title_artist_only = f"{self.normalize_text(query.get('title', ''))}|{self.normalize_text(query.get('artist', ''))}"
                        search_keys.append(title_artist_only)

                else:
                    # String query - use as-is
                    search_keys.append(str(query))

                # Try each key format first
                for search_key in search_keys:
                    cursor.execute(
                        'SELECT plex_ratingkey, cleaned_query FROM cache WHERE query = ?',
                        (search_key,)
                    )
                    row = cursor.fetchone()

                    if row:
                        plex_ratingkey, cleaned_metadata_json = row
                        cleaned_metadata = json.loads(cleaned_metadata_json) if cleaned_metadata_json else None

                        logger.debug('Cache hit for query: {} (key: {}, rating_key: {}, cleaned: {})',
                                   self._sanitize_query_for_log(query),
                                   search_key,
                                   plex_ratingkey,
                                   cleaned_metadata)
                        return (plex_ratingkey, cleaned_metadata)

                # If no exact match found and this is a dict query, try fuzzy matching
                if isinstance(query, dict):
                    title = self.normalize_text(query.get("title", ""))
                    artist = self.normalize_text(query.get("artist", ""))
                    if title and artist:
                        # Look for any cache entries that match title and artist with pipe format
                        cursor.execute(
                            'SELECT query, plex_ratingkey, cleaned_query FROM cache WHERE query LIKE ?',
                            (f'{title}|{artist}|%',)
                        )
                        fuzzy_rows = cursor.fetchall()
                        for cached_query, plex_ratingkey, cleaned_metadata_json in fuzzy_rows:
                            # Found a match with different album - use it
                            cleaned_metadata = json.loads(cleaned_metadata_json) if cleaned_metadata_json else None
                            logger.debug('Fuzzy cache hit (title+artist) for query: {} (key: {}, rating_key: {})',
                                       self._sanitize_query_for_log(query),
                                       cached_query,
                                       plex_ratingkey)
                            return (plex_ratingkey, cleaned_metadata)

                logger.debug('Cache miss for query: {}',
                            self._sanitize_query_for_log(query))
                return None

        except Exception as e:
            logger.error('Cache lookup failed: {}', str(e))
            return None

    def set(self, query, plex_ratingkey, cleaned_metadata=None):
        """Store result in cache using consistent key format."""
        try:
            def datetime_handler(obj):
                if isinstance(obj, datetime):
                    return obj.isoformat()
                raise TypeError(f'Object of type {type(obj)} is not JSON serializable')

            # Use -1 for negative cache entries (when plex_ratingkey is None)
            rating_key = -1 if plex_ratingkey is None else int(plex_ratingkey)

            # Store cleaned metadata as JSON string
            cleaned_json = json.dumps(cleaned_metadata, default=datetime_handler) if cleaned_metadata else None

            if isinstance(query, dict):
                # Use the NEW simple pipe-separated format as primary key
                primary_key = self._make_cache_key(query)

                # Also store with legacy format for backward compatibility
                key_data = {
                    "title": self.normalize_text(query.get("title", "")),
                    "artist": self.normalize_text(query.get("artist", "")),
                    "album": self.normalize_text(query.get("album", ""))
                }
                legacy_key = json.dumps([
                    ["album", key_data["album"]],
                    ["artist", key_data["artist"]],
                    ["title", key_data["title"]]
                ])

                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.cursor()
                    # Store with primary key
                    cursor.execute(
                        'REPLACE INTO cache (query, plex_ratingkey, cleaned_query) VALUES (?, ?, ?)',
                        (primary_key, rating_key, cleaned_json)
                    )
                    # Also store with legacy key for backward compatibility
                    cursor.execute(
                        'REPLACE INTO cache (query, plex_ratingkey, cleaned_query) VALUES (?, ?, ?)',
                        (legacy_key, rating_key, cleaned_json)
                    )
                    conn.commit()
                    logger.debug('Cached result for query: "{}" (rating_key: {}, cleaned: {})',
                               self._sanitize_query_for_log(primary_key),
                               rating_key,
                               cleaned_metadata)
            else:
                # String query
                cache_key = str(query)
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        'REPLACE INTO cache (query, plex_ratingkey, cleaned_query) VALUES (?, ?, ?)',
                        (cache_key, rating_key, cleaned_json)
                    )
                    conn.commit()
                    logger.debug('Cached result for string query: "{}" (rating_key: {})',
                               cache_key, rating_key)

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
                    'REPLACE INTO playlist_cache (playlist_id, source, data) VALUES (?, ?, ?)',
                    (playlist_id, source, json_data)
                )
                conn.commit()
                logger.debug('Cached {} playlist data for: {}',
                           source, playlist_id)

        except Exception as e:
            logger.error('{} playlist cache storage failed: {}', source, e)

    # Legacy methods for backward compatibility
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

    def debug_cache_keys(self, query):
        """Debug method to see what cache keys exist for a query."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Get all cache entries that might match
                if isinstance(query, dict):
                    title = self.normalize_text(query.get("title", ""))
                    artist = self.normalize_text(query.get("artist", ""))

                    logger.debug("=== CACHE DEBUG for title='{}', artist='{}', album='{}' ===",
                               title, artist, query.get("album"))

                    # Show all entries that contain the title or artist
                    cursor.execute(
                        'SELECT query, plex_ratingkey FROM cache WHERE query LIKE ? OR query LIKE ?',
                        (f'%{title}%', f'%{artist}%')
                    )
                    rows = cursor.fetchall()

                    logger.debug("Found {} potential cache matches:", len(rows))
                    for i, (cached_query, rating_key) in enumerate(rows):
                        logger.debug("  {}: key='{}' -> rating_key={}", i+1, cached_query, rating_key)
                          # Try to parse if it's JSON
                        try:
                            parsed = json.loads(cached_query)
                            logger.debug("      Parsed as JSON: {}", parsed)
                        except Exception:
                            logger.debug("      Not JSON format")

                    logger.debug("=== END CACHE DEBUG ===")

        except Exception as e:
            logger.error('Cache debug failed: {}', e)