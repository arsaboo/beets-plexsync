import sqlite3
import json
import os
from datetime import datetime
from plexapi.video import Video
from plexapi.audio import Track
from plexapi.server import PlexServer

class PlexJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder for Plex objects."""
    def default(self, obj):
        if isinstance(obj, (Track, Video)):
            return {
                '_type': obj.__class__.__name__,
                'ratingKey': obj.ratingKey,
                'title': obj.title,
                'parentTitle': getattr(obj, 'parentTitle', None),
                'originalTitle': getattr(obj, 'originalTitle', None),
                'userRating': getattr(obj, 'userRating', None),
                'viewCount': getattr(obj, 'viewCount', 0),
                'lastViewedAt': obj.lastViewedAt.isoformat() if getattr(obj, 'lastViewedAt', None) else None,
            }
        elif isinstance(obj, datetime):
            return obj.isoformat()
        elif isinstance(obj, PlexServer):
            return None  # Skip PlexServer objects
        return super().default(obj)

class Cache:
    def __init__(self, db_path):
        self.db_path = db_path
        self._initialize_db()

    def _initialize_db(self):
        """Initialize the SQLite database."""
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

    def get(self, query):
        """Retrieve cached result for a given query."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT result FROM cache WHERE query = ?', (query,))
            row = cursor.fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except json.JSONDecodeError:
                    # If the cached data is invalid, remove it
                    cursor.execute('DELETE FROM cache WHERE query = ?', (query,))
                    conn.commit()
                    return None
            return None

    def set(self, query, result):
        """Store the result for a given query in the cache."""
        if result is None:
            return

        try:
            # Convert result to JSON using custom encoder
            json_result = json.dumps(result, cls=PlexJSONEncoder)

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'REPLACE INTO cache (query, result) VALUES (?, ?)',
                    (query, json_result)
                )
                conn.commit()
        except (TypeError, json.JSONDecodeError) as e:
            # Log error but don't crash if serialization fails
            print(f"Cache serialization failed: {e}")
            return None

    def clear(self):
        """Clear all cached entries."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM cache')
            conn.commit()
