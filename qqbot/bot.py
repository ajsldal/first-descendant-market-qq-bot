"""QQ 机器人主程序：WebSocket 收事件，HTTP 发消息，内置定时调度。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets

from .card import render_card
from .analytics import AnalyticsRecorder
from .dsl import (
    DslError,
    describe_spec,
    describe_subscription,
    fingerprint_for,
    format_duration,
    help_text,
    parse_dsl,
    parse_settings,
    query_template,
    query_template_overview,
)
from .market import FetchResult, MarketService
from .friends import FriendDirectory
from .napcat_client import NapCatClient, image, reply, text
from .nexon_auth import (
    PLATFORM,
    TFD_DIRECT_HOSTS,
    USER_AGENT,
    CookieImportError,
    LoginCancelled,
    LoginError,
    LoginTimeout,
    ManualLoginSession,
    QRLoginSession,
    parse_cookie_payload,
)
from .bark import (
    BarkError,
    BarkService,
    build_payload,
    normalize_sound,
    parse_bark_target,
    post_bark,
    protect_key,
    unprotect_key,
)
from .flip import build_fuzzy_spec, overrides_to_text, parse_flip_overrides
from .pusher import Pusher, format_item_summary
from .permissions import PermissionService
from .scheduler import Scheduler
from .steam_vault import VaultError, default_vault
from .store import Store, Subscription
from .trade import TradeClient, check_session, describe_code, session_usable
from .wizard import Wizard

logger = logging.getLogger(__name__)

PREFIXES = ("/", "／", "#", "！", "!")
REPLY_LIMIT = 1500
GROUP_REPLY_WINDOW = 8
GROUP_REPLY_WINDOW_SECONDS = 60
GROUP_MUTE_SECONDS = 600
IMPORT_TTL_SECONDS = 300
IMPORT_MAX_CHARS = 20000
TRADE_SESSION_TTL = 300
BROWSE_LIMIT = 10
BROWSE_LIMIT_OFFICIAL = 5

SUBSCRIBE_COMMANDS = {"订阅", "sub", "subscribe"}
LOGIN_COMMANDS = {
    "登录", "login", "浏览器登录", "browserlogin", "login-browser",
    "自动登录", "自动", "auto", "steamauto",
    "导入登录", "importlogin", "login-import", "完成导入", "finishimport",
    "importdone", "取消登录", "cancellogin", "cancel-login", "登出", "logout",
    "登录状态", "账号", "account", "me", "检查登录", "checklogin", "token",
}
TRADE_COMMANDS = {
    "购买", "buy", "求购", "采购", "prepare", "组队", "休闲室", "lounge",
    "选择", "select", "确认", "confirm", "采购记录", "订单", "orders",
}
# 订阅相关命令要求好友关系；查价/物品/选项等群聊查询命令不在此列
SUBSCRIPTION_COMMANDS = {
    "订阅", "sub", "subscribe",
    "我的订阅", "订阅列表", "列表", "list",
    "删除", "取消订阅", "del", "delete",
    "暂停", "pause", "恢复", "resume",
    "立即查", "check", "now",
    "设置", "set",
    "模糊", "倒卖", "flip",
}
PRICE_COMMANDS = {"查价", "查询", "价格", "price", "query"}
# 退出登录只涉及本地凭据删除，不受白名单限制
LOGIN_SAFE_COMMANDS = {"登出", "logout"}
GUIDE_IMAGE_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "design" / "qqbot-command-guide.png"
)
FRIEND_REQUEST_TTL = 600
FRIEND_WELCOME_DELAY = 1.0
FRIEND_RETRY_DELAY = 2.0
BARK_COMMANDS = {"bark", "手机推送", "推送设置", "bark设置"}
FLIP_COMMANDS = {"模糊", "倒卖", "flip"}
TRADE_COOLDOWN = 30


@dataclass
class ImportSession:
    parts: list[str]
    updated_at: float
    notified: bool = False

    @property
    def text(self) -> str:
        # JSON 分片必须原样拼接，不能插入换行
        return "".join(self.parts)

    @property
    def size(self) -> int:
        return sum(len(p) for p in self.parts)


@dataclass
class TradeBrowseSession:
    """一次“浏览筛选结果 → 选商品 → 选购买方式 → 确认”的会话。"""

    qq: int
    mode: str                      # ask / chat / lounge
    item: Any                      # CatalogItem
    spec_dict: dict[str, Any]
    fingerprint: str
    items: list[dict[str, Any]]
    expires_at: float
    stage: str = "select"          # select / method / confirm
    selected: dict[str, Any] | None = None
    enc: str = ""
    record_id: int | None = None
    seller: str = ""


@dataclass
class BotSettings:
    url: str = "http://127.0.0.1:3000"
    ws_url: str = "ws://127.0.0.1:3000/"
    token: str = ""
    db_path: str = "data/qqbot.db"
    tick_seconds: int = 30
    workers: int = 2
    max_pages: int = 3
    # 为空表示允许所有群；配置后只处理白名单群
    group_whitelist: frozenset[int] = frozenset()
    # 永远不响应的 QQ（例如 QQ管家/其他机器人），避免互相触发
    ignore_qq: frozenset[int] = frozenset()
    # 输出图片卡片；失败自动退回纯文字
    image_enabled: bool = True
    image_style: str = "hybrid"
    # 每个用户最多可创建的订阅数，由 config.json 覆盖
    max_subscriptions: int = 5
    # 订阅抓取分析：仅超越武器、仅白名单 QQ
    analytics_enabled: bool = True
    analytics_whitelist: frozenset[int] = frozenset()
    analytics_runs_retention_days: int = 30
    # Steam 自动登录：仅白名单 QQ；凭据另存在 DPAPI 加密文件
    steam_auto_login_enabled: bool = True
    steam_auto_allowed_qq: frozenset[int] = frozenset()
    steam_auto_guard_timeout: int = 1800
    # 全部登录命令的白名单；为空表示所有人禁止
    login_allowed_qq: frozenset[int] = frozenset()
    # 好友申请：自动同意与欢迎
    friend_auto_accept_enabled: bool = True
    friend_welcome_enabled: bool = True
    friend_accept_max_per_min: int = 5
    # Steam 扫码登录用的代理（国内直连 steamcommunity.com 会超时）
    login_proxy: str = ""
    login_timeout: int = 300
    # 登录浏览器里 tfd.nexon.com / tfd-api.nexon.com 直连，避免代理 403
    login_bypass: bool = True
    # 输出 [audit] 级详细日志，排查登录问题用
    login_debug: bool = False


class QQBot:
    def __init__(self, settings: BotSettings) -> None:
        self.settings = settings
        self.store = Store(settings.db_path)
        self.market = MarketService()
        self.client = NapCatClient(settings.url, settings.token)
        self.friends = FriendDirectory(lambda: self.client)
        self.pusher = Pusher(
            self.client,
            image_enabled=settings.image_enabled,
            image_style=settings.image_style,
        )
        self.permissions = PermissionService(
            self.store,
            defaults={
                "max_subscriptions": settings.max_subscriptions,
                "analytics_enabled": settings.analytics_enabled,
                "analytics_whitelist_qq": sorted(settings.analytics_whitelist),
                "analytics_runs_retention_days": settings.analytics_runs_retention_days,
                "login_allowed_qq": sorted(settings.login_allowed_qq),
                "friend_auto_accept_enabled": settings.friend_auto_accept_enabled,
                "friend_welcome_enabled": settings.friend_welcome_enabled,
                "friend_accept_max_per_min": settings.friend_accept_max_per_min,
            },
        )
        self.bark = BarkService(self.store, self.permissions)
        self.scheduler = Scheduler(
            self.store,
            self.market,
            self.pusher,
            tick_seconds=settings.tick_seconds,
            workers=settings.workers,
            max_pages=settings.max_pages,
            analytics=AnalyticsRecorder(
                self.store,
                whitelist=self.permissions.analytics_whitelist,
                enabled=self.permissions.analytics_enabled,
            ),
            permissions=self.permissions,
            bark=self.bark,
            friends=self.friends,
        )
        self.wizard = Wizard(self._resolve_for_wizard)
        self.self_id: int | None = None
        self._seen_messages: deque[int] = deque(maxlen=500)
        self._locks: dict[int, asyncio.Lock] = {}
        self._lock_used: dict[int, float] = {}
        self._last_command: dict[int, float] = {}
        self._group_reply_times: dict[tuple[int, int], deque[float]] = {}
        self._group_muted_until: dict[tuple[int, int], float] = {}
        # 限制命令触发的市场请求并发，避免大量用户同时解析物品
        self._market_gate = asyncio.Semaphore(2)
        self._maintenance_task: asyncio.Task | None = None
        # 扫码登录会话（线程内跑 Playwright，最多 2 个并发）
        self._login_sessions: dict[int, QRLoginSession] = {}
        self._login_lock = threading.Lock()
        self._login_slots = threading.BoundedSemaphore(2)
        # 手动导入 Cookie 的多段缓冲
        self._cookie_imports: dict[int, ImportSession] = {}
        # 购买浏览会话：筛选结果 + 当前选择
        self._trade_sessions: dict[int, TradeBrowseSession] = {}
        # 服务端购买有 30 秒冷却，本地也做一次节流
        self._last_trade: dict[int, float] = {}
        # /帮助 使用图：只读取/压缩一次，之后复用 base64
        self._guide_image_b64: str | None = None
        # 好友申请去重与每分钟限流
        self._friend_request_seen: dict[str, float] = {}
        self._friend_accept_times: deque[float] = deque(maxlen=100)

    async def run(self) -> None:
        info = await asyncio.to_thread(self.client.get_login_info)
        self.self_id = int(info.get("user_id") or 0)
        logger.info("机器人已登录：%s（%s）", self.self_id, info.get("nickname"))
        self.store.prune_seen(days=30)

        scheduler_task = asyncio.create_task(self.scheduler.run(), name="scheduler")
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(), name="maintenance"
        )
        tasks = (scheduler_task, self._maintenance_task)
        try:
            await self._ws_loop()
        finally:
            self.scheduler.stop()
            with self._login_lock:
                for session in self._login_sessions.values():
                    session.cancel()
            for task in tasks:
                if task:
                    task.cancel()
            for task in tasks:
                if not task:
                    continue
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self.scheduler.close()
            self.market.close()
            self.store.close()

    async def _maintenance_loop(self) -> None:
        """定期清理过期会话、锁、缓存和数据库历史，避免长期运行膨胀。"""
        while True:
            await asyncio.sleep(600)
            try:
                now = time.time()
                expired_wizard = self.wizard.purge_expired()
                expired_seen = self.store.prune_seen(days=30)
                expired_runs = self.store.prune_runs(days=30)
                expired_crawl_runs = self.store.prune_crawl_runs(
                    days=self.permissions.analytics_retention_days()
                )
                expired_purchases = self.store.prune_purchases(days=90)
                expired_cache = self.scheduler.prune_cache(max_entries=200, max_age=600)
                expired_render = self.pusher.prune_render(max_entries=200, max_age=86400)

                idle = [
                    qq for qq, used in self._lock_used.items()
                    if now - used > 3600 and qq not in self._locks
                ]
                for qq in idle:
                    self._lock_used.pop(qq, None)
                    self._last_command.pop(qq, None)
                stale_locks = [
                    qq for qq, lock in self._locks.items()
                    if not lock.locked() and now - self._lock_used.get(qq, 0) > 3600
                ]
                for qq in stale_locks:
                    self._locks.pop(qq, None)
                    self._lock_used.pop(qq, None)
                    self._last_command.pop(qq, None)

                for key, times in list(self._group_reply_times.items()):
                    while times and now - times[0] > 3600:
                        times.popleft()
                    if not times:
                        self._group_reply_times.pop(key, None)
                        if self._group_muted_until.get(key, 0) < now:
                            self._group_muted_until.pop(key, None)
                for key, until in list(self._group_muted_until.items()):
                    if until < now:
                        self._group_muted_until.pop(key, None)

                expired_imports = 0
                for import_qq, session in list(self._cookie_imports.items()):
                    if now - session.updated_at > IMPORT_TTL_SECONDS:
                        self._cookie_imports.pop(import_qq, None)
                        expired_imports += 1

                expired_trade = 0
                for trade_qq, session in list(self._trade_sessions.items()):
                    if session.expires_at <= now:
                        self._trade_sessions.pop(trade_qq, None)
                        expired_trade += 1

                logger.debug(
                    "维护完成：向导 %s、seen %s、runs %s、采购 %s、缓存 %s、渲染图 %s、导入 %s、购买会话 %s、锁 %s",
                    expired_wizard, expired_seen, expired_runs, expired_purchases,
                    expired_cache, expired_render, expired_imports, expired_trade,
                    len(stale_locks),
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 维护失败不能影响主流程
                logger.exception("维护任务异常")

    def _login_proxy(self) -> str | None:
        """登录浏览器代理：后台可开关/改地址，未配置时回退 --login-proxy。"""
        if not self.permissions.login_proxy_enabled():
            return None
        url = self.permissions.login_proxy_url() or self.settings.login_proxy
        return url.strip() or None

    async def _ws_loop(self) -> None:
        headers = {"Authorization": f"Bearer {self.settings.token}"} if self.settings.token else None
        delay = 2
        while True:
            try:
                async with websockets.connect(
                    self.settings.ws_url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=None,
                    open_timeout=10,
                ) as ws:
                    logger.info("WebSocket 已连接：%s", self.settings.ws_url)
                    delay = 2
                    async for raw in ws:
                        try:
                            await self._on_raw(raw)
                        except Exception:  # noqa: BLE001 — 单条事件不能打断连接
                            logger.exception("处理事件失败")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 自动重连
                logger.warning("WebSocket 断开：%s；%s 秒后重连", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _on_raw(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("非 JSON 消息：%s", str(raw)[:200])
            return
        await self.handle_event(event)

    async def handle_event(self, event: dict[str, Any]) -> None:
        if event.get("post_type") == "request":
            await self._handle_friend_request(event)
            return
        if event.get("post_type") != "message":
            return
        if event.get("message_type") not in ("private", "group"):
            return

        sender = event.get("sender") or {}
        user_id = int(event.get("user_id") or sender.get("user_id") or 0)
        if not user_id or user_id == self.self_id:
            return
        if user_id in self.settings.ignore_qq:
            return
        if self.permissions.is_blocked(user_id):
            logger.info("忽略被封禁用户：%s", user_id)
            return
        message_id = int(event.get("message_id") or 0)
        if message_id and message_id in self._seen_messages:
            return
        if message_id:
            self._seen_messages.append(message_id)

        group_id = int(event.get("group_id") or 0) if event.get("message_type") == "group" else 0
        if (
            group_id
            and self.settings.group_whitelist
            and group_id not in self.settings.group_whitelist
        ):
            logger.info("忽略未授权群消息：%s", group_id)
            return
        if group_id and not self._group_allowed(group_id, user_id):
            return
        segments = event.get("message")
        if not isinstance(segments, list):
            segments = []
        content = "".join(
            str(seg.get("data", {}).get("text") or "")
            for seg in segments
            if seg.get("type") == "text"
        ).strip()

        command = self._strip_prefix(content)
        first_cmd = command.split(maxsplit=1)[0].lower() if command is not None else ""
        is_private = event.get("message_type") == "private"
        pending_import = self._cookie_imports.get(user_id) if is_private else None

        # 导入登录：用户正在分多条粘贴 Cookie，此时整条消息都当作数据
        if pending_import is not None and command is None:
            if not content:
                await self._reply(
                    user_id, group_id, message_id,
                    "没有收到文本。请把 Cookie 文本直接粘贴给我，或发送 /取消登录。",
                )
                return
            if not self._allow_command(user_id, 0.5):
                return
            async with self._lock_for(user_id):
                self.store.upsert_user(user_id)
                await self._feed_cookie_import(user_id, group_id, message_id, content)
            return

        # 购买浏览：用户直接回复数字（1/2/3…）选择商品或操作
        trade_session = self._trade_sessions.get(user_id)
        if (
            trade_session is not None
            and command is None
            and content
            and not self.wizard.active(user_id)
        ):
            token = content.strip().lstrip("#")
            if token.isdigit():
                if not self._allow_command(user_id, 0.5):
                    return
                async with self._lock_for(user_id):
                    self.store.upsert_user(user_id)
                    await self._handle_trade_choice(
                        user_id, group_id, message_id, int(token)
                    )
                return

        # 用户改为发送其他命令时，结束导入缓冲；完成/取消/重新导入由命令自己处理
        if pending_import is not None and first_cmd not in (
            "完成导入", "finishimport", "importdone",
            "取消登录", "cancellogin", "cancel-login",
            "导入登录", "importlogin", "login-import",
        ):
            self._cookie_imports.pop(user_id, None)

        max_len = IMPORT_MAX_CHARS if first_cmd in (
            "导入登录", "importlogin", "login-import",
        ) else 800
        if len(content) > max_len:
            await self._reply(
                user_id, group_id, message_id,
                f"输入过长，请控制在 {max_len} 字以内。",
            )
            return

        if command is None:
            if self.wizard.active(user_id) and content:
                if not self._allow_command(user_id, 0.5):
                    return
                async with self._lock_for(user_id):
                    await self._feed_wizard(user_id, group_id, message_id, content)
                return
            # 群里的纯 @ 或普通聊天不回复，避免和其他机器人互相触发
            return

        cmd = first_cmd
        interval = 3.0 if cmd in ("选项", "options", "查价", "查询", "价格", "price") else 1.0
        if not self._allow_command(user_id, interval):
            await self._reply(user_id, group_id, message_id, "操作太频繁，请稍后再试。")
            return

        async with self._lock_for(user_id):
            self.store.upsert_user(user_id)
            try:
                await self._handle_command(user_id, group_id, message_id, command)
            except Exception:  # noqa: BLE001 — 命令异常要有回复
                logger.exception("命令处理失败")
                await self._reply(user_id, group_id, message_id, "处理失败，请稍后再试。")

    def _permission_denied(self, qq: int, cmd: str) -> str | None:
        """后台权限开关拦截；帮助/取消等基础命令不受影响。"""
        features = self.permissions.features(qq)
        if cmd in SUBSCRIBE_COMMANDS and not features["allow_subscribe"]:
            return "当前账号没有创建订阅的权限，请联系管理员。"
        if cmd in LOGIN_COMMANDS and cmd not in LOGIN_SAFE_COMMANDS:
            if not self.permissions.login_allowed(qq):
                return "登录功能仅对白名单 QQ 开放，如需使用请联系管理员。"
        if cmd in LOGIN_COMMANDS and not features["allow_login"]:
            return "当前账号没有登录/账号管理权限，请联系管理员。"
        if cmd in TRADE_COMMANDS and not features["allow_trade"]:
            return "当前账号没有购买/交易权限，请联系管理员。"
        if cmd in PRICE_COMMANDS and not features["allow_price_query"]:
            return "当前账号没有价格查询权限，请联系管理员。"
        return None

    async def _handle_command(
        self, qq: int, group_id: int, message_id: int, command: str
    ) -> None:
        parts = command.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        denied = self._permission_denied(qq, cmd)
        if denied:
            await self._reply(qq, group_id, message_id, denied)
            return

        if cmd in ("取消", "cancel"):
            wizard_cancelled = self.wizard.cancel(qq)
            had_import = self._cookie_imports.pop(qq, None) is not None
            had_trade = self._trade_sessions.pop(qq, None) is not None
            if wizard_cancelled or had_import or had_trade:
                await self._reply(qq, group_id, message_id, "已取消当前会话。")
            else:
                await self._reply(qq, group_id, message_id, "当前没有进行中的会话。")
            return

        if cmd in SUBSCRIPTION_COMMANDS:
            is_friend = await asyncio.to_thread(self.friends.is_friend, qq)
            if is_friend is False:
                await self._reply(
                    qq, group_id, message_id,
                    "订阅功能仅对好友开放，请先添加机器人为好友后再使用。",
                )
                return

        if self.wizard.active(qq) and cmd not in ("帮助", "help", "?"):
            if cmd not in ("订阅", "sub", "subscribe"):
                self.wizard.cancel(qq)
            else:
                await self._reply(
                    qq, group_id, message_id, "正在向导中，请按提示输入，或发送 /取消 退出。"
                )
                return

        if cmd in ("帮助", "help", "?", "菜单"):
            guide = self._guide_image_segment()
            if guide is not None:
                try:
                    await self._reply_with_image(
                        qq, group_id, message_id, help_text(), guide
                    )
                    return
                except Exception:  # noqa: BLE001 — 图片发送失败退回文字
                    logger.warning("帮助图片发送失败，退回文字", exc_info=True)
            await self._reply(qq, group_id, message_id, help_text())
        elif cmd in ("订阅", "sub", "subscribe"):
            await self._subscribe(qq, group_id, message_id, args)
        elif cmd in ("我的订阅", "订阅列表", "列表", "list"):
            await self._list_subscriptions(qq, group_id, message_id)
        elif cmd in ("删除", "取消订阅", "del", "delete"):
            await self._toggle_delete(qq, group_id, message_id, args, delete=True)
        elif cmd in ("暂停", "pause"):
            await self._toggle_delete(qq, group_id, message_id, args, delete=False, enable=False)
        elif cmd in ("恢复", "resume"):
            await self._toggle_delete(qq, group_id, message_id, args, delete=False, enable=True)
        elif cmd in ("立即查", "check", "now"):
            await self._check_now(qq, group_id, message_id, args)
        elif cmd in ("设置", "set"):
            await self._settings(qq, group_id, message_id, args)
        elif cmd in BARK_COMMANDS:
            await self._bark(qq, group_id, message_id, args)
        elif cmd in FLIP_COMMANDS:
            await self._flip_settings(qq, group_id, message_id, args)
        elif cmd in ("选项", "options"):
            await self._options(qq, group_id, message_id, args)
        elif cmd in ("物品", "search"):
            await self._search(qq, group_id, message_id, args)
        elif cmd in ("查价", "查询", "价格", "price", "query"):
            await self._price_query(qq, group_id, message_id, args)
        elif cmd in ("自动登录", "自动", "steamauto", "auto"):
            await self._auto_login(qq, group_id, message_id)
        elif cmd in ("登录", "login"):
            await self._login(qq, group_id, message_id, args)
        elif cmd in ("浏览器登录", "browserlogin", "login-browser"):
            await self._browser_login(qq, group_id, message_id)
        elif cmd in ("导入登录", "importlogin", "login-import"):
            await self._import_login(qq, group_id, message_id, args)
        elif cmd in ("完成导入", "finishimport", "importdone"):
            await self._finish_import(qq, group_id, message_id)
        elif cmd in ("取消登录", "cancellogin", "cancel-login"):
            await self._cancel_login(qq, group_id, message_id)
        elif cmd in ("登出", "logout"):
            await self._logout(qq, group_id, message_id)
        elif cmd in ("登录状态", "账号", "account", "me"):
            await self._account_status(qq, group_id, message_id)
        elif cmd in ("检查登录", "checklogin", "token"):
            await self._check_login(qq, group_id, message_id)
        elif cmd in ("购买", "buy"):
            await self._trade(qq, group_id, message_id, args, mode="ask")
        elif cmd in ("求购", "采购", "prepare"):
            await self._trade(qq, group_id, message_id, args, mode="chat")
        elif cmd in ("组队", "休闲室", "lounge"):
            await self._trade(qq, group_id, message_id, args, mode="lounge")
        elif cmd in ("选择", "select"):
            choice = _parse_id(args)
            if choice is None:
                await self._reply(
                    qq, group_id, message_id,
                    "用法：/选择 <序号>；也可以直接回复图片里的序号。",
                )
            else:
                await self._handle_trade_choice(qq, group_id, message_id, choice)
        elif cmd in ("确认", "confirm"):
            await self._confirm_purchase(qq, group_id, message_id, args)
        elif cmd in ("采购记录", "订单", "orders"):
            await self._list_purchases(qq, group_id, message_id, args)
        else:
            await self._reply(
                qq, group_id, message_id,
                f"不认识的命令：{cmd}\n发送 /帮助 查看可用命令。",
            )

    # ── 命令实现 ────────────────────────────────────────────

    async def _subscribe(self, qq: int, group_id: int, message_id: int, args: str) -> None:
        if not args:
            await self._reply(qq, group_id, message_id, self.wizard.start(qq).text)
            return
        await self._create_from_dsl(qq, group_id, message_id, args)

    async def _create_from_dsl(
        self, qq: int, group_id: int, message_id: int, dsl: str
    ) -> None:
        if self.store.count_subscriptions(qq) >= self.permissions.max_subscriptions(qq):
            await self._reply(
                qq, group_id, message_id,
                f"最多只能创建 {self.permissions.max_subscriptions(qq)} 个订阅，"
                "请先 /删除 旧的订阅。",
            )
            return
        try:
            parsed = parse_dsl(dsl)
        except DslError as exc:
            await self._reply(qq, group_id, message_id, f"条件格式错误：{exc}")
            return

        spec = parsed.spec
        try:
            async with self._market_gate:
                item = await asyncio.to_thread(
                    self.market.resolve, spec.item_name or spec.item_id or "", spec.item_type
                )
            spec.validate_for_type(item.item_type)
        except Exception as exc:  # noqa: BLE001 — 解析/校验失败直接回复原因
            await self._reply(qq, group_id, message_id, f"创建失败：{exc}")
            return

        spec_dict = dict(parsed.spec_dict)
        spec_dict["item"] = {
            "name": item.name("cn"),
            "id": item.item_id,
            "type": item.item_type,
        }
        fingerprint = fingerprint_for(spec, item)
        existing = self.store.find_by_fingerprint(qq, fingerprint)
        if existing:
            await self._reply(
                qq, group_id, message_id,
                f"你已有相同条件的订阅 #{existing.id}，"
                "可用 /设置 修改间隔，或 /删除 后重建。",
            )
            return
        opts = parsed.options
        bark_sound = ""
        if opts.bark_sound:
            try:
                bark_sound = normalize_sound(opts.bark_sound)
            except BarkError as exc:
                await self._reply(qq, group_id, message_id, f"铃声无效：{exc}")
                return
        sub_id = self.store.add_subscription(
            qq,
            spec_dict,
            fingerprint,
            interval_sec=opts.interval_sec,
            max_push=opts.max_push,
            only_new=opts.only_new,
            quiet_hours=opts.quiet_hours,
            first_push=opts.first_push,
            bark_sound=bark_sound,
        )
        if bark_sound:
            self.store.update_options(sub_id, qq, {"bark_enabled": 1})
        sub = self.store.get_subscription(sub_id, qq)
        reply_text = (
            f"已创建订阅 #{sub_id}：{item.name('cn')}（{self.market.type_label(item)}）\n"
            f"条件：{describe_spec(spec)}\n"
            f"频率：每 {format_duration(opts.interval_sec)} · 最多 {opts.max_push} 条 · "
            f"{'仅新上架' if opts.only_new else '每轮全量'}"
        )
        if group_id:
            reply_text += "\n推送结果只会私聊发送，不会发到本群。"
        if bark_sound:
            reply_text += f"\n手机推送已开启，铃声 {bark_sound}。"
        if sub:
            reply_text += f"\n首次检查约 15 秒后开始，当前共 {self.store.count_subscriptions(qq)} 个订阅。"
        await self._reply(qq, group_id, message_id, reply_text)

    async def _list_subscriptions(self, qq: int, group_id: int, message_id: int) -> None:
        subs = self.store.list_subscriptions(qq)
        if not subs:
            await self._reply(qq, group_id, message_id, "还没有订阅。发送 /订阅 物品名 价格=100-5000 创建。")
            return
        lines = [describe_subscription(sub) for sub in subs]
        await self._reply(qq, group_id, message_id, "\n".join(lines))

    async def _toggle_delete(
        self,
        qq: int,
        group_id: int,
        message_id: int,
        args: str,
        *,
        delete: bool,
        enable: bool | None = None,
    ) -> None:
        sub_id = _parse_id(args)
        if sub_id is None:
            await self._reply(qq, group_id, message_id, "请提供订阅编号，例如 /暂停 3")
            return
        if delete:
            ok = self.store.delete_subscription(sub_id, qq)
            word = "删除"
        else:
            ok = self.store.set_enabled(sub_id, qq, bool(enable))
            if ok and enable:
                self.store.reschedule(sub_id, int(time.time()) + 5)
            word = "恢复" if enable else "暂停"
        await self._reply(
            qq, group_id, message_id,
            f"已{word}订阅 #{sub_id}" if ok else f"没有找到属于你的订阅 #{sub_id}",
        )

    async def _check_now(self, qq: int, group_id: int, message_id: int, args: str) -> None:
        sub_id = _parse_id(args)
        sub = self.store.get_subscription(sub_id, qq) if sub_id is not None else None
        if not sub:
            await self._reply(qq, group_id, message_id, "请提供属于你的订阅编号，例如 /立即查 3")
            return
        # 群聊触发的检查同样只走私聊，避免群里出现定时/检查刷屏
        target_group = 0 if group_id else group_id
        target_message = 0 if group_id else message_id
        await self._reply(qq, target_group, target_message, f"正在检查订阅 #{sub_id}…")
        matched, error = await self.scheduler.run_one(sub)
        if error:
            await self._reply(qq, target_group, target_message, f"检查失败：{error}")
        else:
            await self._reply(
                qq, target_group, target_message, f"检查完成：命中 {matched} 条。"
            )

    async def _settings(self, qq: int, group_id: int, message_id: int, args: str) -> None:
        sub_id_text, _, rest = args.partition(" ")
        sub_id = _parse_id(sub_id_text)
        if sub_id is None or not rest.strip():
            await self._reply(
                qq, group_id, message_id,
                "用法：/设置 3 间隔=10m 条数=20 仅新=0 静默=23:00-08:00 铃声=alarm",
            )
            return
        try:
            fields = parse_settings(rest)
        except DslError as exc:
            await self._reply(qq, group_id, message_id, str(exc))
            return
        if not fields:
            await self._reply(qq, group_id, message_id, "没有可修改的项。")
            return
        if "bark_sound" in fields:
            try:
                fields["bark_sound"] = normalize_sound(str(fields["bark_sound"]))
            except BarkError as exc:
                await self._reply(qq, group_id, message_id, f"铃声无效：{exc}")
                return
        if not self.store.update_options(sub_id, qq, fields):
            await self._reply(qq, group_id, message_id, f"没有找到属于你的订阅 #{sub_id}")
            return
        if "interval_sec" in fields:
            self.store.reschedule(sub_id, int(time.time()) + int(fields["interval_sec"]))
        await self._reply(qq, group_id, message_id, f"订阅 #{sub_id} 已更新。")

    async def _bark(self, qq: int, group_id: int, message_id: int, args: str) -> None:
        if group_id:
            await self._reply(
                qq, group_id, message_id,
                "手机推送涉及个人 Key，请私聊发送 /bark。",
            )
            return
        parts = args.split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        user = self.store.get_user_bark(qq)

        if not action or action in ("状态", "status"):
            if user is None:
                await self._reply(
                    qq, group_id, message_id,
                    "还没有配置手机推送。\n"
                    "私聊发送 /bark 设置 https://api.day.app/你的Key 完成绑定。\n"
                    "确认无误后请撤回包含 Key 的消息。",
                )
                return
            lines = [
                "Bark 手机推送",
                f"状态：{'开启' if user.enabled else '关闭'}",
                f"服务器：{user.server}",
                f"默认铃声：{normalize_sound(user.sound)}",
                f"最近推送：{_fmt_ts(user.last_sent_at) if user.last_sent_at else '-'}",
            ]
            if user.last_error:
                lines.append(f"最近错误：{user.last_error[:120]}")
            lines.append(
                "命令：/bark 设置 <Key或URL>、/bark 铃声 alarm、/bark 测试、"
                "/bark 开、/bark 关、/bark 订阅 3 开"
            )
            await self._reply(qq, group_id, message_id, "\n".join(lines))
            return

        if action in ("设置", "绑定", "set"):
            if not rest:
                await self._reply(
                    qq, group_id, message_id,
                    "用法：/bark 设置 https://api.day.app/你的Key（也可只发 Key）",
                )
                return
            try:
                server, key = parse_bark_target(rest)
                encrypted = protect_key(key)
            except BarkError as exc:
                await self._reply(qq, group_id, message_id, f"设置失败：{exc}")
                return
            self.store.save_user_bark(
                qq, server=server, device_key_enc=encrypted, enabled=True
            )
            await self._reply(
                qq, group_id, message_id,
                f"已绑定 Bark（{server}，Key 末四位 ****{key[-4:]}）。\n"
                "请撤回刚才包含 Key 的消息，避免泄露。\n"
                "可在订阅里用 铃声=alarm 指定铃声，或发送 /bark 测试。",
            )
            return

        if action in ("铃声", "sound"):
            if not rest:
                await self._reply(qq, group_id, message_id, "用法：/bark 铃声 alarm")
                return
            try:
                sound = normalize_sound(rest)
            except BarkError as exc:
                await self._reply(qq, group_id, message_id, f"铃声无效：{exc}")
                return
            if user is None:
                await self._reply(
                    qq, group_id, message_id, "请先 /bark 设置 <Key> 再改铃声。"
                )
                return
            self.store.set_user_bark_sound(qq, sound)
            await self._reply(
                qq, group_id, message_id,
                f"默认铃声已改为 {sound}；订阅未单独指定时会使用它。",
            )
            return

        if action in ("开", "开启", "on"):
            if user is None:
                await self._reply(qq, group_id, message_id, "请先 /bark 设置 <Key>。")
                return
            self.store.set_user_bark_enabled(qq, True)
            await self._reply(qq, group_id, message_id, "手机推送已开启。")
            return

        if action in ("关", "关闭", "off"):
            if user is None:
                await self._reply(qq, group_id, message_id, "还没有配置手机推送。")
                return
            self.store.set_user_bark_enabled(qq, False)
            await self._reply(qq, group_id, message_id, "手机推送已关闭。")
            return

        if action in ("订阅", "sub"):
            tokens = rest.split()
            sub_id = _parse_id(tokens[0]) if tokens else None
            flag = tokens[1].lower() if len(tokens) > 1 else ""
            if sub_id is None or flag not in ("开", "开启", "on", "1", "关", "关闭", "off", "0"):
                await self._reply(
                    qq, group_id, message_id,
                    "用法：/bark 订阅 3 开（或 /bark 订阅 3 关）",
                )
                return
            enabled = flag in ("开", "开启", "on", "1")
            if not self.store.update_options(sub_id, qq, {"bark_enabled": int(enabled)}):
                await self._reply(qq, group_id, message_id, f"没有找到属于你的订阅 #{sub_id}")
                return
            await self._reply(
                qq, group_id, message_id,
                f"订阅 #{sub_id} 的手机推送已{'开启' if enabled else '关闭'}。",
            )
            return

        if action in ("测试", "test"):
            if user is None:
                await self._reply(qq, group_id, message_id, "请先 /bark 设置 <Key>。")
                return
            try:
                key = unprotect_key(user.device_key_enc)
            except BarkError as exc:
                await self._reply(qq, group_id, message_id, str(exc))
                return
            payload = build_payload(
                sub_id=0,
                matched=1,
                new_count=1,
                command="/购买 怪物猎人 类型=超越武器 品质=400-400 价格=950-950",
                item_name="怪物猎人",
                price="950",
                sound=normalize_sound(user.sound),
                group=self.permissions.bark_group(),
                level=self.permissions.bark_level(),
                call=self.permissions.bark_call(),
            )
            ok, error = await asyncio.to_thread(post_bark, user.server, key, payload)
            if ok:
                await self._reply(qq, group_id, message_id, "测试推送已发送，请查看手机。")
            else:
                self.store.mark_bark_error(qq, error)
                logger.warning("Bark 测试推送失败 qq=%s：%s", qq, error)
                await self._reply(
                    qq, group_id, message_id, f"测试推送失败：{error}"
                )
            return

        if action in ("删除", "清空", "clear"):
            if not self.store.delete_user_bark(qq):
                await self._reply(qq, group_id, message_id, "还没有配置手机推送。")
                return
            await self._reply(qq, group_id, message_id, "已删除 Bark 配置。")
            return

        await self._reply(
            qq, group_id, message_id,
            "用法：\n"
            "/bark                      查看状态\n"
            "/bark 设置 <Key或URL>       绑定你的 Bark\n"
            "/bark 铃声 alarm            改默认铃声\n"
            "/bark 测试                  发一条测试推送\n"
            "/bark 开 | /bark 关         总开关\n"
            "/bark 订阅 3 开              开启某条订阅的手机推送\n"
            "/bark 删除                  清空配置",
        )

    def _flip_view(self, sub: Subscription, overrides: dict[str, Any]) -> str:
        fuzzy = build_fuzzy_spec(sub.spec, overrides)
        text = overrides_to_text(overrides) or "（无，全部使用默认）"
        lines = [
            f"订阅 #{sub.id} 的模糊过滤（倒卖利润）",
            f"覆盖项：{text}",
        ]
        try:
            from market_v2.filters import FilterSpec

            lines.append(f"生效条件：{describe_spec(FilterSpec.from_dict(fuzzy))}")
        except Exception:  # noqa: BLE001 — 仅展示用，失败不阻塞
            pass
        lines.append("仅对已开启手机推送的订阅生效；留空的项按默认（品质照抄、辅助忽略、能力取一条）")
        lines.append("用法：/模糊 3 价格=100-8000 能力2=150-150；/模糊 3 清除")
        return "\n".join(lines)

    async def _flip_settings(
        self, qq: int, group_id: int, message_id: int, args: str
    ) -> None:
        sub_id_text, _, rest = args.partition(" ")
        sub_id = _parse_id(sub_id_text)
        if sub_id is None:
            await self._reply(
                qq, group_id, message_id,
                "用法：/模糊 3 [价格=100-8000 能力2=150-150]；不带参数查看，/模糊 3 清除 恢复默认",
            )
            return
        sub = self.store.get_subscription(sub_id, qq)
        if sub is None:
            await self._reply(qq, group_id, message_id, f"没有找到属于你的订阅 #{sub_id}")
            return

        text = rest.strip()
        if text in ("清除", "清空", "clear"):
            self.store.update_options(sub_id, qq, {"flip_overrides_json": ""})
            refreshed = self.store.get_subscription(sub_id, qq) or sub
            await self._reply(
                qq, group_id, message_id,
                "已恢复默认。\n" + self._flip_view(refreshed, {}),
            )
            return
        if not text:
            await self._reply(
                qq, group_id, message_id,
                self._flip_view(sub, sub.flip_overrides or {}),
            )
            return
        try:
            overrides = parse_flip_overrides(text)
        except DslError as exc:
            await self._reply(qq, group_id, message_id, f"模糊条件格式错误：{exc}")
            return
        self.store.update_options(
            sub_id, qq,
            {"flip_overrides_json": json.dumps(overrides, ensure_ascii=False) if overrides else ""},
        )
        refreshed = self.store.get_subscription(sub_id, qq) or sub
        await self._reply(
            qq, group_id, message_id,
            "已保存。\n" + self._flip_view(refreshed, overrides),
        )

    async def _options(self, qq: int, group_id: int, message_id: int, args: str) -> None:
        if not args:
            await self._reply(qq, group_id, message_id, "用法：/选项 怪物猎人")
            return
        try:
            async with self._market_gate:
                item = await asyncio.to_thread(self.market.resolve, args, None)
                lists = await asyncio.to_thread(self.market.options, item)
        except Exception as exc:  # noqa: BLE001
            await self._reply(qq, group_id, message_id, f"查询失败：{exc}")
            return

        lines = [f"{item.name('cn')}（{self.market.type_label(item)}，ID {item.item_id}）"]
        if item.item_type == "transcendent":
            lines.append("可用：品质(1-400)、辅助(≤2)、能力1~3(0-150)")
            lines.extend(_dump_options("辅助能力（名称或 ID）", lists.subperks))
        elif item.item_type in ("ancestor", "descendant"):
            lines.append("可用：品级(Tier1~4)、增益(≤3，名称[:品级])、减益(1)")
            lines.extend(_dump_options("增益属性", lists.advantage))
            lines.extend(_dump_options("减益属性", lists.penalty))
        else:
            lines.append("触发器只支持：价格、品级、排序")
        await self._reply(qq, group_id, message_id, "\n".join(lines))

    async def _search(self, qq: int, group_id: int, message_id: int, args: str) -> None:
        if not args:
            await self._reply(qq, group_id, message_id, "用法：/物品 怪物")
            return
        try:
            async with self._market_gate:
                items = await asyncio.to_thread(self.market.search_items, args, 10)
        except Exception as exc:  # noqa: BLE001
            await self._reply(qq, group_id, message_id, f"搜索失败：{exc}")
            return
        if not items:
            await self._reply(qq, group_id, message_id, f"没有找到包含 {args!r} 的物品。")
            return
        lines = [
            f"{it.name('cn')}（{self.market.type_label(it)}，ID {it.item_id}）"
            for it in items
        ]
        await self._reply(qq, group_id, message_id, "\n".join(lines))

    async def _price_query(
        self, qq: int, group_id: int, message_id: int, args: str
    ) -> None:
        """价格查询：只给物品名返回模板；带参数时查最低价前 3 条（不落库）。"""
        if not args:
            await self._reply(qq, group_id, message_id, query_template_overview())
            return

        # 只有物品名：返回该物品类型的查询模板
        if "=" not in args:
            try:
                async with self._market_gate:
                    item = await asyncio.to_thread(self.market.resolve, args, None)
            except Exception as exc:  # noqa: BLE001
                await self._reply(
                    qq, group_id, message_id,
                    f"查询失败：{exc}\n可先 /物品 关键词 查找准确名称。",
                )
                return
            await self._reply(
                qq, group_id, message_id,
                query_template(item.item_type, item.name("cn")),
            )
            return

        try:
            parsed = parse_dsl(args)
        except DslError as exc:
            await self._reply(
                qq, group_id, message_id,
                f"条件格式错误：{exc}\n可发送 /查价 物品名 查看该类型模板。",
            )
            return

        spec = parsed.spec
        # 本功能固定按最低价取前 3 条
        spec.sort = "price_asc"

        try:
            async with self._market_gate:
                item = await asyncio.to_thread(
                    self.market.resolve,
                    spec.item_name or spec.item_id or "",
                    spec.item_type,
                )
        except Exception as exc:  # noqa: BLE001
            await self._reply(
                qq, group_id, message_id,
                f"查询失败：{exc}\n可先 /物品 关键词 查找准确名称。",
            )
            return

        try:
            spec.validate_for_type(item.item_type)
        except Exception as exc:  # noqa: BLE001
            await self._reply(
                qq, group_id, message_id,
                f"条件不适用于 {item.name('cn')}：{exc}\n\n"
                + query_template(item.item_type, item.name("cn")),
            )
            return

        try:
            async with self._market_gate:
                result = await asyncio.to_thread(self.market.fetch, spec, max_pages=1)
        except Exception as exc:  # noqa: BLE001
            await self._reply(qq, group_id, message_id, f"查询失败：{exc}")
            return

        raws = [raw for raw in result.items if str(raw.get("n8Id") or "")]
        raws.sort(key=_sale_price)
        top = raws[:3]
        if not top:
            await self._reply(qq, group_id, message_id, "当前没有符合条件的在售商品。")
            return

        lines = [
            f"【{item.name('cn')}】{self.market.type_label(item)} · 最低价前 {len(top)} 条"
        ]
        condition = describe_spec(spec, include_sort=False)
        if condition != "无附加筛选":
            lines.append(f"筛选：{condition}")
        for index, raw in enumerate(top, start=1):
            lines.append(f"{index}. {format_item_summary(raw, item.item_type)}")
        if len(raws) > len(top):
            lines.append(f"本次取到 {len(raws)} 条，仅列最低 3 条。")
        lines.append("结果实时来自市场接口，未保存到本地。")

        if self.settings.image_enabled:
            try:
                await self._send_query_image(
                    qq, group_id, message_id, item, spec,
                    dict(parsed.spec_dict), result, top, lines[0],
                )
                return
            except Exception as exc:  # noqa: BLE001 — 图片失败退回文字
                logger.warning("查询结果图片渲染/发送失败，退回文字：%s", exc)

        await self._reply(qq, group_id, message_id, "\n".join(lines))

    async def _send_query_image(
        self,
        qq: int,
        group_id: int,
        message_id: int,
        item: Any,
        spec: Any,
        spec_dict: dict[str, Any],
        result: FetchResult,
        top: list[dict[str, Any]],
        caption: str,
    ) -> None:
        """把最低价前 3 条渲染成订阅同款商品卡。"""
        spec_dict["item"] = {
            "name": item.name("cn"),
            "id": item.item_id,
            "type": item.item_type,
        }
        sub = Subscription(
            id=0,
            qq=qq,
            spec=spec_dict,
            fingerprint=fingerprint_for(spec, item),
            interval_sec=0,
            next_run_at=0,
            enabled=True,
            only_new=False,
            max_push=len(top),
            quiet_hours="",
            first_push=True,
            fail_count=0,
            last_run_at=None,
            last_count=None,
            last_hash=None,
            created_at=0,
        )
        snapshot = FetchResult(
            item=item,
            resolved=result.resolved,
            items=top,
            requests=result.requests,
            total_count=len(top),
            complete=result.complete,
        )
        path = await asyncio.to_thread(
            render_card,
            snapshot,
            sub,
            [],
            style=self.settings.image_style,
            max_rows=len(top),
            numbering=True,
        )
        data = await asyncio.to_thread(path.read_bytes)
        await self._reply_with_image(
            qq,
            group_id,
            message_id,
            caption,
            image("base64://" + base64.b64encode(data).decode("ascii")),
        )
        logger.info(
            "查询结果图片已发送：%s %s 条（%s，%sKB）",
            item.name("cn"),
            len(top),
            self.settings.image_style,
            len(data) // 1024,
        )

    # ── 扫码登录 ────────────────────────────────────────────

    async def _login(
        self, qq: int, group_id: int, message_id: int, args: str = ""
    ) -> None:
        sub = (args or "").strip().lower()
        if sub in ("浏览器", "browser", "手动", "manual"):
            await self._browser_login(qq, group_id, message_id)
            return
        if sub in ("自动", "auto", "自动登录", "steamauto"):
            await self._auto_login(qq, group_id, message_id)
            return
        if sub in ("导入", "import", "cookie", "cookie导入"):
            await self._import_login(qq, group_id, message_id, "")
            return
        if sub not in ("", "扫码", "二维码", "qr"):
            await self._reply(
                qq, group_id, message_id,
                "登录方式：/登录（Steam 扫码）、/登录 自动、/浏览器登录、/导入登录。",
            )
            return
        if group_id:
            await self._reply(
                qq, group_id, message_id,
                "扫码登录涉及账号凭据，请在私聊中发送 /登录。",
            )
            return
        with self._login_lock:
            if qq in self._login_sessions:
                await self._reply(
                    qq, group_id, message_id,
                    "已有登录会话在等待扫码，请先在私聊完成，或发送 /取消登录。",
                )
                return
        if not self._login_slots.acquire(blocking=False):
            await self._reply(
                qq, group_id, message_id,
                "当前登录人数较多，请稍后再试。",
            )
            return

        session = QRLoginSession(
            on_qr=lambda png, refreshed: self._send_qr(qq, png, refreshed),
            proxy=self._login_proxy(),
            proxy_bypass=TFD_DIRECT_HOSTS if self.settings.login_bypass else None,
            timeout_s=self.settings.login_timeout,
            debug=self.settings.login_debug,
        )
        with self._login_lock:
            self._login_sessions[qq] = session
        try:
            await self._reply(
                qq, group_id, message_id,
                "正在启动 Steam 扫码登录，二维码会随即私聊发给你…",
            )
            threading.Thread(
                target=self._login_worker,
                args=(qq, session, "steam_qr"),
                name=f"login-{qq}",
                daemon=True,
            ).start()
        except Exception:
            with self._login_lock:
                self._login_sessions.pop(qq, None)
            self._login_slots.release()
            raise

    def _login_worker(self, qq: int, session: QRLoginSession, method: str) -> None:
        label = {
            "manual_browser": "浏览器登录",
            "steam_auto": "Steam 自动登录",
        }.get(method, "扫码登录")
        try:
            result = session.run()
            if not result.has_core:
                raise LoginError(f"{label}未完成：缺少核心登录 Cookie。")

            # 先校验，避免一次失败的重新登录覆盖掉原来的有效 token
            try:
                check = check_session(
                    result.cookies,
                    user_agent=result.user_agent,
                )
            except Exception as exc:  # noqa: BLE001 — 校验失败不影响已有登录态
                existing = self.store.get_token(qq)
                if existing and existing.status == "valid":
                    self._send_private(
                        qq,
                        f"{label}已完成，但交易校验请求失败；已保留你原来的登录态。"
                        "可稍后发送 /检查登录 重试。",
                    )
                else:
                    self.store.save_token(
                        qq,
                        result.cookies,
                        user_agent=result.user_agent,
                        platform=result.platform,
                        method=method,
                        status="unknown",
                        error=f"校验请求失败：{exc}",
                    )
                    self._send_private(
                        qq,
                        f"{label}已完成并保存 Cookie，但交易校验请求失败。"
                        "可稍后发送 /检查登录 重试。",
                    )
                return

            if session_usable(check):
                self.store.save_token(
                    qq,
                    result.cookies,
                    user_agent=result.user_agent,
                    platform=result.platform,
                    method=method,
                    status="valid",
                )
                self._send_private(
                    qq,
                    f"{label}成功，已保存你的 Nexon 登录态。\n"
                    "现在可以用 /购买、/求购、/组队（默认选当前最便宜的一条）。",
                )
                return

            reason = describe_code(check.code, check.message)
            existing = self.store.get_token(qq)
            if existing and existing.status == "valid":
                self._send_private(
                    qq,
                    f"本次{label}未通过校验：{reason}\n已保留你原来的登录态。",
                )
            else:
                self.store.save_token(
                    qq,
                    result.cookies,
                    user_agent=result.user_agent,
                    platform=result.platform,
                    method=method,
                    status="invalid",
                    error=reason,
                )
                self._send_private(
                    qq,
                    f"{label} Cookie 已保存，但交易会话校验未通过：{reason}\n建议重新登录。",
                )
        except LoginCancelled as exc:
            self._send_private(qq, str(exc))
        except LoginTimeout as exc:
            self._send_private(qq, str(exc))
        except LoginError as exc:
            self._send_private(qq, f"{label}失败：{exc}")
        except Exception:  # noqa: BLE001 — 线程异常必须有回复
            logger.exception("%s异常", label)
            self._send_private(qq, f"{label}失败：内部错误，请稍后再试。")
        finally:
            with self._login_lock:
                self._login_sessions.pop(qq, None)
            self._login_slots.release()

    def _send_qr(self, qq: int, png: bytes, refreshed: bool) -> None:
        caption = "二维码已刷新，请使用最新的二维码。" if refreshed else "请用 Steam 手机 App 扫描二维码登录。"
        segments = [
            text(
                caption
                + "\n二维码仅发给你本人，请勿转发；扫码后请在手机上确认。"
                "\n二维码过期可重新发送 /登录。"
            ),
            image("base64://" + base64.b64encode(png).decode("ascii")),
        ]
        self.client.send_private_msg(qq, segments)

    def _send_private(self, qq: int, content: str) -> None:
        try:
            self.client.send_private_msg(qq, [text(content)])
        except Exception:  # noqa: BLE001 — 登录线程不能因发送失败崩溃
            logger.exception("发送登录结果失败")

    # ── Steam 自动登录 ──────────────────────────────────────

    async def _auto_login(
        self, qq: int, group_id: int, message_id: int
    ) -> None:
        if group_id:
            await self._reply(
                qq, group_id, message_id,
                "Steam 自动登录涉及账号凭据，请在私聊中发送 /登录 自动。",
            )
            return
        if not self.permissions.steam_auto_login_enabled():
            await self._reply(
                qq, group_id, message_id,
                "Steam 自动登录已在后台关闭，请联系管理员。",
            )
            return
        allowed = self.permissions.steam_auto_allowed_qq()
        if qq not in allowed:
            await self._reply(
                qq, group_id, message_id,
                "当前 QQ 不在 Steam 自动登录白名单中，请联系管理员。",
            )
            return
        try:
            credentials = default_vault.load()
        except VaultError as exc:
            await self._reply(
                qq, group_id, message_id,
                f"Steam 凭据读取失败：{exc}",
            )
            return
        if credentials is None:
            await self._reply(
                qq, group_id, message_id,
                "还没有配置 Steam 自动登录。请在后台「系统设置 → Steam 自动登录」录入账号密码。",
            )
            return

        with self._login_lock:
            if qq in self._login_sessions:
                await self._reply(
                    qq, group_id, message_id,
                    "已有登录会话在进行，请先完成或发送 /取消登录。",
                )
                return
        if not self._login_slots.acquire(blocking=False):
            await self._reply(
                qq, group_id, message_id,
                "当前登录任务已满，请稍后再试。",
            )
            return

        guard_timeout = max(600, self.permissions.steam_auto_guard_timeout())
        session = QRLoginSession(
            on_ready=lambda: self._send_private(
                qq, "浏览器已打开，正在自动填写 Steam 账号密码。"
            ),
            on_notice=lambda message: self._send_private(qq, message),
            proxy=self._login_proxy(),
            proxy_bypass=TFD_DIRECT_HOSTS if self.settings.login_bypass else None,
            timeout_s=guard_timeout,
            headless=False,
            manual=False,
            debug=self.settings.login_debug,
            steam_credentials=(credentials.username, credentials.password),
        )
        with self._login_lock:
            self._login_sessions[qq] = session
        try:
            await self._reply(
                qq,
                group_id,
                message_id,
                "正在启动 Steam 自动登录。浏览器会保持打开；"
                f"若出现邮箱/手机验证码，请在本机浏览器完成，最长等待 {guard_timeout // 60} 分钟。",
            )
        except Exception:
            with self._login_lock:
                self._login_sessions.pop(qq, None)
            self._login_slots.release()
            raise
        thread = threading.Thread(
            target=self._login_worker,
            args=(qq, session, "steam_auto"),
            name=f"login-auto-{qq}",
            daemon=True,
        )
        thread.start()

    # ── 有头浏览器登录 ──────────────────────────────────────

    async def _browser_login(
        self, qq: int, group_id: int, message_id: int
    ) -> None:
        if group_id:
            await self._reply(
                qq, group_id, message_id,
                "浏览器登录涉及账号凭据，请在私聊中发送 /浏览器登录。",
            )
            return
        with self._login_lock:
            if qq in self._login_sessions:
                await self._reply(
                    qq, group_id, message_id,
                    "已有登录会话在进行中，请先完成或发送 /取消登录。",
                )
                return
        if not self._login_slots.acquire(blocking=False):
            await self._reply(
                qq, group_id, message_id,
                "当前登录人数较多，请稍后再试。",
            )
            return

        session = ManualLoginSession(
            on_ready=lambda: self._send_private(
                qq,
                "已在机器人所在电脑打开 Nexon 登录选择页。\n"
                "请选择 Steam / Nexon ID / Xbox / PSN 完成登录；"
                "登录成功后会自动保存。看不到窗口请改用 /导入登录。",
            ),
            proxy=self._login_proxy(),
            proxy_bypass=TFD_DIRECT_HOSTS if self.settings.login_bypass else None,
            timeout_s=max(600, self.settings.login_timeout),
            debug=self.settings.login_debug,
        )
        with self._login_lock:
            self._login_sessions[qq] = session
        try:
            await self._reply(
                qq, group_id, message_id,
                "正在打开有头浏览器，请稍候…",
            )
            threading.Thread(
                target=self._login_worker,
                args=(qq, session, "manual_browser"),
                name=f"login-manual-{qq}",
                daemon=True,
            ).start()
        except Exception:
            with self._login_lock:
                self._login_sessions.pop(qq, None)
            self._login_slots.release()
            raise

    # ── Cookie 文本导入 ─────────────────────────────────────

    async def _import_login(
        self, qq: int, group_id: int, message_id: int, args: str
    ) -> None:
        if group_id:
            await self._reply(
                qq, group_id, message_id,
                "Cookie 属于账号凭据，请在私聊中导入。",
            )
            return
        with self._login_lock:
            if qq in self._login_sessions:
                await self._reply(
                    qq, group_id, message_id,
                    "已有登录会话在进行中，请先完成或发送 /取消登录。",
                )
                return
        if args.strip():
            await self._import_cookie_text(qq, group_id, message_id, args)
            return
        self._cookie_imports[qq] = ImportSession(
            parts=[],
            updated_at=time.time(),
        )
        await self._reply(
            qq, group_id, message_id,
            "请把以下任意一种内容私聊发给我：\n"
            "1) Cookie 字符串：_if_game_auth=...; _ifwt_guest_2392=...; A2SK=...\n"
            "2) cookies.json / auth_cookies.json\n"
            "3) Playwright storage_state.json（只取其中 Cookie）\n"
            "内容较长可分多条发送，完成后发送 /完成导入；5 分钟内有效。\n"
            "导入成功后请撤回包含 Cookie 的消息。",
        )

    async def _finish_import(
        self, qq: int, group_id: int, message_id: int
    ) -> None:
        session = self._cookie_imports.pop(qq, None)
        if not session or not session.text.strip():
            await self._reply(
                qq, group_id, message_id,
                "没有收到 Cookie 内容。请先发送 /导入登录，再粘贴 Cookie。",
            )
            return
        await self._import_cookie_text(qq, group_id, message_id, session.text)

    async def _feed_cookie_import(
        self, qq: int, group_id: int, message_id: int, content: str
    ) -> None:
        session = self._cookie_imports.get(qq)
        if not session:
            return
        session.parts.append(content)
        session.updated_at = time.time()
        if session.size > IMPORT_MAX_CHARS:
            self._cookie_imports.pop(qq, None)
            await self._reply(
                qq, group_id, message_id,
                f"内容超过 {IMPORT_MAX_CHARS} 字符，已取消导入。"
                "请只保留关键 Cookie 或分多次导入。",
            )
            return

        # 一次发完的短 JSON / Cookie 字符串可以直接导入
        try:
            parsed = parse_cookie_payload(session.text)
        except CookieImportError as exc:
            if not session.notified:
                session.notified = True
                await self._reply(
                    qq, group_id, message_id,
                    f"已收到 {session.size} 字符，继续粘贴；完成后发送 /完成导入。\n"
                    f"（当前解析：{exc}）",
                )
            return
        self._cookie_imports.pop(qq, None)
        await self._import_parsed(qq, group_id, message_id, parsed)

    async def _import_cookie_text(
        self, qq: int, group_id: int, message_id: int, text: str
    ) -> None:
        try:
            parsed = parse_cookie_payload(text)
        except CookieImportError as exc:
            await self._reply(qq, group_id, message_id, f"导入失败：{exc}")
            return
        await self._import_parsed(qq, group_id, message_id, parsed)

    async def _import_parsed(
        self,
        qq: int,
        group_id: int,
        message_id: int,
        parsed,  # noqa: ANN001 — ParsedCookiePayload
    ) -> None:
        await self._reply(qq, group_id, message_id, "正在校验导入的 Cookie…")
        try:
            check = await asyncio.to_thread(
                check_session,
                parsed.cookies,
                user_agent=USER_AGENT,
            )
        except Exception as exc:  # noqa: BLE001
            existing = self.store.get_token(qq)
            if existing and existing.status == "valid":
                await self._reply(
                    qq, group_id, message_id,
                    "Cookie 已解析，但校验请求失败；已保留你原来的登录态。",
                )
            else:
                self.store.save_token(
                    qq,
                    parsed.cookies,
                    user_agent=USER_AGENT,
                    platform=PLATFORM,
                    method="cookie_import",
                    status="unknown",
                    error=f"校验请求失败：{exc}",
                )
                await self._reply(
                    qq, group_id, message_id,
                    "Cookie 已解析并保存，但校验请求失败。可稍后发送 /检查登录。",
                )
            return

        if session_usable(check):
            self.store.save_token(
                qq,
                parsed.cookies,
                user_agent=USER_AGENT,
                platform=PLATFORM,
                method="cookie_import",
                status="valid",
            )
            await self._reply(
                qq, group_id, message_id,
                f"导入成功（{_source_label(parsed.source)}），登录态有效。\n"
                "请撤回包含 Cookie 的消息。",
            )
            return

        reason = describe_code(check.code, check.message)
        existing = self.store.get_token(qq)
        if existing and existing.status == "valid":
            await self._reply(
                qq, group_id, message_id,
                f"导入的 Cookie 已失效：{reason}\n已保留你原来的登录态。",
            )
            return
        await self._reply(
            qq, group_id, message_id,
            f"导入的 Cookie 已失效：{reason}\n请重新导出后再试。",
        )

    async def _cancel_login(self, qq: int, group_id: int, message_id: int) -> None:
        with self._login_lock:
            session = self._login_sessions.get(qq)
        had_import = self._cookie_imports.pop(qq, None) is not None
        if not session and not had_import:
            await self._reply(qq, group_id, message_id, "当前没有等待中的登录会话。")
            return
        if session:
            session.cancel()
        await self._reply(qq, group_id, message_id, "已取消当前登录/导入。")

    async def _logout(self, qq: int, group_id: int, message_id: int) -> None:
        with self._login_lock:
            session = self._login_sessions.pop(qq, None)
        if session:
            session.cancel()
        self._cookie_imports.pop(qq, None)
        removed = self.store.delete_token(qq)
        await self._reply(
            qq, group_id, message_id,
            "已删除登录态。" if removed else "当前本来就没有登录态。",
        )

    async def _account_status(self, qq: int, group_id: int, message_id: int) -> None:
        token = self.store.get_token(qq)
        if not token:
            await self._reply(
                qq, group_id, message_id,
                "还没有登录。请私聊发送 /登录，用 Steam 手机 App 扫码绑定账号。",
            )
            return
        lines = [
            "已绑定 Nexon 登录态",
            f"登录方式：{_method_label(token.method)}",
            f"状态：{_status_label(token.status)}",
            f"更新时间：{_fmt_ts(token.updated_at)}",
        ]
        if token.last_checked_at:
            lines.append(f"最近校验：{_fmt_ts(token.last_checked_at)}")
        if token.last_error:
            lines.append(f"最近错误：{token.last_error[:160]}")
        lines.append("发送 /检查登录 可立即校验；/登出 删除登录态。")
        await self._reply(qq, group_id, message_id, "\n".join(lines))

    async def _check_login(self, qq: int, group_id: int, message_id: int) -> None:
        token = self.store.get_token(qq)
        if not token:
            await self._reply(
                qq, group_id, message_id,
                "还没有登录。请先私聊发送 /登录。",
            )
            return
        await self._reply(qq, group_id, message_id, "正在校验登录态…")
        try:
            result = await asyncio.to_thread(
                check_session,
                token.cookies,
                user_agent=token.user_agent,
            )
        except Exception as exc:  # noqa: BLE001
            await self._reply(qq, group_id, message_id, f"校验请求失败：{exc}")
            return
        if session_usable(result):
            self.store.update_token_status(qq, "valid")
            await self._reply(
                qq, group_id, message_id,
                f"登录态有效（探测返回 {result.code}）。",
            )
        else:
            reason = describe_code(result.code, result.message)
            self.store.update_token_status(qq, "invalid", reason)
            await self._reply(
                qq, group_id, message_id,
                f"登录态已失效：{reason}\n请重新发送 /登录。",
            )

    # ── 购买 / 求购 / 组队 ──────────────────────────────────

    async def _trade(
        self,
        qq: int,
        group_id: int,
        message_id: int,
        args: str,
        *,
        mode: str,
    ) -> None:
        usage = {
            "ask": "/购买 怪物猎人 价格=100-5000 品质=390-400",
            "chat": "/求购 怪物猎人 价格=100-5000",
            "lounge": "/休闲室 怪物猎人 价格=100-5000",
        }[mode]
        if not args:
            await self._reply(qq, group_id, message_id, f"用法：{usage}")
            return

        token = self.store.get_token(qq)
        if not token:
            await self._reply(
                qq, group_id, message_id,
                "还没有登录。请私聊发送 /登录，用 Steam 扫码绑定账号。",
            )
            return
        if token.status == "invalid":
            await self._reply(
                qq, group_id, message_id,
                "登录态已失效，请重新发送 /登录。",
            )
            return

        try:
            parsed = parse_dsl(args)
        except DslError as exc:
            await self._reply(qq, group_id, message_id, f"条件格式错误：{exc}")
            return
        spec = parsed.spec
        # 浏览默认按价格从低到高；显式写排序时尊重用户
        if not _has_sort(args):
            spec.sort = "price_asc"

        limit = (
            BROWSE_LIMIT_OFFICIAL
            if self.settings.image_style == "official"
            else BROWSE_LIMIT
        )
        try:
            async with self._market_gate:
                item = await asyncio.to_thread(
                    self.market.resolve,
                    spec.item_name or spec.item_id or "",
                    spec.item_type,
                )
            spec.validate_for_type(item.item_type)
            async with self._market_gate:
                result = await asyncio.to_thread(self.market.fetch, spec, max_pages=1)
        except Exception as exc:  # noqa: BLE001
            await self._reply(qq, group_id, message_id, f"查询失败：{exc}")
            return

        items = [
            raw for raw in result.items if str(raw.get("n8Id") or "")
        ][:limit]
        if not items:
            await self._reply(qq, group_id, message_id, "当前没有符合条件的在售商品。")
            return

        session = TradeBrowseSession(
            qq=qq,
            mode=mode,
            item=item,
            spec_dict=dict(parsed.spec_dict),
            fingerprint=fingerprint_for(spec, item),
            items=items,
            expires_at=time.time() + TRADE_SESSION_TTL,
        )
        self._trade_sessions[qq] = session
        caption = self._browse_caption(session, spec, len(result.items))

        sent = False
        if self.settings.image_enabled:
            try:
                sub = Subscription(
                    id=0,
                    qq=qq,
                    spec=dict(parsed.spec_dict),
                    fingerprint=session.fingerprint,
                    interval_sec=0,
                    next_run_at=0,
                    enabled=True,
                    only_new=False,
                    max_push=len(items),
                    quiet_hours="",
                    first_push=True,
                    fail_count=0,
                    last_run_at=None,
                    last_count=None,
                    last_hash=None,
                    created_at=0,
                )
                snapshot = FetchResult(
                    item=item,
                    resolved=result.resolved,
                    items=list(result.items),
                    requests=result.requests,
                )
                path = await asyncio.to_thread(
                    render_card,
                    snapshot,
                    sub,
                    [],
                    style=self.settings.image_style,
                    max_rows=len(items),
                    numbering=True,
                )
                data = await asyncio.to_thread(path.read_bytes)
                header, _, rest = caption.partition("\n")
                segments: list[dict[str, Any]] = [text(header)]
                segments.append(
                    image("base64://" + base64.b64encode(data).decode("ascii"))
                )
                if group_id:
                    if message_id:
                        segments.insert(0, reply(message_id))
                    await asyncio.to_thread(
                        self.client.send_group_msg, group_id, segments
                    )
                else:
                    await asyncio.to_thread(
                        self.client.send_private_msg, qq, segments
                    )
                sent = True
                if rest:
                    await self._reply(qq, group_id, message_id, rest)
            except Exception as exc:  # noqa: BLE001 — 图片失败退回文字
                logger.warning("购买列表图片渲染/发送失败，退回文字：%s", exc)

        if not sent:
            await self._reply(qq, group_id, message_id, caption)

    def _browse_caption(
        self,
        session: TradeBrowseSession,
        spec: Any,
        total: int,
    ) -> str:
        item = session.item
        sort_label = {
            "price_asc": "价格从低到高",
            "price_desc": "价格从高到低",
            "latest": "最新上架",
        }.get(spec.sort, spec.sort)
        lines = [
            f"【{item.name('cn')}】{self.market.type_label(item)} · "
            f"共 {total} 条，显示前 {len(session.items)} 条（{sort_label}）",
            f"筛选：{_spec_text(spec)}",
        ]
        for index, raw in enumerate(session.items, start=1):
            lines.append(_browse_line(index, raw, item.item_type))
        if total > len(session.items):
            lines.append(f"… 仅显示前 {len(session.items)} 条，可加价格/品质等条件缩小范围")
        if session.mode == "ask":
            lines.append("回复序号选择商品；之后可选：1 私聊购买 / 2 休闲室购买。")
        elif session.mode == "chat":
            lines.append("回复序号选择商品；之后走私聊购买（/chat → /chat/buy）。")
        else:
            lines.append("回复序号选择商品；之后走休闲室购买（/lounge → /lounge/buy）。")
        lines.append("发送 /取消 退出。")
        return "\n".join(lines)

    async def _handle_trade_choice(
        self, qq: int, group_id: int, message_id: int, choice: int
    ) -> None:
        session = self._trade_sessions.get(qq)
        if not session:
            await self._reply(
                qq, group_id, message_id,
                "没有进行中的购买选择。请先发送 /购买 物品 条件。",
            )
            return
        if session.expires_at <= time.time():
            self._trade_sessions.pop(qq, None)
            await self._reply(
                qq, group_id, message_id,
                "选择已过期，请重新发送 /购买 ...。",
            )
            return
        session.expires_at = time.time() + TRADE_SESSION_TTL

        if session.stage == "select":
            if not 1 <= choice <= len(session.items):
                await self._reply(
                    qq, group_id, message_id,
                    f"序号无效，请输入 1~{len(session.items)}。",
                )
                return
            session.selected = session.items[choice - 1]
            if session.mode == "ask":
                session.stage = "method"
                await self._show_method_menu(qq, group_id, message_id, session, choice)
            else:
                await self._prepare_trade(
                    qq, group_id, message_id, session, session.mode
                )
            return

        if session.stage == "method":
            if choice not in (1, 2):
                await self._reply(qq, group_id, message_id, "请输入 1 或 2。")
                return
            method = "chat" if choice == 1 else "lounge"
            session.mode = method
            await self._prepare_trade(qq, group_id, message_id, session, method)
            return

        # stage == confirm：1 确认执行第二步，0 取消
        if choice == 1:
            await self._execute_confirmation(qq, group_id, message_id, session)
        elif choice == 0:
            await self._cancel_trade(qq, group_id, message_id, session)
        else:
            await self._reply(
                qq, group_id, message_id,
                "回复 1 确认执行，回复 0 取消；发送 /取消 退出。",
            )

    async def _show_method_menu(
        self,
        qq: int,
        group_id: int,
        message_id: int,
        session: TradeBrowseSession,
        index: int,
    ) -> None:
        raw = session.selected or {}
        price = _to_float(raw.get("n4SalePrice"))
        seller = str((raw.get("seller") or {}).get("strNickName") or "")
        state = _seller_state(raw)
        state_text = "在线" if state == 1 else "离线" if state == 0 else "状态未知"
        item = session.item
        lines = [
            f"已选 #{index}：{item.name('cn')} | {_fmt_price(price)} 卡利弗 | "
            f"卖家 {seller or '?'}（{state_text}）",
        ]
        detail = _browse_detail(raw, item.item_type)
        if detail:
            lines.append(detail)
        lines.append("请选择购买方式：")
        lines.append(
            "1. 私聊购买：打开私聊框 → 确认后发送游戏内私聊（/chat → /chat/buy）"
        )
        lines.append(
            "2. 休闲室购买：打开添加框 → 确认后添加（/lounge → /lounge/buy）"
        )
        if state == 0:
            lines.append("注意：卖家当前离线，1 私聊购买会失败；可改选 2 或 /取消 后换一条。")
        lines.append("回复 1 或 2；发送 /取消 退出。")
        lines.append("发起前请确认：游戏 App 已启动并进入主角色。")
        await self._reply(qq, group_id, message_id, "\n".join(lines))

    async def _prepare_trade(
        self,
        qq: int,
        group_id: int,
        message_id: int,
        session: TradeBrowseSession,
        method: str,
    ) -> None:
        raw = session.selected or {}
        n8_id = str(raw.get("n8Id") or "")
        if not n8_id:
            await self._reply(qq, group_id, message_id, "商品数据缺少 n8Id，无法交易。")
            return
        token = self.store.get_token(qq)
        if not token:
            await self._reply(
                qq, group_id, message_id,
                "还没有登录。请私聊发送 /登录。",
            )
            return
        if token.status == "invalid":
            await self._reply(
                qq, group_id, message_id,
                "登录态已失效，请重新 /登录。",
            )
            return

        remain = TRADE_COOLDOWN - (time.time() - self._last_trade.get(qq, 0.0))
        if remain > 0:
            await self._reply(
                qq, group_id, message_id,
                f"距离上次交易操作不足 {TRADE_COOLDOWN} 秒，请 {int(remain) + 1} 秒后再试。",
            )
            return

        item = session.item
        price = _to_float(raw.get("n4SalePrice"))
        seller = str((raw.get("seller") or {}).get("strNickName") or "")
        lounge = method == "lounge"
        method_label = "休闲室购买" if lounge else "私聊购买"
        step_label = "打开休闲室添加框" if lounge else "打开私聊框"

        if not lounge:
            state = _seller_state(raw)
            if state == 0:
                if session.stage == "method":
                    hint = "请回复 2 改用休闲室购买，或 /取消 后换一条商品。"
                else:
                    hint = "请 /取消 后改用 /休闲室，或换一条商品。"
                await self._reply(
                    qq, group_id, message_id,
                    f"卖家当前不在线，私聊购买会失败。\n{hint}",
                )
                return

        await self._reply(
            qq, group_id, message_id,
            f"正在{step_label}：{item.name('cn')} · {_fmt_price(price)} 卡利弗 …",
        )

        try:
            client = TradeClient(token.cookies, user_agent=token.user_agent)
            try:
                first = await asyncio.to_thread(
                    client.chat, n8_id, item.item_type, item.item_id, lounge=lounge
                )
                self._last_trade[qq] = time.time()
                ok = first.ok and bool(first.enc)
                action = "lounge_prepare" if lounge else "chat_prepare"
                pid = self.store.add_purchase(
                    qq,
                    mode="lounge" if lounge else "chat",
                    action=action,
                    item_name=item.name("cn"),
                    item_type=item.item_type,
                    item_id=item.item_id,
                    n8_id=n8_id,
                    price=price,
                    seller=first.seller or seller,
                    enc=first.enc,
                    status="prepared" if ok else "failed",
                    result_code=first.code if first.enc or not first.ok else "NO_ENC",
                    result_message=first.message,
                    raw=first.raw,
                )
                if ok:
                    session.enc = first.enc
                    session.record_id = pid
                    session.seller = first.seller or seller
                    session.stage = "confirm"
                    confirm_text = (
                        "确认添加卖家休闲室到游戏中"
                        if lounge
                        else "确认发送游戏内私聊"
                    )
                    await self._reply(
                        qq, group_id, message_id,
                        f"第 1 步成功（{method_label}）：{step_label}\n"
                        f"{item.name('cn')} | {_fmt_price(price)} 卡利弗 | "
                        f"卖家 {first.seller or seller or '?'}\n"
                        f"回复 1 {confirm_text}；回复 0 取消。\n"
                        f"记录 #{pid}（待确认）。",
                    )
                elif first.ok and not first.enc:
                    await self._reply(
                        qq, group_id, message_id,
                        f"{method_label}第 1 步成功，但没有返回确认凭证，无法继续。\n"
                        f"记录 #{pid} 已保存为失败。",
                    )
                    self._trade_sessions.pop(qq, None)
                else:
                    await self._reply(
                        qq, group_id, message_id,
                        f"{method_label}第 1 步失败：{first.readable}\n"
                        f"{_trade_hint(first.code)}\n记录 #{pid} 已保存。",
                    )
                    self._trade_sessions.pop(qq, None)
                    if first.code in ("NO_LOGIN", "INVALID_SESSION"):
                        self.store.update_token_status(qq, "invalid", first.readable)
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001
            logger.exception("准备购买失败")
            await self._reply(qq, group_id, message_id, f"交易请求失败：{exc}")

    async def _execute_confirmation(
        self,
        qq: int,
        group_id: int,
        message_id: int,
        session: TradeBrowseSession,
    ) -> None:
        if not session.enc or session.record_id is None:
            self._trade_sessions.pop(qq, None)
            await self._reply(
                qq, group_id, message_id,
                "没有待确认的购买，请重新 /购买 物品 条件。",
            )
            return
        token = self.store.get_token(qq)
        if not token:
            await self._reply(
                qq, group_id, message_id,
                "还没有登录。请私聊发送 /登录。",
            )
            return
        if token.status == "invalid":
            await self._reply(
                qq, group_id, message_id,
                "登录态已失效，请重新 /登录。",
            )
            return

        raw = session.selected or {}
        item = session.item
        price = _to_float(raw.get("n4SalePrice"))
        seller = session.seller or str((raw.get("seller") or {}).get("strNickName") or "")
        lounge = session.mode == "lounge"
        method_label = "休闲室购买" if lounge else "私聊购买"
        done_label = "添加卖家休闲室" if lounge else "发送游戏内私聊"

        await self._reply(
            qq, group_id, message_id,
            f"正在{done_label}：{item.name('cn')} · {_fmt_price(price)} 卡利弗 …",
        )

        try:
            client = TradeClient(token.cookies, user_agent=token.user_agent)
            try:
                second = await asyncio.to_thread(
                    client.confirm, session.enc, lounge=lounge
                )
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001
            logger.exception("确认购买失败")
            await self._reply(qq, group_id, message_id, f"确认请求失败：{exc}")
            return

        self.store.update_purchase(
            session.record_id,
            qq,
            status="success" if second.ok else "failed",
            seller=second.seller or seller,
            result_code=second.code,
            result_message=second.message,
        )
        if second.ok:
            self._trade_sessions.pop(qq, None)
            await self._reply(
                qq, group_id, message_id,
                f"#{session.record_id} {method_label}完成：{item.name('cn')} | "
                f"{_fmt_price(price)} 卡利弗 | 卖家 {seller or '?'}",
            )
        else:
            await self._reply(
                qq, group_id, message_id,
                f"#{session.record_id} {method_label}确认失败：{second.readable}\n"
                f"{_trade_hint(second.code)}",
            )
            if _should_clear_trade_session(second.code):
                self._trade_sessions.pop(qq, None)
            if second.code in ("NO_LOGIN", "INVALID_SESSION"):
                self.store.update_token_status(qq, "invalid", second.readable)

    async def _cancel_trade(
        self,
        qq: int,
        group_id: int,
        message_id: int,
        session: TradeBrowseSession,
    ) -> None:
        pid = session.record_id
        if pid is not None:
            self.store.update_purchase(
                pid,
                qq,
                status="canceled",
                result_code="CANCELED",
                result_message="用户取消",
            )
        self._trade_sessions.pop(qq, None)
        if pid is not None:
            await self._reply(
                qq, group_id, message_id,
                f"已取消，记录 #{pid} 标记为已取消。",
            )
        else:
            await self._reply(qq, group_id, message_id, "已取消。")

    async def _confirm_purchase(
        self, qq: int, group_id: int, message_id: int, args: str
    ) -> None:
        purchase_id = _parse_id(args)
        if purchase_id is None:
            await self._reply(
                qq, group_id, message_id,
                "用法：/确认 <采购记录编号>，编号见 /采购记录。",
            )
            return
        purchase = self.store.get_purchase(purchase_id, qq)
        if not purchase:
            await self._reply(qq, group_id, message_id, f"没有找到属于你的采购记录 #{purchase_id}。")
            return
        if purchase.status == "success":
            await self._reply(qq, group_id, message_id, f"采购记录 #{purchase_id} 已经是成功状态。")
            return
        if purchase.status == "canceled":
            await self._reply(
                qq, group_id, message_id,
                f"采购记录 #{purchase_id} 已取消，请重新 /购买。",
            )
            return
        if not purchase.enc:
            await self._reply(
                qq, group_id, message_id,
                f"采购记录 #{purchase_id} 没有确认凭证。\n"
                "请重新 /购买，完成第 1 步（打开私聊框/休闲室框）后再 /确认。",
            )
            return
        token = self.store.get_token(qq)
        if not token:
            await self._reply(
                qq, group_id, message_id,
                "还没有登录。请私聊发送 /登录。",
            )
            return

        await self._reply(
            qq, group_id, message_id,
            f"正在确认 #{purchase_id}（{'休闲室购买' if purchase.mode == 'lounge' else '私聊购买'}）…",
        )
        lounge = purchase.mode == "lounge"
        try:
            client = TradeClient(
                token.cookies,
                user_agent=token.user_agent,
            )
            try:
                result = await asyncio.to_thread(client.confirm, purchase.enc, lounge=lounge)
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001
            await self._reply(qq, group_id, message_id, f"确认请求失败：{exc}")
            return

        status = "success" if result.ok else "failed"
        self.store.update_purchase(
            purchase_id,
            qq,
            status=status,
            seller=result.seller or None,
            result_code=result.code,
            result_message=result.message,
        )
        method_label = "休闲室购买" if lounge else "私聊购买"
        seller = result.seller or purchase.seller
        if result.ok:
            self._trade_sessions.pop(qq, None)
            await self._reply(
                qq, group_id, message_id,
                f"#{purchase_id} {method_label}完成：{purchase.item_name} | "
                f"卖家 {seller or '?'}",
            )
        else:
            await self._reply(
                qq, group_id, message_id,
                f"#{purchase_id} {method_label}确认失败：{result.readable}\n"
                f"{_trade_hint(result.code)}",
            )
            if _should_clear_trade_session(result.code):
                self._trade_sessions.pop(qq, None)
            if result.code in ("NO_LOGIN", "INVALID_SESSION"):
                self.store.update_token_status(qq, "invalid", result.readable)

    async def _list_purchases(
        self, qq: int, group_id: int, message_id: int, args: str
    ) -> None:
        limit = _parse_id(args) if args else 10
        if limit is None:
            await self._reply(qq, group_id, message_id, "用法：/采购记录 [数量]")
            return
        limit = max(1, min(limit, 20))
        rows = self.store.list_purchases(qq, limit)
        if not rows:
            await self._reply(qq, group_id, message_id, "还没有采购记录。")
            return
        lines = ["最近采购记录："]
        for p in rows:
            action_label = {
                "chat_prepare": "私聊购买",
                "lounge_prepare": "休闲室购买",
                "chat": "私聊卖家",
                "chat_buy": "添加到休闲室",
                "lounge_chat": "组队大厅",
                "lounge_buy": "组队确认",
                "prepare": "求购",
                "buy": "购买",
            }.get(p.action, p.action or "操作")
            lines.append(
                f"#{p.id} {p.item_name} | {_fmt_price(p.price)} | "
                f"{p.seller or '?'} | {_status_label(p.status)} | "
                f"{action_label} | {_fmt_ts(p.created_at)}"
            )
        await self._reply(qq, group_id, message_id, "\n".join(lines))

    # ── 向导 ────────────────────────────────────────────────

    async def _feed_wizard(
        self, qq: int, group_id: int, message_id: int, content: str
    ) -> None:
        self.store.upsert_user(qq)
        async with self._market_gate:
            result = await asyncio.to_thread(self.wizard.feed, qq, content)
        await self._reply(qq, group_id, message_id, result.text)
        if result.done and result.dsl:
            _, _, dsl = result.dsl.partition(" ")
            await self._create_from_dsl(qq, group_id, message_id, dsl)

    def _resolve_for_wizard(self, query: str) -> tuple[str, str]:
        item = self.market.resolve(query)
        return item.name("cn"), self.market.type_label(item)

    # ── 基础设施 ────────────────────────────────────────────

    def _lock_for(self, qq: int) -> asyncio.Lock:
        lock = self._locks.get(qq)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[qq] = lock
        self._lock_used[qq] = time.time()
        return lock

    def _allow_command(self, qq: int, min_interval: float) -> bool:
        """简单的用户级命令限速，避免刷命令打爆市场接口。"""
        now = time.time()
        if now - self._last_command.get(qq, 0.0) < min_interval:
            return False
        self._last_command[qq] = now
        return True

    @staticmethod
    def _strip_prefix(content: str) -> str | None:
        if not content:
            return None
        for prefix in PREFIXES:
            if content.startswith(prefix):
                return content[len(prefix):].strip()
        return None

    async def _handle_friend_request(self, event: dict[str, Any]) -> None:
        """自动同意好友申请，并给新好友发送帮助文字与使用图。"""
        if event.get("request_type") != "friend":
            return
        flag = str(event.get("flag") or "").strip()
        user_id = int(event.get("user_id") or 0)
        if not flag or not user_id or user_id == self.self_id:
            return
        if user_id in self.settings.ignore_qq:
            return
        if self.permissions.is_blocked(user_id):
            logger.info("忽略被封禁用户的好友申请：%s", user_id)
            return
        if not self.permissions.friend_auto_accept_enabled():
            return

        now = time.monotonic()
        for key, stamp in list(self._friend_request_seen.items()):
            if now - stamp > FRIEND_REQUEST_TTL:
                self._friend_request_seen.pop(key, None)
        if flag in self._friend_request_seen:
            return
        self._friend_request_seen[flag] = now

        while (
            self._friend_accept_times
            and now - self._friend_accept_times[0] >= 60
        ):
            self._friend_accept_times.popleft()
        limit = self.permissions.friend_accept_max_per_min()
        if len(self._friend_accept_times) >= limit:
            logger.warning("好友申请超过每分钟上限（%s），已跳过：%s", limit, user_id)
            return
        self._friend_accept_times.append(now)

        try:
            await asyncio.to_thread(
                self.client.set_friend_add_request, flag, True
            )
        except Exception:  # noqa: BLE001 — 同意失败只记录，不影响连接
            logger.exception("同意好友申请失败：%s（flag=%s）", user_id, flag)
            return
        self.friends.add(user_id)
        logger.info(
            "已通过好友申请：%s（验证信息：%s）",
            user_id,
            str(event.get("comment") or "")[:80] or "-",
        )

        if self.permissions.friend_welcome_enabled():
            await asyncio.sleep(FRIEND_WELCOME_DELAY)
            await self._send_friend_welcome(user_id)

    async def _send_friend_welcome(self, user_id: int) -> None:
        content = (
            "已通过你的好友申请。发送 /帮助 可再次获取说明；"
            "以下是命令与使用图。\n\n" + help_text()
        )
        for attempt in range(1, 3):
            try:
                guide = self._guide_image_segment()
                if guide is not None:
                    await self._reply_with_image(user_id, 0, 0, content, guide)
                else:
                    await self._reply(user_id, 0, 0, content)
                logger.info("已向新好友 %s 发送帮助", user_id)
                return
            except Exception:  # noqa: BLE001 — 好友关系可能尚未生效
                logger.warning(
                    "向新好友 %s 发送帮助失败（第 %s 次）", user_id, attempt, exc_info=True
                )
                if attempt == 1:
                    await asyncio.sleep(FRIEND_RETRY_DELAY)
        logger.warning("向新好友 %s 发送帮助最终失败", user_id)

    def _guide_image_segment(self) -> dict[str, Any] | None:
        """读取 docs/design 使用图，压缩一次后缓存 base64。"""
        if self._guide_image_b64 is None:
            try:
                from io import BytesIO

                from PIL import Image

                if not GUIDE_IMAGE_PATH.exists():
                    logger.warning("帮助图片不存在：%s", GUIDE_IMAGE_PATH)
                    return None
                with Image.open(GUIDE_IMAGE_PATH) as img:
                    img = img.convert("RGB")
                    if img.width > 1600:
                        height = round(img.height * 1600 / img.width)
                        img = img.resize((1600, height), Image.LANCZOS)
                    buf = BytesIO()
                    img.save(buf, "PNG", optimize=True)
                data = buf.getvalue()
                self._guide_image_b64 = base64.b64encode(data).decode("ascii")
                logger.info("帮助图片已加载：%s（%sKB）", GUIDE_IMAGE_PATH.name, len(data) // 1024)
            except Exception:  # noqa: BLE001 — 图片不可用时退回文字
                logger.warning("帮助图片加载失败", exc_info=True)
                return None
        return image("base64://" + self._guide_image_b64)

    async def _reply_with_image(
        self,
        qq: int,
        group_id: int,
        message_id: int,
        content: str,
        image_segment: dict[str, Any],
    ) -> None:
        """文字 + 图片作为一条消息发送。"""
        if group_id:
            self._mark_group_reply(group_id, qq)
            segments: list[dict[str, Any]] = []
            if message_id:
                segments.append(reply(message_id))
            segments.append(text(content))
            segments.append(image_segment)
            await asyncio.to_thread(self.client.send_group_msg, group_id, segments)
        else:
            await asyncio.to_thread(
                self.client.send_private_msg, qq, [text(content), image_segment]
            )

    async def _reply(
        self, qq: int, group_id: int, message_id: int, content: str
    ) -> None:
        if group_id:
            self._mark_group_reply(group_id, qq)
        chunks = _split_text(content)
        for index, chunk in enumerate(chunks):
            if index:
                await asyncio.sleep(0.3)
            if group_id:
                segments: list[dict[str, Any]] = []
                if message_id:
                    segments.append(reply(message_id))
                segments.append(text(chunk))
                await asyncio.to_thread(self.client.send_group_msg, group_id, segments)
            else:
                await asyncio.to_thread(self.client.send_private_msg, qq, [text(chunk)])

    # ── 群 @ 回环防护 ──────────────────────────────────────

    def _group_allowed(self, group_id: int, qq: int) -> bool:
        """某个 (群,成员) 触发过多回复时静音该成员一段时间，打断互相 @ 的回环。"""
        now = time.time()
        key = (group_id, qq)
        muted_until = self._group_muted_until.get(key, 0.0)
        if now < muted_until:
            return False
        return True

    def _mark_group_reply(self, group_id: int, qq: int) -> None:
        now = time.time()
        key = (group_id, qq)
        times = self._group_reply_times.setdefault(key, deque(maxlen=GROUP_REPLY_WINDOW))
        times.append(now)
        if len(times) >= GROUP_REPLY_WINDOW and now - times[0] <= GROUP_REPLY_WINDOW_SECONDS:
            self._group_muted_until[key] = now + GROUP_MUTE_SECONDS
            logger.warning(
                "群 %s 的 %s 在 %s 秒内触发过多回复，静音 %s 秒以打断互相触发",
                group_id, qq, GROUP_REPLY_WINDOW_SECONDS, GROUP_MUTE_SECONDS,
            )


def _parse_id(value: str) -> int | None:
    value = (value or "").strip().lstrip("#")
    return int(value) if value.isdigit() else None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sale_price(raw: dict[str, Any]) -> float:
    """挂单价格（用于 /查价 按最低价排序）。"""
    return _to_float(raw.get("n4SalePrice")) or 0.0


def _fmt_price(value: float | None) -> str:
    return f"{value:,.0f}" if value is not None else "?"


def _brief_desc(desc: str) -> str:
    first = (desc or "").split("\t")[0]
    first = first.split("[")[0].split("［")[0]
    return first.strip()[:18]


def _spec_text(spec: Any) -> str:
    """筛选说明；排序已经在标题里单独展示，不重复。"""
    try:
        parts = [
            p for p in describe_spec(spec).split(" | ")
            if not p.startswith("排序")
        ]
    except Exception:  # noqa: BLE001
        return "无附加筛选"
    return " | ".join(parts) if parts else "无附加筛选"


def _seller_state(raw: dict[str, Any]) -> int | None:
    """1=在线，0=离线，None=未知（字段缺失或异常）。"""
    seller = raw.get("seller") or {}
    value = seller.get("isAccessState")
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number == 1:
        return 1
    if number == 0:
        return 0
    return None


def _browse_detail(raw: dict[str, Any], item_type: str) -> str:
    data = raw.get("data") or {}
    parts: list[str] = []
    if item_type == "transcendent":
        quality = data.get("n4WeaponQuality")
        if quality is not None:
            parts.append(f"品质{quality}")
        subperks = [
            str(sp.get("strName") or "")
            for sp in data.get("subperks") or []
            if sp.get("strName")
        ]
        if subperks:
            parts.append("/".join(subperks[:2]))
    else:
        options = data.get("options") or []
        advantage = [
            opt
            for opt in options
            if str(opt.get("strSlot") or "").lower() == "advantage"
        ]
        source = advantage or (options if item_type == "trigger" else [])
        names = [_brief_desc(str(opt.get("strDesc") or "")) for opt in source[:2]]
        names = [n for n in names if n]
        if names:
            parts.append("/".join(names))
    return " | ".join(p for p in parts if p)


def _browse_line(index: int, raw: dict[str, Any], item_type: str) -> str:
    price = _fmt_price(_to_float(raw.get("n4SalePrice")))
    seller = str((raw.get("seller") or {}).get("strNickName") or "")
    state = _seller_state(raw)
    state_text = "在线" if state == 1 else "离线" if state == 0 else "状态未知"
    bits = [f"{price} 卡利弗", f"卖家 {seller or '?'}", state_text]
    detail = _browse_detail(raw, item_type)
    if detail:
        bits.insert(1, detail)
    return f"{index}. " + " | ".join(bits)


def _should_clear_trade_session(code: str) -> bool:
    """商品已失效/自己的商品才清理会话；冷却、卖家离线、游戏未启动等可重试。"""
    return code in (
        "NO_ITEM",
        "NO_COUNT",
        "MATCHED_USERINFO",
        "INVALID_PARAM",
        "NOT_MATCHED_ALLOWED",
    )


def _trade_hint(code: str) -> str:
    return {
        "NO_LOGIN": "请先 /登录。",
        "INVALID_SESSION": "登录已失效，请重新 /登录。",
        "NO_PRIMARY": "请先在游戏里设置主角色。",
        "NO_ACCESS": "请先启动游戏 App 并进入主角色；也可能是卖家不可访问。",
        "NO_ACCESS_SELLER": "卖家当前不在线；可改用「添加到休闲室」或换一条。",
        "RETRY_LATER": "服务端 30 秒冷却，请稍后再试。",
        "NO_ITEM": "商品已下架，请重新 /购买。",
        "NO_COUNT": "商品已被买走，请重新 /购买。",
        "MATCHED_USERINFO": "这是你自己上架的商品，不能操作。",
        "RESTRICT_GAME": "买家账号被游戏限制。",
        "RESTRICT_TRADE": "买家账号被交易限制。",
        "RESTRICT_CHAT": "买家账号被聊天限制。",
        "MAINTENANCE": "游戏维护中，请稍后再试。",
    }.get(code, "")


def _has_sort(text: str) -> bool:
    for token in (text or "").split():
        key, sep, _ = token.partition("=")
        if sep and key.strip().lower() in ("排序", "sort"):
            return True
    return False


def _status_label(status: str) -> str:
    return {
        "valid": "有效",
        "invalid": "已失效",
        "unknown": "未校验",
        "prepared": "待确认",
        "success": "成功",
        "failed": "失败",
        "canceled": "已取消",
    }.get(status, status or "未知")


def _method_label(method: str) -> str:
    return {
        "steam_qr": "Steam 扫码",
        "manual_browser": "有头浏览器",
        "cookie_import": "Cookie 导入",
    }.get(method or "", method or "未记录")


def _source_label(source: str) -> str:
    return {
        "flat_json": "扁平 JSON",
        "cookies_json": "cookies.json",
        "cookies_dict": "Cookie 字典",
        "cookie_list": "Cookie 数组",
        "cookie_object": "单个 Cookie",
        "storage_state": "storage_state",
        "cookie_header": "Cookie 字符串",
    }.get(source, source)


def _fmt_ts(timestamp: int | None) -> str:
    if not timestamp:
        return "-"
    return time.strftime("%m-%d %H:%M", time.localtime(int(timestamp)))


def _split_text(content: str, limit: int = REPLY_LIMIT) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in content.splitlines() or [""]:
        if current and size + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _dump_options(title: str, entries: list, limit: int = 40) -> list[str]:
    if not entries:
        return []
    lines = [f"{title}："]
    for entry in entries[:limit]:
        lines.append(f"  {entry.id}  {entry.value}")
    if len(entries) > limit:
        lines.append(f"  … 共 {len(entries)} 个，可用名称或 ID 填写")
    return lines
