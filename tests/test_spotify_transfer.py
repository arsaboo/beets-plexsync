import importlib
import types

import pytest

from tests.test_playlist_import import ensure_stubs, DummyLogger


class Selection:
    """Stand-in for playlist_lookup.PlaylistSelection."""

    def __init__(self, items, missing=(), dropped_by_query=0, plex_total=0):
        self.items = list(items)
        self.missing = list(missing)
        self.dropped_by_query = dropped_by_query
        self.plex_total = plex_total or len(self.items) + len(self.missing)


class TransferPlugin:
    """Fake PlexSync carrying the Spotify responses a test needs.

    ``responses`` maps a requested track id to the fields Spotify answers with.
    An id missing from the map, or an entry whose ``playable`` key is absent,
    keeps the "market did not answer" shape.
    """

    def __init__(self, selection, responses=None, market="US", search=None,
                 tracks_error=None, plex_items=None):
        self._log = DummyLogger()
        self._selection = selection
        self._responses = responses or {}
        self._market = market
        self._search = search
        self._tracks_error = tracks_error
        self.calls = []
        self.plex = types.SimpleNamespace(
            playlist=lambda name: types.SimpleNamespace(items=lambda: list(plex_items or []))
        )
        self.sp = types.SimpleNamespace(tracks=self._tracks, track=self._track)
        self.sent = None
        self.search_calls = []

    def _tracks(self, ids, market=None):
        self.calls.append(("tracks", list(ids), market))
        if self._tracks_error is not None:
            raise self._tracks_error
        tracks = []
        for track_id in ids:
            entry = self._responses.get(track_id)
            if entry is None:
                tracks.append(None)
                continue
            track = {"id": entry.get("id", track_id)}
            if "playable" in entry:
                track["is_playable"] = entry["playable"]
            if "markets" in entry:
                track["available_markets"] = entry["markets"]
            if "reason" in entry:
                track["restrictions"] = {"reason": entry["reason"]}
            if "linked" in entry:
                track["linked_from"] = {"id": entry["linked"]}
            tracks.append(track)
        return {"tracks": tracks}

    def _track(self, track_id, market=None):
        self.calls.append(("track", track_id, market))
        return self._tracks([track_id], market=market)["tracks"][0]

    def authenticate_spotify(self):
        pass

    def _spotify_market(self):
        return self._market

    def _resolve_playlist_items(self, lib, plex_items, query_args=None):
        self.calls.append(("resolve", len(plex_items), tuple(query_args or ())))
        return self._selection

    def _search_spotify_track(self, beets_item, *, raise_on_error=False, market=None):
        self.search_calls.append((getattr(beets_item, "title", None), market))
        if isinstance(self._search, Exception):
            raise self._search
        if self._search is None and raise_on_error:
            raise RuntimeError("search failed")
        return self._search

    def add_tracks_to_spotify_playlist(self, playlist, tracks, *, allow_removals=True, id_groups=None):
        self.sent = (playlist, list(tracks), allow_removals, id_groups)
        return True

    def create_progress_counter(self, *args, **kwargs):
        return None


def library_item(rating_key, spotify_id, title="Song", artist="Artist", album="Album"):
    return types.SimpleNamespace(
        plex_ratingkey=str(rating_key),
        spotify_track_id=spotify_id,
        title=title,
        artist=artist,
        album=album,
    )


def plex_item(rating_key, title="Song", parent="Album"):
    return types.SimpleNamespace(ratingKey=rating_key, title=title, parentTitle=parent)


class SpotifyTransferTest:
    @pytest.fixture(autouse=True)
    def setup(self, request):
        ensure_stubs({'plexsync': {}}, request.addfinalizer)
        if 'beetsplug.plex.spotify_transfer' in importlib.sys.modules:
            importlib.reload(importlib.sys.modules['beetsplug.plex.spotify_transfer'])
        else:
            importlib.import_module('beetsplug.plex.spotify_transfer')
        self.transfer = importlib.import_module('beetsplug.plex.spotify_transfer')

    # --- availability entry semantics -------------------------------------

    @pytest.mark.parametrize("response", [{}, {"tracks": []}, {"tracks": None}])
    def test_incomplete_availability_blocks_removals(self, response):
        item = library_item(1, "known")
        plugin = TransferPlugin(Selection([item]))
        plugin.sp.tracks = lambda ids, market=None: response

        assert self.transfer.plex_to_spotify(plugin, types.SimpleNamespace(), "Mix")
        assert plugin.sent[1] == ["known"]
        assert plugin.sent[2] is False
        assert plugin.search_calls == []

    def test_market_answer_is_authoritative(self):
        entry = self.transfer._availability_entry(
            {"id": "t", "is_playable": False, "restrictions": {"reason": "market"}}
        )
        assert entry["playable"] is False
        assert entry["reason"] == "market"

    def test_null_entry_is_authoritative_gone(self):
        assert self.transfer._availability_entry(None)["playable"] is False

    def test_missing_market_answer_falls_back_to_available_markets(self):
        # Without a market Spotify omits is_playable; an empty market list is
        # the only signal that the id plays nowhere.
        assert self.transfer._availability_entry(
            {"id": "t", "available_markets": []}
        )["playable"] is False
        assert self.transfer._availability_entry(
            {"id": "t", "available_markets": ["US"]}
        )["playable"] is True
        # No signal at all stays "playable", as it has been historically.
        assert self.transfer._availability_entry({"id": "t"})["playable"] is True

    def test_relink_is_recorded_from_linked_from(self):
        entry = self.transfer._availability_entry(
            {"id": "variant", "is_playable": True, "linked_from": {"id": "orig"}}
        )
        assert entry["id"] == "variant"
        assert entry["links"] == ["orig"]

    # --- grouping --------------------------------------------------------

    def test_build_id_groups_links_cached_served_and_parent_ids(self):
        availability = {
            "orig": {"id": "variant", "playable": True, "reason": None, "links": ["orig"]},
            "other": {"id": "other", "playable": True, "reason": None, "links": []},
        }
        groups = self.transfer.build_id_groups(["orig", "variant", "other"], availability)
        assert groups["orig"] == groups["variant"]
        assert groups["other"] != groups["orig"]

    def test_build_id_groups_chains_transitively(self):
        availability = {
            "a": {"id": "a", "playable": True, "reason": None, "links": []},
            "b": {"id": "b", "playable": True, "reason": None, "links": ["a"]},
            "c": {"id": "c", "playable": True, "reason": None, "links": ["b"]},
        }
        groups = self.transfer.build_id_groups(["a", "b", "c"], availability)
        assert groups["a"] == groups["b"] == groups["c"]

    # --- transfer behaviour ----------------------------------------------

    def test_transfers_tracks_with_existing_ids(self):
        item = library_item(1, 'song-id')
        plugin = TransferPlugin(
            Selection([item], plex_total=1),
            responses={'song-id': {"playable": True}},
            plex_items=[plex_item(1)],
        )
        lib = types.SimpleNamespace(items=lambda *a, **k: pytest.fail("library scanned"))

        result = self.transfer.plex_to_spotify(plugin, lib, 'Mix', ['plex_userrating:2..'])

        assert result
        playlist, tracks, allow_removals, groups = plugin.sent
        assert playlist == 'Mix'
        assert tracks == ['song-id']
        assert allow_removals is True
        assert groups == {'song-id': 'song-id'}
        # The market of the account is what availability is asked about.
        assert [c for c in plugin.calls if c[0] == "tracks"] == [("tracks", ['song-id'], 'US')]

    def test_no_whole_library_scan_when_resolving(self):
        item = library_item(1, 'song-id')
        plugin = TransferPlugin(Selection([item], plex_total=1),
                               responses={'song-id': {"playable": True}},
                               plex_items=[plex_item(1)])
        lib = types.SimpleNamespace(items=lambda *a, **k: pytest.fail("library scanned"))
        self.transfer.plex_to_spotify(plugin, lib, 'Mix')
        assert ("resolve", 1, ()) in plugin.calls

    def test_falls_back_to_search_when_unplayable(self):
        item = library_item(1, 'orig')
        plugin = TransferPlugin(
            Selection([item], plex_total=1),
            responses={'orig': {"playable": False, "reason": "market"}},
            search='fallback',
            plex_items=[plex_item(1)],
        )
        self.transfer.plex_to_spotify(plugin, types.SimpleNamespace(), 'Mix')

        assert plugin.sent[1] == ['fallback']
        assert plugin.sent[2] is True
        # The search stays on the same market as the availability answer.
        assert plugin.search_calls == [('Song', 'US')]

    def test_relinked_id_replaces_the_cached_id(self):
        item = library_item(1, 'orig')
        plugin = TransferPlugin(
            Selection([item], plex_total=1),
            responses={'orig': {"id": "variant", "playable": True, "linked": "orig"}},
            plex_items=[plex_item(1)],
        )
        self.transfer.plex_to_spotify(plugin, types.SimpleNamespace(), 'Mix')

        assert plugin.sent[1] == ['variant']
        assert item.spotify_track_id == 'orig'
        assert plugin.sent[2] is True
        # The variant and the id cached in beets must reconcile as one track.
        groups = plugin.sent[3]
        assert groups['variant'] == groups['orig']

    def test_ids_of_one_recording_are_transferred_once(self):
        first = library_item(1, 'orig', title='Song')
        second = library_item(2, 'variant', title='Song')
        plugin = TransferPlugin(
            Selection([first, second], plex_total=2),
            responses={
                'orig': {"playable": True},
                'variant': {"playable": True, "linked": "orig"},
            },
            plex_items=[plex_item(1), plex_item(2)],
        )
        self.transfer.plex_to_spotify(plugin, types.SimpleNamespace(), 'Mix')

        assert plugin.sent[1] == ['orig']

    def test_availability_is_asked_in_batches_of_fifty(self):
        items = [library_item(i, f'id-{i}') for i in range(51)]
        responses = {f'id-{i}': {"playable": True} for i in range(51)}
        plugin = TransferPlugin(Selection(items, plex_total=51), responses=responses,
                               plex_items=[plex_item(i) for i in range(51)])
        self.transfer.plex_to_spotify(plugin, types.SimpleNamespace(), 'Mix')

        batches = [c for c in plugin.calls if c[0] == 'tracks']
        assert [len(c[1]) for c in batches] == [50, 1]

    def test_missing_beets_mapping_disables_removals(self):
        item = library_item(1, 'known')
        plugin = TransferPlugin(
            Selection([item], missing=[plex_item(2, 'Missing')], plex_total=2),
            responses={'known': {"playable": True}},
            plex_items=[plex_item(1), plex_item(2, 'Missing')],
        )
        assert self.transfer.plex_to_spotify(plugin, types.SimpleNamespace(), 'Mix')
        assert plugin.sent[1] == ['known']
        assert plugin.sent[2] is False

    def test_availability_failure_retains_known_id_and_disables_removals(self):
        item = library_item(1, 'known')
        plugin = TransferPlugin(
            Selection([item], plex_total=1),
            tracks_error=RuntimeError('offline'),
            plex_items=[plex_item(1)],
        )
        assert self.transfer.plex_to_spotify(plugin, types.SimpleNamespace(), 'Mix')
        assert plugin.sent[1] == ['known']
        assert plugin.sent[2] is False

    def test_search_failure_disables_removals(self):
        broken = library_item(1, 'orig', title='Broken')
        keeper = library_item(2, 'ok', title='Fine')

        class Plugin(TransferPlugin):
            def _search_spotify_track(self, beets_item, *, raise_on_error=False, market=None):
                if beets_item.title == 'Broken':
                    raise RuntimeError('search exploded')
                return 'replacement'

        plugin = Plugin(
            Selection([broken, keeper], plex_total=2),
            responses={'orig': {"playable": False, "reason": "market"}},
            plex_items=[plex_item(1, 'Broken'), plex_item(2, 'Fine')],
        )
        assert self.transfer.plex_to_spotify(plugin, types.SimpleNamespace(), 'Mix')
        assert plugin.sent[1] == ['replacement']
        assert plugin.sent[2] is False

    def test_nothing_resolved_refuses_to_touch_the_playlist(self):
        item = library_item(1, 'orig')
        plugin = TransferPlugin(
            Selection([item], plex_total=1),
            responses={'orig': {"playable": False, "reason": "market"}},
            search=RuntimeError('search exploded'),
            plex_items=[plex_item(1)],
        )
        assert self.transfer.plex_to_spotify(plugin, types.SimpleNamespace(), 'Mix') is False
        assert plugin.sent is None

    def test_authoritative_no_match_keeps_removals_enabled(self):
        ghost = library_item(1, 'orig', title='Gone')
        keeper = library_item(2, 'ok', title='Fine')

        class Plugin(TransferPlugin):
            def _search_spotify_track(self, beets_item, *, raise_on_error=False, market=None):
                if beets_item.title == 'Gone':
                    return None  # the search answered: nothing playable exists
                return 'replacement'

        plugin = Plugin(
            Selection([ghost, keeper], plex_total=2),
            responses={'orig': {"playable": False, "reason": "market"}},
            plex_items=[plex_item(1, 'Gone'), plex_item(2, 'Fine')],
        )
        assert self.transfer.plex_to_spotify(plugin, types.SimpleNamespace(), 'Mix')
        assert plugin.sent[1] == ['replacement']
        assert plugin.sent[2] is True

    def test_track_without_cached_id_is_searched(self):
        item = library_item(1, None)
        plugin = TransferPlugin(Selection([item], plex_total=1), search='found',
                               plex_items=[plex_item(1)])
        self.transfer.plex_to_spotify(plugin, types.SimpleNamespace(), 'Mix')
        assert plugin.sent[1] == ['found']
        assert not [c for c in plugin.calls if c[0] == 'tracks']

    def test_empty_resolution_never_updates_destination(self):
        plugin = TransferPlugin(Selection([], plex_total=0), plex_items=[])
        assert self.transfer.plex_to_spotify(plugin, types.SimpleNamespace(), 'Mix') is False
        assert plugin.sent is None

    def test_market_is_omitted_when_country_is_unknown(self):
        item = library_item(1, 'song-id')
        plugin = TransferPlugin(
            Selection([item], plex_total=1),
            responses={'song-id': {"markets": ["US"]}},
            market=None,
            plex_items=[plex_item(1)],
        )
        self.transfer.plex_to_spotify(plugin, types.SimpleNamespace(), 'Mix')
        assert [c for c in plugin.calls if c[0] == "tracks"] == [("tracks", ['song-id'], None)]
        assert plugin.sent[1] == ['song-id']
