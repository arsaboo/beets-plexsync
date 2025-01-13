"""LLM integration for beets plugins."""

import logging
from typing import Optional
from openai import OpenAI
from beets import config
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

class CleanedMetadata(BaseModel):
    """Pydantic model for cleaned metadata response."""
    title: Optional[str] = Field(None, description="Cleaned song title")
    album: Optional[str] = Field(None, description="Cleaned album name")
    artist: Optional[str] = Field(None, description="Cleaned artist name")

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

        client_args = {
            "api_key": specific_config.get("api_key") or base_config["api_key"].get(),
        }

        base_url = specific_config.get("base_url") or base_config["base_url"].get()
        if (base_url):
            client_args["base_url"] = base_url

        return OpenAI(**client_args)
    except Exception as e:
        return None


def clean_search_string(client, title=None, album=None, artist=None):
    """Clean and format search strings using LLM."""
    if not client or not any([title, album, artist]):
        return title, album, artist

    logger.info("Starting LLM cleaning process for: %s - %s - %s", title, album, artist)

    # Get model name for logging
    model = config["llm"].get(dict).get("search", {}).get("model") or config["llm"]["model"].get()
    base_url = config["llm"].get(dict).get("search", {}).get("base_url") or config["llm"]["base_url"].get()
    logger.info("Using LLM model: %s at %s", model, base_url)

    sys_prompt = """
    You are a music metadata cleaner. Clean and format the provided music metadata
    following these rules:
    1. Remove unnecessary information in parentheses/brackets; remove punctuations
    2. Remove featuring artists or 'ft.' mentions from title
    3. Remove version indicators (Original Mix, Radio Edit, etc.)
    4. Remove soundtrack/movie references
    5. Return only core metadata
    6. Return a valid JSON object matching this structure:
    {
        "title": "cleaned song title",
        "album": "cleaned album name",
        "artist": "cleaned artist name"
    }
    7. Return null for missing fields
    8. Do not infer or add information
    """

    # Build context from provided fields
    context = {
        "title": title if title else None,
        "album": album if album else None,
        "artist": artist if artist else None
    }

    user_prompt = f"Clean this music metadata (return only valid JSON):\n{context}"

    try:
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt}
        ]

        logger.info("Sending request to LLM service...")
        logger.debug("Request details: model=%s, messages=%s", model, messages)

        # Add timeout for the request
        try:
            from contextlib import contextmanager
            import signal

            @contextmanager
            def timeout(seconds):
                def handler(signum, frame):
                    raise TimeoutError("LLM request timed out")

                # Set the timeout handler
                signal.signal(signal.SIGALRM, handler)
                signal.alarm(seconds)
                try:
                    yield
                finally:
                    signal.alarm(0)

            # Attempt request with timeout
            with timeout(30):  # 30 second timeout
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.1
                )
                logger.info("LLM request successful")

        except TimeoutError:
            logger.error("LLM request timed out after 30 seconds")
            return title, album, artist
        except Exception as e:
            logger.error("Error during LLM request: %s", str(e))
            return title, album, artist

        raw_response = response.choices[0].message.content.strip()
        logger.debug("Raw LLM response: %s", raw_response)

        import json
        import re

        # Try to extract JSON from the response
        json_match = re.search(r'\{.*\}', raw_response, re.DOTALL)
        if not json_match:
            logger.error("No JSON object found in LLM response: %s", raw_response)
            return title, album, artist

        try:
            # Parse response using Pydantic model
            cleaned = CleanedMetadata.model_validate_json(json_match.group())
            logger.info("Successfully cleaned metadata: %s", cleaned.model_dump())

            return (
                cleaned.title or title,
                cleaned.album or album,
                cleaned.artist or artist
            )

        except Exception as e:
            logger.error("Error parsing LLM response: %s", str(e))
            # Try one more time with basic JSON fixes
            try:
                json_str = json_match.group()
                # Ensure property names are double-quoted
                json_str = re.sub(r'(\w+):', r'"\1":', json_str)
                cleaned = CleanedMetadata.model_validate_json(json_str)
                logger.info("Successfully cleaned metadata after fixes: %s", cleaned.model_dump())

                return (
                    cleaned.title or title,
                    cleaned.album or album,
                    cleaned.artist or artist
                )
            except Exception as e:
                logger.error("Failed to parse JSON even after fixes: %s", str(e))
                return title, album, artist

    except Exception as e:
        logger.error("Error in LLM cleaning process: %s", str(e))
        return title, album, artist