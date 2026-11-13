from types import SimpleNamespace

from beetsplug.providers import spotify
from tests.test_playlist_import import DummyLogger


def test_search_cache_is_scoped_to_market(monkeypatch):
    monkeypatch.setattr(spotify, "_spotify_search_result_cache", {})
    calls = []

    def search(*, market, **kwargs):
        calls.append(market)
        return {"tracks": {"items": [{
            "id": market, "name": "Song", "artists": [{"name": "Artist"}],
            "is_playable": True,
        }]}}

    plugin = SimpleNamespace(sp=SimpleNamespace(search=search), _log=DummyLogger())
    item = SimpleNamespace(title="Song", artist="Artist", album="Album")
    assert spotify.search_spotify_track(plugin, item, market="US") == "US"
    assert spotify.search_spotify_track(plugin, item, market="GB") == "GB"
    assert spotify.search_spotify_track(plugin, item, market="US") == "US"
    assert calls == ["US", "GB"]
