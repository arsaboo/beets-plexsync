import pytest

from beetsplug.providers.spotify import add_tracks_to_spotify_playlist
from tests.test_playlist_import import DummyLogger


class SpotifyClient:
    def __init__(self, current_ids, fail_add=False):
        self.current_ids = list(current_ids)
        self.fail_add = fail_add
        self.events = []

    def current_user(self):
        return {"id": "user"}

    def user_playlists(self, user_id, limit=50):
        return {
            "items": [{"name": "Mix", "id": "playlist"}],
            "next": None,
        }

    def playlist_items(self, playlist_id, additional_types=None):
        return {
            "items": [{"track": {"id": track_id}} for track_id in self.current_ids],
            "next": None,
        }

    def user_playlist_add_tracks(self, user_id, playlist_id, tracks, position=0):
        self.events.append(("add", list(tracks)))
        if self.fail_add:
            raise RuntimeError("add failed")
        self.current_ids[position:position] = list(tracks)

    def user_playlist_remove_all_occurrences_of_tracks(
            self, user_id, playlist_id, tracks):
        self.events.append(("remove", list(tracks)))
        remove = set(tracks)
        self.current_ids = [track_id for track_id in self.current_ids
                            if track_id not in remove]


class Plugin:
    def __init__(self, client):
        self.sp = client
        self._log = DummyLogger()


def test_add_is_verified_before_obsolete_tracks_are_removed():
    client = SpotifyClient(["old", "keep"])

    assert add_tracks_to_spotify_playlist(
        Plugin(client), "Mix", ["new", "keep"]
    )

    assert [event[0] for event in client.events] == ["add", "remove"]
    assert client.current_ids == ["new", "keep"]


def test_failed_add_never_removes_existing_tracks():
    client = SpotifyClient(["old"] , fail_add=True)

    with pytest.raises(RuntimeError, match="add failed"):
        add_tracks_to_spotify_playlist(Plugin(client), "Mix", ["new"])

    assert client.current_ids == ["old"]
    assert client.events == [("add", ["new"])]


def test_add_only_mode_preserves_obsolete_tracks():
    client = SpotifyClient(["old"])

    assert add_tracks_to_spotify_playlist(
        Plugin(client), "Mix", ["new"], allow_removals=False
    )

    assert client.current_ids == ["new", "old"]
    assert client.events == [("add", ["new"])]


def test_empty_target_is_rejected():
    client = SpotifyClient(["old"])

    assert not add_tracks_to_spotify_playlist(Plugin(client), "Mix", [])
    assert client.current_ids == ["old"]
    assert client.events == []
