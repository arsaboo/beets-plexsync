"""LLM integration for beets plugins."""

import logging
from openai import OpenAI
from beets import config

logger = logging.getLogger(__name__)

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
        if base_url:
            client_args["base_url"] = base_url

        return OpenAI(**client_args)
    except Exception as e:
        return None


def clean_search_string(client, title=None, album=None, artist=None):
    """Clean and format search strings using LLM.

    Args:
        client: OpenAI client instance
        title: Track title (optional)
        album: Album name (optional)
        artist: Artist name (optional)

    Returns:
        tuple: (cleaned_title, cleaned_album, cleaned_artist)
    """
    if not client or not any([title, album, artist]):
        return title, album, artist

    sys_prompt = """
    You are a music metadata cleaner. Clean and format the provided music metadata
    following these rules:
    1. Remove unnecessary information in parentheses/brackets; remove punctuations
    2. Remove featuring artists or 'ft.' mentions from title
    3. Remove version indicators (Original Mix, Radio Edit, etc.)
    4. Remove soundtrack/movie references
    5. Return only core metadata
    6. Return a valid JSON object with double-quoted property names
    7. Return null for missing fields
    8. Do not infer or add information

    Example format:
    {
        "title": "cleaned song title",
        "album": "cleaned album name",
        "artist": "cleaned artist name"
    }
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

        logger.debug(f"Original query: {context}")

        response = client.chat.completions.create(
            model=config["llm"].get(dict).get("search", {}).get("model") or config["llm"]["model"].get(),
            messages=messages,
            temperature=0.1
        )

        raw_response = response.choices[0].message.content.strip()
        logger.debug(f"Raw LLM response: {raw_response}")

        import json
        import re

        # Try to extract JSON from the response
        json_match = re.search(r'\{.*\}', raw_response, re.DOTALL)
        if not json_match:
            logger.error(f"No JSON object found in LLM response: {raw_response}")
            return title, album, artist

        try:
            # Try parsing the extracted JSON
            cleaned = json.loads(json_match.group())
            logger.debug(f"LLM cleaned metadata: {cleaned}")

            # Validate the cleaned data structure
            if not isinstance(cleaned, dict):
                logger.error("LLM response is not a dictionary")
                return title, album, artist

            return (
                cleaned.get("title", title),
                cleaned.get("album", album),
                cleaned.get("artist", artist)
            )

        except json.JSONDecodeError as e:
            # If JSON parsing fails, try to fix common formatting issues
            json_str = json_match.group()
            # Ensure property names are double-quoted
            json_str = re.sub(r'(\w+):', r'"\1":', json_str)
            try:
                cleaned = json.loads(json_str)
                logger.debug(f"Fixed and parsed JSON: {cleaned}")
                return (
                    cleaned.get("title", title),
                    cleaned.get("album", album),
                    cleaned.get("artist", artist)
                )
            except json.JSONDecodeError:
                logger.error(f"Failed to parse JSON even after fixes: {json_str}")
                return title, album, artist

    except Exception as e:
        logger.error(f"Error in LLM cleaning: {e}")
        return title, album, artist