"""超越武器成交统计：抓取 Nexon statis 接口、解析、增量入库与定时任务。

实测要点（2026-11）：
- 匿名可读，但 WAF 对 TLS 指纹敏感，chrome 系列会被 403，safari17_0/edge101 正常；
- days=2 返回 48 小时窗口，只含有成交的小时；
- 时间按 CST（UTC+8），average_price 是字符串。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from market_v2.api import MarketClient

from .db import AdminDB, BotDB

logger = logging.getLogger(__name__)

API = "https://tfd-api.nexon.com/api/v2/trademarket/{item_type}/{item_id}/{days}/statis"
ITEM_TYPE = "transcendent"
DAYS = 2
# WAF 只放行部分 TLS 指纹，按优先级依次尝试
IMPERSONATE = ("safari17_0", "edge101", "chrome133a")
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Origin": "https://tfd.nexon.com",
    "Referer": "https://tfd.nexon.com/",
}
CST = timezone(timedelta(hours=8))
REQUEST_TIMEOUT = 20.0
ITEM_DELAY = 0.2
FIRST_DELAY_SEC = 60
MAX_INTERVAL_HOURS = 168


@dataclass(frozen=True)
class StatPoint:
    bucket_at: int
    average_price: float
    trade_count: int


def parse_points(payload: dict) -> list[StatPoint]:
    """把 API 响应转成 (小时起点, 均价, 笔数)，跳过坏行。"""
    points: dict[int, StatPoint] = {}
    for row in payload.get("ResultData") or []:
        if not isinstance(row, dict):
            continue
        try:
            stamp = datetime.strptime(str(row.get("date") or "").strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        try:
            price = float(row.get("average_price"))
            count = int(row.get("trade_count"))
        except (TypeError, ValueError):
            continue
        if price <= 0 or count <= 0:
            continue
        bucket = int(stamp.replace(tzinfo=CST).timestamp())
        points[bucket] = StatPoint(bucket, price, count)
    return [points[key] for key in sorted(points)]


def _get_once(url: str, impersonate: str) -> tuple[dict | None, str]:
    from curl_cffi import requests

    session = requests.Session(impersonate=impersonate, headers=HEADERS)
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        content_type = response.headers.get("content-type", "")
        if response.status_code != 200 or "json" not in content_type:
            return None, f"HTTP {response.status_code}"
        return response.json(), ""
    except Exception as exc:  # noqa: BLE001 — 网络层异常统一返回文字
        return None, f"{type(exc).__name__}: {exc}"[:160]
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass


def fetch_points(
    item_id: str, *, item_type: str = ITEM_TYPE, days: int = DAYS
) -> tuple[list[StatPoint], str]:
    """抓一件商品；返回 (点集, 错误描述)。"""
    url = API.format(item_type=item_type, item_id=item_id, days=days)
    last_error = ""
    for impersonate in IMPERSONATE:
        payload, error = _get_once(url, impersonate)
        if payload is not None:
            if str(payload.get("ResultCode") or "") != "SUCCESS":
                return [], str(payload.get("ResultMessage") or payload.get("ResultCode") or "接口失败")
            return parse_points(payload), ""
        last_error = error
    return [], last_error or "抓取失败"


class TradeStatsCrawler:
    """定时抓取 12 件超越武器的 2 天成交统计，增量写入 admin.db。"""

    def __init__(self, admin_db: AdminDB, bot_db: BotDB) -> None:
        self.admin_db = admin_db
        self.bot_db = bot_db
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    # ── 设置 ────────────────────────────────────────────────

    def _settings(self) -> dict:
        try:
            return self.bot_db.get_settings()
        except Exception:  # noqa: BLE001 — 设置读失败时按默认值
            return {}

    def enabled(self) -> bool:
        return bool(self._settings().get("trade_stats_enabled", True))

    def interval_hours(self) -> float:
        try:
            value = float(self._settings().get("trade_stats_interval_hours") or 12)
        except (TypeError, ValueError):
            value = 12.0
        return min(MAX_INTERVAL_HOURS, max(1.0, value))

    # ── 抓取 ────────────────────────────────────────────────

    def _load_catalog(self) -> tuple[list[tuple[str, str]], str]:
        client = MarketClient()
        try:
            items = client.fetch_catalog()
        except Exception as exc:  # noqa: BLE001
            return [], f"{type(exc).__name__}: {exc}"[:160]
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        rows = [
            (item.item_id, item.name("cn") or item.item_id)
            for item in items
            if item.item_type == ITEM_TYPE
        ]
        rows.sort(key=lambda pair: pair[1])
        return rows, ""

    def _fallback_items(self) -> tuple[list[tuple[str, str]], str]:
        rows = self.admin_db.list_trade_stats_items()
        items = [(str(row["item_id"]), str(row["item_name"] or row["item_id"])) for row in rows]
        return items, "" if items else "物品字典不可用，且数据库里还没有历史条目"

    def crawl_sync(self) -> dict:
        started = int(time.time())
        items, catalog_error = self._load_catalog()
        if catalog_error:
            logger.warning("成交统计：物品字典拉取失败，改用历史条目：%s", catalog_error)
            items, fallback_error = self._fallback_items()
            catalog_error = fallback_error or catalog_error
        if not items:
            finished = int(time.time())
            self.admin_db.record_trade_stats_run(
                started_at=started, finished_at=finished, items=0,
                inserted=0, failed=0, error=catalog_error,
            )
            return {
                "started_at": started, "finished_at": finished, "items": 0,
                "inserted": 0, "failed": 0, "error": catalog_error,
            }

        inserted = 0
        failed = 0
        errors: list[str] = []
        for item_id, name in items:
            points, error = fetch_points(item_id)
            if error:
                failed += 1
                errors.append(f"{item_id}: {error}")
                self.admin_db.upsert_trade_stats_item(
                    item_id, name, crawled_at=started, error=error
                )
            else:
                inserted += self.admin_db.insert_trade_stats_points(
                    item_id,
                    [(p.bucket_at, p.average_price, p.trade_count) for p in points],
                )
                self.admin_db.upsert_trade_stats_item(item_id, name, crawled_at=started)
            time.sleep(ITEM_DELAY)

        finished = int(time.time())
        summary = {
            "started_at": started,
            "finished_at": finished,
            "items": len(items),
            "inserted": inserted,
            "failed": failed,
            "error": "; ".join(errors)[:500],
        }
        self.admin_db.record_trade_stats_run(
            started_at=started, finished_at=finished, items=len(items),
            inserted=inserted, failed=failed, error=summary["error"],
        )
        logger.info(
            "成交统计完成：%s 件，新增 %s 点，失败 %s，耗时 %ss",
            len(items), inserted, failed, finished - started,
        )
        return summary

    async def run_once(self) -> dict:
        """手动或定时触发一次；同一时刻只允许一个任务。"""
        async with self._lock:
            return await asyncio.to_thread(self.crawl_sync)

    async def run_forever(self) -> None:
        try:
            await asyncio.sleep(FIRST_DELAY_SEC)
            while True:
                if self.enabled():
                    try:
                        await self.run_once()
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 — 单轮失败不影响下次
                        logger.exception("成交统计抓取失败")
                await asyncio.sleep(self.interval_hours() * 3600)
        except asyncio.CancelledError:
            logger.info("成交统计定时任务已停止")
            raise

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
