"""LLM-enhanced search for track metadata cleanup."""

from __future__ import annotations

import json
import logging
import re
from typing import Dict, Optional

logger = logging.getLogger("harmony.ai.search")


def search_track_info(
    llm_agent,
    search_query: str,
    timeout: int = 30
) -> Optional[Dict[str, str]]:
    """Search for track information using LLM with search tools.

    Args:
        llm_agent: MusicSearchTools instance with agent
        search_query: Search query (e.g., "Song Title by Artist from Album")
        timeout: Timeout in seconds

    Returns:
        Dict with cleaned metadata {'title', 'artist', 'album'} or None
    """
    if not llm_agent or not hasattr(llm_agent, 'agent') or not llm_agent.agent:
        logger.debug("LLM agent not available")
        return None

    try:
        prompt = f"""Search for information about this song: {search_query}

Return ONLY the most accurate metadata in this exact JSON format:
{{
  "title": "exact song title",
  "artist": "exact artist name",
  "album": "exact album name"
}}

Do not include any additional text or explanation, just the JSON object."""

        response = llm_agent.agent.run(prompt)

        # Extract text from RunOutput (agno returns RunOutput object)
        # RunOutput.content contains the main response text
        response_text = str(response.content) if response and response.content else ""

        # Try to parse JSON from response
        try:
            # Extract JSON object if embedded in text
            json_match = re.search(r'\{[^{}]*\}', response_text, re.DOTALL)
            if json_match:
                metadata = json.loads(json_match.group())

                # Validate required fields
                if isinstance(metadata, dict) and 'title' in metadata:
                    cleaned = {
                        'title': str(metadata.get('title', '')).strip(),
                        'artist': str(metadata.get('artist', '')).strip(),
                        'album': str(metadata.get('album', '')).strip(),
                    }

                    # Only return if we have at least title
                    if cleaned['title']:
                        logger.debug(f"LLM cleaned metadata: {cleaned}")
                        return cleaned
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug(f"Could not parse JSON from LLM response: {exc}")

        return None
    except Exception as exc:
        logger.error(f"Error in LLM track search: {exc}")
        return None


def is_llm_search_enabled(config: Dict) -> bool:
    """Check if LLM search is enabled in configuration.

    Args:
        config: Configuration dict with llm settings

    Returns:
        True if LLM search is enabled
    """
    if not config:
        return False

    llm_config = config.get('llm', {})
    if not llm_config:
        return False

    # Check if explicitly enabled
    if not llm_config.get('enabled', False):
        return False

    # Check for use_llm_search flag
    return llm_config.get('use_llm_search', False)
