"""Unit tests for Apple Music provider with parsing utilities."""

import json
import unittest
from unittest.mock import Mock, patch

from harmony.providers.apple import import_apple_playlist


class TestAppleMusicProvider(unittest.TestCase):
    """Test Apple Music playlist import with parsing enhancements."""
    
    def test_parses_from_clauses_in_titles(self):
        """Test that titles with 'From' clauses are properly parsed."""
        mock_html = '''
        <html>
        <script id="serialized-server-data">
        [{"data": {"sections": [{}, {
            "items": [
                {
                    "title": "Chaleya (From \\"Jawan\\")",
                    "subtitleLinks": [{"title": "Anirudh Ravichander"}],
                    "tertiaryLinks": [{"title": "Chaleya (From \\"Jawan\\") - Single"}]
                }
            ]
        }]}}]
        </script>
        </html>
        '''
        
        with patch('harmony.providers.apple.requests.get') as mock_get:
            mock_response = Mock()
            mock_response.text = mock_html
            mock_response.raise_for_status = Mock()
            mock_get.return_value = mock_response
            
            songs = import_apple_playlist('https://music.apple.com/playlist/test/pl.test123')
            
            self.assertEqual(len(songs), 1)
            self.assertEqual(songs[0]['title'], 'Chaleya')
            self.assertEqual(songs[0]['album'], 'Jawan')
            self.assertEqual(songs[0]['artist'], 'Anirudh Ravichander')
    
    def test_removes_ost_suffix_from_albums(self):
        """Test that OST suffixes are removed from albums."""
        mock_html = '''
        <html>
        <script id="serialized-server-data">
        [{"data": {"sections": [{}, {
            "items": [
                {
                    "title": "Let's Nacho",
                    "subtitleLinks": [{"title": "Nucleya"}],
                    "tertiaryLinks": [{"title": "Kapoor & Sons (Since 1921) [Original Motion Picture Soundtrack]"}]
                }
            ]
        }]}}]
        </script>
        </html>
        '''
        
        with patch('harmony.providers.apple.requests.get') as mock_get:
            mock_response = Mock()
            mock_response.text = mock_html
            mock_response.raise_for_status = Mock()
            mock_get.return_value = mock_response
            
            songs = import_apple_playlist('https://music.apple.com/playlist/test/pl.test123')
            
            self.assertEqual(len(songs), 1)
            self.assertEqual(songs[0]['title'], "Let's Nacho")
            self.assertEqual(songs[0]['album'], 'Kapoor & Sons (Since 1921)')
            self.assertEqual(songs[0]['artist'], 'Nucleya')
    
    def test_cleans_html_entities(self):
        """Test that HTML entities are properly decoded."""
        mock_html = '''
        <html>
        <script id="serialized-server-data">
        [{"data": {"sections": [{}, {
            "items": [
                {
                    "title": "Song &quot;Title&quot;",
                    "subtitleLinks": [{"title": "Artist &amp; Friends"}],
                    "tertiaryLinks": [{"title": "Album Name"}]
                }
            ]
        }]}}]
        </script>
        </html>
        '''
        
        with patch('harmony.providers.apple.requests.get') as mock_get:
            mock_response = Mock()
            mock_response.text = mock_html
            mock_response.raise_for_status = Mock()
            mock_get.return_value = mock_response
            
            songs = import_apple_playlist('https://music.apple.com/playlist/test/pl.test123')
            
            self.assertEqual(len(songs), 1)
            self.assertEqual(songs[0]['title'], 'Song "Title"')
            self.assertEqual(songs[0]['artist'], 'Artist & Friends')
    
    def test_combined_parsing_workflow(self):
        """Test realistic Apple Music data with multiple parsing needs."""
        # Note: In real Apple Music JSON, &quot; in the JSON string becomes " after json.loads()
        mock_html = '''
        <html>
        <script id="serialized-server-data">
        [{"data": {"sections": [{}, {
            "items": [
                {
                    "title": "Mere Mehboob Mere Sanam (From \\"Bad Newz\\")",
                    "subtitleLinks": [{"title": "Udit Narayan"}],
                    "tertiaryLinks": [{"title": "Mere Mehboob Mere Sanam (From \\"Bad Newz\\") - Single"}]
                },
                {
                    "title": "Regular Song",
                    "subtitleLinks": [{"title": "Regular Artist"}],
                    "tertiaryLinks": [{"title": "Regular Album (Original Motion Picture Soundtrack)"}]
                }
            ]
        }]}}]
        </script>
        </html>
        '''
        
        with patch('harmony.providers.apple.requests.get') as mock_get:
            mock_response = Mock()
            mock_response.text = mock_html
            mock_response.raise_for_status = Mock()
            mock_get.return_value = mock_response
            
            songs = import_apple_playlist('https://music.apple.com/playlist/test/pl.test123')
            
            self.assertEqual(len(songs), 2)
            
            # First song: From clause extracted
            self.assertEqual(songs[0]['title'], 'Mere Mehboob Mere Sanam')
            self.assertEqual(songs[0]['album'], 'Bad Newz')
            self.assertEqual(songs[0]['artist'], 'Udit Narayan')
            
            # Second song: OST suffix removal
            self.assertEqual(songs[1]['title'], 'Regular Song')
            self.assertEqual(songs[1]['album'], 'Regular Album')
            self.assertEqual(songs[1]['artist'], 'Regular Artist')
    
    def test_handles_invalid_json_gracefully(self):
        """Test that invalid JSON is handled without crashing."""
        mock_html = '''
        <html>
        <script id="serialized-server-data">
        INVALID JSON
        </script>
        </html>
        '''
        
        with patch('harmony.providers.apple.requests.get') as mock_get:
            mock_response = Mock()
            mock_response.text = mock_html
            mock_response.raise_for_status = Mock()
            mock_get.return_value = mock_response
            
            songs = import_apple_playlist('https://music.apple.com/playlist/test/pl.test123')
            
            self.assertEqual(len(songs), 0)
    
    def test_uses_cache_when_available(self):
        """Test that cached data is returned when available."""
        mock_cache = Mock()
        mock_cache.get_playlist_cache.return_value = [
            {'title': 'Cached Song', 'artist': 'Cached Artist', 'album': 'Cached Album'}
        ]
        
        with patch('harmony.providers.apple.requests.get') as mock_get:
            songs = import_apple_playlist(
                'https://music.apple.com/playlist/test/pl.test123',
                cache=mock_cache
            )
            
            # Should not make HTTP request when cache hits
            mock_get.assert_not_called()
            
            # Should return cached data
            self.assertEqual(len(songs), 1)
            self.assertEqual(songs[0]['title'], 'Cached Song')
    
    def test_caches_results_after_import(self):
        """Test that results are cached after successful import."""
        mock_html = '''
        <html>
        <script id="serialized-server-data">
        [{"data": {"sections": [{}, {
            "items": [
                {
                    "title": "Test Song",
                    "subtitleLinks": [{"title": "Test Artist"}],
                    "tertiaryLinks": [{"title": "Test Album"}]
                }
            ]
        }]}}]
        </script>
        </html>
        '''
        
        mock_cache = Mock()
        mock_cache.get_playlist_cache.return_value = None
        
        with patch('harmony.providers.apple.requests.get') as mock_get:
            mock_response = Mock()
            mock_response.text = mock_html
            mock_response.raise_for_status = Mock()
            mock_get.return_value = mock_response
            
            songs = import_apple_playlist(
                'https://music.apple.com/playlist/test/pl.test123',
                cache=mock_cache
            )
            
            # Should cache the results
            mock_cache.set_playlist_cache.assert_called_once()
            call_args = mock_cache.set_playlist_cache.call_args
            self.assertEqual(call_args[0][0], 'pl.test123')
            self.assertEqual(call_args[0][1], 'apple')
            self.assertEqual(len(call_args[0][2]), 1)


if __name__ == '__main__':
    unittest.main()
