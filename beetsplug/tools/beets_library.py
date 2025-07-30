import logging
from beets.library import Library

log = logging.getLogger('beets.plexsync.beets_library')

class BeetsLibrary:
    def __init__(self, lib: Library):
        self.lib = lib

    def query_library(self, query: str):
        log.debug(f"Querying beets library with: {query}")
        return self.lib.items(query)

    def update_item(self, item, values: dict):
        log.debug(f"Updating item {item} with {values}")
        for key, value in values.items():
            setattr(item, key, value)
        item.store()
