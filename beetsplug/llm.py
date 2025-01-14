"""LLM integration for beets plugins."""

import logging
from typing import Optional
import httpx
from openai import OpenAI
from beets import config
from pydantic import BaseModel, Field

# Simple logger for standalone use
logger = logging.getLogger('beets')


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


def clean_search_string(client, title=None, album=None, artist=None):
    """Clean and format search strings using LLM."""
    if not client or not any([title, album, artist]):
        return title, album, artist

    logger.info("Starting LLM cleaning for: %s - %s - %s", title, album, artist)

    if (
        title
        and not title.strip()
        or album
        and not album.strip()
        or artist
        and not artist.strip()
    ):
        logger.debug("Empty string detected after stripping, returning original values")
        return title, album, artist

    try:
        model = config["llm"].get(dict).get("search", {}).get("model") or config["llm"]["model"].get()
        logger.debug("Using model: %s", model)

        messages = [
            {
                "role": "system",
                "content": """Clean the provided music metadata by removing:
- Text in parentheses/brackets (unless part of primary name)
- Featuring artists, 'ft.', 'feat.' mentions
- Version indicators (Original Mix, Radio Edit, etc.)
- Soundtrack references (From the motion picture, OST)
- Qualifiers (Single, Album Version)
Keep language indicators and core artist/song names unchanged.""",
            },
            {
                "role": "user",
                "content": f"Clean this music metadata - Title: {title or 'None'}, Album: {album or 'None'}, Artist: {artist or 'None'}",
            },
        ]

        logger.debug("Sending request to LLM...")

        # Standard chat completion request (works with Ollama and OpenAI)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.1,
            max_tokens=150,
            response_format={"type": "json_object"},
            timeout=15.0
        )

        if not response.choices:
            logger.error("Empty response from LLM")
            return title, album, artist

        raw_response = response.choices[0].message.content.strip()
        logger.debug("Raw LLM response: %s", raw_response)

        # Parse response using Pydantic model
        cleaned = CleanedMetadata.model_validate_json(raw_response)
        logger.info("Successfully cleaned metadata: %s", cleaned.model_dump())

        return (
            cleaned.title or title,
            cleaned.album or album,
            cleaned.artist or artist,
        )

    except httpx.TimeoutException:
        logger.error("LLM request timed out")
        return title, album, artist
    except Exception as e:
        logger.error("Error in clean_search_string: %s", str(e))
        logger.debug(
            "Original values: title=%s, album=%s, artist=%s", title, album, artist
        )
        return title, album, artist
