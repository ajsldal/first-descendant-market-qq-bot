from __future__ import annotations

import json
from typing import Any

from market_v2.transport import Response
from qqbot.store import Store
from qqbot.trade import (
    TradeClient,
    check_session,
    describe_code,
    session_usable,
)


class RecordingTransport:
    """记录请求头/请求体，并按顺序返回预设响应。"""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        content_type: str | None = None,
        timeout: float | None = None,
    ) -> Response:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "body": json_body,
                "headers": headers or {},
                "content_type": content_type,
            }
        )
        data = self._responses.pop(0) if self._responses else {"ResultCode": "SUCCESS"}
        return Response(status_code=200, text=json.dumps(data))

    def close(self) -> None:
        return None


def test_chat_payload_and_cookie_header() -> None:
    transport = RecordingTransport(
        [{"IsSuccess": True, "ResultCode": "SUCCESS", "ResultData": {"seller": "s#1", "enc": "E1"}}]
    )
    client = TradeClient({"a": "1", "b": "2"}, user_agent="UA", transport=transport)
    result = client.chat("898484701286067202", "transcendent", "211064002")

    assert result.ok
    assert result.seller == "s#1"
    assert result.enc == "E1"
    call = transport.calls[0]
    assert call["url"] == "https://tfd-api.nexon.com/api/v2/trademarket/chat"
    assert call["content_type"] == "application/json"
    assert call["headers"]["Cookie"] == "a=1; b=2"
    assert call["body"] == {
        "now": None,
        "maintenance": None,
        "restrict": None,
        "access": None,
        "n8TMID": "898484701286067202",
        "strItemType": "transcendent",
        "strItemId": "211064002",
    }


def test_lounge_and_confirm_paths() -> None:
    transport = RecordingTransport(
        [
            {"IsSuccess": True, "ResultCode": "SUCCESS", "ResultData": {"seller": "s", "enc": "E"}},
            {"IsSuccess": True, "ResultCode": "SUCCESS"},
        ]
    )
    client = TradeClient({"x": "y"}, transport=transport)
    first, second = client.buy("123", "ancestor", "261001023", lounge=True)

    assert first.ok and second and second.ok
    assert transport.calls[0]["url"].endswith("/lounge")
    assert transport.calls[0]["body"]["strItemType"] == "ancestor"
    assert transport.calls[1]["url"].endswith("/lounge/buy")
    assert transport.calls[1]["body"] == {
        "now": None,
        "maintenance": None,
        "restrict": None,
        "access": None,
        "enc": "E",
    }


def test_business_error_keeps_enc_empty() -> None:
    transport = RecordingTransport(
        [{"IsSuccess": False, "ResultCode": "NO_ITEM", "ResultMessage": "gone"}]
    )
    result = TradeClient({}, transport=transport).chat("1", "transcendent", "211064002")
    assert not result.ok
    assert result.code == "NO_ITEM"
    assert result.enc == ""
    assert "商品不存在" in result.readable


def test_check_session_uses_placeholder_listing() -> None:
    transport = RecordingTransport(
        [{"IsSuccess": False, "ResultCode": "NO_COUNT", "ResultMessage": ""}]
    )
    result = check_session({"a": "b"}, transport=transport)
    assert result.code == "NO_COUNT"
    assert session_usable(result)
    assert transport.calls[0]["body"]["n8TMID"] == "1"


def test_session_usable_rejects_login_errors() -> None:
    transport = RecordingTransport([{"IsSuccess": False, "ResultCode": "NO_LOGIN"}])
    assert not session_usable(check_session({}, transport=transport))
    transport = RecordingTransport([{"IsSuccess": False, "ResultCode": "INVALID_SESSION"}])
    assert not session_usable(check_session({}, transport=transport))


def test_describe_code_unknown() -> None:
    assert describe_code("WEIRD", "msg") == "WEIRD：msg"
    assert "30 秒" in describe_code("RETRY_LATER")


def test_store_token_roundtrip() -> None:
    store = Store(":memory:")
    try:
        store.save_token(
            1001,
            {"_if_game_auth": "a", "x": "1"},
            user_agent="UA",
            platform="arena",
            method="cookie_import",
        )
        token = store.get_token(1001)
        assert token is not None
        assert token.cookies["_if_game_auth"] == "a"
        assert token.cookie_header == "_if_game_auth=a; x=1"
        assert token.status == "valid"
        assert token.method == "cookie_import"

        assert store.update_token_status(1001, "invalid", "NO_LOGIN")
        token = store.get_token(1001)
        assert token and token.status == "invalid" and token.last_error == "NO_LOGIN"

        store.save_token(1001, {"_if_game_auth": "b"}, method="steam_qr")
        token = store.get_token(1001)
        assert token and token.method == "steam_qr"

        assert store.delete_token(1001)
        assert store.get_token(1001) is None
    finally:
        store.close()


def test_store_migrates_old_tokens_table(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE tokens (
            qq INTEGER PRIMARY KEY,
            cookies_json TEXT NOT NULL,
            user_agent TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'unknown',
            last_error TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            last_checked_at INTEGER
        )
        """
    )
    conn.commit()
    conn.close()

    store = Store(path)
    try:
        store.save_token(1001, {"_if_game_auth": "a"}, method="cookie_import")
        token = store.get_token(1001)
        assert token is not None
        assert token.method == "cookie_import"
    finally:
        store.close()


def test_store_purchase_roundtrip() -> None:
    store = Store(":memory:")
    try:
        pid = store.add_purchase(
            1001,
            mode="chat",
            action="prepare",
            item_name="怪物猎人",
            item_type="transcendent",
            item_id="211064002",
            n8_id="898484701286067202",
            price=1050.0,
            seller="s#1",
            enc="ENC",
            status="prepared",
            result_code="SUCCESS",
            raw={"ok": True},
        )
        purchase = store.get_purchase(pid, 1001)
        assert purchase is not None
        assert purchase.enc == "ENC"
        assert purchase.price == 1050.0
        assert purchase.status == "prepared"

        assert store.update_purchase(pid, 1001, status="success", result_code="SUCCESS")
        purchase = store.get_purchase(pid, 1001)
        assert purchase and purchase.status == "success"

        rows = store.list_purchases(1001)
        assert len(rows) == 1 and rows[0].id == pid
        assert store.get_purchase(pid, 9999) is None
    finally:
        store.close()
