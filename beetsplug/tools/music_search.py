import logging
from beetsplug.llm import MusicSearchTools, get_search_toolkit

log = logging.getLogger('beets.plexsync.music_search')

class MusicSearch:
    def __init__(self):
        self.toolkit = get_search_toolkit()

    def run(self, query: str):
        if not self.toolkit:
            log.error("Search toolkit unavailable. Install agno and configure search engines.")
            return {"title": query, "artist": "", "album": None}

        try:
            log.info("Searching for track info: {0}", query)
            song_info = self.toolkit.search_song_info(query)

            result = {
                "title": song_info.get("title") or query,
                "album": song_info.get("album"),
                "artist": song_info.get("artist") or ""
            }

            log.info("Found track info: {}", result)
            return result
        except Exception as e:
            log.error("Error in agent-based search: {0}", str(e))
            return {"title": query, "artist": "", "album": None}
