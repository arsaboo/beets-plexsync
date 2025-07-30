import logging
from agno.agent import Agent
from agno.models.ollama import Ollama
from beets.library import Library

from beetsplug.caching import Cache
from beetsplug.tools.beets_library import BeetsLibrary
from beetsplug.tools.music_search import MusicSearch
from beetsplug.tools.plex_tool import PlexTool
from beetsplug.tools.playlist_importer import PlaylistImporter

log = logging.getLogger('beets.plexsync.agent')

class BeetsAgent:
    def __init__(self, lib: Library, cache: Cache):
        self.lib = lib
        self.cache = cache
        self.agent = self._initialize_agent()

    def _initialize_agent(self):
        # Initialize tools
        playlist_importer = PlaylistImporter(self.cache)
        plex_tool = PlexTool(self.cache)
        music_search = MusicSearch()
        beets_library = BeetsLibrary(self.lib)

        # Create a list of tools
        tools = [
            playlist_importer,
            plex_tool,
            music_search,
            beets_library,
        ]

        # Initialize the agent with the tools
        # We can use a simple Ollama model for now, as the main logic is in the tools
        agent = Agent(
            model=Ollama(id="qwen3:latest", host="http://localhost:11434"),
            tools=tools
        )
        return agent

    def run(self, command: str, **kwargs):
        log.debug(f"Running agent with command: {command} and args: {kwargs}")

        # Get the tools from the agent
        playlist_importer = self.agent.tools[0]
        plex_tool = self.agent.tools[1]
        music_search = self.agent.tools[2]
        beets_library = self.agent.tools[3]

        if command == "import_playlist":
            url = kwargs.get("url")
            playlist_name = kwargs.get("playlist_name")
            if url and playlist_name:
                songs = playlist_importer.run(url)
                if songs:
                    plex_tool.add_to_playlist(songs, playlist_name)
        elif command == "update_library":
            plex_tool.update_library()
        elif command == "sync":
            items = kwargs.get("items")
            write = kwargs.get("write")
            force = kwargs.get("force")
            plex_tool.fetch_plex_info(items, write, force)
        elif command == "add_to_playlist":
            items = kwargs.get("items")
            playlist_name = kwargs.get("playlist_name")
            plex_tool.add_to_playlist(items, playlist_name)
        elif command == "remove_from_playlist":
            items = kwargs.get("items")
            playlist_name = kwargs.get("playlist_name")
            plex_tool.remove_from_playlist(items, playlist_name)
        elif command == "sync_recent":
            days = kwargs.get("days")
            plex_tool.update_recently_played(self.lib, days)
        else:
            log.error(f"Unknown command: {command}")
