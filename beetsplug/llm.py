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

# Track available dependencies
PHI_AVAILABLE = False
TAVILY_AVAILABLE = False
SEARXNG_AVAILABLE = False
EXA_AVAILABLE = False

try:
    from phi.agent import Agent
    from phi.model.ollama import Ollama
    PHI_AVAILABLE = True

    # Check for individual search providers
    try:
        from phi.tools.tavily import TavilyTools
        TAVILY_AVAILABLE = True
    except ImportError:
        logger.debug("Tavily tools not available")

    try:
        from phi.tools.searxng import Searxng
        SEARXNG_AVAILABLE = True
    except ImportError:
        logger.debug("SearxNG tools not available")

    try:
        from phi.tools.exa import ExaTools
        EXA_AVAILABLE = True
    except ImportError:
        logger.debug("Exa tools not available")

except ImportError:
    logger.error("Phi package not available. Please install with: pip install phidata")

# Add default configuration for LLM search
config['llm'].add({
    'search': {
        'provider': 'ollama',
        'model': 'qwen2.5:latest',
        'ollama_host': 'http://localhost:11434',
        'tavily_api_key': '',
        'searxng_host': '',
        'exa_api_key': '',
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

    def __init__(self, tavily_api_key=None, searxng_host=None, model_id=None, ollama_host=None, exa_api_key=None):
        self.name = "music_search_tools"
        self.model_id = model_id
        self.ollama_host = ollama_host

        # Initialize search agents only if dependencies are available
        self.tavily_agent = None
        if tavily_api_key and TAVILY_AVAILABLE:
            try:
                self.tavily_agent = Agent(tools=[TavilyTools(
                    api_key=tavily_api_key,
                    include_answer=True,
                    search_depth="advanced",
                    format="json"
                )])
            except Exception as e:
                logger.warning(f"Failed to initialize Tavily agent: {e}")

        self.searxng_agent = None
        if searxng_host and SEARXNG_AVAILABLE:
            try:
                self.searxng_agent = Agent(
                    model=Ollama(id=model_id, host=ollama_host),
                    tools=[Searxng(host=searxng_host, fixed_max_results=5)]
                )
            except Exception as e:
                logger.warning(f"Failed to initialize SearxNG agent: {e}")

        # Initialize Exa search if API key is provided and dependency is available
        self.exa_agent = None
        if exa_api_key and EXA_AVAILABLE:
            try:
                self.exa_agent = Agent(
                    model=Ollama(id=model_id, host=ollama_host),
                    tools=[ExaTools(api_key=exa_api_key)]
                )
            except Exception as e:
                logger.warning(f"Failed to initialize Exa agent: {e}")

        # Always initialize the Ollama agent for extraction
        try:
            self.ollama_agent = Agent(
                model=Ollama(id=model_id, host=ollama_host),
                response_model=SongBasicInfo,
                structured_outputs=True
            )
        except Exception as e:
            logger.error(f"Failed to initialize Ollama agent: {e}")
            self.ollama_agent = None

    def _fetch_results_searxng(self, song_name: str) -> Optional[str]:
        """Query SearxNG for song information."""
        query = f"{song_name} song album, title, and artist"
        logger.debug(f"SearxNG querying: {query}")
        try:
            response = self.searxng_agent.run(query, timeout=20)
            return getattr(response, 'content', str(response))
        except Exception as e:
            logger.warning(f"SearxNG failed: {e}")
            return None

    def _fetch_results_tavily(self, song_name: str) -> Optional[Dict]:
        """Query Tavily for song information."""
        query = f"{song_name} song album, title, and artist"
        logger.info(f"Tavily querying: {query}")
        try:
            tavily_tool = next((t for t in self.tavily_agent.tools if isinstance(t, TavilyTools)), None)
            response = tavily_tool.web_search_using_tavily(query)

            # Check if response is a string (JSON string) and parse it
            if isinstance(response, str):
                try:
                    response = json.loads(response)
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse Tavily JSON response: {e}")
                    return {"results": [], "content": response}

            # If we have an AI-generated answer, just return that directly
            if "answer" in response:
                ai_answer = response["answer"]
                logger.info(f"\n🤖 AI Generated Answer from Tavily: {ai_answer[:100]}...")
                return {"ai_answer": ai_answer}

            # Otherwise return the full response
            return response
        except Exception as e:
            logger.warning(f"Tavily failed: {e}")
            return None

    def _tavily_search(self, query: str) -> str:
        """Helper method to perform the actual Tavily search."""
        tavily_tool = next((t for t in self.tavily_agent.tools if isinstance(t, TavilyTools)), None)
        response = tavily_tool.web_search_using_tavily(query)
        return str(response)

    def _fetch_results_exa(self, song_name: str) -> Optional[str]:
        """Query Exa for song information."""
        query = f"{song_name} song album, title, and artist"
        logger.debug(f"Exa querying: {query}")
        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(self._exa_search, query)
                return future.result(timeout=15)  # 15 second timeout
        except concurrent.futures.TimeoutError:
            logger.warning(f"Exa search timed out for: {query}")
            return None
        except Exception as e:
            logger.warning(f"Exa search failed: {e}")
            return None

    def _exa_search(self, query: str) -> str:
        """Helper method to perform the actual Exa search."""
        # Get the ExaTools instance from the agent
        exa_tool = next((t for t in self.exa_agent.tools if isinstance(t, ExaTools)), None)
        if not exa_tool:
            logger.warning("Exa tool not found in agent tools")
            return None

        # Use search_exa
        response = exa_tool.search_exa(query)
        return str(response)

    def _get_search_results(self, song_name: str) -> Dict[str, str]:
        """Get search results from available search engines."""

        # Try SearxNG first if available
        if self.searxng_agent:
            content = self._fetch_results_searxng(song_name)
            if (content):
                return {"source": "searxng", "content": content}

        # Then try Exa
        if self.exa_agent:
            content = self._fetch_results_exa(song_name)
            if (content):
                return {"source": "exa", "content": content}

        # Finally try Tavily
        if self.tavily_agent:
            response = self._fetch_results_tavily(song_name)
            if response:
                # If Tavily returned an AI-generated answer, use it directly
                if "ai_answer" in response:
                    return {"source": "tavily_ai", "content": response["ai_answer"]}

                # Otherwise, format the search results for processing
                if isinstance(response, dict) and "results" in response:
                    content = json.dumps(response["results"])
                    return {"source": "tavily", "content": content}

                return {"source": "tavily", "content": str(response)}

        return {"source": "error", "content": f"No results for '{song_name}'"}

    def _extract_song_details(self, content: str, song_name: str) -> SongBasicInfo:
        """Extract structured song details from search results."""
        prompt = f"""
        <instruction>
        Based on the search results below, extract specific information about the song "{song_name}".

        Return ONLY these fields in a structured JSON format:
        - Song Title: The exact title of the song (not an album or artist name)
        - Artist Name: The primary artist or band who performed the song
        - Album Name: The album that contains this song (if mentioned)

        If any information is not clearly stated in the search results, use the most likely value based on available context.
        If you cannot determine a value with reasonable confidence, respond with "Unknown" for that field.

        Format your response as valid JSON with these exact keys:
        {{
            "title": "The song title",
            "artist": "The artist name",
            "album": "The album name"
        }}
        </instruction>

        <search_results>
        {content}
        </search_results>
        """

        # Debug log to show what's being sent to Ollama - using Beets logger format
        logger.debug("Sending to Ollama for parsing - Song: {0}", song_name)
        logger.debug("Content source length: {0} characters", len(content))
        # Use a safe substring approach for the content preview
        content_preview = content[:1000] if len(content) > 1000 else content
        logger.debug("First chars of content: {0}...", content_preview)

        try:
            response = self.ollama_agent.run(prompt)
            return response.content
        except Exception as e:
            logger.error(f"Ollama extraction failed: {e}")
            return SongBasicInfo(title=song_name, artist="Unknown")

    def search_song_info(self, song_name: str) -> Dict:
        """Search for song information using available search engines."""
        search_results = self._get_search_results(song_name)

        if (search_results["source"] == "error"):
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
    exa_api_key = config["llm"]["search"]["exa_api_key"].get()

    if not tavily_api_key and not searxng_host and not exa_api_key:
        logger.warning("No search providers configured. Search functionality limited.")

    try:
        return MusicSearchTools(
            tavily_api_key=tavily_api_key,
            searxng_host=searxng_host,
            model_id=model_id,
            ollama_host=ollama_host,
            exa_api_key=exa_api_key
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

        logger.info("Found track info: {}", result)
        return result
    except Exception as e:
        logger.error(f"Error in agent-based search: {e}")
        return {"title": query, "artist": "Unknown", "album": None}
