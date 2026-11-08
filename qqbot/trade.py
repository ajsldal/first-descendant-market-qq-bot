"""已登录的交易市场写操作：私聊购买 / 休闲室购买。

接口（2026-10 实测）：
- ``POST /api/v2/trademarket/chat``      n8TMID + strItemType + strItemId → seller + enc
- ``POST /api/v2/trademarket/chat/buy``  enc → 确认并发送游戏内私聊
- ``POST /api/v2/trademarket/lounge``    打开“添加信息到休闲室”框，参数同上
- ``POST /api/v2/trademarket/lounge/buy`` enc → 确认并添加休闲室

每对端点必须成对使用：chat → chat/buy，lounge → lounge/buy。
未登录返回 ``NO_LOGIN``，Cookie 过期返回 ``INVALID_SESSION``。
本模块不做自动重试，避免写操作被重复提交。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from market_v2.transport import CurlTransport, Response, Transport, parse_json

API_ROOT = "https://tfd-api.nexon.com/api/v2/trademarket"

# 服务端 ResultCode → 可直接展示给用户的中文说明
RESULT_MESSAGES = {
    "SUCCESS": "成功",
    "NO_LOGIN": "未登录或登录态已丢失，请先 /登录",
    "INVALID_SESSION": "登录已过期，请重新 /登录 或 /续期登录",
    "MAINTENANCE": "游戏维护中，暂时无法交易",
    "INVALID_PARAM": "请求参数不合法",
    "NO_PRIMARY": "账号没有主角色；请先在游戏里设置主角色",
    "RESTRICT_GAME": "买家账号被游戏限制",
    "RESTRICT_GAME_SELLER": "卖家账号被游戏限制",
    "RESTRICT_TRADE": "买家账号被交易限制",
    "RESTRICT_TRADE_SELLER": "卖家被交易限制",
    "RESTRICT_CHAT": "买家账号被聊天限制",
    "MATCHED_USERINFO": "不能购买/私聊自己上架的商品",
    "NO_ACCESS": "买家未启动游戏 App 或不在游戏中；也可能是卖家不可访问，请先启动游戏并进入主角色",
    "NO_ACCESS_SELLER": "卖家当前不在线，无法私聊；可改用「添加到休闲室」",
    "NO_ITEM": "商品不存在或已下架",
    "NO_COUNT": "商品已被买走",
    "RETRY_LATER": "操作太频繁，请 30 秒后再试（服务端冷却）",
    "OVER_LIMIT_COUNT": "私聊次数已达上限，请稍后再试",
    "COUNTRY_RESTRICTED": "当前地区不支持该交易",
    "NOT_MATCHED_ALLOWED": "物品类型与物品 ID 不匹配",
}


def describe_code(code: str, message: str = "") -> str:
    base = RESULT_MESSAGES.get(code or "")
    if base:
        return base
    return f"{code or 'UNKNOWN'}：{message}" if message else (code or "未知错误")


@dataclass
class TradeResult:
    step: str
    code: str
    message: str = ""
    seller: str = ""
    enc: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.code == "SUCCESS"

    @property
    def readable(self) -> str:
        return describe_code(self.code, self.message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "code": self.code,
            "message": self.message,
            "seller": self.seller,
            "has_enc": bool(self.enc),
        }


class TradeClient:
    """带 Cookie 的市场写操作客户端。transport 可注入以便测试。"""

    def __init__(
        self,
        cookies: dict[str, str],
        *,
        user_agent: str = "",
        timeout: float = 30.0,
        transport: Transport | None = None,
    ) -> None:
        self._transport = transport or CurlTransport(
            impersonate="chrome136", default_timeout=timeout
        )
        self._owns_transport = transport is None
        self._timeout = timeout
        self._headers = {
            "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
            "Accept": "application/json, text/plain, */*",
            "Cache-Control": "no-cache",
            "Origin": "https://tfd.nexon.com",
            "Referer": "https://tfd.nexon.com/",
        }

    # ── 基础请求 ────────────────────────────────────────────

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp: Response = self._transport.request(
            "POST",
            f"{API_ROOT}{path}",
            json_body=payload,
            headers=self._headers,
            content_type="application/json",
            timeout=self._timeout,
        )
        return parse_json(resp)

    @staticmethod
    def _result(step: str, data: dict[str, Any]) -> TradeResult:
        code = str(data.get("ResultCode") or ("SUCCESS" if data.get("IsSuccess") else "UNKNOWN"))
        message = str(data.get("ResultMessage") or "")
        result_data = data.get("ResultData")
        if not isinstance(result_data, dict):
            result_data = {}
        seller = result_data.get("seller")
        if isinstance(seller, dict):
            seller = seller.get("strNickName") or seller.get("name") or json.dumps(
                seller, ensure_ascii=False
            )
        return TradeResult(
            step=step,
            code=code,
            message=message,
            seller=str(seller or ""),
            enc=str(result_data.get("enc") or ""),
            raw=data,
        )

    # ── 业务动作 ────────────────────────────────────────────

    def chat(
        self,
        n8_tmid: str,
        item_type: str,
        item_id: str,
        *,
        lounge: bool = False,
    ) -> TradeResult:
        """发起私聊/组队请求，成功时返回 seller + enc。"""
        payload = {
            "now": None,
            "maintenance": None,
            "restrict": None,
            "access": None,
            "n8TMID": str(n8_tmid),
            "strItemType": str(item_type),
            "strItemId": str(item_id),
        }
        data = self._post("/lounge" if lounge else "/chat", payload)
        return self._result("lounge" if lounge else "chat", data)

    def confirm(self, enc: str, *, lounge: bool = False) -> TradeResult:
        """用 enc 确认上一步；成功时交易流程结束。"""
        payload = {
            "now": None,
            "maintenance": None,
            "restrict": None,
            "access": None,
            "enc": str(enc),
        }
        data = self._post("/lounge/buy" if lounge else "/chat/buy", payload)
        return self._result("lounge/buy" if lounge else "chat/buy", data)

    def buy(
        self,
        n8_tmid: str,
        item_type: str,
        item_id: str,
        *,
        lounge: bool = False,
    ) -> tuple[TradeResult, TradeResult | None]:
        """完整两步流程；chat 失败时不发 confirm。"""
        first = self.chat(n8_tmid, item_type, item_id, lounge=lounge)
        if not first.ok:
            return first, None
        second = self.confirm(first.enc, lounge=lounge)
        if second.seller == "":
            second.seller = first.seller
        return first, second

    def close(self) -> None:
        if self._owns_transport:
            self._transport.close()


def check_session(
    cookies: dict[str, str],
    *,
    user_agent: str = "",
    timeout: float = 20.0,
    transport: Transport | None = None,
) -> TradeResult:
    """校验 Cookie 是否可用于交易。

    用不存在的 n8TMID 调 /chat：登录有效时服务端会返回 NO_ITEM/NO_COUNT
    等业务错误；只有 NO_LOGIN / INVALID_SESSION 才说明登录态不可用。
    """
    client = TradeClient(
        cookies,
        user_agent=user_agent,
        timeout=timeout,
        transport=transport,
    )
    try:
        result = client.chat("1", "transcendent", "211064002")
    finally:
        client.close()
    return result


def session_usable(result: TradeResult) -> bool:
    # 只有明确的登录态错误才算失效；NO_ITEM/INVALID_PARAM 等业务错误
    # 仍说明服务端已经识别到了登录用户。
    return result.code not in ("NO_LOGIN", "INVALID_SESSION")
