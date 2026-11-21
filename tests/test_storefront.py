"""Storefront resolution: preference > APPLEMUSIC_STOREFRONT > the account's own > 'us'."""

import json
import time

import pytest
import responses as resp_lib

from applemusic_mcp import amp_api, auth, server

ME_SF = "https://api.music.apple.com/v1/me/storefront"


@pytest.fixture
def signed_in(mock_config_dir, mock_developer_token, mock_user_token, monkeypatch):
    monkeypatch.delenv(auth.ENV_STOREFRONT, raising=False)
    monkeypatch.delenv("APPLEMUSIC_USER_TOKEN", raising=False)
    (mock_config_dir / "developer_token.json").write_text(
        json.dumps({"token": mock_developer_token, "expires": time.time() + 86400 * 60})
    )
    (mock_config_dir / "music_user_token.json").write_text(
        json.dumps({"music_user_token": mock_user_token})
    )
    return mock_config_dir


def _pref(config_dir, sf):
    (config_dir / "config.json").write_text(json.dumps({"preferences": {"storefront": sf}}))


@resp_lib.activate
def test_detects_the_account_storefront_once(signed_in, mock_user_token):
    resp_lib.add(resp_lib.GET, ME_SF, json={"data": [{"id": "jp", "type": "storefronts"}]})
    assert auth.resolve_storefront() == ("jp", "your account")
    assert server.get_storefront() == "jp"
    assert len(resp_lib.calls) == 1  # cached after the first success
    req = resp_lib.calls[0].request
    assert req.headers["Music-User-Token"] == mock_user_token
    assert req.headers["Origin"] == "https://music.apple.com"


@resp_lib.activate
def test_failure_is_not_cached(signed_in):
    resp_lib.add(resp_lib.GET, ME_SF, status=429)
    resp_lib.add(resp_lib.GET, ME_SF, json={"data": [{"id": "gb"}]})
    assert auth.resolve_storefront() == ("us", "default")
    assert auth.resolve_storefront() == ("gb", "your account")


@resp_lib.activate
@pytest.mark.parametrize(
    "body", [{"data": []}, {"data": [{"id": "JPN"}]}, {}, {"data": [{"id": 7}]}]
)
def test_unusable_answer_falls_back_to_us(signed_in, body):
    resp_lib.add(resp_lib.GET, ME_SF, json=body)
    assert auth.resolve_storefront() == ("us", "default")


@resp_lib.activate
def test_network_error_falls_back(signed_in):
    resp_lib.add(resp_lib.GET, ME_SF, body=ConnectionError("down"))
    assert auth.resolve_storefront() == ("us", "default")


def test_not_signed_in_makes_no_request(mock_config_dir, monkeypatch):
    monkeypatch.delenv(auth.ENV_STOREFRONT, raising=False)
    monkeypatch.delenv("APPLEMUSIC_USER_TOKEN", raising=False)
    with resp_lib.RequestsMock() as r:  # any request would raise here
        assert auth.resolve_storefront() == ("us", "default")
        assert not r.calls


def test_env_beats_detection(signed_in, monkeypatch):
    monkeypatch.setenv(auth.ENV_STOREFRONT, " JP ")
    with resp_lib.RequestsMock():
        assert auth.resolve_storefront() == ("jp", auth.ENV_STOREFRONT)


def test_bad_env_is_ignored(signed_in, monkeypatch):
    monkeypatch.setenv(auth.ENV_STOREFRONT, "japan")
    with resp_lib.RequestsMock() as r:
        r.add(resp_lib.GET, ME_SF, json={"data": [{"id": "jp"}]})
        assert auth.resolve_storefront() == ("jp", "your account")


def test_preference_beats_everything(signed_in, monkeypatch):
    monkeypatch.setenv(auth.ENV_STOREFRONT, "jp")
    _pref(signed_in, "GB")
    with resp_lib.RequestsMock():
        assert auth.resolve_storefront() == ("gb", "preference")


def test_corrupt_config_is_ignored(signed_in, monkeypatch):
    (signed_in / "config.json").write_text("{not json")
    monkeypatch.setenv(auth.ENV_STOREFRONT, "kr")
    assert auth.resolve_storefront() == ("kr", auth.ENV_STOREFRONT)


def test_amp_catalog_search_uses_resolved_storefront(signed_in, monkeypatch):
    monkeypatch.setenv(auth.ENV_STOREFRONT, "jp")
    monkeypatch.setattr(amp_api, "_headers", lambda: {})
    with resp_lib.RequestsMock() as r:
        r.add(
            resp_lib.GET,
            f"{amp_api.AMP}/catalog/jp/search",
            json={"results": {"songs": {"data": []}}},
        )
        amp_api.search_catalog_songs("x")
        assert r.calls[0].request.url.startswith(f"{amp_api.AMP}/catalog/jp/search")


def test_config_info_and_auth_status_show_the_source(signed_in, monkeypatch):
    monkeypatch.setenv(auth.ENV_STOREFRONT, "jp")
    assert f"storefront: jp (from {auth.ENV_STOREFRONT}" in server.config(action="info")
    monkeypatch.setattr(amp_api, "session_status", lambda: "ok", raising=False)
    out = server.config(action="auth-status")
    assert f"Storefront: jp (from {auth.ENV_STOREFRONT})" in out
