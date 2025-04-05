"""LLM integration for beets plugins."""

import json
import logging
import os
import re
from typing import Optional, Dict

from beets import config
from pydantic import BaseModel, Field, field_validator

# Simple logger for standalone use
logger = logging.getLogger('beets')

try:
    from phi.agent import Agent
    from phi.tools.tavily import TavilyTools
    from phi.tools.searxng import Searxng
    from phi.model.ollama import Ollama
except ImportError:
    logger.error("Required classes not found in phi. Ensure you have the correct version of phidata installed.")

PHI_AVAILABLE = True


# Add default configuration for LLM search
config['llm'].add({
    'search': {
        'provider': 'ollama',
        'model': 'qwen2.5:latest',
        'embedding_model': 'mxbai-embed-large',
        'ollama_host': 'http://localhost:11434',
        'tavily_api_key': '',
        'searxng_host': '',
    }
})


class SongBasicInfo(BaseModel):
    """Pydantic model for structured song information."""
    title: str = Field(..., description="The title of the song")
    artist: str = Field(..., description="The name of the artist or band")
    album: Optional[str] = Field(None, description="The album the song appears on")

    @field_validator('title', 'artist', 'album', mode='before')
    @classmethod
    def default_unknown(cls, v):
        if not v or not isinstance(v, str):
            return "Unknown"
        return v.strip() if v.strip() else "Unknown"


class MusicSearchTools:
    """Standalone class for music metadata search using multiple search engines."""

    def __init__(self, tavily_api_key=None, searxng_host=None, model_id=None, ollama_host=None):
        self.name = "music_search_tools"
        self.model_id = model_id
        self.ollama_host = ollama_host

        self.tavily_agent = Agent(tools=[TavilyTools(api_key=tavily_api_key)]) if tavily_api_key else None
        self.searxng_agent = Agent(
            model=Ollama(id=model_id, host=ollama_host),
            tools=[Searxng(host=searxng_host, fixed_max_results=5)]
        ) if searxng_host else None

        self.ollama_agent = Agent(
            model=Ollama(id=model_id, host=ollama_host),
            response_model=SongBasicInfo,
            structured_outputs=True
        )

    def _fetch_results_searxng(self, song_name: str) -> Optional[str]:
        """Query SearxNG for song information."""
        query = f"{song_name} song album, title, and artist"
        logger.debug(f"SearxNG querying: {query}")
        try:
            response = self.searxng_agent.run(query, timeout=10)
            return getattr(response, 'content', str(response))
        except Exception as e:
            logger.warning(f"SearxNG failed: {e}")
            return None

    def _fetch_results_tavily(self, song_name: str) -> Optional[str]:
        """Query Tavily for song information."""
        query = f"{song_name} song album, title, and artist"
        logger.debug(f"Tavily querying: {query}")
        try:
            tavily_tool = next((t for t in self.tavily_agent.tools if isinstance(t, TavilyTools)), None)
            response = tavily_tool.web_search_using_tavily(query)
            return str(response)
        except Exception as e:
            logger.warning(f"Tavily failed: {e}")
            return None

    def _get_search_results(self, song_name: str) -> Dict[str, str]:
        """Get search results from available search engines."""
        content = self._fetch_results_searxng(song_name) if self.searxng_agent else None
        if content:
            return {"source": "searxng", "content": content}

        content = self._fetch_results_tavily(song_name) if self.tavily_agent else None
        if content:
            return {"source": "tavily", "content": content}

        return {"source": "error", "content": f"No results for '{song_name}'"}

    def _extract_song_details(self, content: str, song_name: str) -> SongBasicInfo:
        """Extract structured song details from search results."""
        prompt = f"""
        From the text provided, clearly extract only:
        - Song Title
        - Artist Name
        - Album Name (if mentioned, else indicate unavailable)

        Source text:
        {content}
        """
        try:
            response = self.ollama_agent.run(prompt)
            return response.content
        except Exception as e:
            logger.error(f"Ollama extraction failed: {e}")
            return SongBasicInfo(title=song_name, artist="Unknown")

    def search_song_info(self, song_name: str) -> Dict:
        """Search for song information using available search engines."""
        search_results = self._get_search_results(song_name)

        if search_results["source"] == "error":
            return {
                "title": song_name,
                "artist": "Unknown",
                "album": None,
                "error": search_results["content"]
            }

        song_details = self._extract_song_details(search_results["content"], song_name).model_dump()
        song_details["search_source"] = search_results["source"]
        return song_details


def initialize_search_toolkit():
    """Initialize the music search toolkit with configuration from beets config."""
    if not PHI_AVAILABLE:
        logger.error("Phi package not available. Please install with: pip install phidata")
        return None

    # Get configuration from beets config
    tavily_api_key = config["llm"]["search"]["tavily_api_key"].get()
    searxng_host = config["llm"]["search"]["searxng_host"].get()
    model_id = config["llm"]["search"]["model"].get() or "qwen2.5:latest"
    ollama_host = config["llm"]["search"]["ollama_host"].get() or "http://localhost:11434"

    if not tavily_api_key and not searxng_host:
        logger.warning("Neither Tavily API key nor SearxNG host configured. Search functionality limited.")

    try:
        return MusicSearchTools(
            tavily_api_key=tavily_api_key,
            searxng_host=searxng_host,
            model_id=model_id,
            ollama_host=ollama_host
        )
    except Exception as e:
        logger.error(f"Failed to initialize search toolkit: {e}")
        return None


# Singleton toolkit instance
_search_toolkit = None


def get_search_toolkit():
    """Get or initialize the search toolkit singleton."""
    global _search_toolkit
    if (_search_toolkit is None):
        _search_toolkit = initialize_search_toolkit()
    return _search_toolkit


def search_track_info(query: str):
    """
    Sends a search query to get structured track information using phidata agent.

    Args:
        query (str): The user-provided search query for a song.

    Returns:
        dict: A dictionary containing the track's title, album, and artist, with missing fields set to None.
    """
    toolkit = get_search_toolkit()

    if not toolkit:
        logger.error("Search toolkit unavailable. Install phidata and configure search engines.")
        return {"title": query, "artist": "Unknown", "album": None}

    try:
        logger.info(f"Searching for track info: {query}")
        song_info = toolkit.search_song_info(query)

        # Format response to match expected structure
        result = {
            "title": song_info.get("title"),
            "album": song_info.get("album"),
            "artist": song_info.get("artist")
        }

        logger.info(f"Found track info: {result}")
        return result
    except Exception as e:
        logger.error(f"Error in agent-based search: {e}")
        return {"title": query, "artist": "Unknown", "album": None}
