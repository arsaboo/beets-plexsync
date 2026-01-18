"""LLM integration for Harmony music library management.

Provides AI-powered playlist generation and music search using OpenAI, Ollama, or other LLM backends.
"""

import json
import logging
from typing import Optional, Dict, List, Any
from pydantic import BaseModel, Field

logger = logging.getLogger("harmony.ai.llm")

# Track available dependencies
AGNO_AVAILABLE = False
try:
    from agno.agent import Agent
    from agno.models.ollama import Ollama
    from agno.models.openai.like import OpenAILike
    AGNO_AVAILABLE = True
except ImportError:
    logger.debug("Agno not available - LLM features will be limited")

# Search provider availability
TAVILY_AVAILABLE = False
SEARXNG_AVAILABLE = False
EXA_AVAILABLE = False
BRAVE_AVAILABLE = False

try:
    from agno.tools.tavily import TavilyTools
    TAVILY_AVAILABLE = True
except ImportError:
    pass

try:
    from agno.tools.searxng import Searxng
    SEARXNG_AVAILABLE = True
except ImportError:
    pass

try:
    from agno.tools.exa import ExaTools
    EXA_AVAILABLE = True
except ImportError:
    pass

try:
    from agno.tools.bravesearch import BraveSearchTools
    BRAVE_AVAILABLE = True
except ImportError:
    pass


class SongBasicInfo(BaseModel):
    """Pydantic model for basic song information."""
    title: str = Field(..., description="The title of the song")
    artist: str = Field("", description="The artist or band who performed the song")
    album: Optional[str] = Field(None, description="The album or collection name")


class Song(BaseModel):
    """Song model with year information."""
    title: str = Field(..., description="Song title")
    artist: str = Field(..., description="Artist name")
    album: str = Field("", description="Album name")
    year: str = Field("", description="Release year")


class SongRecommendations(BaseModel):
    """List of song recommendations."""
    songs: List[Song]


class MusicSearchTools:
    """Music search tool using multiple search backends."""

    def __init__(
        self,
        provider: str = "ollama",
        model: str = None,
        api_key: str = None,
        base_url: str = None,
        ollama_host: str = "http://localhost:11434",
        tavily_api_key: str = None,
        searxng_host: str = None,
        exa_api_key: str = None,
        brave_api_key: str = None,
    ):
        """Initialize music search tools.

        Args:
            provider: LLM provider ('ollama', 'openai', etc)
            model: Model name/ID
            api_key: API key for provider
            base_url: Base URL for provider
            ollama_host: Ollama server URL
            tavily_api_key: Tavily search API key
            searxng_host: SearxNG host URL
            exa_api_key: Exa search API key
            brave_api_key: Brave search API key
        """
        self.provider = provider
        self.model = model or "qwen3:latest"
        self.api_key = api_key
        self.base_url = base_url
        self.ollama_host = ollama_host
        self.tavily_api_key = tavily_api_key
        self.searxng_host = searxng_host
        self.exa_api_key = exa_api_key
        self.brave_api_key = brave_api_key
        self.agent = None

        if AGNO_AVAILABLE:
            self._init_agent()
        else:
            logger.warning("Agno not available - LLM features disabled")

    def _init_agent(self):
        """Initialize the LLM agent with available search tools."""
        try:
            # Create model
            if self.provider == "ollama":
                model = Ollama(
                    id=self.model,
                    host=self.ollama_host,
                )
            else:
                # OpenAI-compatible
                model = OpenAILike(
                    id=self.model,
                    api_key=self.api_key,
                    base_url=self.base_url,
                )

            # Build tool list
            tools = []
            if SEARXNG_AVAILABLE and self.searxng_host:
                tools.append(Searxng(host=self.searxng_host))
            if EXA_AVAILABLE and self.exa_api_key:
                tools.append(ExaTools(api_key=self.exa_api_key))
            if BRAVE_AVAILABLE and self.brave_api_key:
                tools.append(BraveSearchTools(api_key=self.brave_api_key))
            if TAVILY_AVAILABLE and self.tavily_api_key:
                tools.append(TavilyTools(api_key=self.tavily_api_key))

            # Create agent with telemetry disabled for performance
            self.agent = Agent(
                model=model,
                tools=tools if tools else None,
                markdown=True,
                telemetry=False,
            )
            logger.info(f"LLM agent initialized with provider={self.provider}, model={self.model}")
        except Exception as e:
            logger.error(f"Failed to initialize LLM agent: {e}")
            self.agent = None

    def search_music(self, query: str, limit: int = 10) -> List[SongBasicInfo]:
        """Search for songs using the LLM with search tools.

        Args:
            query: Search query (song title, artist, etc)
            limit: Maximum results to return

        Returns:
            List of SongBasicInfo objects
        """
        if not self.agent:
            logger.warning("LLM agent not initialized")
            return []

        try:
            prompt = f"""Search for music information about: {query}

Return the top {limit} songs matching this query.
For each song, provide:
- Title
- Artist
- Album

Format as JSON array of objects with keys: title, artist, album"""

            response = self.agent.run(prompt)

            # Extract text from RunOutput (agno returns RunOutput object)
            # RunOutput.content contains the main response text
            response_text = str(response.content) if response and response.content else ""

            # Try to parse JSON from response
            try:
                # Extract JSON if embedded in text
                import re
                json_match = re.search(r'\[.*\]', response_text, re.DOTALL)
                if json_match:
                    songs_data = json.loads(json_match.group())
                    return [SongBasicInfo(**song) for song in songs_data if isinstance(song, dict)]
            except (json.JSONDecodeError, ValueError):
                logger.debug(f"Could not parse JSON from LLM response")

            return []
        except Exception as e:
            logger.error(f"Error searching music: {e}")
            return []

    def generate_playlist_prompt(
        self,
        mood: str = None,
        genre: str = None,
        era: str = None,
        num_songs: int = 50,
    ) -> str:
        """Generate a prompt for LLM playlist generation.

        Args:
            mood: Playlist mood (relaxing, energetic, sad, etc)
            genre: Music genre (rock, pop, jazz, etc)
            era: Time period (70s, 80s, 90s, modern, etc)
            num_songs: Number of songs to generate

        Returns:
            Formatted prompt for the LLM
        """
        conditions = []
        if mood:
            conditions.append(f"mood: {mood}")
        if genre:
            conditions.append(f"genre: {genre}")
        if era:
            conditions.append(f"era: {era}")

        conditions_str = ", ".join(conditions) if conditions else "diverse"

        prompt = f"""Generate a playlist of {num_songs} songs with the following characteristics: {conditions_str}

For each song, provide:
- Title
- Artist
- Album (or None if unknown)
- Year (or empty string if unknown)

Return as JSON array of objects with keys: title, artist, album, year

Make sure the songs are well-known and available on music streaming services."""

        return prompt

    def generate_playlist(
        self,
        mood: str = None,
        genre: str = None,
        era: str = None,
        num_songs: int = 50,
    ) -> List[Song]:
        """Generate a playlist using the LLM.

        Args:
            mood: Playlist mood
            genre: Music genre
            era: Time period
            num_songs: Number of songs

        Returns:
            List of Song objects
        """
        if not self.agent:
            logger.warning("LLM agent not initialized")
            return []

        try:
            prompt = self.generate_playlist_prompt(mood, genre, era, num_songs)
            response = self.agent.run(prompt)

            # Extract text from RunOutput (agno returns RunOutput object)
            # RunOutput.content contains the main response text
            response_text = str(response.content) if response and response.content else ""

            # Try to parse JSON from response
            try:
                import re
                json_match = re.search(r'\[.*\]', response_text, re.DOTALL)
                if json_match:
                    songs_data = json.loads(json_match.group())
                    return [Song(**song) for song in songs_data if isinstance(song, dict)]
            except (json.JSONDecodeError, ValueError):
                logger.debug("Could not parse JSON from LLM response")

            return []
        except Exception as e:
            logger.error(f"Error generating playlist: {e}")
            return []


def create_llm_from_config(config_dict: Dict[str, Any]) -> Optional[MusicSearchTools]:
    """Create an LLM instance from configuration dict.

    Args:
        config_dict: Configuration with keys:
            - provider: LLM provider (ollama, openai, etc)
            - model: Model name
            - api_key: API key (for OpenAI)
            - base_url: Base URL (for OpenAI-compatible)
            - ollama_host: Ollama server URL
            - search: Dict with search provider config

    Returns:
        MusicSearchTools instance or None if not configured
    """
    if not config_dict or not config_dict.get("enabled"):
        return None

    search_config = config_dict.get("search", {})

    return MusicSearchTools(
        provider=config_dict.get("provider", "ollama"),
        model=config_dict.get("model"),
        api_key=config_dict.get("api_key"),
        base_url=config_dict.get("base_url"),
        ollama_host=config_dict.get("ollama_host", "http://localhost:11434"),
        tavily_api_key=search_config.get("tavily_api_key"),
        searxng_host=search_config.get("searxng_host"),
        exa_api_key=search_config.get("exa_api_key"),
        brave_api_key=search_config.get("brave_api_key"),
    )
