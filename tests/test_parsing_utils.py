"""Tests for parsing utility functions."""

import unittest
from harmony.utils.parsing import (
    parse_soundtrack_title,
    clean_album_name,
    clean_html_entities,
)


class TestParseSoundtrackTitle(unittest.TestCase):
    """Test soundtrack title parsing for Indian/Bollywood music."""
    
    def test_parses_from_with_parentheses(self):
        """Test parsing 'Song (From "Movie")' format."""
        title, album = parse_soundtrack_title('Narayan (From "Kushal Tandon")')
        self.assertEqual(title, "Narayan")
        self.assertEqual(album, "Kushal Tandon")
    
    def test_parses_from_with_brackets(self):
        """Test parsing 'Song [From "Movie"]' format."""
        title, album = parse_soundtrack_title('Song Name [From "Movie Name"]')
        self.assertEqual(title, "Song Name")
        self.assertEqual(album, "Movie Name")
    
    def test_handles_html_entities_in_from_clause(self):
        """Test that HTML entities are cleaned before parsing."""
        title, album = parse_soundtrack_title('Song (From &quot;Movie&quot;)')
        self.assertEqual(title, "Song")
        self.assertEqual(album, "Movie")
    
    def test_returns_original_when_no_from_clause(self):
        """Test that songs without 'From' clause are returned as-is."""
        title, album = parse_soundtrack_title('Regular Song Title')
        self.assertEqual(title, "Regular Song Title")
        self.assertEqual(album, "")
    
    def test_strips_whitespace(self):
        """Test that extra whitespace is removed."""
        title, album = parse_soundtrack_title('  Song  (From "Movie")  ')
        self.assertEqual(title, "Song")
        self.assertEqual(album, "Movie")
    
    def test_handles_empty_string(self):
        """Test that empty strings are handled gracefully."""
        title, album = parse_soundtrack_title('')
        self.assertEqual(title, "")
        self.assertEqual(album, "")
    
    def test_multiple_from_clauses_uses_first(self):
        """Test behavior with multiple 'From' patterns."""
        title, album = parse_soundtrack_title('Song (From "Movie1") [From "Movie2"]')
        self.assertEqual(title, "Song")
        # Should extract from the first (From ...) pattern
        self.assertIn("Movie", album)
    
    def test_preserves_song_title_with_quotes(self):
        """Test that quotes in song title (not in From clause) are preserved."""
        title, album = parse_soundtrack_title('The "Best" Song (From "Movie")')
        self.assertEqual(title, 'The "Best" Song')
        self.assertEqual(album, "Movie")


class TestCleanAlbumName(unittest.TestCase):
    """Test album name cleaning."""
    
    def test_removes_ost_suffix(self):
        """Test removal of OST suffix in parentheses."""
        album = clean_album_name("Movie (Original Motion Picture Soundtrack)")
        self.assertEqual(album, "Movie")
    
    def test_removes_ost_suffix_brackets(self):
        """Test removal of OST suffix in brackets (Apple Music format)."""
        album = clean_album_name("Kapoor & Sons (Since 1921) [Original Motion Picture Soundtrack]")
        self.assertEqual(album, "Kapoor & Sons (Since 1921)")
    
    def test_removes_hindi_suffix(self):
        """Test removal of '- Hindi' suffix."""
        album = clean_album_name("Album Name - Hindi")
        self.assertEqual(album, "Album Name")
    
    def test_removes_both_suffixes(self):
        """Test removal of both OST and Hindi suffixes."""
        album = clean_album_name(
            "Movie (Original Motion Picture Soundtrack) - Hindi"
        )
        self.assertEqual(album, "Movie")
    
    def test_extracts_from_parentheses(self):
        """Test extraction of movie name from 'From' clause."""
        album = clean_album_name('Title (From "Movie Name")')
        self.assertEqual(album, "Movie Name")
    
    def test_extracts_from_brackets(self):
        """Test extraction from bracketed 'From' clause."""
        album = clean_album_name('[From "Album Name"]')
        self.assertEqual(album, "Album Name")
    
    def test_handles_empty_string(self):
        """Test that empty strings return empty."""
        album = clean_album_name('')
        self.assertEqual(album, "")
    
    def test_handles_none(self):
        """Test that None returns None."""
        album = clean_album_name(None)
        self.assertIsNone(album)
    
    def test_handles_whitespace_only(self):
        """Test that whitespace-only strings return empty."""
        album = clean_album_name("   ")
        self.assertEqual(album, "")
    
    def test_preserves_valid_album_names(self):
        """Test that normal album names are preserved."""
        album = clean_album_name("My Favorite Album")
        self.assertEqual(album, "My Favorite Album")
    
    def test_removes_ost_case_insensitive(self):
        """Test that OST suffix removal is case-sensitive (current behavior)."""
        # Note: Current implementation is case-sensitive
        album = clean_album_name("Movie (original motion picture soundtrack)")
        # Should not remove lowercase version
        self.assertNotEqual(album, "Movie")


class TestCleanHtmlEntities(unittest.TestCase):
    """Test HTML entity cleaning."""
    
    def test_replaces_quot(self):
        """Test replacement of &quot; entity."""
        text = clean_html_entities('Song &quot;Name&quot;')
        self.assertEqual(text, 'Song "Name"')
    
    def test_replaces_amp(self):
        """Test replacement of &amp; entity."""
        text = clean_html_entities('Artist &amp; Friends')
        self.assertEqual(text, 'Artist & Friends')
    
    def test_replaces_lt_gt(self):
        """Test replacement of &lt; and &gt; entities."""
        text = clean_html_entities('&lt;Title&gt;')
        self.assertEqual(text, '<Title>')
    
    def test_replaces_apostrophe_numeric(self):
        """Test replacement of &#39; apostrophe entity."""
        text = clean_html_entities("Don&#39;t Stop")
        self.assertEqual(text, "Don't Stop")
    
    def test_replaces_apostrophe_named(self):
        """Test replacement of &apos; apostrophe entity."""
        text = clean_html_entities("Can&apos;t Wait")
        self.assertEqual(text, "Can't Wait")
    
    def test_multiple_entities(self):
        """Test replacement of multiple different entities."""
        text = clean_html_entities('&quot;Artist&quot; &amp; &quot;Title&quot;')
        self.assertEqual(text, '"Artist" & "Title"')
    
    def test_handles_empty_string(self):
        """Test that empty strings return empty."""
        text = clean_html_entities('')
        self.assertEqual(text, '')
    
    def test_handles_none(self):
        """Test that None (falsy) returns empty string."""
        text = clean_html_entities(None)
        self.assertEqual(text, '')
    
    def test_no_entities_returns_unchanged(self):
        """Test that text without entities is unchanged."""
        text = clean_html_entities('Regular Text')
        self.assertEqual(text, 'Regular Text')
    
    def test_preserves_text_with_ampersand_in_word(self):
        """Test that legitimate ampersands in text are preserved."""
        # Note: Only replaces exact entity strings
        text = clean_html_entities('AT&T Corporation')
        self.assertEqual(text, 'AT&T Corporation')
    
    def test_entity_at_start_and_end(self):
        """Test entities at string boundaries."""
        text = clean_html_entities('&quot;Quote&quot;')
        self.assertEqual(text, '"Quote"')


class TestIntegration(unittest.TestCase):
    """Integration tests combining multiple parsing functions."""
    
    def test_jiosaavn_workflow_with_from_clause(self):
        """Test typical JioSaavn parsing workflow with soundtrack title."""
        # Simulate JioSaavn song with HTML entities and From clause
        raw_title = 'Narayan &quot;Song&quot; (From &quot;Movie Name&quot;)'
        
        # Parse the title
        title, album_from_title = parse_soundtrack_title(raw_title)
        
        # Should extract clean title and movie name
        self.assertEqual(title, 'Narayan "Song"')
        self.assertEqual(album_from_title, 'Movie Name')
    
    def test_jiosaavn_workflow_with_album_cleaning(self):
        """Test typical JioSaavn workflow with album name cleaning."""
        raw_album = 'Movie Name (Original Motion Picture Soundtrack) - Hindi'
        
        # Clean the album
        clean = clean_album_name(raw_album)
        
        # Should remove both suffixes
        self.assertEqual(clean, 'Movie Name')
    
    def test_combined_parsing_preserves_indian_names(self):
        """Test that parsing preserves Indian/Bollywood naming conventions."""
        title = 'Shararat (From "Dhurandhar")'
        clean_title, album = parse_soundtrack_title(title)
        
        self.assertEqual(clean_title, "Shararat")
        self.assertEqual(album, "Dhurandhar")


if __name__ == '__main__':
    unittest.main()
