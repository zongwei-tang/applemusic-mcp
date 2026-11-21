"""Catalog playlists: catalog(action='playlist_tracks') and library(action='add', playlist=...)."""

import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import responses as resp_lib

from applemusic_mcp import capabilities, server

BASE = "https://api.music.apple.com/v1"
PL = "pl.f4d106fed2bd41149aaacabb233eb5eb"


@pytest.fixture
def tokens(mock_config_dir, mock_developer_token, mock_user_token):
    (mock_config_dir / "developer_token.json").write_text(
        json.dumps({"token": mock_developer_token, "expires": time.time() + 86400 * 60})
    )
    (mock_config_dir / "music_user_token.json").write_text(
        json.dumps({"music_user_token": mock_user_token})
    )
    return mock_config_dir


def _song(i, kind="songs"):
    return {
        "id": str(1000 + i),
        "type": kind,
        "attributes": {
            "name": f"Song {i}",
            "artistName": "Artist",
            "albumName": "Album",
            "durationInMillis": 200000,
        },
    }


def _meta(pl_id=PL, name="Today's Hits", curator="Apple Music", desc="<b>The</b> biggest songs"):
    return {
        "data": [
            {
                "id": pl_id,
                "type": "playlists",
                "attributes": {
                    "name": name,
                    "curatorName": curator,
                    "description": {"short": desc},
                },
            }
        ]
    }


def _mock_playlist(pl_id=PL, sf="us", pages=((0, 2),), with_video=False):
    resp_lib.add(resp_lib.GET, f"{BASE}/catalog/{sf}/playlists/{pl_id}", json=_meta(pl_id))
    for n, (start, count) in enumerate(pages):
        data = [_song(start + i) for i in range(count)]
        if with_video and n == 0:
            video = _song(999, kind="music-videos")
            video["attributes"]["name"] = "A Music Video"
            data.append(video)
        body = {"data": data}
        if n < len(pages) - 1:
            body["next"] = f"/v1/catalog/{sf}/playlists/{pl_id}/tracks?offset={start + count}"
        resp_lib.add(resp_lib.GET, f"{BASE}/catalog/{sf}/playlists/{pl_id}/tracks", json=body)


class TestResolveCatalogPlaylist:
    def test_bare_id(self, tokens):
        assert server._resolve_catalog_playlist(PL) == (PL, "us", None, False)

    def test_shared_user_id(self, tokens):
        assert server._resolve_catalog_playlist("pl.u-abc123")[0] == "pl.u-abc123"

    def test_url_carries_storefront(self, tokens):
        url = f"https://music.apple.com/jp/playlist/todays-hits/{PL}?l=en"
        assert server._resolve_catalog_playlist(url) == (PL, "jp", None, False)

    def test_url_without_slug(self, tokens):
        assert server._resolve_catalog_playlist(f"music.apple.com/GB/playlist/{PL}")[:2] == (
            PL,
            "gb",
        )

    def test_empty(self, tokens):
        pid, _, err, _ = server._resolve_catalog_playlist("  ")
        assert pid is None and "required" in err

    @pytest.mark.parametrize("lib_id", ["p.abc123", "l.xyz"])
    def test_library_id_is_redirected(self, tokens, lib_id):
        pid, _, err, _ = server._resolve_catalog_playlist(lib_id)
        assert pid is None and "playlist(action='tracks')" in err

    def _search(self, *names):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={
                "results": {
                    "playlists": {
                        "data": [
                            {"id": f"pl.{i}", "attributes": {"name": n}}
                            for i, n in enumerate(names)
                        ]
                    }
                }
            },
        )

    @pytest.mark.parametrize(
        "url",
        [
            "https://music.apple.com/us/library/playlist/p.abc123",
            "https://music.apple.com/us/album/some-album/123456",
        ],
    )
    def test_other_apple_music_urls_are_not_searched(self, tokens, url):
        pid, _, err, _ = server._resolve_catalog_playlist(url)
        assert pid is None and "not a catalog playlist URL" in err

    @resp_lib.activate
    def test_name_exact_beats_loose(self, tokens):
        self._search("Chill Mix Deluxe", "Chill Mix")
        assert server._resolve_catalog_playlist("chill mix")[:2] == ("pl.1", "us")
        assert server._resolve_catalog_playlist("chill mix", strict=True)[0] == "pl.1"
        qs = parse_qs(urlsplit(resp_lib.calls[0].request.url).query)
        assert qs["types"] == ["playlists"]

    @resp_lib.activate
    def test_name_loose_then_top(self, tokens):
        self._search("Something Else", "Workout Chill Mix")
        pid, _, err, approximate = server._resolve_catalog_playlist("chill mix")
        assert (pid, err, approximate) == ("pl.1", None, True)

    @resp_lib.activate
    def test_name_falls_back_to_top_result(self, tokens):
        self._search("Zzz", "Yyy")
        assert server._resolve_catalog_playlist("chill mix")[::3] == ("pl.0", True)

    @resp_lib.activate
    def test_strict_refuses_without_exact_name(self, tokens):
        self._search("Workout Chill Mix", "Chill Mixtape", "Zzz", "Extra")
        pid, _, err, _ = server._resolve_catalog_playlist("chill mix", strict=True)
        assert pid is None
        assert "Workout Chill Mix (pl.0)" in err and "Zzz (pl.2)" in err
        assert "Extra" not in err

    @resp_lib.activate
    def test_name_not_found(self, tokens):
        resp_lib.add(resp_lib.GET, f"{BASE}/catalog/us/search", json={"results": {}})
        pid, _, err, _ = server._resolve_catalog_playlist("nothing")
        assert pid is None and "No catalog playlist" in err


class TestPlaylistTracks:
    @resp_lib.activate
    def test_text_output_and_header(self, tokens):
        _mock_playlist()
        out = server.catalog(action="playlist_tracks", playlist=PL)
        assert "=== Today's Hits ===" in out
        assert "Curator: Apple Music" in out
        assert "The biggest songs" in out and "<b>" not in out
        assert "Song 0" in out and "Song 1" in out
        assert "library(action='add', playlist=" in out

    @resp_lib.activate
    def test_follows_next_and_drops_videos(self, tokens):
        _mock_playlist(pages=((0, 100), (100, 3)), with_video=True)
        out = server._catalog_playlist_tracks(PL, format="json")
        rows = json.loads(out)
        assert len(rows) == 103
        assert all(r["name"].startswith("Song ") for r in rows)
        assert "A Music Video" not in out
        tracks_calls = [
            c for c in resp_lib.calls if c.request.url.split("?")[0].endswith("/tracks")
        ]
        offsets = [parse_qs(urlsplit(c.request.url).query)["offset"] for c in tracks_calls]
        assert offsets == [["0"], ["101"]]  # the page held 100 songs + 1 video

    @resp_lib.activate
    def test_query_is_accepted_as_playlist(self, tokens):
        _mock_playlist()
        assert "Song 0" in server.catalog(action="playlist_tracks", query=PL)

    @resp_lib.activate
    def test_url_uses_its_storefront(self, tokens):
        _mock_playlist(sf="jp")
        out = server.catalog(
            action="playlist_tracks", playlist=f"https://music.apple.com/jp/playlist/x/{PL}"
        )
        assert "Song 0" in out

    @resp_lib.activate
    def test_paging(self, tokens):
        _mock_playlist(pages=((0, 5),))
        rows = json.loads(server._catalog_playlist_tracks(PL, limit=2, offset=2, format="json"))
        assert [r["name"] for r in rows] == ["Song 2", "Song 3"]

    @resp_lib.activate
    def test_not_found(self, tokens):
        resp_lib.add(resp_lib.GET, f"{BASE}/catalog/us/playlists/{PL}", status=404)
        out = server.catalog(action="playlist_tracks", playlist=PL)
        assert "not found" in out

    @resp_lib.activate
    def test_empty_playlist(self, tokens):
        resp_lib.add(resp_lib.GET, f"{BASE}/catalog/us/playlists/{PL}", json=_meta())
        resp_lib.add(resp_lib.GET, f"{BASE}/catalog/us/playlists/{PL}/tracks", status=404)
        assert "has no songs" in server.catalog(action="playlist_tracks", playlist=PL)

    @resp_lib.activate
    def test_http_error(self, tokens):
        resp_lib.add(resp_lib.GET, f"{BASE}/catalog/us/playlists/{PL}", status=500)
        out = server.catalog(action="playlist_tracks", playlist=PL)
        assert out  # surfaced through _api_error, not raised

    def test_missing_playlist(self, tokens):
        assert "required" in server.catalog(action="playlist_tracks")

    def test_no_network_level(self, tokens, monkeypatch):
        monkeypatch.setenv(capabilities.ENV_LEVEL, "music")
        out = server.catalog(action="playlist_tracks", playlist=PL)
        assert out.startswith("Error:")

    def test_no_tokens(self, mock_config_dir):
        out = server.catalog(action="playlist_tracks", playlist=PL)
        assert "login" in out.lower() or "token" in out.lower()


class TestLibraryAddPlaylist:
    @resp_lib.activate
    def test_adds_by_playlist_id(self, tokens):
        resp_lib.add(resp_lib.POST, f"{BASE}/me/library", status=202)
        out = server.library(action="add", playlist=PL)
        assert "Added catalog playlist" in out
        qs = parse_qs(urlsplit(resp_lib.calls[0].request.url).query)
        assert qs == {"ids[playlists]": [PL]}

    @resp_lib.activate
    def test_api_failure(self, tokens):
        resp_lib.add(resp_lib.POST, f"{BASE}/me/library", status=401)
        out = server.library(action="add", playlist=PL)
        assert out.startswith("Error:") and "Not authorized" in out

    def test_library_id_rejected(self, tokens):
        assert "playlist(action='tracks')" in server.library(action="add", playlist="p.abc")

    @resp_lib.activate
    def test_search_error(self, tokens):
        resp_lib.add(resp_lib.GET, f"{BASE}/catalog/us/search", status=500)
        assert server.library(action="add", playlist="road trip")

    def test_track_still_wins(self, tokens, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            server, "_library_add", lambda t, a, ar: seen.setdefault("args", (t, a, ar)) and "ok"
        )
        server.library(action="add", track="Song", playlist=PL)
        assert seen["args"] == ("Song", "", "")

    def test_no_network_level(self, tokens, monkeypatch):
        monkeypatch.setenv(capabilities.ENV_LEVEL, "music")
        assert server.library(action="add", playlist=PL).startswith("Error:")

    def test_not_signed_in(self, mock_config_dir, monkeypatch):
        monkeypatch.setattr(server, "_can_use_library_api", lambda: False)
        assert "login" in server.library(action="add", playlist=PL)

    def test_add_to_library_api_label(self, tokens, monkeypatch):
        with resp_lib.RequestsMock() as r:
            r.add(resp_lib.POST, f"{BASE}/me/library", status=204)
            ok, msg = server._add_to_library_api([PL], "playlists")
        assert ok and "playlist(s)" in msg


def _search_results(*pairs):
    return {
        "results": {
            "playlists": {"data": [{"id": pid, "attributes": {"name": n}} for pid, n in pairs]}
        }
    }


@resp_lib.activate
def test_read_by_inexact_name_is_labelled(tokens):
    resp_lib.add(
        resp_lib.GET, f"{BASE}/catalog/us/search", json=_search_results((PL, "Today's Hits"))
    )
    _mock_playlist()
    out = server.catalog(action="playlist_tracks", playlist="hits")
    assert "Closest match for 'hits'" in out and "Song 0" in out


@resp_lib.activate
def test_add_by_inexact_name_writes_nothing(tokens):
    resp_lib.add(
        resp_lib.GET, f"{BASE}/catalog/us/search", json=_search_results((PL, "Today's Hits"))
    )
    out = server.library(action="add", playlist="hits")
    assert out.startswith("Error:") and f"Today's Hits ({PL})" in out
    assert all(c.request.method == "GET" for c in resp_lib.calls)


@resp_lib.activate
def test_add_by_exact_name(tokens):
    resp_lib.add(
        resp_lib.GET, f"{BASE}/catalog/us/search", json=_search_results((PL, "Today's Hits"))
    )
    resp_lib.add(resp_lib.POST, f"{BASE}/me/library", status=202)
    assert "Added catalog playlist" in server.library(action="add", playlist="today's hits")
