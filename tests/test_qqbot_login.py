from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from qqbot.nexon_auth import (
    CookieImportError,
    LoginError,
    _expand_qr,
    _playwright_proxy,
    parse_cookie_payload,
)

CORE = {"_if_game_auth": "auth", "_ifwt_guest_2392": "wt", "A2SK": "act"}


def test_proxy_parsing() -> None:
    assert _playwright_proxy("") is None
    assert _playwright_proxy("none") is None
    assert _playwright_proxy("127.0.0.1:7897") == {"server": "http://127.0.0.1:7897"}
    assert _playwright_proxy("http://127.0.0.1:7897") == {"server": "http://127.0.0.1:7897"}
    proxy = _playwright_proxy("http://user:p%40ss@proxy.example:1080")
    assert proxy == {
        "server": "http://proxy.example:1080",
        "username": "user",
        "password": "p@ss",
    }


def test_proxy_parsing_rejects_invalid() -> None:
    with pytest.raises(LoginError):
        _playwright_proxy("http://127.0.0.1")


def test_expand_qr_adds_border_and_scale() -> None:
    source = Image.new("L", (10, 10), 0)
    buf = io.BytesIO()
    source.save(buf, format="PNG")
    out = _expand_qr(buf.getvalue())
    image = Image.open(io.BytesIO(out))
    assert image.width > 100
    assert image.width > 10 * 4  # 至少 4 倍放大 + 白边
    assert image.width == image.height


def test_parse_flat_json() -> None:
    parsed = parse_cookie_payload(json.dumps(CORE))
    assert parsed.cookies == CORE
    assert parsed.source == "flat_json"


def test_parse_cookie_header() -> None:
    text = "Cookie: _if_game_auth=a; _ifwt_guest_2392=b; A2SK=c;"
    parsed = parse_cookie_payload(text)
    assert parsed.cookies == {"_if_game_auth": "a", "_ifwt_guest_2392": "b", "A2SK": "c"}
    assert parsed.source == "cookie_header"


def test_parse_old_cookies_json() -> None:
    payload = {
        "saved_at": "2026-10-01 00:00:00",
        "count": 3,
        "cookies": [
            {
                "name": "_if_game_auth",
                "value": "auth",
                "domain": ".nexon.com",
                "path": "/",
                "expires": -1,
                "secure": True,
                "sameSite": "no_restriction",
            },
            {"name": "_ifwt_guest_2392", "value": "wt", "domain": ".nexon.com", "path": "/"},
            {"name": "A2SK", "value": "act", "domain": ".nexon.com", "path": "/"},
        ],
    }
    parsed = parse_cookie_payload(json.dumps(payload))
    assert parsed.cookies == CORE
    assert parsed.source == "cookies_json"


def test_parse_storage_state() -> None:
    payload = {
        "cookies": [
            {"name": "_if_game_auth", "value": "auth", "domain": ".nexon.com", "path": "/"},
            {"name": "_ifwt", "value": "wt", "domain": ".nexon.com", "path": "/"},
            {"name": "A2SK", "value": "act", "domain": ".nexon.com", "path": "/"},
        ],
        "origins": [{"origin": "https://tfd.nexon.com", "localStorage": [{"name": "x", "value": "y"}]}],
    }
    parsed = parse_cookie_payload(json.dumps(payload))
    assert parsed.cookies["_ifwt"] == "wt"
    assert parsed.source == "storage_state"


def test_parse_rejects_missing_login_cookie() -> None:
    with pytest.raises(CookieImportError) as exc:
        parse_cookie_payload(json.dumps({"A2SK": "act"}))
    assert "_if_game_auth" in str(exc.value)


def test_parse_rejects_garbage() -> None:
    with pytest.raises(CookieImportError):
        parse_cookie_payload("hello world")


def test_parse_fenced_json() -> None:
    parsed = parse_cookie_payload("```json\n" + json.dumps(CORE) + "\n```")
    assert parsed.cookies == CORE
