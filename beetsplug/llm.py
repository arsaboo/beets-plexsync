"""LLM integration for beets plugins."""

import json
import logging
import re
from typing import Optional

import requests
from beets import config
from json_repair import repair_json  # New import for JSON repair
from pydantic import ValidationError

# Simple logger for standalone use
logger = logging.getLogger('beets')


def search_track_info(query: str):
    """
    Sends a search query to the Perplexica Search API and extracts structured track information.

    Args:
        query (str): The user-provided search query for a song.

    Returns:
        dict: A dictionary containing the track's Title, Album, and Artist, with missing fields set to None.
    """

    payload = {
        "chatModel": {
            "provider": config["llm"]["search"]["provider"].get(),
            "model": config["llm"]["search"]["model"].get()
        },
        "embeddingModel": {
            "provider": config["llm"]["search"]["provider"].get(),
            "model": config["llm"]["search"]["embedding_model"].get()
        },
        "optimizationMode": "balanced",
        "focusMode": "webSearch",
        "query": f"""
        Extract structured music metadata from the following query and return ONLY in JSON format.
        Do not include any explanations, markdown formatting, or additional text.
        Ensure the JSON contains the exact extracted details.

        Query: {query}

        Return JSON in this exact structure:
        {{
            "Title": "<track title>",
            "Album": "<album name>",
            "Artist": "<artist name>"
        }}
        """,
        "history": []
    }

    base_url = config["llm"]["search"]["base_url"].get()

    try:
        response = requests.post(base_url, json=payload, timeout=30)
        logger.debug("API Response: {}", response.json().get("message"))
        if not response.text.strip():
            logger.error("Error: Received empty response from API")
            return None  # Return None if no response

        response.raise_for_status()
        data = response.json()

        message = data.get("message", "").strip()
        if not message:
            logger.error("Error: 'message' field is missing or empty in API response")
            return None  # Return None if message is missing

        # Clean & Repair JSON before parsing
        cleaned_message = clean_json_string(message)

        try:
            track_info = json.loads(cleaned_message)  # Convert cleaned string to JSON
            normalized_track_info = normalize_keys(track_info)  # Convert keys to lowercase

            # Create a dictionary with default None values
            structured_data = {
                "title": normalized_track_info.get("title"),
                "album": normalized_track_info.get("album"),
                "artist": normalized_track_info.get("artist")
            }

            return structured_data

        except (json.JSONDecodeError, ValidationError) as e:
            logger.error("JSON Parsing Error: {}", str(e))
            return None  # Return None if JSON is invalid

    except requests.exceptions.RequestException as e:
        logger.error("Request Error: {}", str(e))
        return None  # Return None on API failure

def clean_json_string(json_string: str):
    """
    Cleans the API response by removing Markdown-style JSON formatting,
    trimming excess whitespace, and ensuring the JSON string is well-formed.

    Args:
        json_string (str): The raw JSON string from the API response.

    Returns:
        str: A cleaned JSON string ready for parsing.
    """
    try:
        json_string = repair_json(json_string)
    except Exception as e:
        logger.error("Error repairing JSON: {}", str(e))

    return json_string

def normalize_keys(data: dict):
    """
    Converts API response keys to lowercase to match Pydantic's expected field names.

    Args:
        data (dict): The API response dictionary.

    Returns:
        dict: Normalized dictionary with lowercase keys.
    """
    return {key.lower(): value for key, value in data.items()}
