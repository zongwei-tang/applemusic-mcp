"""playlist duplicates/compare, library stats, and the richer artist_details."""

import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
import responses as resp_lib

from applemusic_mcp import amp_api, capabilities, server

BASE = "https://api.music.apple.com/v1"


def _t(name, artist="Artist", cid=None):
    return {"relationship_id": f"i.{name}", "name": name, "artist": artist, "catalog_id": cid}


@pytest.fixture
def playlists(monkeypatch):
    """Two fake library playlists read over the web API."""
    data = {
        "Mix": [
            _t("One", cid="1"),
            _t("Two", cid="2"),
            _t("One", cid="1"),
            _t("Three", cid="3"),
            _t("Two (Remastered)", cid="22"),
            _t("Two", cid="2"),
            _t("Four", cid="4b"),
            _t("Four", cid=None),
        ],
        "Other": [_t("Three", cid="3"), _t("Five", cid="5"), _t("One", cid="1")],
        "Empty": [],
    }
    ids = {name: f"p.{name}" for name in data}
    monkeypatch.setattr(
        amp_api, "resolve_playlist_id", lambda name, api_created_only=False: ids.get(name)
    )
    monkeypatch.setattr(amp_api, "get_tracks", lambda pid: data.get(pid[2:]))
    return data


class TestDuplicates:
    def test_reports_exact_repeats_with_positions(self, playlists):
        out = server.playlist(action="duplicates", playlist="Mix")
        assert "=== Duplicates in 'Mix' (8 tracks) ===" in out
        assert "2 songs, 2 extra" in out
        assert "One - Artist x2 at positions 1, 3 [1]" in out
        assert "Two - Artist x2 at positions 2, 6 [2]" in out
        assert "Nothing was removed" in out

    def test_reports_other_editions_apart(self, playlists):
        out = server.playlist(action="duplicates", playlist="Mix")
        assert "different editions: 1 songs" in out
        assert "Four - Artist: #7 [4b], #8 [no catalog id]" in out
        # a differently titled remaster isn't called a duplicate
        assert "Remastered" not in out

    def test_clean_and_empty(self, playlists):
        assert "No duplicates in 'Other' (3 tracks)" == server.playlist(
            action="duplicates", playlist="Other"
        )
        assert server.playlist(action="dupes", playlist="Empty") == "'Empty' is empty"

    def test_errors(self, playlists, monkeypatch):
        assert "playlist required" in server.playlist(action="duplicates")
        assert "not found" in server.playlist(action="duplicates", playlist="Nope")
        monkeypatch.setattr(amp_api, "get_tracks", lambda pid: None)
        assert server.playlist(action="duplicates", playlist="Mix").startswith("Error")

    def test_needs_network_level(self, playlists, monkeypatch):
        monkeypatch.setenv(capabilities.ENV_LEVEL, "music")
        assert server.playlist(action="duplicates", playlist="Mix").startswith("Error:")


class TestCompare:
    def test_diff(self, playlists):
        out = server.playlist(action="compare", playlist="Mix", source="Other")
        assert "'Mix' (8 tracks) vs 'Other' (3 tracks)" in out
        assert "Shared songs: 2" in out
        assert "Only in 'Other': 1\n  Five - Artist" in out
        only_mix = out.split("Only in 'Mix': ")[1].split("\n\n")[0]
        assert only_mix.startswith("4")  # Two, Two (Remastered), Four [4b], Four [no id]

    def test_limit_caps_lists(self, playlists):
        out = server.playlist(action="diff", playlist="Mix", source="Other", limit=1)
        assert "... and 3 more (raise `limit`)" in out

    def test_errors(self, playlists):
        assert "needs `playlist` and `source`" in server.playlist(action="compare", playlist="Mix")
        assert "not found" in server.playlist(action="compare", playlist="Mix", source="Nope")
        assert "not found" in server.playlist(action="compare", playlist="Nope", source="Mix")

    def test_unknown_action_lists_new_ones(self):
        out = server.playlist(action="bogus")
        assert "duplicates, compare" in out


@pytest.fixture
def tokens(mock_config_dir, mock_developer_token, mock_user_token):
    (mock_config_dir / "developer_token.json").write_text(
        json.dumps({"token": mock_developer_token, "expires": time.time() + 86400 * 60})
    )
    (mock_config_dir / "music_user_token.json").write_text(
        json.dumps({"music_user_token": mock_user_token})
    )
    return mock_config_dir


class TestLibraryStats:
    @resp_lib.activate
    def test_counts(self, tokens):
        totals = {"songs": 12345, "albums": 900, "artists": 400, "playlists": 42}
        for kind, n in totals.items():
            resp_lib.add(
                resp_lib.GET, f"{BASE}/me/library/{kind}", json={"data": [], "meta": {"total": n}}
            )
        resp_lib.add(resp_lib.GET, f"{BASE}/me/library/music-videos", status=404)
        out = server.library(action="stats")
        assert "songs: 12,345" in out and "playlists: 42" in out
        assert "music videos: 0" in out

    @resp_lib.activate
    def test_missing_total_is_unknown(self, tokens):
        for kind in server._LIBRARY_STAT_TYPES:
            resp_lib.add(resp_lib.GET, f"{BASE}/me/library/{kind}", json={"data": []})
        assert "Error: couldn't read any library counts" in server.library(action="stats")

    @resp_lib.activate
    def test_partial(self, tokens):
        resp_lib.add(resp_lib.GET, f"{BASE}/me/library/songs", json={"meta": {"total": 3}})
        for kind in server._LIBRARY_STAT_TYPES[1:]:
            resp_lib.add(resp_lib.GET, f"{BASE}/me/library/{kind}", status=500)
        out = server.library(action="count")
        assert "songs: 3" in out and "albums: unknown (status 500)" in out

    @resp_lib.activate
    def test_unauthorized(self, tokens):
        resp_lib.add(resp_lib.GET, f"{BASE}/me/library/songs", status=401)
        assert "not authorized" in server.library(action="stats")

    @resp_lib.activate
    def test_network_error(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/me/library/songs",
            body=requests.exceptions.ConnectionError("down"),
        )
        assert server.library(action="stats")

    def test_no_tokens(self, mock_config_dir):
        assert server.library(action="stats").startswith("Error")

    def test_needs_network_level(self, tokens, monkeypatch):
        monkeypatch.setenv(capabilities.ENV_LEVEL, "music")
        assert server.library(action="stats").startswith("Error:")


def _album(aid, name, day):
    return {"id": aid, "type": "albums", "attributes": {"name": name, "releaseDate": day}}


class TestArtistDetails:
    ART = f"{BASE}/catalog/us/artists/42"

    def _artist(self):
        resp_lib.add(
            resp_lib.GET,
            self.ART,
            json={"data": [{"id": "42", "attributes": {"name": "Band", "genreNames": ["Pop"]}}]},
        )

    @resp_lib.activate
    def test_views(self, tokens):
        self._artist()
        resp_lib.add(
            resp_lib.GET,
            f"{self.ART}/view/latest-release",
            json={"data": [_album("9", "New One", "2026-10-01")]},
        )
        resp_lib.add(
            resp_lib.GET,
            f"{self.ART}/view/top-songs",
            json={"data": [{"id": "7", "attributes": {"name": "Hit", "albumName": "LP"}}]},
        )
        resp_lib.add(
            resp_lib.GET,
            f"{self.ART}/view/full-albums",
            json={"data": [_album("1", "Old", "2001-01-01"), _album("2", "Mid", "2015-05-05")]},
        )
        resp_lib.add(
            resp_lib.GET,
            f"{self.ART}/view/singles",
            json={"data": [_album("3", "Single", "2024-02-02")]},
        )
        out = server.catalog(action="artist_details", artist="42")
        assert "Latest release: New One (2026-10-01) [catalog ID: 9]" in out
        assert "Top songs:\n  - Hit (LP) [catalog ID: 7]" in out
        assert out.index("Mid (2015)") < out.index("Old (2001)")
        assert "Recent singles:\n  - Single (2024)" in out
        assert "/albums" not in "".join(c.request.url for c in resp_lib.calls)

    @resp_lib.activate
    def test_falls_back_to_albums_sorted(self, tokens):
        self._artist()
        for v in ("latest-release", "top-songs", "full-albums"):
            resp_lib.add(resp_lib.GET, f"{self.ART}/view/{v}", status=404)
        resp_lib.add(
            resp_lib.GET,
            f"{self.ART}/albums",
            json={"data": [_album("1", "Old", "2001-01-01"), _album("2", "New", "2020-01-01")]},
        )
        out = server.catalog(action="artist_details", artist="42")
        assert "Recent Albums:" in out and "Latest release" not in out
        assert out.index("New (2020)") < out.index("Old (2001)")

    @resp_lib.activate
    def test_bad_view_payload(self, tokens):
        self._artist()
        resp_lib.add(resp_lib.GET, f"{self.ART}/view/latest-release", json={"data": "x"})
        resp_lib.add(resp_lib.GET, f"{self.ART}/view/top-songs", body="not json")
        resp_lib.add(resp_lib.GET, f"{self.ART}/view/full-albums", json={})
        resp_lib.add(resp_lib.GET, f"{self.ART}/albums", json={"data": []})
        out = server.catalog(action="artist_details", artist="42")
        assert out.startswith("Artist: Band")


class TestNonLatinTitles:
    """Titles in other scripts used to normalize to '' and all compare equal."""

    @pytest.mark.parametrize(
        "a,b",
        [
            ("分手總要在雨天", "吻別"),
            ("夜に駆ける", "群青"),
            ("はがゆい唇", "はかゆい唇"),  # the voicing mark matters
            ("Кино", "Звезда"),
            ("사랑", "이별"),
        ],
    )
    def test_distinct_titles_stay_distinct(self, a, b):
        assert server._normalize_for_match(a) != server._normalize_for_match(b)
        assert server._normalize_for_match(a)

    def test_full_width_and_spacing_fold(self):
        assert server._normalize_for_match("ＹＯＡＳＯＢＩ") == "yoasobi"
        assert server._normalize_for_match("アイドル!") == server._normalize_for_match("アイドル")
        assert server._normalize_for_match("ｱｲﾄﾞﾙ") == server._normalize_for_match("アイドル")

    def test_latin_accents_still_fold(self):
        assert server._normalize_for_match("Björk") == "bjork"

    def test_fuzzy_variations_keep_cjk(self):
        variations, _ = server._normalize_with_tracking("分手總要在雨天")
        assert variations == ["分手總要在雨天"]

    def test_duplicates_no_longer_merge_different_cjk_songs(self, monkeypatch):
        songs = [
            _t("分手總要在雨天", "張學友", "1"),
            _t("吻別", "張學友", "2"),
            _t("一路上有你", "張學友", "3"),
        ]
        monkeypatch.setattr(amp_api, "resolve_playlist_id", lambda n, api_created_only=False: "p.x")
        monkeypatch.setattr(amp_api, "get_tracks", lambda pid: songs)
        assert (
            server.playlist(action="duplicates", playlist="x") == "No duplicates in 'x' (3 tracks)"
        )

    @resp_lib.activate
    def test_strict_catalog_playlist_guard_holds_for_japanese(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={
                "results": {
                    "playlists": {
                        "data": [{"id": "pl.a", "attributes": {"name": "今日のトップソング"}}]
                    }
                }
            },
        )
        pid, _, err, _ = server._resolve_catalog_playlist("チル", strict=True)
        assert pid is None and "今日のトップソング (pl.a)" in err


def _song(sid, name, artist, album="A"):
    return {
        "id": sid,
        "type": "songs",
        "attributes": {"name": name, "artistName": artist, "albumName": album},
    }


class TestLiveSweepFixes:
    """Bugs a live sweep against a JP account found."""

    def test_cjk_artist_spacing_is_ignored(self):
        assert server._loose_contains("松田聖子", "松田 聖子")
        assert server._loose_equals("松田 聖子", "松田聖子")
        # Latin word boundaries keep their meaning
        assert not server._loose_equals("the band", "theband")

    def test_resolve_splits_song_dash_artist(self, monkeypatch):
        seen = []

        def search(term, limit=5):
            seen.append(term)
            return [
                _song("live", "夜に駆ける (LIVE 2024)", "YOASOBI"),
                _song("orig", "夜に駆ける", "YOASOBI"),
                _song("para", "白いパラソル", "松田 聖子"),
            ]

        monkeypatch.setattr(server, "_search_catalog_songs", search)
        out = json.loads(
            server.catalog(
                action="resolve",
                tracks="夜に駆ける - YOASOBI\n白いパラソル - 松田聖子",
                format="json",
            )
        )
        by_input = {m["input"]: m for m in out["matched"]}
        assert by_input["夜に駆ける - YOASOBI"]["id"] == "orig"
        assert by_input["夜に駆ける - YOASOBI"]["confidence"] == "exact"
        assert by_input["白いパラソル - 松田聖子"]["id"] == "para"
        assert seen[0] == "夜に駆ける YOASOBI"  # the split, not the raw line

    def test_resolve_falls_back_to_whole_line(self, monkeypatch):
        def search(term, limit=5):
            # only the unsplit title exists: "Prelude - Fugue" is one work
            return [_song("pf", "Prelude - Fugue", "Bach")] if " - " in term else []

        monkeypatch.setattr(server, "_search_catalog_songs", search)
        out = json.loads(server.catalog(action="resolve", tracks="Prelude - Fugue", format="json"))
        assert out["ids"] == ["pf"] and out["requests"] == 2

    @resp_lib.activate
    def test_album_exact_title_beats_loose(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={
                "results": {
                    "albums": {
                        "data": [
                            {
                                "id": "b3",
                                "attributes": {"name": "THE BOOK 3", "artistName": "YOASOBI"},
                            },
                            {
                                "id": "b1",
                                "attributes": {"name": "THE BOOK", "artistName": "YOASOBI"},
                            },
                        ]
                    }
                }
            },
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/b1/tracks",
            json={"data": [_song("1", "Epilogue", "YOASOBI", "THE BOOK")]},
        )
        out = server.catalog(action="album_tracks", album="THE BOOK", artist="YOASOBI")
        assert "Epilogue" in out

    @resp_lib.activate
    def test_charts_carry_ids_and_honor_limit(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/charts",
            json={
                "results": {
                    "songs": [
                        {
                            "name": "Top",
                            "data": [_song(str(i), f"S{i}", "A") for i in range(30)],
                        }
                    ]
                }
            },
        )
        out = server.discover(action="charts", limit=3)
        assert "1. S0 - A [0]" in out and "3. S2 - A [2]" in out and "S3" not in out
        assert parse_qs(urlsplit(resp_lib.calls[0].request.url).query)["limit"] == ["3"]
        default = server.discover(action="charts")
        assert "20. S19" in default and "S20" not in default

    @resp_lib.activate
    def test_heavy_rotation_unnamed_item_and_limit(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/me/history/heavy-rotation",
            json={
                "data": [
                    {"id": "l.x", "type": "library-albums"},
                    {
                        "id": "9",
                        "type": "albums",
                        "attributes": {"name": "Real", "artistName": "A"},
                    },
                ]
            },
        )
        out = server.discover(action="heavy_rotation", limit=99)
        assert "(unnamed albums)" in out and "Real - A" in out
        assert parse_qs(urlsplit(resp_lib.calls[0].request.url).query)["limit"] == ["10"]


class TestWriteSweepFixes:
    """Bugs a live write test found (create/add/rename/remove/delete round trip)."""

    def test_resolve_playlist_accepts_a_library_id(self, monkeypatch):
        pls = [
            {"id": "p.AAA", "name": "MCP Test", "canEdit": True},
            {"id": "p.BBB", "name": "Other", "canEdit": True},
        ]
        monkeypatch.setattr(amp_api, "list_playlists", lambda: pls)
        monkeypatch.setattr(amp_api, "is_api_created", lambda pl: True)
        assert amp_api.resolve_playlist_id("p.BBB", api_created_only=False) == "p.BBB"
        assert amp_api.resolve_playlist_id(" p.AAA ") == "p.AAA"
        assert amp_api.resolve_playlist_id("p.aaa") is None  # ids are case-sensitive

    def _playlist(self, monkeypatch, entries, after_add=None):
        state = {"tracks": list(entries), "adds": []}
        monkeypatch.setattr(amp_api, "resolve_playlist_id", lambda n, api_created_only=False: "p.T")
        monkeypatch.setattr(amp_api, "get_tracks", lambda pid: list(state["tracks"]))

        def remove(pid, rid):
            # Apple's real behavior: every copy sharing the id goes.
            state["tracks"] = [t for t in state["tracks"] if t["relationship_id"] != rid]
            return True, "Removed"

        def add(pid, items):
            state["adds"].append(items)
            if after_add is None:
                rid = items[0][0]
                src = next(t for t in entries if t["relationship_id"] == rid)
                state["tracks"].append(dict(src))
            return True, "Added 1 track(s)"

        monkeypatch.setattr(amp_api, "remove_track", remove)
        monkeypatch.setattr(amp_api, "add_tracks", add)
        monkeypatch.setattr(server.audit_log, "log_action", lambda *a, **k: None)
        return state

    def test_removing_one_copy_keeps_the_others(self, monkeypatch):
        idol = {
            "relationship_id": "i.X",
            "name": "アイドル",
            "artist": "YOASOBI",
            "catalog_id": "1",
        }
        other = {"relationship_id": "i.Y", "name": "群青", "artist": "YOASOBI", "catalog_id": "2"}
        state = self._playlist(monkeypatch, [idol, other, dict(idol), dict(idol)])
        out = server._playlist_remove_api("T", "アイドル", "YOASOBI")
        assert "It was in the playlist 3 times" in out and "other 2 were added back" in out
        assert [t["relationship_id"] for t in state["tracks"]].count("i.X") == 2
        assert state["adds"] == [[("i.X", "library-songs")]] * 2
        assert "other track(s) also matched" not in out  # copies aren't "other tracks"

    def test_single_copy_needs_no_restore(self, monkeypatch):
        idol = {
            "relationship_id": "i.X",
            "name": "アイドル",
            "artist": "YOASOBI",
            "catalog_id": "1",
        }
        state = self._playlist(monkeypatch, [idol])
        out = server._playlist_remove_api("T", "アイドル")
        assert out.startswith("Removed アイドル - YOASOBI") and "added back" not in out
        assert state["adds"] == [] and state["tracks"] == []

    def test_failed_restore_is_reported_not_hidden(self, monkeypatch):
        idol = {
            "relationship_id": "i.X",
            "name": "アイドル",
            "artist": "YOASOBI",
            "catalog_id": "1",
        }
        self._playlist(monkeypatch, [idol, dict(idol)], after_add="nothing lands")
        out = server._playlist_remove_api("T", "アイドル")
        assert out.startswith("Warning:") and "left 0 in the playlist" in out

    def test_distinct_other_matches_still_noted(self, monkeypatch):
        a = {"relationship_id": "i.A", "name": "Love", "artist": "X", "catalog_id": "1"}
        b = {"relationship_id": "i.B", "name": "Love Song", "artist": "X", "catalog_id": "2"}
        self._playlist(monkeypatch, [a, b])
        out = server._playlist_remove_api("T", "Love")
        assert "1 other track(s) also matched 'Love'" in out

    @resp_lib.activate
    def test_library_search_title_and_artist(self, tokens, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        url = f"{BASE}/me/library/search"

        def reply(request):
            term = parse_qs(urlsplit(request.url).query)["term"][0]
            rows = []
            if term == "アイドル":
                rows = [
                    {
                        "id": "i.X",
                        "type": "library-songs",
                        "attributes": {
                            "name": "アイドル",
                            "artistName": "YOASOBI",
                            "albumName": "アイドル - Single",
                        },
                    },
                    {
                        "id": "i.Z",
                        "type": "library-songs",
                        "attributes": {
                            "name": "アイドル",
                            "artistName": "Someone",
                            "albumName": "B",
                        },
                    },
                ]
            return (200, {}, json.dumps({"results": {"library-songs": {"data": rows}}}))

        resp_lib.add_callback(resp_lib.GET, url, callback=reply)
        out = server.library(action="search", query="アイドル YOASOBI")
        assert "YOASOBI" in out and "Someone" not in out
        terms = [parse_qs(urlsplit(c.request.url).query)["term"][0] for c in resp_lib.calls]
        assert terms == ["アイドル YOASOBI", "YOASOBI", "アイドル"]

    @resp_lib.activate
    def test_library_search_single_word_has_no_retry(self, tokens, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        resp_lib.add(resp_lib.GET, f"{BASE}/me/library/search", json={"results": {}})
        assert server.library(action="search", query="nothing") == "No songs found"
        assert len(resp_lib.calls) == 1


def test_write_resolver_takes_a_library_id_first(monkeypatch):
    pls = [
        {"id": "p.ONE", "name": "p.TWO"},  # a playlist literally named like an id
        {"id": "p.TWO", "name": "Mix"},
    ]
    monkeypatch.setattr(amp_api, "list_playlists", lambda: pls)
    pl, err = server._resolve_playlist_for_write("p.TWO")
    assert err is None and pl["name"] == "Mix"
    pl, err = server._resolve_playlist_for_write(" p.ONE ")
    assert pl["id"] == "p.ONE"
    renamed = {}
    monkeypatch.setattr(
        amp_api, "rename_playlist", lambda pid, new: renamed.setdefault(pid, new) and (True, "")
    )
    assert server._playlist_rename_api("p.TWO", "Mix 2") == "Renamed 'Mix' to 'Mix 2'"
    assert renamed == {"p.TWO": "Mix 2"}
