"""Bark 手机推送：每用户一份配置，订阅级开关与铃声。

- device key 用 Windows DPAPI（当前用户作用域）加密后存 `user_bark.device_key_enc`；
  换 Windows 用户/机器后无法解密，需要重新录入。
- 推送内容只放文字：标题/副标题 + 该商品的 `/购买` 精确条件，`copy` 可直接复制到 QQ。
- 发送走 `POST {server}/push`，key 放 JSON body，不出现在 URL 与日志里。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any
from urllib.parse import urlparse

from .steam_vault import VaultError, _protect, _unprotect

logger = logging.getLogger(__name__)

DEFAULT_SERVER = "https://api.day.app"
DEFAULT_SOUND = "alarm"
DEFAULT_GROUP = "TFD市场"
MARKET_URL = "https://tfd.nexon.com/zh-Hans/market"
BODY_LIMIT = 400
REQUEST_TIMEOUT = 5

_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_SOUND_RE = re.compile(r"^[A-Za-z0-9._-]{1,60}$")
_UNSAFE = re.compile(r"[\s=,]+")


class BarkError(RuntimeError):
    """Bark 配置或发送失败；message 可直接回复给用户。"""


# ── 配置解析 ────────────────────────────────────────────


def protect_key(key: str) -> bytes:
    """DPAPI 加密 device key。"""
    return _protect(key.encode("utf-8"))


def unprotect_key(blob: bytes) -> str:
    try:
        return _unprotect(bytes(blob)).decode("utf-8")
    except VaultError as exc:
        raise BarkError("Bark 密钥无法解密：可能换了 Windows 用户或机器，请重新录入。") from exc


def parse_bark_target(text: str) -> tuple[str, str]:
    """接受完整地址 `https://api.day.app/KEY` 或裸 KEY，返回 (server, key)。"""
    raw = (text or "").strip()
    if not raw:
        raise BarkError("请输入 Bark 地址或 Key。")
    if raw.lower().startswith(("http://", "https://")):
        parsed = urlparse(raw)
        if not parsed.netloc:
            raise BarkError("Bark 地址不完整，应为 https://api.day.app/你的Key")
        server = f"{parsed.scheme.lower()}://{parsed.netloc}".rstrip("/")
        key = parsed.path.strip("/").split("/")[0]
    else:
        server = DEFAULT_SERVER
        key = raw.strip("/")
    if not key or not _KEY_RE.match(key):
        raise BarkError("Bark Key 格式不正确（应为 8~64 位字母、数字或 . _ -）。")
    return server, key


def normalize_sound(name: str | None) -> str:
    """铃声名直接使用，不加 `.caf` 等后缀（实测带后缀反而不响）。"""
    value = (name or "").strip()
    if not value:
        value = DEFAULT_SOUND
    if not _SOUND_RE.match(value):
        raise BarkError("铃声名只能包含字母、数字、点、下划线和短横线，例如 alarm。")
    return value


# ── 消息构造 ────────────────────────────────────────────


def _safe_token(value: str) -> str:
    """DSL 用空格分隔参数，含空格/逗号/等号的值不能写进命令。"""
    return "" if _UNSAFE.search(value) else value


def _price(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _ability_points(item: dict[str, Any]) -> dict[int, str]:
    data = item.get("data") or {}
    points: dict[int, str] = {}
    for option in data.get("options") or []:
        try:
            group = int(option.get("n4Group"))
        except (TypeError, ValueError):
            continue
        value = option.get("n4Point")
        if group in (0, 1, 2) and value not in (None, ""):
            points[group] = str(value)
    return points


def build_command(item: dict[str, Any], spec: dict[str, Any]) -> str:
    """把单条商品渲染成可直接执行的 `/购买` 精确条件。"""
    item_meta = spec.get("item") or {}
    name = _safe_token(str(item_meta.get("name") or "").strip()) or "物品"
    tokens: list[str] = [f"/购买 {name}"]
    type_labels = {
        "transcendent": "超越武器",
        "ancestor": "模块",
        "descendant": "继承者",
        "trigger": "触发器",
    }
    item_type = str(item_meta.get("type") or "")
    if item_type in type_labels:
        tokens.append(f"类型={type_labels[item_type]}")

    data = item.get("data") or {}
    quality = data.get("n4WeaponQuality")
    if quality not in (None, ""):
        tokens.append(f"品质={quality}-{quality}")
    price = _price(item.get("n4SalePrice"))
    if price:
        tokens.append(f"价格={price}-{price}")

    subperks: list[str] = []
    for entry in spec.get("subperks") or []:
        token = _safe_token(str(entry or "").strip())
        if token:
            subperks.append(token)
    if not subperks:
        for subperk in data.get("subperks") or []:
            token = _safe_token(str(subperk.get("strName") or "").strip())
            if token:
                subperks.append(token)
    if subperks:
        tokens.append("辅助=" + ",".join(subperks[:2]))

    for group, point in sorted(_ability_points(item).items()):
        tokens.append(f"能力{group + 1}={point}-{point}")

    optional: list[str] = []
    for index, cond in enumerate(spec.get("advantage") or []):
        key = _safe_token(str(cond.get("key") or "").strip())
        grade = _safe_token(str(cond.get("grade") or "").strip())
        if key:
            optional.append(f"增益={key}:{grade}" if grade else f"增益={key}")
    if spec.get("penalty"):
        penalty = _safe_token(str(spec["penalty"].get("key") or "").strip())
        if penalty:
            optional.append(f"减益={penalty}")

    for extra in optional:
        candidate = tokens + [extra]
        if len(" ".join(candidate)) <= BODY_LIMIT:
            tokens = candidate

    # 超长时逐步裁剪：能力3 -> 辅助 -> 最低只保留价格/品质
    while len(" ".join(tokens)) > BODY_LIMIT and "能力3" in " ".join(tokens):
        tokens = [t for t in tokens if not t.startswith("能力3=")]
    while len(" ".join(tokens)) > BODY_LIMIT and any(t.startswith("辅助=") for t in tokens):
        tokens = [t for t in tokens if not t.startswith("辅助=")]
    return " ".join(tokens)[:BODY_LIMIT]


def build_payload(
    *,
    sub_id: int,
    matched: int,
    new_count: int,
    command: str,
    item_name: str,
    price: str,
    sound: str,
    group: str,
    level: str,
    call: bool,
) -> dict[str, Any]:
    subtitle = f"订阅 #{sub_id} · 新 {new_count} 条"
    if price:
        subtitle += f" · 最低 ¥{price}"
    payload: dict[str, Any] = {
        "title": f"{item_name} 命中 {matched} 条",
        "subtitle": subtitle,
        "body": command,
        "copy": command,
        "sound": sound,
        "group": group or DEFAULT_GROUP,
        "level": level or "timeSensitive",
        "url": MARKET_URL,
        "id": f"sub-{sub_id}",
        "isArchive": 1,
    }
    if call:
        payload["call"] = "1"
    return payload


# ── 发送 ────────────────────────────────────────────────


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


def _brief(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:120]


def _read_response(status: int, raw: str) -> tuple[bool, str, bool]:
    """解析一次响应；返回 (成功, 错误描述, 是否值得换实现重试)。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False, f"HTTP {status}，响应不是 JSON：{raw[:80]}", status >= 500
    if int(data.get("code") or 0) == 200:
        return True, "", False
    message = str(data.get("message") or raw)[:120]
    return False, f"HTTP {status} {message}".strip(), status >= 500


def _post_urllib(url: str, body: bytes) -> tuple[bool, str, bool]:
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return _read_response(int(resp.status), raw)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            raw = ""
        return _read_response(int(exc.code), raw)
    except Exception as exc:  # noqa: BLE001 — TLS/网络问题，交给 curl_cffi 重试
        return False, f"网络/TLS 异常：{_brief(exc)}", True


def _post_curl(url: str, body: bytes) -> tuple[bool, str, bool]:
    try:
        from curl_cffi import requests
    except ImportError:  # pragma: no cover — 依赖缺失时只保留 urllib
        return False, "未安装 curl_cffi", False
    try:
        resp = requests.post(
            url,
            data=body,
            impersonate="chrome133a",
            timeout=REQUEST_TIMEOUT,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        return _read_response(int(resp.status_code), resp.text)
    except Exception as exc:  # noqa: BLE001
        return False, f"网络/TLS 异常：{_brief(exc)}", True


def post_bark(server: str, key: str, payload: dict[str, Any]) -> tuple[bool, str]:
    """同步发送一次；先 urllib，失败再换 curl_cffi。key 只放 body。"""
    body = json.dumps({**payload, "device_key": key}, ensure_ascii=False).encode("utf-8")
    url = f"{server.rstrip('/')}/push"
    errors: list[str] = []
    for sender in (_post_urllib, _post_curl):
        ok, error, retryable = sender(url, body)
        if ok:
            return True, ""
        errors.append(error)
        if not retryable:
            return False, error
    return False, "；".join(errors)[:200]


class BarkService:
    """按订阅触发 Bark；冷却、每分钟上限、每 tick 预算都在这里统一控制。"""

    def __init__(self, store, permissions, *, tick_limit: int = 5) -> None:
        self.store = store
        self.permissions = permissions
        self.tick_limit = max(1, int(tick_limit))
        self._budget = 0
        self._sent: dict[int, deque[float]] = {}

    def begin_tick(self) -> None:
        self._budget = self.tick_limit

    def _rate_ok(self, qq: int, now: float) -> bool:
        limit = max(1, int(self.permissions.bark_max_per_min()))
        bucket = self._sent.setdefault(int(qq), deque())
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        return len(bucket) < limit

    async def notify(
        self,
        sub,  # noqa: ANN001 — qqbot.store.Subscription
        items: list[dict[str, Any]],
        matched: int,
        new_count: int,
    ) -> bool:
        if matched <= 0 or not items:
            return False
        if not self.permissions.bark_enabled():
            return False
        user = self.store.get_user_bark(sub.qq)
        if user is None or not user.enabled:
            return False
        if not sub.bark_enabled or self._budget <= 0:
            return False

        now = int(time.time())
        cooldown = int(self.permissions.bark_cooldown_sec())
        if now - max(int(sub.bark_last_at or 0), int(user.last_sent_at or 0)) < cooldown:
            return False
        if not self._rate_ok(int(sub.qq), time.monotonic()):
            return False

        try:
            key = unprotect_key(user.device_key_enc)
            sound = normalize_sound(sub.bark_sound or user.sound)
        except BarkError as exc:
            self.store.mark_bark_error(int(sub.qq), str(exc)[:200])
            logger.warning("Bark 配置不可用 qq=%s：%s", sub.qq, exc)
            return False

        try:
            lowest = min(items, key=lambda it: float(it.get("n4SalePrice") or 1e18))
        except (TypeError, ValueError):
            lowest = items[0]
        item_meta = (sub.spec or {}).get("item") or {}
        item_name = str(item_meta.get("name") or "物品")
        command = build_command(lowest, sub.spec or {})
        payload = build_payload(
            sub_id=int(sub.id),
            matched=int(matched),
            new_count=int(new_count),
            command=command,
            item_name=item_name,
            price=_price(lowest.get("n4SalePrice")) or "",
            sound=sound,
            group=self.permissions.bark_group(),
            level=self.permissions.bark_level(),
            call=self.permissions.bark_call(),
        )

        self._budget -= 1
        ok, error = await asyncio.to_thread(post_bark, user.server, key, payload)
        if ok:
            self.store.mark_bark_sent(int(sub.id), int(sub.qq), now)
            self._sent.setdefault(int(sub.qq), deque()).append(time.monotonic())
            logger.info("Bark 已推送：qq=%s 订阅 #%s 命中 %s 条", sub.qq, sub.id, matched)
            return True
        self.store.mark_bark_error(int(sub.qq), error)
        logger.warning("Bark 推送失败 qq=%s 订阅 #%s：%s", sub.qq, sub.id, error)
        return False
