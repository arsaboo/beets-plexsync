"""
Utility functions for interacting with Language Models (LLMs).
"""
import logging
import re
import json
from openai import OpenAI
from pydantic import BaseModel, Field # Assuming SongRecommendations might be defined here or passed
from typing import List


_log = logging.getLogger('beets.plexsync.llm_utils')

# Define Pydantic models here if they are primarily used by LLM functions
# Otherwise, they should be passed or imported from a central models location.
# For now, moving them here as they are tightly coupled with get_llm_recommendations output.

class Song(BaseModel):
    title: str
    artist: str
    album: str
    year: str = Field(description="Year of release")

class SongRecommendations(BaseModel):
    songs: List[Song]


def setup_llm_client(api_key, base_url=None):
    """
    Sets up and returns an LLM client (OpenAI compatible).
    """
    try:
        client_args = {"api_key": api_key}
        if base_url:
            client_args["base_url"] = base_url

        llm_client = OpenAI(**client_args)
        _log.info("Successfully initialized LLM client.")
        return llm_client
    except Exception as e:
        _log.error(f"Unable to connect to LLM service during setup: {e}")
        return None

def get_llm_song_recommendations(llm_client, model_name, num_songs, user_prompt):
    """
    Gets song recommendations from the LLM service.
    """
    if not llm_client:
        _log.error("LLM client not available for recommendations.")
        return None
    if not user_prompt:
        _log.error("User prompt not provided for LLM recommendations.")
        return None

    sys_prompt = f"""
    You are a music recommender. You will reply with {num_songs} song
    recommendations in a JSON format. Only reply with the JSON object,
    no need to send anything else. Include title, artist, album, and
    year in the JSON response. Use the JSON format:
    {{
        "songs": [
            {{
                "title": "Title of song 1",
                "artist": "Artist of Song 1",
                "album": "Album of Song 1",
                "year": "Year of release"
            }}
        ]
    }}
    """
    messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}]

    try:
        _log.info(f"Sending request to LLM service (model: {model_name}) for song recommendations.")
        chat_completion = llm_client.chat.completions.create(
            model=model_name, messages=messages, temperature=0.7
        )
        reply_content = chat_completion.choices[0].message.content
        tokens_used = chat_completion.usage.total_tokens
        _log.debug(f"LLM service used {tokens_used} tokens and replied: {reply_content}")

        # Extract and parse JSON from the reply
        json_match = re.search(r"\{.*\}", reply_content, re.DOTALL)
        if not json_match:
            _log.error(f"No JSON object found in LLM reply: {reply_content}")
            return None

        json_string = json_match.group()
        parsed_recommendations = SongRecommendations.model_validate_json(json_string)
        return parsed_recommendations

    except json.JSONDecodeError as e:
        _log.error(f"Unable to parse JSON from LLM reply. Error: {e}. Reply was: {reply_content}")
        return None
    except Exception as e: # Catch other potential errors (API errors, Pydantic validation, etc.)
        _log.error(f"Error getting LLM recommendations: {e}")
        return None


def search_track_info_llm(search_llm_client, search_model, search_embedding_model, query):
    """
    Uses an LLM (potentially a search-optimized one) to clean up or find track metadata.
    This function adapts the existing `search_track_info` logic if it were to use a generic LLM client.
    The original `search_track_info` in `llm.py` seems to use a specific POST request structure.
    This utility function provides a more standard OpenAI client compatible way if that's the direction.

    If the existing `search_track_info` from `beetsplug.llm` is to be kept as is (with its own client/endpoint),
    then this function might not be needed, or `PlexSync` would call that directly.
    For now, this provides an alternative based on the `OpenAI` client.
    """
    if not search_llm_client:
        _log.error("Search LLM client not available for track info search.")
        return None

    # This prompt would need to be designed based on how the search LLM is expected to behave.
    # For example, asking it to correct or complete metadata.
    prompt = f"""
    Given the search query: "{query}", provide the corrected and complete track information
    including title, artist, and album. If a field cannot be determined, omit it or set it to null.
    Return the information as a JSON object with keys "title", "artist", "album".
    Example: {{"title": "Bohemian Rhapsody", "artist": "Queen", "album": "A Night at the Opera"}}
    """
    messages = [{"role": "system", "content": "You are a helpful music metadata assistant."}, {"role": "user", "content": prompt}]

    try:
        _log.info(f"Sending request to Search LLM service (model: {search_model}) for query: {query}")
        chat_completion = search_llm_client.chat.completions.create(
            model=search_model, # Potentially different model for search
            messages=messages,
            temperature=0.2 # Lower temperature for more factual responses
        )
        reply_content = chat_completion.choices[0].message.content
        _log.debug(f"Search LLM replied: {reply_content}")

        json_match = re.search(r"\{.*\}", reply_content, re.DOTALL)
        if not json_match:
            _log.error(f"No JSON object found in Search LLM reply: {reply_content}")
            return None

        json_string = json_match.group()
        # We expect a simple dict here, not necessarily the Song/SongRecommendations model
        cleaned_data = json.loads(json_string)
        # Basic validation for expected keys
        if not all(k in cleaned_data for k in ["title", "artist"]):
            _log.warning(f"Search LLM JSON missing required keys (title, artist): {cleaned_data}")
            # Return partial data if possible, or None
            return {k: cleaned_data.get(k) for k in ["title", "artist", "album"] if cleaned_data.get(k)}

        return cleaned_data

    except json.JSONDecodeError as e:
        _log.error(f"Unable to parse JSON from Search LLM reply. Error: {e}. Reply was: {reply_content}")
        return None
    except Exception as e:
        _log.error(f"Error searching track info with LLM: {e}")
        return None

# Note: The original `search_track_info` in `beetsplug/llm.py` uses a direct `requests.post`
# to a specific endpoint. If that mechanism is preferred for the "search LLM",
# then `PlexSync.search_track_info` would remain as is, or that specific function
# would be moved/called. The `search_track_info_llm` above is an alternative
# if the search LLM is also an OpenAI-compatible API.
# For this refactoring, I'll assume the original `search_track_info` from `beetsplug.llm` will be kept
# and called directly by PlexSync, as it has a different client interaction pattern.
# The `get_llm_song_recommendations` is more aligned with the general OpenAI client pattern.

# The `_plex_sonicsage` method in PlexSync orchestrates calls to `get_llm_recommendations`
# and then processes the results (searching Plex, adding to playlist).
# So, `_plex_sonicsage` itself will largely remain in PlexSync, but its call to
# `get_llm_recommendations` will be to the one in this util file.
# `extract_json` is also effectively part of `get_llm_song_recommendations` now.
# `setup_llm` is replaced by `setup_llm_client`.
