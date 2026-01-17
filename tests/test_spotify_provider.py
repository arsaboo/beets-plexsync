"""Unit tests for Spotify provider with parsing utilities."""

import unittest
from unittest.mock import Mock, patch
from harmony.providers.spotify import (
    import_spotify_playlist,
    extract_playlist_id,
    _extract_tracks_from_json
)


class TestSpotifyProvider(unittest.TestCase):
    """Test Spotify provider functionality."""

    def test_extract_playlist_id_standard(self):
        """Test extracting playlist ID from standard URL."""
        url = "https://open.spotify.com/playlist/7qxn6GsFH77ghVtKzOnAYA"
        playlist_id = extract_playlist_id(url)
        self.assertEqual(playlist_id, "7qxn6GsFH77ghVtKzOnAYA")

    def test_extract_playlist_id_with_params(self):
        """Test extracting playlist ID from URL with query parameters."""
        url = "https://open.spotify.com/playlist/7qxn6GsFH77ghVtKzOnAYA?si=abc123"
        playlist_id = extract_playlist_id(url)
        self.assertEqual(playlist_id, "7qxn6GsFH77ghVtKzOnAYA")

    def test_extract_playlist_id_invalid(self):
        """Test extracting playlist ID from invalid URL."""
        url = "https://example.com/not-a-spotify-url"
        playlist_id = extract_playlist_id(url)
        self.assertIsNone(playlist_id)

    def test_parsing_uses_utilities(self):
        """Test that parsing utilities are imported and available."""
        # This test verifies the module has the right imports
        from harmony.providers import spotify
        
        # Check that parsing utilities are imported
        self.assertTrue(hasattr(spotify, 'parse_soundtrack_title'))
        self.assertTrue(hasattr(spotify, 'clean_album_name'))
        self.assertTrue(hasattr(spotify, 'clean_html_entities'))

    def test_extract_tracks_from_json(self):
        """Test extracting tracks from JSON structure."""
        data = {
            "tracks": [
                {
                    "track": {
                        "name": "Test Song (From \"Test Movie\")",
                        "artists": [{"name": "Test Artist"}],
                        "album": {"name": "Test Album (Original Motion Picture Soundtrack)"}
                    }
                }
            ]
        }
        
        tracks = _extract_tracks_from_json(data)
        
        self.assertEqual(len(tracks), 1)
        # Parsing utilities should be applied
        self.assertEqual(tracks[0]["title"], "Test Song")
        self.assertEqual(tracks[0]["artist"], "Test Artist")
        self.assertEqual(tracks[0]["album"], "Test Album")


if __name__ == "__main__":
    unittest.main()
