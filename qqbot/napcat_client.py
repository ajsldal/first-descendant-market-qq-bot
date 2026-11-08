"""NapCatQQ HTTP 客户端（OneBot v11 协议，仅依赖标准库）。

用法::

    from qqbot.napcat_client import NapCatClient

    bot = NapCatClient()                      # 读 NAPCAT_URL / NAPCAT_TOKEN
    print(bot.get_login_info())
    bot.send_group_msg(123456, "hello")

NapCat 4.x 的 HTTP Server 接收「动作调用」：
POST /{action}，参数**直接平铺在 body 顶层**（不再套 params），例如
{"user_id": 123, "message": "hi", "echo": "x"}；
返回 {"status", "retcode", "data", "message", "wording", "echo"}。
POST / 只是健康检查（返回 "NapCat4 Is Running"），不执行 action。
"""

from __future__ import annotations

import itertools
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable

DEFAULT_URL = "http://127.0.0.1:3000"


class NapCatError(Exception):
    """传输层/协议层错误（网络、鉴权、非 JSON 响应等）。"""

    def __init__(self, message: str, *, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass
class ApiResponse:
    retcode: int
    status: str
    data: Any = None
    message: str = ""
    wording: str = ""
    echo: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.retcode == 0

    def require_ok(self) -> "ApiResponse":
        """发送类动作失败时抛异常，避免调用方误当成功。"""
        if not self.ok:
            detail = self.message or self.wording or ""
            raise NapCatError(
                f"{self.status or 'failed'}/{self.retcode}: {detail}"[:200],
                body=str(self.raw)[:300],
            )
        return self


def text(content: str) -> dict[str, str]:
    return {"type": "text", "data": {"text": content}}


def at(user_id: int | str) -> dict[str, str]:
    return {"type": "at", "data": {"qq": str(user_id)}}


def reply(message_id: int | str) -> dict[str, str]:
    return {"type": "reply", "data": {"id": str(message_id)}}


def image(file: str) -> dict[str, str]:
    """file 可以是 URL、本地路径（需 enableLocalFile2Url）或 base64:// 字符串。"""
    return {"type": "image", "data": {"file": file}}


class NapCatClient:
    """最小可用的 NapCat/OneBot v11 HTTP 客户端。"""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = (base_url or os.getenv("NAPCAT_URL") or DEFAULT_URL).rstrip("/")
        self.token = token if token is not None else os.getenv("NAPCAT_TOKEN", "")
        self.timeout = timeout
        self._echo = itertools.count(1)

    # ── 底层调用 ─────────────────────────────────────────────

    def call(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        method: str = "POST",
        timeout: float | None = None,
    ) -> ApiResponse:
        """调用任意 OneBot action。

        当前 NapCat 4.x 只接受「参数平铺」的 body；套 {"action", "params"}
        会被忽略，带必填参数的动作会报 Schema compilation error。
        """
        payload: dict[str, Any] = dict(params or {})
        payload.setdefault("echo", f"py-{next(self._echo)}")
        url = f"{self.base_url}/{action}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if method == "POST" else None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            hint = (
                "（token 缺失或不匹配：请核对 napcat/config/onebot11_<QQ号>.json "
                "中 network.httpServers[0].token，修改后需重启 NapCat）"
                if exc.code == 403 else ""
            )
            raise NapCatError(
                f"HTTP {exc.code} {action}{hint}", status_code=exc.code, body=raw
            ) from exc
        except urllib.error.URLError as exc:
            raise NapCatError(f"无法连接 {url}: {exc.reason}") from exc

        try:
            obj = json.loads(body)
        except json.JSONDecodeError as exc:
            raise NapCatError(f"响应不是 JSON: {body[:200]}", body=body) from exc

        return ApiResponse(
            retcode=int(obj.get("retcode", -1)),
            status=str(obj.get("status", "")),
            data=obj.get("data"),
            message=str(obj.get("message", "")),
            wording=str(obj.get("wording", "")),
            echo=str(obj.get("echo", "")),
            raw=obj,
        )

    # ── 常用封装 ─────────────────────────────────────────────

    def get_version_info(self) -> dict[str, Any]:
        return self.call("get_version_info").data

    def get_status(self) -> dict[str, Any]:
        return self.call("get_status").data

    def get_login_info(self) -> dict[str, Any]:
        return self.call("get_login_info").data

    def get_friend_list(self) -> list[dict[str, Any]]:
        return self.call("get_friend_list").data or []

    def set_friend_add_request(
        self, flag: str, approve: bool = True, remark: str = ""
    ) -> dict[str, Any]:
        """同意/拒绝好友申请；flag 来自 request 事件的 flag 字段。"""
        params: dict[str, Any] = {"flag": flag, "approve": bool(approve)}
        if remark:
            params["remark"] = remark
        resp = self.call("set_friend_add_request", params)
        resp.require_ok()
        return resp.data

    def get_group_list(self) -> list[dict[str, Any]]:
        return self.call("get_group_list").data or []

    def send_private_msg(
        self, user_id: int | str, message: str | list[dict[str, Any]]
    ) -> dict[str, Any]:
        resp = self.call(
            "send_private_msg", {"user_id": user_id, "message": message}
        )
        resp.require_ok()
        return resp.data

    def send_group_msg(
        self, group_id: int | str, message: str | list[dict[str, Any]]
    ) -> dict[str, Any]:
        resp = self.call("send_group_msg", {"group_id": group_id, "message": message})
        resp.require_ok()
        return resp.data

    def send_msg(
        self,
        message: str | list[dict[str, Any]],
        *,
        user_id: int | str | None = None,
        group_id: int | str | None = None,
    ) -> dict[str, Any]:
        if (user_id is None) == (group_id is None):
            raise ValueError("user_id 与 group_id 必须且只能提供一个")
        params: dict[str, Any] = {"message": message}
        if user_id is not None:
            params["user_id"] = user_id
            params["message_type"] = "private"
        else:
            params["group_id"] = group_id
            params["message_type"] = "group"
        return self.call("send_msg", params).data

    def send_group_text(self, group_id: int | str, content: str) -> dict[str, Any]:
        return self.send_group_msg(group_id, [text(content)])


def messages_to_text(segments: Iterable[dict[str, Any]]) -> str:
    """把消息段数组拼成纯文本（调试用，忽略图片等非文本段）。"""
    return "".join(
        seg.get("data", {}).get("text", "")
        for seg in segments
        if seg.get("type") == "text"
    )
