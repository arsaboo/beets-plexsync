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
AGNO_AVAILABLE = False
TAVILY_AVAILABLE = False
SEARXNG_AVAILABLE = False
EXA_AVAILABLE = False

try:
    from agno.agent import Agent
    from agno.models.ollama import Ollama
    AGNO_AVAILABLE = True

    # Check for individual search providers
    try:
        from agno.tools.tavily import TavilyTools
        TAVILY_AVAILABLE = True
    except ImportError:
        logger.debug("Tavily tools not available")

    try:
        from agno.tools.searxng import Searxng
        SEARXNG_AVAILABLE = True
    except ImportError:
        logger.debug("SearxNG tools not available")

    try:
        from agno.tools.exa import ExaTools
        EXA_AVAILABLE = True
    except ImportError:
        logger.debug("Exa tools not available")

except ImportError:
    logger.error("Agno package not available. Please install with: pip install agno")

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
        if not v or not isinstance(v, str) or not v.strip():
            return None  # Return None instead of "Unknown"
        return v.strip()


class MusicSearchTools:
    """Standalone class for music metadata search using multiple search engines."""

    def __init__(self, tavily_api_key=None, searxng_host=None, model_id=None, ollama_host=None, exa_api_key=None):
        """Initialize music search tools with available search providers.

        Args:
            tavily_api_key: API key for Tavily search
            searxng_host: Host URL for SearxNG instance
            model_id: Ollama model ID to use
            ollama_host: Ollama API host URL
            exa_api_key: API key for Exa search
        """
        self.name = "music_search_tools"
        self.model_id = model_id or "qwen2.5:latest"
        self.ollama_host = ollama_host or "http://localhost:11434"

        # Initialize Ollama agent for extraction (required for all search methods)
        self._init_ollama_agent()

        # Initialize optional search providers
        self.tavily_agent = self._init_tavily_agent(tavily_api_key) if tavily_api_key and TAVILY_AVAILABLE else None
        self.searxng_agent = self._init_searxng_agent(searxng_host) if searxng_host and SEARXNG_AVAILABLE else None
        self.exa_agent = self._init_exa_agent(exa_api_key) if exa_api_key and EXA_AVAILABLE else None

        # Log available search providers
        self._log_available_providers()

    def _init_ollama_agent(self) -> None:
        """Initialize the Ollama agent for text extraction."""
        try:
            self.ollama_agent = Agent(
                model=Ollama(id=self.model_id, host=self.ollama_host),
                response_model=SongBasicInfo,
                reasoning=True,
                structured_outputs=True
            )
        except Exception as e:
            logger.error(f"Failed to initialize Ollama agent: {e}")
            self.ollama_agent = None

    def _init_tavily_agent(self, api_key: str) -> Optional[Agent]:
        """Initialize the Tavily search agent.

        Args:
            api_key: Tavily API key

        Returns:
            Configured Tavily agent or None if initialization fails
        """
        try:
            return Agent(tools=[TavilyTools(
                api_key=api_key,
                include_answer=True,
                search_depth="advanced",
                format="json"
            )])
        except Exception as e:
            logger.warning(f"Failed to initialize Tavily agent: {e}")
            return None

    def _init_searxng_agent(self, host: str) -> Optional[Agent]:
        """Initialize the SearxNG search agent.

        Args:
            host: SearxNG host URL

        Returns:
            Configured SearxNG agent or None if initialization fails
        """
        try:
            return Agent(
                model=Ollama(id=self.model_id, host=self.ollama_host),
                tools=[Searxng(host=host, fixed_max_results=5)]
            )
        except Exception as e:
            logger.warning(f"Failed to initialize SearxNG agent: {e}")
            return None

    def _init_exa_agent(self, api_key: str) -> Optional[Agent]:
        """Initialize the Exa search agent.

        Args:
            api_key: Exa API key

        Returns:
            Configured Exa agent or None if initialization fails
        """
        try:
            return Agent(
                model=Ollama(id=self.model_id, host=self.ollama_host),
                tools=[ExaTools(api_key=api_key)]
            )
        except Exception as e:
            logger.warning(f"Failed to initialize Exa agent: {e}")
            return None

    def _log_available_providers(self) -> None:
        """Log which search providers are available."""
        providers = []
        if self.tavily_agent:
            providers.append("Tavily")
        if self.searxng_agent:
            providers.append("SearxNG")
        if self.exa_agent:
            providers.append("Exa")

        if providers:
            logger.info(f"Initialized music search with providers: {', '.join(providers)}")
            if self.ollama_agent:
                logger.info(f"Using Ollama model '{self.model_id}' for result extraction")
        else:
            logger.warning("No music search providers available!")
            if self.ollama_agent:
                logger.info("Only Ollama extraction is available, which requires at least one search provider")

    def _fetch_results_searxng(self, song_name: str) -> Optional[str]:
        """Query SearxNG for song information.

        Args:
            song_name: The song name to search for

        Returns:
            String containing search results or None if search failed
        """
        query = f"{song_name} song album, title, and artist"
        logger.debug(f"SearxNG querying: {query}")
        try:
            response = self.searxng_agent.run(query, timeout=20)
            return getattr(response, 'content', str(response))
        except Exception as e:
            logger.warning(f"SearxNG failed: {e}")
            return None

    def _fetch_results_tavily(self, song_name: str) -> Optional[Dict]:
        """Query Tavily for song information.

        Args:
            song_name: The song name to search for

        Returns:
            Dictionary containing search results or None if search failed
        """
        query = f"{song_name} song album, title, and artist"
        logger.debug(f"Tavily querying: {query}")

        try:
            tavily_tool = next((t for t in self.tavily_agent.tools if isinstance(t, TavilyTools)), None)
            if not tavily_tool:
                logger.warning("Tavily tool not found in agent tools")
                return None

            response = tavily_tool.web_search_using_tavily(query)

            # Handle response parsing
            if isinstance(response, str):
                try:
                    response = json.loads(response)
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse Tavily JSON response: {e}")
                    return {"results": [], "content": response}

            # If we have an AI-generated answer, just return that directly
            if isinstance(response, dict) and "answer" in response:
                ai_answer = response["answer"]
                logger.debug(f"AI Generated Answer from Tavily: {ai_answer[:100]}...")
                return {"ai_answer": ai_answer}

            return response
        except Exception as e:
            logger.warning(f"Tavily search failed: {e}")
            return None

    def _tavily_search(self, query: str) -> str:
        """Helper method to perform the actual Tavily search."""
        tavily_tool = next((t for t in self.tavily_agent.tools if isinstance(t, TavilyTools)), None)
        response = tavily_tool.web_search_using_tavily(query)
        return str(response)

    def _fetch_results_exa(self, song_name: str) -> Optional[str]:
        """Query Exa for song information using AI-generated answers.

        Args:
            song_name: The song name to search for

        Returns:
            String containing AI-generated answer or None if search failed
        """
        query = f"{song_name} song album, title, and artist"
        logger.debug(f"Searching Exa for: {query}")
        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(self._exa_search, query)
                response = future.result(timeout=15)  # 15 second timeout

                if response:
                    try:
                        # Parse the response as JSON
                        results = json.loads(response) if isinstance(response, str) else response

                        # Extract the answer from the response
                        if isinstance(results, dict) and "answer" in results:
                            logger.debug(f"AI Generated Answer from Exa: {results['answer'][:100]}...")
                            return results["answer"]

                        # Return raw response if we couldn't extract answer
                        return str(response)

                    except (json.JSONDecodeError, TypeError) as e:
                        logger.warning(f"Failed to process Exa response: {e}")
                        return str(response)

                return None
        except concurrent.futures.TimeoutError:
            logger.warning(f"Exa search timed out for: {query}")
            return None
        except Exception as http_err:
            if http_err.response.status_code == 504:
                logger.error(f"Exa API returned 504 Gateway Timeout for query: {query}")
            else:
                logger.error(f"HTTP error occurred during Exa search: {http_err}")
            return None
        except Exception as e:
            logger.warning(f"Exa search failed: {e}")
            return None

    def _exa_search(self, query: str) -> Optional[str]:
        """Helper method to perform Exa search using AI-generated answers."""
        # Get the ExaTools instance from the agent
        exa_tool = next((t for t in self.exa_agent.tools if isinstance(t, ExaTools)), None)
        if not exa_tool:
            logger.warning("Exa tool not found in agent tools")
            return None

        # Use exa_answer to get AI-generated answers
        try:
            response = exa_tool.exa_answer(query, text=True)
            return response
        except Exception as e:
            logger.warning(f"Exa answer failed: {e}")
            return None

    def _get_search_results(self, song_name: str) -> Dict[str, str]:
        """Get search results from available search engines."""

        # Try SearxNG first if available
        if self.searxng_agent:
            content = self._fetch_results_searxng(song_name)
            if content:
                return {"source": "searxng", "content": content}

        # Then try Exa
        if self.exa_agent:
            content = self._fetch_results_exa(song_name)
            if content:
                return {"source": "exa_ai", "content": content}

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
        If you cannot determine a value with reasonable confidence, return null for that field.

        Format your response as valid JSON with these exact keys:
        {{
            "title": "The song title or null if uncertain",
            "artist": "The artist name or null if uncertain",
            "album": "The album name or null if uncertain"
        }}
        </instruction>

        <search_results>
        {content}
        </search_results>
        """

        logger.debug("Sending to Ollama for parsing - Song: {0}", song_name)
        content_preview = content[:1000] if len(content) > 1000 else content
        logger.debug("First chars of content: {0}...", content_preview)

        try:
            response = self.ollama_agent.run(prompt)
            return response.content
        except Exception as e:
            logger.error("Ollama extraction failed: {0}", str(e))
            # Return None for artist and album on failure
            return SongBasicInfo(title=song_name, artist=None, album=None)

    def search_song_info(self, song_name: str) -> Dict:
        """Search for song information using available search engines."""
        search_results = self._get_search_results(song_name)

        if (search_results["source"] == "error"):
            return {
                "title": song_name,
                "artist": None,
                "album": None,
                "search_source": "error"
            }

        song_details = self._extract_song_details(search_results["content"], song_name).model_dump()
        song_details["search_source"] = search_results["source"]
        return song_details


def initialize_search_toolkit():
    """Initialize the music search toolkit with configuration from beets config.

    Returns:
        MusicSearchTools instance or None if initialization fails
    """
    if not AGNO_AVAILABLE:
        logger.error("Agno package not available. Please install with: pip install agno")
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
    """Get or initialize the search toolkit singleton.

    Returns:
        MusicSearchTools instance or None if toolkit initialization fails
    """
    global _search_toolkit
    if (_search_toolkit is None):
        _search_toolkit = initialize_search_toolkit()
    return _search_toolkit


def search_track_info(query: str) -> Dict:
    """
    Searches for track information using available search engines.

    Args:
        query: The song name or partial information to search for

    Returns:
        Dictionary containing title, album, and artist information
    """
    toolkit = get_search_toolkit()

    if not toolkit:
        logger.error("Search toolkit unavailable. Install agno and configure search engines.")
        return {"title": query, "artist": None, "album": None}

    try:
        logger.info("Searching for track info: {0}", query)
        song_info = toolkit.search_song_info(query)

        # Format response: Use extracted title if available, otherwise fallback to original query.
        # Pass through artist and album (which could be None if not found).
        result = {
            "title": song_info.get("title") or query, # Use query if title is None or empty
            "album": song_info.get("album"),
            "artist": song_info.get("artist")
        }

        logger.info("Found track info: {}", result)
        return result
    except Exception as e:
        logger.error("Error in agent-based search: {0}", str(e))
        # General fallback: use original query for title
        return {"title": query, "artist": None, "album": None}
