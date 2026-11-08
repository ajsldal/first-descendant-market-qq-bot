"""好友关系缓存：供订阅巡检与订阅命令门槛使用。

`get_friend_list` 是同步 HTTP 调用，缓存 10 分钟，避免每次命令/每轮调度都请求。
拿不到好友列表时返回 None（调用方按失败开放处理，避免 NapCat 抖动导致误暂停）。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_TTL = 600


class FriendDirectory:
    def __init__(
        self,
        client_provider: Callable[[], Any],
        *,
        ttl: int = DEFAULT_TTL,
    ) -> None:
        self._client_provider = client_provider
        self._ttl = max(60, int(ttl))
        self._lock = threading.RLock()
        self._ids: set[int] | None = None
        self._checked_at = 0.0
        # 自动通过好友后先记在待合并集合，避免把其他好友误判成非好友
        self._additions: set[int] = set()

    def refresh(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        with self._lock:
            if (
                not force
                and self._ids is not None
                and now - self._checked_at <= self._ttl
            ):
                return True
        try:
            raw = self._client_provider().get_friend_list()
            ids = {
                int(item.get("user_id") or item.get("qq") or 0)
                for item in (raw or [])
                if isinstance(item, dict)
            }
            ids.discard(0)
        except Exception:  # noqa: BLE001 — 网络/接口异常时保留旧缓存
            logger.warning("获取好友列表失败", exc_info=True)
            return False
        with self._lock:
            ids |= self._additions
            self._additions.clear()
            self._ids = ids
            self._checked_at = now
        return True

    def is_friend(self, qq: int, *, refresh: bool = True) -> bool | None:
        """True/False 为已知结果；None 表示好友列表不可用（调用方自行决定）。"""
        if refresh:
            self.refresh()
        with self._lock:
            if self._ids is None:
                return None
            return int(qq) in self._ids

    def add(self, qq: int) -> None:
        with self._lock:
            if self._ids is not None:
                self._ids.add(int(qq))
            else:
                self._additions.add(int(qq))

    def remove(self, qq: int) -> None:
        with self._lock:
            if self._ids is not None:
                self._ids.discard(int(qq))
