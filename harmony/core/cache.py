"""Cache management for Harmony."""

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("harmony")


class Cache:
    """SQLite-based cache for Plex track search results and playlists."""

    def __init__(self, db_path: str):
        """Initialize cache with database path."""
        self.db_path = db_path
        logger.debug(f"Initializing cache at: {db_path}")
        self._initialize_db()

    def _initialize_db(self) -> None:
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
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP
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
                
                # Check if expires_at column exists
                if "expires_at" not in columns:
                    cursor.execute("ALTER TABLE cache ADD COLUMN expires_at TIMESTAMP")
                    conn.commit()
                    logger.debug("Added expires_at column to cache table")

                # Cleanup old entries on startup
                self._cleanup_expired()

        except Exception as e:
            logger.error(f"Failed to initialize cache database: {e}")
            raise

    def _cleanup_expired(self, days: int = 30) -> None:
        """Remove cache entries that have passed their expiration time."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                now = datetime.now().isoformat()
                
                # Delete entries where expires_at is set and has passed
                cursor.execute(
                    "DELETE FROM cache WHERE expires_at IS NOT NULL AND expires_at < ?",
                    (now,),
                )
                expired_count = cursor.rowcount
                
                # Also cleanup old negative entries without TTL (legacy cleanup)
                expiry = datetime.now() - timedelta(days=days)
                cursor.execute(
                    "DELETE FROM cache WHERE plex_ratingkey = -1 AND expires_at IS NULL AND created_at < ?",
                    (expiry.isoformat(),),
                )
                legacy_count = cursor.rowcount
                
                total_count = expired_count + legacy_count
                if total_count:
                    logger.debug(f"Cleaned up {total_count} expired cache entries ({expired_count} TTL, {legacy_count} legacy)")
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to cleanup expired cache entries: {e}")

    def normalize_text(self, text: Optional[str]) -> str:
        """Normalize text for consistent cache keys."""
        if not text:
            return ""
        # Convert to lowercase
        text = text.lower()
        # Remove featuring artists
        text = re.sub(r"\s*[\(\[]?(?:feat\.?|ft\.?|featuring)\s+[^\]\)]+[\]\)]?\s*", "", text)
        # Remove any parentheses or brackets and their contents
        text = re.sub(r"\s*[\(\[][^\]\)]*[\]\)]\s*", "", text)
        # Remove extra whitespace
        text = " ".join(text.split())
        return text

    def _make_cache_key(self, query_data: Dict[str, str] | str) -> str:
        """Create a consistent cache key regardless of input type."""
        if isinstance(query_data, str):
            return query_data
        elif isinstance(query_data, dict):
            # Normalize and clean the key fields
            normalized_title = self.normalize_text(query_data.get("title", ""))
            normalized_artist = self.normalize_text(query_data.get("artist", ""))
            normalized_album = self.normalize_text(query_data.get("album", ""))

            # Create a pipe-separated key with title|artist|album
            key_str = f"{normalized_title}|{normalized_artist}|{normalized_album}"
            return key_str
        return str(query_data)

    def get(self, query: Dict[str, str] | str) -> Optional[Tuple[Any, Optional[Dict[str, Any]]]]:
        """Retrieve cached result for a given query."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Generate cache key
                cache_key = self._make_cache_key(query)

                # Try exact match first
                cursor.execute(
                    "SELECT plex_ratingkey, cleaned_query, expires_at FROM cache WHERE query = ?",
                    (cache_key,),
                )
                row = cursor.fetchone()

                if row:
                    plex_ratingkey, cleaned_metadata_json, expires_at = row
                    
                    # Check if entry has expired
                    if expires_at:
                        expiry_time = datetime.fromisoformat(expires_at)
                        if datetime.now() > expiry_time:
                            logger.debug(f"Cache entry expired for: {cache_key}")
                            # Delete the expired entry
                            cursor.execute("DELETE FROM cache WHERE query = ?", (cache_key,))
                            conn.commit()
                            return None
                    
                    cleaned_metadata = json.loads(cleaned_metadata_json) if cleaned_metadata_json else None
                    return (plex_ratingkey, cleaned_metadata)

                # If no exact match, try flexible matching for new pipe format only
                if isinstance(query, dict):
                    normalized_title = self.normalize_text(query.get("title", ""))
                    normalized_artist = self.normalize_text(query.get("artist", ""))

                    # Look for entries with same title and artist (new pipe format only)
                    cursor.execute(
                        """SELECT plex_ratingkey, cleaned_query, query, expires_at
                           FROM cache
                           WHERE query LIKE ? AND query LIKE '%|%'""",
                        (f"{normalized_title}|{normalized_artist}|%",),
                    )

                    for row in cursor.fetchall():
                        plex_ratingkey, cleaned_metadata_json, cached_query, expires_at = row
                        
                        # Check if entry has expired
                        if expires_at:
                            expiry_time = datetime.fromisoformat(expires_at)
                            if datetime.now() > expiry_time:
                                logger.debug(f"Cache entry expired for: {cached_query}")
                                cursor.execute("DELETE FROM cache WHERE query = ?", (cached_query,))
                                conn.commit()
                                continue
                        
                        cleaned_metadata = json.loads(cleaned_metadata_json) if cleaned_metadata_json else None
                        logger.debug(f'Found flexible match: "{cached_query}" -> rating_key: {plex_ratingkey}')
                        return (plex_ratingkey, cleaned_metadata)

                return None
        except Exception as e:
            logger.error(f"Cache lookup failed: {e}")
            return None

    def set(
        self,
        query: Dict[str, str] | str,
        plex_ratingkey: Optional[Any],
        cleaned_metadata: Optional[Dict[str, Any]] = None,
        ttl_days: Optional[int] = None,
    ) -> None:
        """Store result in cache with optional TTL.
        
        Args:
            query: Query dict or string to use as cache key
            plex_ratingkey: Plex rating key (None for negative results)
            cleaned_metadata: Optional cleaned metadata dict
            ttl_days: Optional TTL in days (default: 30 days for negative results, None for positive)
        """
        try:
            def datetime_handler(obj: Any) -> str:
                if isinstance(obj, datetime):
                    return obj.isoformat()
                raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

            if plex_ratingkey is None:
                rating_key = -1
                # Default TTL for negative results if not specified
                if ttl_days is None:
                    ttl_days = 30
            else:
                try:
                    rating_key = int(plex_ratingkey)
                except (TypeError, ValueError):
                    rating_key = str(plex_ratingkey)
            
            # Calculate expiration time
            expires_at = None
            if ttl_days:
                expires_at = (datetime.now() + timedelta(days=ttl_days)).isoformat()
            
            cleaned_json = (
                json.dumps(cleaned_metadata, default=datetime_handler)
                if cleaned_metadata
                else None
            )

            # Generate cache key using the same method as get()
            cache_key = self._make_cache_key(query)

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "REPLACE INTO cache (query, plex_ratingkey, cleaned_query, expires_at) VALUES (?, ?, ?, ?)",
                    (cache_key, rating_key, cleaned_json, expires_at),
                )
                conn.commit()
                logger.debug(f'Cached result: "{cache_key}" -> rating_key: {rating_key}, expires_at: {expires_at}')

        except Exception as e:
            logger.error(f'Cache storage failed: {e}')

    def get_playlist_cache(self, playlist_id: str, source: str) -> Optional[Dict[str, Any]]:
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
                    logger.debug(f"Cache hit for {source} playlist: {playlist_id}")
                    return json.loads(row[0])

                logger.debug(f"Cache miss for {source} playlist: {playlist_id}")
                return None

        except Exception as e:
            logger.error(f"{source} playlist cache lookup failed: {e}")
            return None

    def set_playlist_cache(self, playlist_id: str, source: str, data: Any) -> None:
        """Store playlist data in cache for any source."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Convert datetime objects to ISO format strings
                def datetime_handler(obj: Any) -> str:
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
                logger.debug(f"Cached {source} playlist data for: {playlist_id}")

        except Exception as e:
            logger.error(f"{source} playlist cache storage failed: {e}")

    def clear_expired_playlist_cache(self, max_age_hours: int = 168, source: Optional[str] = None) -> None:
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
                    logger.debug(f"Cleaned {deleted_count} expired playlist cache entries")

                conn.commit()
        except Exception as e:
            logger.error(f"Failed to clear expired playlist cache: {e}")

    def clear(self) -> None:
        """Clear all cached entries."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM cache")
                count_before = cursor.fetchone()[0]

                cursor.execute("DELETE FROM cache")
                conn.commit()

                logger.info(f"Cleared {count_before} entries from cache")
        except Exception as e:
            logger.error(f"Failed to clear cache: {e}")

    def clear_negative_cache_entries(self, pattern: Optional[str] = None) -> int:
        """Clear negative cache entries, optionally matching a pattern."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                if pattern:
                    # Clear specific pattern
                    cursor.execute(
                        "DELETE FROM cache WHERE plex_ratingkey = -1 AND query LIKE ?",
                        (f"%{pattern}%",),
                    )
                    logger.debug(
                        f"Cleared {cursor.rowcount} negative cache entries matching pattern: {pattern}"
                    )
                else:
                    # Clear all negative entries
                    cursor.execute("DELETE FROM cache WHERE plex_ratingkey = -1")
                    logger.debug(f"Cleared {cursor.rowcount} negative cache entries")

                conn.commit()
                return cursor.rowcount
        except Exception as e:
            logger.error(f"Failed to clear negative cache entries: {e}")
            return 0

    def clear_old_format_entries(self) -> int:
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
                    logger.info(f"Cleared {cleared_count} old format cache entries")

                conn.commit()
                return cleared_count
        except Exception as e:
            logger.error(f"Failed to clear old format cache entries: {e}")
            return 0
