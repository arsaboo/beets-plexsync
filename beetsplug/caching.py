import sqlite3
import json
import os

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
                    result TEXT
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
                return json.loads(row[0])
            return None

    def set(self, query, result):
        """Store the result for a given query in the cache."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # Convert result to dictionary if it's not serializable
            if not isinstance(result, (dict, list, str, int, float, bool, type(None))):
                result = result.__dict__
            cursor.execute('REPLACE INTO cache (query, result) VALUES (?, ?)', (query, json.dumps(result)))
            conn.commit()
