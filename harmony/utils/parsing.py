"""Shared parsing utilities for playlist providers."""

import re
from typing import Optional, Tuple


def parse_soundtrack_title(title: str) -> Tuple[str, str]:
    """Parse soundtrack-style titles into title and album.
    
    Handles Bollywood/Indian music format where song titles often include
    the movie name: 'Song Name (From "Movie Name")'
    
    Args:
        title: Original title string
    
    Returns:
        tuple: (cleaned_title, album_from_movie)
        
    Examples:
        >>> parse_soundtrack_title('Narayan (From "Kushal Tandon")')
        ('Narayan', 'Kushal Tandon')
        
        >>> parse_soundtrack_title('Song [From "Movie"]')
        ('Song', 'Movie')
        
        >>> parse_soundtrack_title('Regular Song')
        ('Regular Song', '')
    """
    title = clean_html_entities(title)
    
    # Match (From "Movie") or [From "Movie"] pattern
    paren_match = re.search(r'\(From\s+"([^"]+)"\)', title)
    bracket_match = re.search(r'\[From\s+"([^"]+)"\]', title)
    
    album = ""
    if paren_match:
        album = paren_match.group(1)
    elif bracket_match:
        album = bracket_match.group(1)
    
    # Remove all From clauses (both parentheses and brackets)
    clean_title = re.sub(r'\s*[\(\[]From\s+"[^"]+?"[\)\]]', "", title)
    
    return clean_title.strip(), album.strip()


def clean_album_name(album: Optional[str]) -> Optional[str]:
    """Clean album name by removing common Bollywood/Indian music suffixes.
    
    Removes common suffixes and extracts movie names from parenthetical
    or bracketed "From..." clauses.
    
    Args:
        album: Original album name (can be None)
        
    Returns:
        Cleaned album name, or None if input was None
        
    Examples:
        >>> clean_album_name("Movie (Original Motion Picture Soundtrack)")
        'Movie'
        
        >>> clean_album_name("Album Name - Hindi")
        'Album Name'
        
        >>> clean_album_name('Title (From "Movie Name")')
        'Movie Name'
        
        >>> clean_album_name(None)
        None
    """
    if album is None:
        return None
    
    if not album:
        return ""
    
    # Remove common suffixes (both parentheses and brackets)
    album = album.replace("(Original Motion Picture Soundtrack)", "")
    album = album.replace("[Original Motion Picture Soundtrack]", "")
    album = album.replace("- Hindi", "")
    album = album.strip()
    
    # Extract movie name from "From..." clauses
    paren_match = re.search(r'\(From\s+"([^"]+)"\)', album)
    bracket_match = re.search(r'\[From\s+"([^"]+)"\]', album)
    
    if paren_match:
        return paren_match.group(1).strip()
    elif bracket_match:
        return bracket_match.group(1).strip()
    
    return album.strip()


def clean_html_entities(text: str) -> str:
    """Clean HTML entities to proper characters.
    
    Handles common HTML entities that appear in playlist metadata,
    particularly from web scraping or API responses.
    
    Args:
        text: String potentially containing HTML entities
        
    Returns:
        String with entities replaced by actual characters
        
    Examples:
        >>> clean_html_entities('Song &quot;Name&quot;')
        'Song "Name"'
        
        >>> clean_html_entities('Artist &amp; Friends')
        'Artist & Friends'
        
        >>> clean_html_entities('')
        ''
    """
    if not text:
        return ""
    
    replacements = {
        '&quot;': '"',
        '&amp;': '&',
        '&lt;': '<',
        '&gt;': '>',
        '&#39;': "'",
        '&apos;': "'",
    }
    
    for entity, char in replacements.items():
        text = text.replace(entity, char)
    
    return text
