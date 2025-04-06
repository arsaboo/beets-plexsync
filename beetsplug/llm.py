"""LLM integration for beets plugins."""

import json
import logging
import os
import re
from typing import Optional, Dict, List

from beets import config
from pydantic import BaseModel, Field, field_validator

# Simple logger for standalone use
logger = logging.getLogger('beets')

# Track available dependencies
PHI_AVAILABLE = False
TAVILY_AVAILABLE = False
SEARXNG_AVAILABLE = False
EXA_AVAILABLE = False
EXA_PY_AVAILABLE = False

try:
    from phi.agent import Agent
    from phi.model.ollama import Ollama
    from phi.tools import Toolkit
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

    # Check for Exa Python SDK
    try:
        from exa_py import Exa
        EXA_PY_AVAILABLE = True
    except ImportError:
        logger.debug("Exa Python SDK not available. Install with: pip install exa_py")

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


class ExaToolAnswer(Toolkit):
    """Custom toolkit for AI-generated answers using Exa."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        system_prompt: Optional[str] = None
    ):
        super().__init__(name="exa_tool_answer")

        # Use environment variable if api_key not provided
        self.api_key = api_key or os.getenv("EXA_API_KEY")
        if not self.api_key:
            logger.error("EXA_API_KEY not set. Please set the EXA_API_KEY environment variable.")

        # Default system prompt for music search
        self.system_prompt = system_prompt or """
        You are a music information expert. Extract and provide the following details:
        1. Song Title (be precise with capitalization and special characters)
        2. Artist Name (full name of artist or band)
        3. Album Name (the album the song appears on)

        Format your answer in a clear, concise manner. If any information is unavailable,
        explicitly state it's unknown. Do not include additional commentary or explanations.
        """

        self.register(self.get_ai_answer)

    def get_ai_answer(self, query: str) -> str:
        """Get an AI-generated answer for a query using Exa.

        Args:
            query (str): The query to generate an answer for.

        Returns:
            str: The AI-generated answer or an error message.
        """
        if not self.api_key:
            return "Please set the EXA_API_KEY"

        if not EXA_PY_AVAILABLE:
            return "Error: The exa_py package is not installed. Please install using `pip install exa_py`"

        try:
            # Create Exa client
            exa = Exa(self.api_key)

            # Enhance the query if it doesn't contain music-related terms
            if not any(term in query.lower() for term in ["song", "track", "artist", "album"]):
                enhanced_query = f"{query} song artist album information"
                logger.info(f"Enhanced music search query: {enhanced_query}")
                query = enhanced_query
            else:
                logger.info(f"Getting AI answer for: {query}")

            # Get AI-generated answer with the system prompt
            answer_kwargs = {"system_prompt": self.system_prompt}
            answer_response = exa.answer(query, **answer_kwargs)

            if answer_response:
                answer = answer_response.answer
                return answer
            else:
                return "No information found for this query."

        except Exception as e:
            logger.error(f"Failed to get AI answer: {e}")
            return f"Error: {e}"


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
        self.exa_tool_answer_agent = self._init_exa_tool_answer_agent(exa_api_key) if exa_api_key and EXA_PY_AVAILABLE else None

        # Log available search providers
        self._log_available_providers()

    def _init_ollama_agent(self) -> None:
        """Initialize the Ollama agent for text extraction."""
        try:
            self.ollama_agent = Agent(
                model=Ollama(id=self.model_id, host=self.ollama_host),
                response_model=SongBasicInfo,
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

    def _init_exa_tool_answer_agent(self, api_key: str) -> Optional[Agent]:
        """Initialize the Exa Tool Answer agent.

        Args:
            api_key: Exa API key

        Returns:
            Configured Exa Tool Answer agent or None if initialization fails
        """
        try:
            return Agent(
                model=Ollama(id=self.model_id, host=self.ollama_host),
                tools=[ExaToolAnswer(api_key=api_key)]
            )
        except Exception as e:
            logger.warning(f"Failed to initialize Exa Tool Answer agent: {e}")
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
        """Query Exa for song information.

        Args:
            song_name: The song name to search for

        Returns:
            String containing search results or None if search failed
        """
        query = f"{song_name} song album, title, and artist"
        logger.debug(f"Searching Exa for: {query}")
        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(self._exa_search, query)
                response = future.result(timeout=15)  # 15 second timeout

                # Process the Exa response to extract just the text content
                if response:
                    try:
                        # Parse the response as JSON
                        results = json.loads(response) if isinstance(response, str) else response

                        # If we have a list of results, extract and combine the text content
                        if isinstance(results, list):
                            extracted_text = ""
                            for item in results:
                                if isinstance(item, dict):
                                    # Combine title and text for context
                                    if "title" in item:
                                        extracted_text += f"Title: {item['title']}\n"
                                    if "text" in item:
                                        extracted_text += f"{item['text']}\n\n"

                            if extracted_text:
                                return extracted_text

                        # Return the raw response if we couldn't process it
                        return str(response)
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.warning(f"Failed to process Exa response: {e}")
                        return str(response)

                return None
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

        logger.debug("Sending to Ollama for parsing - Song: {0}", song_name)
        content_preview = content[:1000] if len(content) > 1000 else content
        logger.debug("First chars of content: {0}...", content_preview)

        try:
            response = self.ollama_agent.run(prompt)
            return response.content
        except Exception as e:
            logger.error("Ollama extraction failed: {0}", str(e))
            return SongBasicInfo(title=song_name, artist="Unknown")

    def search_song_info(self, song_name: str) -> Dict:
        """Search for song information using available search engines."""
        # First try the ExaToolAnswer if available for a direct AI-generated answer
        if self.exa_tool_answer_agent:
            try:
                # Get the ExaToolAnswer instance from the agent
                exa_tool_answer = next((t for t in self.exa_tool_answer_agent.tools
                                    if isinstance(t, ExaToolAnswer)), None)
                if exa_tool_answer:
                    ai_answer = exa_tool_answer.get_ai_answer(song_name)
                    if ai_answer and "unknown" not in ai_answer.lower():
                        logger.info(f"Got direct AI answer from ExaToolAnswer: {ai_answer[:100]}...")
                        # Extract structured information from the AI answer
                        song_details = self._extract_song_details(ai_answer, song_name).model_dump()
                        song_details["search_source"] = "exa_tool_answer"
                        return song_details
            except Exception as e:
                logger.warning(f"ExaToolAnswer failed: {e}")

        # Fall back to standard search methods
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
    """Initialize the music search toolkit with configuration from beets config.

    Returns:
        MusicSearchTools instance or None if initialization fails
    """
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
        logger.error("Search toolkit unavailable. Install phidata and configure search engines.")
        return {"title": query, "artist": "Unknown", "album": None}

    try:
        logger.info("Searching for track info: {0}", query)
        song_info = toolkit.search_song_info(query)

        # Format response to match expected structure
        result = {
            "title": song_info.get("title"),
            "album": song_info.get("album"),
            "artist": song_info.get("artist")
        }

        # Use beets' numbered placeholder style for logging
        logger.info("Found track info: {}", result)
        return result
    except Exception as e:
        logger.error("Error in agent-based search: {0}", str(e))
        return {"title": query, "artist": "Unknown", "album": None}
