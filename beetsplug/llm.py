"""LLM integration for beets plugins."""

import logging
from typing import Optional
import httpx
from openai import OpenAI
from beets import config
from pydantic import BaseModel, Field

# Simple logger for standalone use
logger = logging.getLogger('beets')

# Add cache at module level
_metadata_cache = {}

class CleanedMetadata(BaseModel):
    """Pydantic model for cleaned metadata response."""

    title: Optional[str] = Field(
        None,
        description="Cleaned song title without features, versions, or other extra info",
    )
    album: Optional[str] = Field(
        None, description="Cleaned album name without soundtrack/movie references"
    )
    artist: Optional[str] = Field(
        None, description="Main artist name without featuring artists"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "title": "Clean Song Name",
                    "album": "Album Name",
                    "artist": "Artist Name",
                }
            ]
        }
    }


def setup_llm(llm_type="plexsonic"):
    """Setup LLM client using OpenAI-compatible API.

    Args:
        llm_type: Type of LLM service to configure ('plexsonic' or 'search')
    """
    try:
        # Get base LLM config
        base_config = config["llm"]

        # Get specific config if it exists, otherwise use base config
        specific_config = config["llm"].get(dict).get(llm_type, {})

        # Create custom httpx client with timeouts
        timeout_settings = httpx.Timeout(
            connect=5.0,  # connection timeout
            read=15.0,  # read timeout
            write=5.0,  # write timeout
            pool=10.0,  # pool timeout
        )

        http_client = httpx.Client(timeout=timeout_settings, follow_redirects=True)

        client_args = {
            "api_key": specific_config.get("api_key") or base_config["api_key"].get(),
            "http_client": http_client,
        }

        base_url = specific_config.get("base_url") or base_config["base_url"].get()
        if (base_url):
            client_args["base_url"] = base_url

        return OpenAI(**client_args)
    except Exception as e:
        logger.error("Failed to setup LLM client: %s", str(e))
        return None


def _make_cache_key(title, album, artist):
    """Create a cache key from metadata."""
    return f"{title}::{album}::{artist}"

def clean_search_string(client, title=None, album=None, artist=None):
    """Clean and format search strings using LLM."""
    if not client or not any([title, album, artist]):
        logger.debug("Skipping LLM cleaning - no input or client")
        return title, album, artist

    # Check cache first
    cache_key = _make_cache_key(title, album, artist)
    if cache_key in _metadata_cache:
        cached = _metadata_cache[cache_key]
        logger.debug("Using cached cleaned metadata - title: {0}, album: {1}, artist: {2}",
                    cached[0], cached[1], cached[2])
        return cached

    # Format metadata for logging
    metadata = {
        "title": title or "None",
        "album": album or "None",
        "artist": artist or "None"
    }

    # Use format strings compatible with beets logger
    logger.debug("Starting LLM cleaning for - title: {0}, album: {1}, artist: {2}",
                metadata["title"], metadata["album"], metadata["artist"])

    # Early validation
    if any(val and not val.strip() for val in [title, album, artist]):
        logger.debug("Empty string detected after stripping")
        return title, album, artist

    try:
        model = config["llm"].get(dict).get("search", {}).get("model") or config["llm"]["model"].get()
        logger.debug("Using model: {0}", model)

        messages = [
            {
                "role": "system",
                "content": """Clean the provided music metadata by removing:
- Text in parentheses/brackets
- Featuring artists, 'ft.', 'feat.' mentions
- Version indicators (Original Mix, Radio Edit, etc.)
- Soundtrack references (From the motion picture, OST)
- Qualifiers (Single, Album Version)
- Any additional data that is not related to the core artist/song
Keep language indicators and core artist/song names unchanged.""",
            },
            {
                "role": "user",
                "content": "Clean this music metadata - "
                          f"Title: {title or 'None'}, "
                          f"Album: {album or 'None'}, "
                          f"Artist: {artist or 'None'}",
            },
        ]

        logger.debug("Sending request to LLM for model: {0}", model)

        # Add retries for failed requests
        max_retries = 3
        retry_count = 0

        while retry_count < max_retries:
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=150,
                    response_format={"type": "json_object"},
                    timeout=15.0
                )
                break
            except Exception as e:
                retry_count += 1
                if retry_count == max_retries:
                    logger.error("LLM request failed after {0} retries: {1}", max_retries, str(e))
                    return title, album, artist
                logger.warning("Retry {0}/{1} - LLM request failed: {2}",
                             retry_count, max_retries, str(e))
                continue

        if not response or not response.choices:
            logger.error("Empty response from LLM service")
            return title, album, artist

        raw_response = response.choices[0].message.content.strip()
        if not raw_response:
            logger.error("Empty content in LLM response")
            return title, album, artist

        logger.debug("Raw LLM response: {0}", raw_response)

        try:
            cleaned = CleanedMetadata.model_validate_json(raw_response)

            # Only use cleaned values if they exist and aren't "None"
            cleaned_title = cleaned.title if cleaned.title and cleaned.title != "None" else title
            cleaned_album = cleaned.album if cleaned.album and cleaned.album != "None" else album
            cleaned_artist = cleaned.artist if cleaned.artist and cleaned.artist != "None" else artist

            # Store actual cleaned values
            cleaned_result = (cleaned_title, cleaned_album, cleaned_artist)

            logger.info("Successfully cleaned metadata - title: {0}, album: {1}, artist: {2}",
                       cleaned_title, cleaned_album, cleaned_artist)

            # Cache the cleaned result
            _metadata_cache[cache_key] = cleaned_result
            return cleaned_result

        except Exception as e:
            logger.error("Failed to parse LLM response: {0}", str(e))
            return title, album, artist

    except httpx.TimeoutException:
        logger.error("LLM request timed out")
        return title, album, artist
    except Exception as e:
        logger.error("Error in clean_search_string: {0}", str(e))
        logger.debug("Original values - title: {0}, album: {1}, artist: {2}",
                    metadata["title"], metadata["album"], metadata["artist"])
        return title, album, artist
