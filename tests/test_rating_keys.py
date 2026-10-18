"""Rating-key type convention across the beets/Plex boundary.

beets stores the `plex_ratingkey` flexible attribute as a string, while plexapi
returns `ratingKey` as an int and rejects strings. Anything that keys a dict or
calls fetchItem must normalize, or lookups silently miss.
"""

import types

from beetsplug.plex import smartplaylists
from beetsplug.plexsync import PlexSync
from beetsplug.utils.helpers import plex_key


class PlexKeyTest:
    def test_normalizes_both_sources_to_str(self):
        assert plex_key(303) == "303"
        assert plex_key("303") == "303"
        assert plex_key(" 303 ") == "303"

    def test_absent_and_placeholder_keys_are_none(self):
        assert plex_key(None) is None
        assert plex_key("") is None
        assert plex_key("0") is None
        assert plex_key("0.0") is None
        assert plex_key(0) is None

    def test_int_and_str_forms_meet_on_one_key(self):
        assert plex_key(7) == plex_key("7")


class LookupKeyTest:
    def test_builder_keys_on_str_from_int_flex_value(self):
        synced = types.SimpleNamespace(plex_ratingkey=7, title="T", artist="A", album="A")
        unsynced = types.SimpleNamespace(plex_ratingkey="0.0", title="U", artist="A", album="A")
        fake_plugin = types.SimpleNamespace(
            _log=types.SimpleNamespace(debug=lambda *a, **k: None),
            _extract_vector_metadata=lambda item: {"id": None},
        )
        fake_lib = types.SimpleNamespace(items=lambda: [synced, unsynced])

        lookup = PlexSync._build_plex_lookup_and_vector_index(fake_plugin, fake_lib)

        assert list(lookup) == ["7"]
        assert lookup.get(plex_key(7)) is synced
        assert plex_key(0) not in lookup

    def test_dedupe_resolves_int_plex_key_against_str_lookup(self):
        item = types.SimpleNamespace(plex_ratingkey="7", title="T", plex_userrating=0)
        track = types.SimpleNamespace(ratingKey=7, title="T")

        result = smartplaylists._dedupe_by_rating_key([track], {"7": item})

        assert result == [item]


class DirectMatchKeyTypeTest:
    def test_fetch_item_receives_an_int(self, monkeypatch):
        from beetsplug import plexsync as plexsync_mod

        fetched = []

        class Music:
            def fetchItem(self, key):
                fetched.append(key)
                return types.SimpleNamespace(title="T", artist="A", album="Al")

        candidate = types.SimpleNamespace(
            metadata={"title": "T", "artist": "A", "album": "Al", "plex_ratingkey": "303"},
            score=0.9,
            as_item_proxy=lambda: types.SimpleNamespace(title="T", artist="A", album="Al"),
        )
        plugin = types.SimpleNamespace(
            music=Music(),
            _log=types.SimpleNamespace(debug=lambda *a, **k: None),
            _match_score_for_query=lambda song, track: 0.95,
            _queue_candidate_confirmation=lambda **kw: None,
        )
        monkeypatch.setattr(plexsync_mod, "plex_track_distance", lambda a, b: (0.9, None))

        track = PlexSync._try_candidate_direct_match(
            plugin, candidate, {"title": "T", "artist": "A", "album": "Al"}
        )

        assert track is not None
        assert fetched == [303]
        assert isinstance(fetched[0], int)

    def test_placeholder_rating_key_skips_fetch(self):
        fetched = []
        plugin = types.SimpleNamespace(
            music=types.SimpleNamespace(fetchItem=lambda key: fetched.append(key)),
            _log=types.SimpleNamespace(debug=lambda *a, **k: None),
        )
        candidate = types.SimpleNamespace(metadata={"plex_ratingkey": "0"})

        assert PlexSync._try_candidate_direct_match(plugin, candidate, {"title": "T"}) is None
        assert fetched == []
