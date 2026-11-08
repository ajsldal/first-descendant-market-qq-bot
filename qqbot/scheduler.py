"""定时调度：按到期时间取订阅、按指纹合并抓取、全量推送。

- 每 30 秒 tick 一次；
- 相同筛选指纹只抓一次，结果分发给组内所有用户；
- 抓取在线程池执行（MarketClient 是同步库）；
- 推送失败或接口失败都做指数退避，不影响其他订阅。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import partial

from market_v2.filters import FilterSpec

from .analytics import AnalyticsRecorder
from .bark import build_command
from .flip import (
    build_fuzzy_spec,
    format_profit_message,
    fuzzy_fingerprint,
    price_of,
    profit_of,
)
from .friends import FriendDirectory
from .market import FetchResult, MarketService
from .permissions import PermissionService
from .pusher import Pusher
from .store import Store, Subscription

logger = logging.getLogger(__name__)

_TICK_SECONDS = 30
_CACHE_TTL = 60
_MAX_BACKOFF = 3600
# 推送/传输层失败（NapCat 掉线等）最多退避 5 分钟，避免恢复后长时间收不到推送
_TRANSPORT_BACKOFF_MAX = 300
# 连续投递失败（拉黑/删除机器人等）达到该次数后自动暂停订阅
_AUTO_PAUSE_FAILURES = 10
_QUIET_RE = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")


def _ids_hash(items: list[dict]) -> str:
    ids = sorted(str(it.get("n8Id") or "") for it in items if it.get("n8Id"))
    return hashlib.sha1(",".join(ids).encode("utf-8")).hexdigest()


def quiet_until(quiet: str, now_ts: int) -> int | None:
    """若当前处于静默时段，返回静默结束的时间戳，否则返回 None。"""
    match = _QUIET_RE.match(quiet or "")
    if not match:
        return None
    h1, m1, h2, m2 = map(int, match.groups())
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if start == end:
        return None
    local = datetime.fromtimestamp(now_ts)
    current = local.hour * 60 + local.minute
    end_dt = local.replace(hour=h2, minute=m2, second=0, microsecond=0)

    if start < end:
        if start <= current < end:
            return int(end_dt.timestamp())
        return None
    # 跨天：23:00-08:00
    if current >= start:
        return int((end_dt + timedelta(days=1)).timestamp())
    if current < end:
        return int(end_dt.timestamp())
    return None


class Scheduler:
    def __init__(
        self,
        store: Store,
        market: MarketService,
        pusher: Pusher,
        *,
        tick_seconds: int = _TICK_SECONDS,
        workers: int = 2,
        max_pages: int = 3,
        cache_ttl: int = _CACHE_TTL,
        analytics: AnalyticsRecorder | None = None,
        permissions: PermissionService | None = None,
        bark=None,  # noqa: ANN001 — qqbot.bark.BarkService，避免循环导入
        friends: FriendDirectory | None = None,
    ) -> None:
        self.store = store
        self.market = market
        self.pusher = pusher
        self.tick_seconds = tick_seconds
        self.max_pages = max_pages
        self.cache_ttl = cache_ttl
        self.analytics = analytics
        self.permissions = permissions
        self.bark = bark
        self.friends = friends
        self._workers = workers
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="market")
        self._cache: dict[str, tuple[float, FetchResult]] = {}
        self._flip_last: dict[int, tuple[str, float]] = {}
        self._running = False

    async def run(self) -> None:
        self._running = True
        recovered = self.store.recover_stale_backoff()
        if recovered:
            logger.info("启动时清理 %s 个订阅的失败退避", recovered)
        logger.info("调度器启动：每 %s 秒检查一次，并发抓取 %s 组", self.tick_seconds, self._workers)
        while self._running:
            started = time.monotonic()
            try:
                count = await self.tick()
                if count:
                    logger.info("本轮处理 %s 个订阅，耗时 %.1fs", count, time.monotonic() - started)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 调度循环不能退出
                logger.exception("调度 tick 异常")
            await asyncio.sleep(self.tick_seconds)

    def stop(self) -> None:
        self._running = False

    async def tick(self) -> int:
        now = int(time.time())
        due = self.store.due_subscriptions(now, limit=100)
        if self.bark is not None:
            self.bark.begin_tick()
        if self.friends is not None and due:
            await asyncio.to_thread(self.friends.refresh)
            kept: list[Subscription] = []
            for sub in due:
                status = self.friends.is_friend(sub.qq, refresh=False)
                if status is False:
                    self.store.set_enabled(sub.id, sub.qq, False)
                    logger.info(
                        "订阅 #%s 的用户 %s 已不在好友列表，自动暂停",
                        sub.id, sub.qq,
                    )
                    continue
                kept.append(sub)
            due = kept
        if not due:
            return 0

        groups: dict[str, list[Subscription]] = defaultdict(list)
        for sub in due:
            groups[sub.fingerprint].append(sub)

        semaphore = asyncio.Semaphore(self._workers)
        await asyncio.gather(
            *(self._run_group(fingerprint, subs, semaphore)
              for fingerprint, subs in groups.items())
        )
        return len(due)

    async def run_one(self, sub: Subscription) -> tuple[int, str | None]:
        """立即执行一个订阅（/立即查）。"""
        try:
            spec = FilterSpec.from_dict(sub.spec)
            result = await self._fetch(sub.fingerprint, spec, force=True)
        except Exception as exc:  # noqa: BLE001 — 直接回复用户
            self._record_failure(sub, exc)
            logger.warning("订阅 #%s 立即检查失败：%s", sub.id, exc)
            return 0, str(exc)
        try:
            matched, _ = await self._deliver(sub, result)
            return matched, None
        except Exception as exc:  # noqa: BLE001 — 推送属于传输层失败
            self._record_failure(sub, exc, transport=True)
            logger.warning("订阅 #%s 立即推送失败：%s", sub.id, exc)
            return 0, str(exc)

    # ── 内部 ────────────────────────────────────────────────

    async def _run_group(
        self,
        fingerprint: str,
        subs: list[Subscription],
        semaphore: asyncio.Semaphore,
    ) -> None:
        async with semaphore:
            try:
                spec = FilterSpec.from_dict(subs[0].spec)
                result = await self._fetch(fingerprint, spec)
            except Exception as exc:  # noqa: BLE001 — 同组一起失败
                logger.warning("筛选组 %s 抓取失败：%s", fingerprint[:8], exc)
                for sub in subs:
                    self._record_failure(sub, exc)
                return

            if self.analytics is not None:
                try:
                    await asyncio.to_thread(
                        self.analytics.record_group, subs, result, fingerprint
                    )
                except Exception:  # noqa: BLE001 — 分析失败不影响推送
                    logger.exception("订阅分析记录失败：%s", fingerprint[:8])

            for sub in subs:
                try:
                    await self._deliver(sub, result)
                except Exception as exc:  # noqa: BLE001 — 单个订阅失败不影响同组
                    logger.exception("订阅 #%s 推送失败", sub.id)
                    self._record_failure(sub, exc, transport=True)

    async def _fetch(
        self, fingerprint: str, spec: FilterSpec, *, force: bool = False
    ) -> FetchResult:
        now = time.time()
        if not force:
            cached = self._cache.get(fingerprint)
            if cached and now - cached[0] <= self.cache_ttl:
                return cached[1]

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            self._executor,
            partial(self.market.fetch, spec, max_pages=self.max_pages),
        )
        self._cache[fingerprint] = (time.time(), result)
        return result

    async def _deliver(self, sub: Subscription, result: FetchResult) -> tuple[int, int]:
        now = int(time.time())
        ids = [str(it.get("n8Id") or "") for it in result.items if it.get("n8Id")]

        if self.permissions is not None and self.permissions.is_blocked(sub.qq):
            self.store.mark_seen(sub.id, ids, now)
            self.store.record_success(
                sub.id,
                started_at=now,
                matched=len(result.items),
                pushed=0,
                new_count=0,
                requests=result.requests,
                last_hash=_ids_hash(result.items),
                next_run_at=now + sub.interval_sec,
            )
            logger.info("订阅 #%s 的用户已被封禁，跳过推送", sub.id)
            return len(result.items), 0

        quiet_end = quiet_until(sub.quiet_hours, now)
        if quiet_end:
            self.store.reschedule(sub.id, quiet_end)
            logger.info("订阅 #%s 处于静默时段，推迟到 %s", sub.id, quiet_end)
            return len(result.items), 0

        if sub.last_run_at is None and not sub.first_push:
            self.store.mark_seen(sub.id, ids, now)
            self.store.record_success(
                sub.id,
                started_at=now,
                matched=len(result.items),
                pushed=0,
                new_count=len(ids),
                requests=result.requests,
                last_hash=_ids_hash(result.items),
                next_run_at=now + sub.interval_sec,
            )
            logger.info("订阅 #%s 首轮只记录基线：%s 条", sub.id, len(ids))
            return len(result.items), 0

        new_ids = self.store.check_new(sub.id, ids)
        items = result.items
        if sub.only_new:
            items = [it for it in items if str(it.get("n8Id") or "") in new_ids]

        if not items:
            self.store.record_success(
                sub.id,
                started_at=now,
                matched=len(result.items),
                pushed=0,
                new_count=len(new_ids),
                requests=result.requests,
                last_hash=_ids_hash(result.items),
                next_run_at=now + sub.interval_sec,
            )
            return len(result.items), 0

        last_hash = _ids_hash(result.items)
        same_as_last = bool(sub.last_hash and sub.last_hash == last_hash)
        pushed = await self.pusher.push(
            sub, result, new_ids, items=items, same_as_last=same_as_last
        )
        # 推送成功后才写入 seen：崩溃/发送失败时下一轮还能重试，不会丢“新上架”
        self.store.mark_seen(sub.id, ids, now)
        self.store.record_success(
            sub.id,
            started_at=now,
            matched=len(result.items),
            pushed=pushed,
            new_count=len(new_ids),
            requests=result.requests,
            last_hash=last_hash,
            next_run_at=now + sub.interval_sec,
        )
        logger.info(
            "订阅 #%s 推送 %s 条（命中 %s，新 %s）",
            sub.id, pushed, len(result.items), len(new_ids),
        )
        if self.bark is not None:
            try:
                await self.bark.notify(sub, items, len(result.items), len(new_ids))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 手机推送失败不能影响订阅
                logger.exception("订阅 #%s 的 Bark 推送异常", sub.id)
        try:
            await self._flip_profit(sub, items, len(result.items))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 利润计算失败不能影响订阅
            logger.exception("订阅 #%s 的倒卖利润计算异常", sub.id)
        return len(result.items), pushed

    async def _flip_profit(
        self, sub: Subscription, items: list[dict], matched: int
    ) -> None:
        """仅对手机订阅：命中 1~3 条时对比模糊结果，推送最高倒卖利润。"""
        if self.permissions is None or not self.permissions.flip_enabled():
            return
        if not sub.bark_enabled or matched < 1 or matched > 3:
            return
        cooldown = self.permissions.flip_cooldown_sec()
        last = self._flip_last.get(sub.id)
        if last is not None and time.monotonic() - last[1] < cooldown:
            return
        priced = [it for it in items if price_of(it) is not None]
        if not priced:
            return
        cheapest = min(priced, key=lambda it: price_of(it) or 0.0)
        p1 = price_of(cheapest) or 0.0

        spec_dict = build_fuzzy_spec(sub.spec or {}, sub.flip_overrides)
        try:
            spec = FilterSpec.from_dict(spec_dict)
            spec.validate()
        except Exception as exc:  # noqa: BLE001 — 模糊条件无效时跳过
            logger.warning("订阅 #%s 的模糊过滤不可用：%s", sub.id, exc)
            return
        fingerprint = fuzzy_fingerprint(spec_dict)
        result = await self._fetch(fingerprint, spec, force=True)
        candidates = [
            it for it in result.items if price_of(it) is not None
        ]
        candidates.sort(key=lambda it: price_of(it) or 0.0)
        targets = candidates[1 : matched + 1]
        if not targets:
            return

        fee = self.permissions.flip_fee()
        tax = self.permissions.flip_tax_rate()
        best_profit = None
        best_target: dict = targets[0]
        best_rank = 2
        for offset, target in enumerate(targets, start=2):
            profit = profit_of(p1, price_of(target) or 0.0, fee=fee, tax_rate=tax)
            if best_profit is None or profit > best_profit:
                best_profit, best_target, best_rank = profit, target, offset

        signature = (
            f"{cheapest.get('n8Id')}|{best_target.get('n8Id')}|"
            f"{round(best_profit or 0)}"
        )
        if last is not None and last[0] == signature:
            return

        message = format_profit_message(
            sub_id=sub.id,
            matched=matched,
            cheapest=cheapest,
            target=best_target,
            target_rank=best_rank,
            profit=best_profit or 0.0,
            fee=fee,
            tax_rate=tax,
            command=build_command(cheapest, sub.spec or {}),
        )
        await self.pusher.send_private(sub.qq, message)
        self._flip_last[sub.id] = (signature, time.monotonic())
        logger.info(
            "订阅 #%s 倒卖利润 %.0f（命中 %s 条）已私聊 %s",
            sub.id, best_profit or 0.0, matched, sub.qq,
        )

    def _record_failure(
        self, sub: Subscription, exc: Exception, *, transport: bool = False
    ) -> None:
        now = int(time.time())
        fail_count = sub.fail_count + 1
        delay = min(sub.interval_sec * (2 ** min(fail_count, 4)), _MAX_BACKOFF)
        if transport:
            # 推送/传输失败通常只需等 NapCat 恢复，不做小时级退避
            delay = min(delay, _TRANSPORT_BACKOFF_MAX)
        next_run = now + max(delay, 60)
        self.store.record_failure(
            sub.id,
            started_at=now,
            error=f"{type(exc).__name__}: {exc}",
            next_run_at=next_run,
            fail_count=fail_count,
        )
        if transport and fail_count >= _AUTO_PAUSE_FAILURES:
            self.store.set_enabled(sub.id, sub.qq, False)
            logger.warning(
                "订阅 #%s 连续 %s 次投递失败，已自动暂停：%s",
                sub.id, fail_count, str(exc)[:120],
            )
        if fail_count == 3:
            asyncio.create_task(
                self._notify_failure(sub, str(exc)),
                name=f"notify-fail-{sub.id}",
            )

    async def _notify_failure(self, sub: Subscription, error: str) -> None:
        try:
            await self.pusher.send_private(
                sub.qq,
                f"订阅 #{sub.id} 连续 3 次检查失败：{error[:120]}\n"
                "我会继续重试；可用 /暂停 暂时关闭它。",
            )
        except Exception:  # noqa: BLE001
            logger.exception("失败通知发送失败")

    def prune_cache(self, max_entries: int = 200, max_age: int = 600) -> int:
        """清理过期/过多的抓取结果缓存，防止长期运行时内存增长。"""
        now = time.time()
        expired = [
            key for key, (stamp, _) in self._cache.items()
            if now - stamp > max_age
        ]
        for key in expired:
            self._cache.pop(key, None)
        if len(self._cache) > max_entries:
            keep = sorted(self._cache.items(), key=lambda kv: kv[1][0], reverse=True)[:max_entries]
            self._cache = dict(keep)
        # 去重签名保留 24 小时：同一挂单在一天内不重复推送，删除的订阅也不会永久占内存
        keep_flip = time.monotonic() - 86400
        stale_flip = [key for key, (_, stamp) in self._flip_last.items() if stamp < keep_flip]
        for key in stale_flip:
            self._flip_last.pop(key, None)
        return len(expired)

    def close(self) -> None:
        self.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)
