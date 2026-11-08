from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import HttpError


@dataclass
class Response:
    status_code: int
    text: str

    def json(self) -> Any:
        return json.loads(self.text)


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        content_type: str | None = None,
        timeout: float | None = None,
    ) -> Response: ...

    def close(self) -> None: ...


class CurlTransport:
    """curl_cffi 同步传输层。

    不手写 Sec-Ch-Ua*，交给 impersonate 生成，避免指纹版本错配。
    """

    def __init__(self, impersonate: str = "chrome133a", default_timeout: float = 30.0) -> None:
        from curl_cffi.requests import Session

        self._session = Session(impersonate=impersonate)
        self._timeout = default_timeout
        self._session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
                "Origin": "https://tfd.nexon.com",
                "Referer": "https://tfd.nexon.com/",
            }
        )

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
        hdrs = dict(headers or {})
        if content_type:
            hdrs["Content-Type"] = content_type
        resp = self._session.request(
            method,
            url,
            json=json_body,
            headers=hdrs,
            timeout=timeout or self._timeout,
        )
        return Response(status_code=resp.status_code, text=resp.text)

    def close(self) -> None:
        self._session.close()


def parse_json(resp: Response) -> dict[str, Any]:
    if resp.status_code != 200:
        raise HttpError(resp.status_code, resp.text)
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise HttpError(resp.status_code, f"invalid json: {resp.text[:200]}") from exc
    if not isinstance(data, dict):
        raise HttpError(resp.status_code, f"unexpected body: {resp.text[:200]}")
    return data
