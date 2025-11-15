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
INSTRUCTOR_AVAILABLE = False
TAVILY_AVAILABLE = False
SEARXNG_AVAILABLE = False
EXA_AVAILABLE = False
BRAVE_AVAILABLE = False
OPENAI_MODEL_AVAILABLE = False

try:
    from agno.agent import Agent
    from agno.models.ollama import Ollama
    from agno.models.openai.like import OpenAILike
    AGNO_AVAILABLE = True
    OPENAI_MODEL_AVAILABLE = True

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

try:
    import instructor
    from openai import OpenAI
    INSTRUCTOR_AVAILABLE = True
except ImportError:
    logger.debug("instructor library not available. Falling back to Agno for structured output.")

# Add default configuration for LLM search
config['llm'].add({
    'search': {
        'provider': '',  # Auto-detect: uses OpenAI if llm.api_key is set, otherwise Ollama
        'api_key': '',  # Will fall back to llm.api_key if empty
        'base_url': '',  # Will fall back to llm.base_url if empty
        'model': '',  # Will fall back to llm.model if empty (when using OpenAI), or 'qwen3:latest' for Ollama
        'ollama_host': 'http://localhost:11434',
        'tavily_api_key': '',
        'searxng_host': '',
        'exa_api_key': '',
        'brave_api_key': '',
    }
})


class SongBasicInfo(BaseModel):
    """Pydantic model for structured song information."""
    title: str = Field(..., description="The title of the song as mentioned in search results")
    artist: str = Field("", description="The primary artist or band who performed the song")
    album: Optional[str] = Field(None, description="The album that contains this song. This could be an actual album name, a movie/film name (if it's a soundtrack), an OST or soundtrack name, or any collection or compilation name.")

    @field_validator('title', 'artist', 'album', mode='before')
    @classmethod
    def default_unknown(cls, v):
        # Handle None values and empty strings
        if v is None or (isinstance(v, str) and not v.strip()):
            return ""  # Return empty string for all fields to avoid validation issues
        if isinstance(v, str):
            return v.strip()
        return str(v)  # Convert any other type to string


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

    def __init__(self, tavily_api_key=None, searxng_host=None, model_id=None, ollama_host=None, exa_api_key=None, brave_api_key=None, provider='ollama', api_key=None, base_url=None):
        """Initialize music search tools with available search providers.

        Args:
            tavily_api_key: API key for Tavily search
            searxng_host: Host URL for SearxNG instance
            model_id: Model ID to use (Ollama model or OpenAI model name)
            ollama_host: Ollama API host URL
            exa_api_key: API key for Exa search
            brave_api_key: API key for Brave Search
            provider: LLM provider ('ollama' or 'openai')
            api_key: API key for OpenAI-compatible providers
            base_url: Base URL for OpenAI-compatible providers
        """
        self.name = "music_search_tools"
        self.provider = provider
        self.model_id = model_id or "qwen3:latest"
        self.ollama_host = ollama_host or "http://localhost:11434"
        self.api_key = api_key
        self.base_url = base_url
        self.search_agent = None

        # Initialize LLM agent for extraction (required for all search methods)
        self._init_llm_agent()

        # Initialize a single search agent with all available tools
        self._init_search_agent(tavily_api_key, searxng_host, exa_api_key, brave_api_key)

        # Log available search providers
        self._log_available_providers()

    def _create_model(self):
        """Create a model (Ollama or OpenAI-compatible) based on provider settings.

        Returns:
            Model instance (Ollama or OpenAILike) or None if creation fails
        """
        # Determine which model to use based on provider
        if self.provider == 'ollama':
            # Use Ollama model
            model = Ollama(id=self.model_id, host=self.ollama_host, timeout=30)
            logger.debug(f"Initializing Ollama agent with model {self.model_id} at {self.ollama_host}")
            return model
        else:
            # Use OpenAI-compatible model
            if not OPENAI_MODEL_AVAILABLE:
                logger.error("OpenAI model not available in agno. Falling back to Ollama.")
                model = Ollama(id=self.model_id, host=self.ollama_host, timeout=30)
                return model
            else:
                model_args = {"id": self.model_id}
                if self.api_key:
                    model_args["api_key"] = self.api_key
                if self.base_url:
                    model_args["base_url"] = self.base_url
                model = OpenAILike(**model_args)
                logger.debug(f"Initializing OpenAI-compatible agent with model {self.model_id}")
                return model

    def _init_llm_agent(self) -> None:
        """Initialize the LLM agent for text extraction.

        Supports both Ollama and OpenAI-compatible models.
        """
        try:
            model = self._create_model()

            self.ollama_agent = Agent(
                model=model,
                description="You extract structured song information from search results.",
                output_schema=SongBasicInfo,  # Use output_schema for structured output
            )
        except Exception as e:
            logger.error(f"Failed to initialize LLM agent: {e}")
            self.ollama_agent = None

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
                model = self._create_model()

                # Create Agent with tools
                self.search_agent = Agent(
                    model=model,
                    tools=tools
                )
            except Exception as e:
                logger.error(f"Failed to initialize search agent with tools: {e}")
                self.search_agent = None

    def _log_available_providers(self) -> None:
        """Log which search providers are available."""
        if not self.search_agent or not self.search_agent.tools:
            logger.warning("No music search providers available!")
            if self.ollama_agent:
                logger.info("Only LLM extraction is available, which requires at least one search provider")
            return

        providers = [tool.name for tool in self.search_agent.tools]
        logger.info("Initialized music search with providers: {}", ', '.join(providers))
        if self.ollama_agent:
            provider_type = "OpenAI-compatible" if self.provider != 'ollama' else "Ollama"
            logger.info("Using {} model '{}' for result extraction and tool selection", provider_type, self.model_id)

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
        logger.debug("Unified search querying: {}", query)
        try:
            response = self.search_agent.run(query, timeout=30)  # Increased timeout from 20 to 30 seconds
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
            logger.warning("Unified search failed: {}", e)
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

    def _create_fallback_song(self, title: str) -> SongBasicInfo:
        """Create a fallback SongBasicInfo with minimal information.

        Args:
            title: The song title to use as fallback

        Returns:
            SongBasicInfo with just the title and empty/None other fields
        """
        return SongBasicInfo(title=title, artist="", album=None)

    def _song_info_from_dict(self, data: dict, fallback_title: str) -> SongBasicInfo:
        """Create SongBasicInfo from dictionary, with fallbacks for missing fields.

        Args:
            data: Dictionary containing song fields (title, artist, album)
            fallback_title: Title to use if not present in data

        Returns:
            SongBasicInfo object with data from dict or fallback values
        """
        return SongBasicInfo(
            title=data.get("title") or fallback_title,
            artist=data.get("artist") or "",
            album=data.get("album")  # Can be None
        )

    def _song_info_from_json(self, json_str: str, fallback_title: str) -> SongBasicInfo:
        """Parse JSON string and create SongBasicInfo.

        Args:
            json_str: JSON string containing song information
            fallback_title: Title to use if parsing fails

        Returns:
            SongBasicInfo object from parsed JSON or fallback
        """
        try:
            data = json.loads(json_str)
            if isinstance(data, dict):
                return self._song_info_from_dict(data, fallback_title)
            logger.warning(f"JSON parsed but not a dict: {type(data)}")
            return self._create_fallback_song(fallback_title)
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse JSON response: {0}", e)
            return self._create_fallback_song(fallback_title)

    def _parse_text_format(self, text: str) -> Optional[dict]:
        """Parse text format like 'title: value\nartist: value\nalbum: value'.

        Args:
            text: String containing key-value pairs separated by newlines

        Returns:
            Dictionary with parsed fields or None if parsing fails
        """
        try:
            result = {}
            lines = text.strip().split('\n')

            for line in lines:
                line = line.strip()
                if ':' in line:
                    key, value = line.split(':', 1)  # Split only on the first colon
                    key = key.strip().lower()
                    value = value.strip()

                    # Convert empty values to None, except for empty strings we want to keep
                    if value.lower() in ['null', 'none', '']:
                        value = None

                    # Map to correct field names
                    if key in ['title', 'song', 'track']:
                        result['title'] = value
                    elif key in ['artist', 'singer', 'performer']:
                        result['artist'] = value
                    elif key in ['album', 'collection', 'record']:
                        result['album'] = value

            # Only return if we got at least a title
            if 'title' in result:
                return result
            else:
                logger.debug("Text format parsing did not find a title field: {0}", text)
                return None
        except Exception as e:
            logger.warning("Failed to parse text format: {0}", e)
            return None

    def _convert_to_song_info(self, data, fallback_title: str) -> SongBasicInfo:
        """Convert various data formats to SongBasicInfo object.

        Handles multiple response formats:
        - SongBasicInfo objects (return as-is)
        - Dictionaries with song fields
        - JSON strings that need parsing

        Args:
            data: Can be SongBasicInfo, dict, or JSON string
            fallback_title: Title to use if conversion fails

        Returns:
            SongBasicInfo object with extracted or fallback data
        """
        # Case 1: Already a SongBasicInfo object
        if isinstance(data, SongBasicInfo):
            return data

        # Case 2: Dictionary with song fields
        if isinstance(data, dict):
            return self._song_info_from_dict(data, fallback_title)

        # Case 3: Text format that needs parsing (e.g., "title: value\nartist: value\nalbum: value")
        if isinstance(data, str):
            # First try to parse as text format
            parsed_data = self._parse_text_format(data)
            if parsed_data:
                return self._song_info_from_dict(parsed_data, fallback_title)
            # If text parsing fails, try JSON parsing
            return self._song_info_from_json(data, fallback_title)

        # Case 4: Unknown format - return fallback
        logger.warning(f"Unexpected response format: {type(data)}")
        return self._create_fallback_song(fallback_title)

    def _extract_response_data(self, response):
        """Extract the raw data from various response wrapper formats.

        Recursively unwraps .content attributes to get to the actual data.

        Args:
            response: Response object from LLM agent

        Returns:
            The innermost data, unwrapped from any .content attributes
        """
        # Unwrap .content attribute if present (recursive)
        if hasattr(response, 'content'):
            return self._extract_response_data(response.content)

        # Already at the data level
        return response

    def _build_extraction_prompt(self, content: str, song_name: str) -> str:
        """Build the prompt for song detail extraction.

        Args:
            content: Search results content to analyze
            song_name: Original song query for context

        Returns:
            Formatted prompt string for LLM extraction
        """
        return textwrap.dedent(f"""\
        <instruction>
        IMPORTANT: Analyze ONLY the search results data below to extract accurate information about a song.
        The query "{song_name}" may contain incorrect or incomplete information - DO NOT rely on the query itself for extracting details.

        Extract structured information about the song based EXCLUSIVELY on the search results content to populate a SongBasicInfo Pydantic model with these fields:
        - title (str): The exact title of the song as mentioned in the search results (not the query)
        - artist (str): The primary artist or band who performed the song
        - album (str or None): The album that contains this song (if mentioned). This could be:
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

        FORMAT INSTRUCTIONS:
        - Return only the extracted information in this exact format:
        title: [song title]
        artist: [artist name]
        album: [album name or null if not found]
        - Do NOT include any additional text, explanations, or code formatting
        - Do NOT wrap the response in markdown code blocks
        - Do NOT return Python code or import statements
        - The response will be parsed by a structured output system
        </instruction>
        <search_results>
        {content}
        </search_results>\
        """)

    def _log_content_preview(self, content: str, max_chars: int = 1000) -> None:
        """Log a preview of the search content for debugging.

        Args:
            content: Content to preview
            max_chars: Maximum characters to include in preview
        """
        preview = content[:max_chars] if len(content) > max_chars else content
        logger.debug("First chars of content: {0}...", preview)

    def _log_response_preview(self, response, max_chars: int = 300) -> None:
        """Log a preview of the LLM response for debugging.

        Args:
            response: Response object from LLM agent
            max_chars: Maximum characters to include in preview
        """
        content_to_log = getattr(response, 'content', str(response)) if hasattr(response, 'content') else str(response)
        preview = content_to_log[:max_chars]
        if len(content_to_log) > max_chars:
            preview += "..."
        logger.debug("Raw LLM response content: {0}", preview)

    def _parse_ollama_response(self, response, fallback_title: str) -> SongBasicInfo:
        """Parse LLM agent response into SongBasicInfo.

        Handles multiple response formats from the Agno agent:
        - Direct SongBasicInfo object
        - Dict with song fields
        - Wrapped response with .content attribute
        - JSON string that needs parsing

        Args:
            response: Response object from LLM agent (Ollama or OpenAI)
            fallback_title: Title to use if parsing fails

        Returns:
            SongBasicInfo object with parsed data or fallback
        """
        # Log response for debugging
        self._log_response_preview(response)

        # Extract the actual data from wrapper formats
        data = self._extract_response_data(response)

        # Convert the data to SongBasicInfo
        return self._convert_to_song_info(data, fallback_title)

    def _extract_song_details(self, content: str, song_name: str) -> SongBasicInfo:
        """Extract structured song details from search results.

        Args:
            content: Search results content to analyze
            song_name: Original song query for context and fallback

        Returns:
            SongBasicInfo object with extracted details or fallback data
        """
        # Check if agent is available
        if not self.ollama_agent:
            logger.error("LLM agent not initialized")
            return self._create_fallback_song(song_name)

        # Build the extraction prompt
        prompt = self._build_extraction_prompt(content, song_name)

        # Log what we're doing
        logger.debug("Sending to LLM for parsing - Song: {0}", song_name)
        self._log_content_preview(content)

        try:
            # With output_schema set, the agent should return a SongBasicInfo object directly
            response = self.ollama_agent.run(prompt, timeout=30)

            # The response should be a SongBasicInfo object due to output_schema
            if isinstance(response.content, SongBasicInfo):
                return response.content
            else:
                # Fallback to parsing if not a SongBasicInfo object
                return self._parse_ollama_response(response, song_name)
        except Exception as e:
            logger.error("LLM extraction failed: {0}", e)
            return self._create_fallback_song(song_name)

    def search_song_info(self, song_name: str) -> Dict:
        """Search for song information using available search engines.

        Args:
            song_name: The song name to search for

        Returns:
            Dictionary containing title, artist, album, and search_source information
        """
        # Check if agent is available
        if not self.ollama_agent:
            logger.error("LLM agent not initialized")
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

    # Get API key from main config first to determine provider
    main_api_key = config["llm"]["api_key"].get()

    # Auto-detect provider if not explicitly set
    provider = config["llm"]["search"]["provider"].get()
    if not provider:
        # If main llm has an api_key, default to OpenAI; otherwise use Ollama
        provider = "openai" if main_api_key else "ollama"
        logger.debug(f"Auto-detected provider: {provider}")

    # Get API key - prefer search-specific, fall back to main llm config
    api_key = config["llm"]["search"]["api_key"].get()
    if not api_key:
        api_key = main_api_key

    # Get base URL - prefer search-specific, fall back to main llm config
    base_url = config["llm"]["search"]["base_url"].get()
    if not base_url:
        base_url = config["llm"]["base_url"].get()

    # Get model configuration - prefer search-specific, fall back to main llm config
    model_id = config["llm"]["search"]["model"].get()
    if not model_id:
        # Fall back to main llm model for OpenAI, or use default for Ollama
        if provider == "openai":
            fallback_model = config["llm"]["model"].get()
            model_id = fallback_model if fallback_model else "gpt-4.1-mini"
        else:
            model_id = "qwen3:latest"

    # Get Ollama-specific configuration
    ollama_host = config["llm"]["search"]["ollama_host"].get() or "http://localhost:11434"

    # Get search provider API keys
    tavily_api_key = config["llm"]["search"]["tavily_api_key"].get()
    searxng_host = config["llm"]["search"]["searxng_host"].get()
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
            brave_api_key=brave_api_key,
            provider=provider,
            api_key=api_key,
            base_url=base_url
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
    """Searches for track information using available search engines.

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
        logger.info("Searching for track info: {}", query)
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
        logger.error("Error in agent-based search: %s", e)
        return {"title": query, "artist": "", "album": None}
