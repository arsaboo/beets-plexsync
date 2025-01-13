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
    6. Return JSON with 'title', 'album', and 'artist' fields
    7. Return null for missing fields
    8. Do not infer or add information

    Example output:
    {
        "title": "cleaned song title",
        "album": "cleaned album name",
        "artist": "cleaned artist name"
    }
    """

    # Build context from provided fields
    context = {}
    if title:
        context["title"] = title
    if album:
        context["album"] = album
    if artist:
        context["artist"] = artist

    user_prompt = f"Clean this music metadata:\n{context}"

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

        import json
        import re

        json_match = re.search(r'\{.*\}', response.choices[0].message.content, re.DOTALL)
        if json_match:
            cleaned = json.loads(json_match.group())
            logger.debug(f"LLM cleaned metadata: {cleaned}")
            return (
                cleaned.get("title", title),
                cleaned.get("album", album),
                cleaned.get("artist", artist)
            )

    except Exception as e:
        logger.error(f"Error in LLM cleaning: {e}")

    return title, album, artist