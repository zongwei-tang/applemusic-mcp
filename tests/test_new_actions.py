"""playlist duplicates/compare, library stats, and the richer artist_details."""

import json
import time

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
