"""LLM integration for beets plugins."""

import json
import logging
import textwrap
import time
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
BRAVE_AVAILABLE = False

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

    try:
        from agno.tools.bravesearch import BraveSearchTools
        BRAVE_AVAILABLE = True
    except ImportError:
        logger.debug("Brave Search tools not available")

except ImportError:
    logger.error("Agno package not available. Please install with: pip install agno")

# Add default configuration for LLM search
config['llm'].add({
    'search': {
        'provider': 'ollama',
        'model': 'qwen3:latest',
        'ollama_host': 'http://localhost:11434',
        'tavily_api_key': '',
        'searxng_host': '',
        'exa_api_key': '',
        'brave_api_key': '',
    }
})


class SongBasicInfo(BaseModel):
    """Pydantic model for structured song information."""
    title: str = Field(..., description="The title of the song")
    artist: str = Field("", description="The name of the artist or band")
    album: Optional[str] = Field(None, description="The album the song appears on")

    @field_validator('title', 'artist', 'album', mode='before')
    @classmethod
    def default_unknown(cls, v):
        if not v or not isinstance(v, str) or not v.strip():
            return None  # Return None instead of "Unknown"
        return v.strip()


# Pydantic models used by plexsync for LLM playlist parsing
class Song(BaseModel):
    title: str
    artist: str
    album: str
    year: str = Field(description="Year of release")


class SongRecommendations(BaseModel):
    songs: list[Song]


class MusicSearchTools:
    """Standalone class for music metadata search using multiple search engines."""
    
    # Class variable to track last Brave Search request time
    _last_brave_request_time = 0

    def __init__(self, tavily_api_key=None, searxng_host=None, model_id=None, ollama_host=None, exa_api_key=None, brave_api_key=None):
        """Initialize music search tools with available search providers.

        Args:
            tavily_api_key: API key for Tavily search
            searxng_host: Host URL for SearxNG instance
            model_id: Ollama model ID to use
            ollama_host: Ollama API host URL
            exa_api_key: API key for Exa search
            brave_api_key: API key for Brave Search
        """
        self.name = "music_search_tools"
        self.model_id = model_id or "qwen3:latest"
        self.ollama_host = ollama_host or "http://localhost:11434"
        self.search_agent = None

        # Initialize Ollama agent for extraction (required for all search methods)
        self._init_ollama_agent()

        # Initialize a single search agent with all available tools
        self._init_search_agent(tavily_api_key, searxng_host, exa_api_key, brave_api_key)

        # Log available search providers
        self._log_available_providers()

    def _init_ollama_agent(self) -> None:
        """Initialize the Ollama agent for text extraction."""
        try:
            # Try to initialize with response_model in Agent initialization (older versions)
            try:
                self.ollama_agent = Agent(
                    model=Ollama(id=self.model_id, host=self.ollama_host),
                    response_model=SongBasicInfo,
                    structured_outputs=True
                )
                self.response_model_in_init = True
            except TypeError:
                # If that fails, try without response_model in Agent initialization (newer versions)
                self.ollama_agent = Agent(
                    model=Ollama(id=self.model_id, host=self.ollama_host),
                    structured_outputs=True
                )
                self.response_model_in_init = False
        except Exception as e:
            logger.error(f"Failed to initialize Ollama agent: {e}")
            self.ollama_agent = None
            self.response_model_in_init = False

    def _enforce_brave_rate_limit(self) -> None:
        """Enforce rate limiting for Brave Search (1 request per second)."""
        if BRAVE_AVAILABLE:
            current_time = time.time()
            time_since_last_request = current_time - self._last_brave_request_time
            if time_since_last_request < 1.0:  # Less than 1 second since last request
                sleep_time = 1.0 - time_since_last_request
                logger.debug(f"Rate limiting Brave Search. Sleeping for {sleep_time:.2f} seconds.")
                time.sleep(sleep_time)
            self._last_brave_request_time = time.time()

    def _init_search_agent(self, tavily_api_key: Optional[str], searxng_host: Optional[str], exa_api_key: Optional[str], brave_api_key: Optional[str]) -> None:
        """Initialize a single agent with all available search tools."""
        tools = []

        # SearxNG (highest priority)
        if searxng_host and SEARXNG_AVAILABLE:
            try:
                tools.append(Searxng(host=searxng_host, fixed_max_results=5))
            except Exception as e:
                logger.warning(f"Failed to initialize SearxNG tool: {e}")

        # Exa
        if exa_api_key and EXA_AVAILABLE:
            try:
                tools.append(ExaTools(api_key=exa_api_key, timeout=15))
            except Exception as e:
                logger.warning(f"Failed to initialize Exa tool: {e}")

        # Brave Search (with rate limiting)
        if brave_api_key and BRAVE_AVAILABLE:
            try:
                tools.append(BraveSearchTools(api_key=brave_api_key, fixed_max_results=5))
            except Exception as e:
                logger.warning(f"Failed to initialize Brave Search tool: {e}")

        # Tavily (lowest priority)
        if tavily_api_key and TAVILY_AVAILABLE:
            try:
                tools.append(TavilyTools(
                    api_key=tavily_api_key,
                    include_answer=True,
                    search_depth="advanced",
                    format="json"
                ))
            except Exception as e:
                logger.warning(f"Failed to initialize Tavily tool: {e}")

        if tools:
            try:
                self.search_agent = Agent(
                    model=Ollama(id=self.model_id, host=self.ollama_host),
                    tools=tools
                )
            except Exception as e:
                logger.error(f"Failed to initialize search agent: {e}")
                self.search_agent = None

    def _log_available_providers(self) -> None:
        """Log which search providers are available."""
        if not self.search_agent or not self.search_agent.tools:
            logger.warning("No music search providers available!")
            if self.ollama_agent:
                logger.info("Only Ollama extraction is available, which requires at least one search provider")
            return

        providers = [tool.name for tool in self.search_agent.tools]
        logger.info(f"Initialized music search with providers: {', '.join(providers)}")
        if self.ollama_agent:
            logger.info(f"Using Ollama model '{self.model_id}' for result extraction and tool selection")

    def _search(self, song_name: str) -> Optional[str]:
        """Query the search agent for song information.

        Args:
            song_name: The song name to search for

        Returns:
            String containing search results or None if search failed
        """
        if not self.search_agent:
            logger.warning("Search agent not available.")
            return None

        # Enforce rate limiting if using Brave Search
        if self.search_agent.tools and any("brave" in tool.name.lower() for tool in self.search_agent.tools):
            self._enforce_brave_rate_limit()

        query = f"{song_name} song album, title, and artist. Please respond in English only."
        logger.debug(f"Unified search querying: {query}")
        try:
            response = self.search_agent.run(query, timeout=20)
            content = getattr(response, 'content', str(response))

            # Handle JSON string responses from tools like Tavily
            try:
                data = json.loads(content)
                if isinstance(data, dict) and "answer" in data:
                    return data["answer"]
                if isinstance(data, dict) and "results" in data:
                    return json.dumps(data["results"])
            except (json.JSONDecodeError, TypeError):
                # Not a JSON string, so return content as is
                pass

            return content
        except Exception as e:
            logger.warning(f"Unified search failed: {e}")
            return None

    def _get_search_results(self, song_name: str) -> Dict[str, str]:
        """Get search results from available search engines."""
        content = self._search(song_name)

        if content:
            # Determine source from the tool used by the agent, if possible
            source = "unified_search"
            if self.search_agent and self.search_agent.tools:
                # A simple heuristic: assume the first tool was used.
                # A more advanced implementation might inspect agent's execution trace.
                source = self.search_agent.tools[0].name

            return {"source": source, "content": content}

        # Return error if search failed
        return {"source": "error", "content": f"No results for '{song_name}'"}

    def _extract_song_details(self, content: str, song_name: str) -> SongBasicInfo:
        """Extract structured song details from search results."""
        # Check if agent is available
        if not self.ollama_agent:
            logger.error("Ollama agent not initialized")
            return SongBasicInfo(title=song_name, artist="", album=None)
            
        prompt = textwrap.dedent(f"""\n
        <instruction>
        IMPORTANT: Analyze ONLY the search results data below to extract accurate information about a song.
        The query "{song_name}" may contain incorrect or incomplete information - DO NOT rely on the query itself for extracting details.

        Based EXCLUSIVELY on the search results content, extract these fields:
        - Song Title: The exact title of the song as mentioned in the search results (not the query)
        - Artist Name: The primary artist or band who performed the song
        - Album Name: The album that contains this song (if mentioned). This could be:
          * An actual album name
          * A movie/film name (if it's a soundtrack) - KEEP THE MOVIE NAME AS THE ALBUM
          * An OST or soundtrack name
          * Any collection or compilation name

        IMPORTANT EXTRACTION RULES:
        For ALBUM extraction:
        - If the song is "from the film" or "from the movie", use the film/movie name as the album
        - For Bollywood/Indian songs, the movie name IS the album name - do not remove it
        - If mentioned as "soundtrack", "OST", or similar, include that information
        - Clean the album name by removing:
          * Years/dates in parentheses (e.g., "(1974)")
          * Excessive descriptive text
          * Leading/trailing spaces
        - Keep the core name that identifies the album/movie/collection
        - IMPORTANT: For songs from movies, the movie name should be the album name

        For TITLE and ARTIST:
        - Extract exactly as mentioned in the search results
        - Clean excessive formatting but keep the essential name

        EXAMPLES:
        - If content says "from the 1974 film 'Ajanabee'", then album should be "Ajanabee"
        - If content says "from the movie 'Sholay'", then album should be "Sholay"
        - If content says "soundtrack of 'Dilwale Dulhania Le Jayenge'", then album should be "Dilwale Dulhania Le Jayenge"

        If any information is not clearly stated in the search results, use the most likely value based on available context.
        If you cannot determine a value with reasonable confidence, return null for that field.

        Format your response as valid JSON with these exact keys:
        {{
            "title": "The song title based ONLY on search results, or null if uncertain",
            "artist": "The artist name or null if uncertain",
            "album": "The album/movie/collection name or null if uncertain"
        }}
        </instruction>
        <search_results>
        {content}
        </search_results>
        """)

        logger.debug("Sending to Ollama for parsing - Song: {0}", song_name)
        content_preview = content[:1000] if len(content) > 1000 else content
        logger.debug("First chars of content: {0}...", content_preview)

        try:
            # Use response_model in run() method if it wasn't supported in Agent initialization
            if hasattr(self, 'response_model_in_init') and not self.response_model_in_init:
                response = self.ollama_agent.run(prompt, response_model=SongBasicInfo)
            else:
                response = self.ollama_agent.run(prompt)

            # Log the raw response for debugging
            logger.debug("Raw Ollama response: {}", response)

            # Handle both string and object responses
            if isinstance(response, str):
                # If response is a string, try to parse it as JSON
                try:
                    import json
                    data = json.loads(response)
                    return SongBasicInfo(**data)
                except:
                    # If parsing fails, return the response as content
                    return SongBasicInfo(title=song_name, artist="", album=None)
            elif hasattr(response, 'content'):
                # If response has a content attribute, use it
                content = response.content
                if isinstance(content, str):
                    # If content is a string, try to parse it as JSON
                    try:
                        import json
                        data = json.loads(content)
                        return SongBasicInfo(**data)
                    except:
                        # If parsing fails, create a SongBasicInfo with the content as title
                        return SongBasicInfo(title=content or song_name, artist="", album=None)
                else:
                    # If content is already a SongBasicInfo object, return it
                    return content
            else:
                # For other response types, try to convert to SongBasicInfo
                if isinstance(response, dict):
                    return SongBasicInfo(**response)
                else:
                    return SongBasicInfo(title=str(response) or song_name, artist="", album=None)
        except Exception as e:
            logger.error("Ollama extraction failed: {0}", str(e))
            # Return a default SongBasicInfo object on failure to avoid validation errors
            return SongBasicInfo(title=song_name, artist="", album=None)

    def search_song_info(self, song_name: str) -> Dict:
        """Search for song information using available search engines."""
        # Check if agent is available
        if not self.ollama_agent:
            logger.error("Ollama agent not initialized")
            return {
                "title": song_name,
                "artist": "",
                "album": None,
                "search_source": "error"
            }
            
        search_results = self._get_search_results(song_name)

        if (search_results["source"] == "error"):
            return {
                "title": song_name,
                "artist": "",
                "album": None,
                "search_source": "error"
            }

        song_details = self._extract_song_details(search_results["content"], song_name)
        if isinstance(song_details, SongBasicInfo):
            song_details = song_details.model_dump()
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
    model_id = config["llm"]["search"]["model"].get() or "qwen3:latest"
    ollama_host = config["llm"]["search"]["ollama_host"].get() or "http://localhost:11434"
    exa_api_key = config["llm"]["search"]["exa_api_key"].get()
    brave_api_key = config["llm"]["search"]["brave_api_key"].get()

    if not tavily_api_key and not searxng_host and not exa_api_key and not brave_api_key:
        logger.warning("No search providers configured. Search functionality limited.")

    try:
        return MusicSearchTools(
            tavily_api_key=tavily_api_key,
            searxng_host=searxng_host,
            model_id=model_id,
            ollama_host=ollama_host,
            exa_api_key=exa_api_key,
            brave_api_key=brave_api_key
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
        return {"title": query, "artist": "", "album": None}

    try:
        logger.info("Searching for track info: {0}", query)
        song_info = toolkit.search_song_info(query)

        # Format response: Use extracted title if available, otherwise fallback to original query.
        # Pass through artist and album (which could be None if not found).
        # Ensure artist is never None to prevent validation errors
        result = {
            "title": song_info.get("title") or query, # Use query if title is None or empty
            "album": song_info.get("album"),
            "artist": song_info.get("artist") or ""  # Default to empty string to avoid None
        }

        logger.info("Found track info: {}", result)
        return result
    except Exception as e:
        logger.error("Error in agent-based search: {0}", str(e))
        # General fallback: use original query for title and empty string for artist to avoid validation errors
        return {"title": query, "artist": "", "album": None}
