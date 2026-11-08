"""订阅抓取数据分析：记录超越武器的消失历史、卖家与导狗（倒卖）记录。

只处理定时订阅抓取，且只处理超越武器（transcendent）。
本模块不产生任何 QQ 消息，供外部后端读取 SQLite 分析。

判定口径（2026-11 修订）：
- n8Id 只是挂单实例，不参与任何商品比对；
- 卖家身份取昵称末尾的 #数字（改名不影响），展示仍用完整昵称；
- 原始卖家 = 同一商品身份（identity_key）里最早出现的卖家；
- 导狗 = 原始卖家已有该身份的消失记录，且此后新卖家上架时间晚于最后一次消失。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from .market import FetchResult
from .store import Store, Subscription, seller_key

logger = logging.getLogger(__name__)

ITEM_TYPE = "transcendent"
ABILITY_GROUPS = ("0", "1", "2")


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ListingSnapshot:
    """超越武器挂单的身份快照；价格/卖家/上架时间不参与身份。"""

    n8_id: str
    dt_register: str
    price: float | None
    seller: str
    seller_key: str
    quality: int | None
    subperks: list[dict[str, Any]]      # 只含 isFixed=0 的辅助词条
    abilities: dict[str, int | None]    # {"0": 113, "1": 5, "2": 32}
    identity_json: str
    identity_key: str


def normalize_transcendent(raw: dict[str, Any]) -> ListingSnapshot | None:
    """把服务端原始商品转成可比较快照；固定辅助词条丢弃。"""
    n8_id = str(raw.get("n8Id") or "").strip()
    if not n8_id:
        return None

    data = raw.get("data") or {}
    subperks: list[dict[str, Any]] = []
    for sp in data.get("subperks") or []:
        if bool(sp.get("isFixed")):
            continue
        name = str(sp.get("strName") or "").strip()
        if not name:
            continue
        details = sorted(
            str(opt.get("strDesc") or "").strip()
            for opt in sp.get("options") or []
            if str(opt.get("strDesc") or "").strip()
        )
        subperks.append({"name": name, "details": details})
    subperks.sort(key=lambda x: (x["name"], x["details"]))

    groups: dict[int, int] = {}
    for opt in data.get("options") or []:
        group = _to_int(opt.get("n4Group"))
        point = _to_int(opt.get("n4Point"))
        if group is None or point is None or group not in (0, 1, 2):
            continue
        groups[group] = max(groups.get(group, point), point)
    abilities = {str(g): groups.get(g) for g in (0, 1, 2)}

    quality = _to_int(data.get("n4WeaponQuality"))
    attr = {"quality": quality, "subperks": subperks, "abilities": abilities}
    identity_json = json.dumps(
        attr, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    # 同商品身份：品质 + 2 条可选辅助词条 + 3 组能力必须齐全。
    comparable = (
        quality is not None
        and len(subperks) == 2
        and all(abilities.get(group) is not None for group in ABILITY_GROUPS)
    )
    identity_key = (
        hashlib.sha1(identity_json.encode("utf-8")).hexdigest() if comparable else ""
    )

    seller_info = raw.get("seller") or {}
    seller = str(seller_info.get("strNickName") or "")
    return ListingSnapshot(
        n8_id=n8_id,
        dt_register=str(raw.get("dtRegister") or ""),
        price=_to_float(raw.get("n4SalePrice")),
        seller=seller,
        seller_key=seller_key(seller),
        quality=quality,
        subperks=subperks,
        abilities=abilities,
        identity_json=identity_json,
        identity_key=identity_key,
    )


class AnalyticsRecorder:
    """把一次定时订阅抓取写入分析表；非白名单/非超越武器直接跳过。"""

    def __init__(
        self,
        store: Store,
        whitelist: frozenset[int] | Callable[[], frozenset[int]] = frozenset(),
        *,
        enabled: Callable[[], bool] | None = None,
    ) -> None:
        self.store = store
        if callable(whitelist):
            self._whitelist_provider = whitelist
            self._static_whitelist: frozenset[int] | None = None
        else:
            self._whitelist_provider = None
            self._static_whitelist = frozenset(int(q) for q in whitelist)
        self._enabled_provider = enabled

    @property
    def whitelist(self) -> frozenset[int]:
        if self._whitelist_provider is not None:
            return frozenset(self._whitelist_provider())
        return self._static_whitelist or frozenset()

    @property
    def enabled(self) -> bool:
        if self._enabled_provider is not None and not self._enabled_provider():
            return False
        return bool(self.whitelist)

    def record_group(
        self,
        subs: list[Subscription],
        result: FetchResult,
        fingerprint: str,
        *,
        now: int | None = None,
    ) -> int:
        if not self.enabled:
            return 0
        whitelist = self.whitelist
        if result.item.item_type != ITEM_TYPE:
            return 0
        eligible = [sub for sub in subs if sub.qq in whitelist]
        if not eligible:
            return 0

        snapshots: list[ListingSnapshot] = []
        for raw in result.items:
            snapshot = normalize_transcendent(raw)
            if snapshot is not None:
                snapshots.append(snapshot)

        stamp = int(now if now is not None else time.time())
        run_id = self.store.create_crawl_run(
            fingerprint=fingerprint,
            item_type=result.item.item_type,
            item_id=result.item.item_id,
            item_name=result.item.name("cn"),
            started_at=stamp,
            finished_at=stamp,
            matched=len(snapshots),
            requests=int(result.requests),
            complete=bool(result.complete),
        )
        for sub in eligible:
            try:
                self._record_subscription(sub, result, snapshots, run_id, stamp)
            except Exception:  # noqa: BLE001 — 分析失败不能影响推送
                logger.exception("分析记录失败：订阅 #%s", sub.id)
        return len(eligible)

    # ── 内部 ────────────────────────────────────────────────

    def _record_subscription(
        self,
        sub: Subscription,
        result: FetchResult,
        snapshots: list[ListingSnapshot],
        run_id: int,
        now: int,
    ) -> None:
        item_id = result.item.item_id
        item_name = result.item.name("cn")
        current_ids = {snap.n8_id for snap in snapshots}
        existing = {
            str(row["n8_id"]): row
            for row in self.store.list_sub_listings(sub.id, item_id)
        }

        candidates: list[tuple[ListingSnapshot, int]] = []
        for snapshot in snapshots:
            row = existing.get(snapshot.n8_id)
            new_listing = row is None
            if new_listing:
                self.store.insert_sub_listing(
                    sub.id, sub.qq, result.item, snapshot, run_id, now
                )
                first_seen = now
            else:
                disappeared = str(row["state"]) == "disappeared"
                if (
                    not disappeared
                    and snapshot.identity_key
                    and str(row["identity_key"] or "") != snapshot.identity_key
                ):
                    logger.warning(
                        "同一挂单属性发生变化：%s（订阅 #%s）", snapshot.n8_id, sub.id
                    )
                self.store.mark_sub_listing_seen(
                    sub.id, item_id, snapshot.n8_id, snapshot, run_id, now,
                    reappeared=disappeared,
                )
                first_seen = int(row["first_seen_at"])
            self.store.upsert_item_seller(
                item_id,
                snapshot.identity_key,
                item_name,
                snapshot.seller_key,
                snapshot.seller,
                now=now,
                price=snapshot.price,
                quality=snapshot.quality,
                new_listing=new_listing,
            )
            # 消失与上架可能发生在同一轮，导狗判定放到消失写入之后
            candidates.append((snapshot, first_seen))

        if not result.complete:
            return

        for row in self.store.list_active_sub_listings(sub.id, item_id):
            if str(row["n8_id"]) in current_ids:
                continue
            self.store.mark_sub_listing_disappeared(
                row,
                item_name,
                run_id,
                now,
                json.dumps(sub.spec, ensure_ascii=False),
            )

        for snapshot, first_seen in candidates:
            self._maybe_record_flip(item_id, item_name, snapshot, first_seen, now)

    def _maybe_record_flip(
        self,
        item_id: str,
        item_name: str,
        snapshot: ListingSnapshot,
        first_seen: int,
        now: int,
    ) -> None:
        """原始卖家已消失、且新卖家上架晚于最后一次消失时，记录一条导狗倒卖。"""
        identity = snapshot.identity_key
        if not identity or not snapshot.seller_key:
            return
        original = self.store.get_original_seller(item_id, identity)
        if original is None:
            return
        original_key = str(original["seller_key"] or "")
        if not original_key or original_key == snapshot.seller_key:
            return

        original_last = self.store.original_last_disappeared(
            item_id, identity, original_key, first_seen
        )
        if original_last is None:
            return
        prev = self.store.latest_disappeared(
            item_id, identity, first_seen, snapshot.seller_key
        )
        if prev is None:
            return
        prev_at = int(prev["disappeared_at"])
        if first_seen <= prev_at:
            return

        prev_key = str(prev["seller_key"] or "") or seller_key(str(prev["seller"] or ""))
        prev_name = str(prev["seller"] or "")
        self.store.insert_seller_flip(
            {
                "item_id": item_id,
                "item_name": item_name,
                "identity_key": identity,
                "original_seller_key": original_key,
                "original_seller": str(original["seller_name"] or ""),
                "prev_seller_key": prev_key,
                "prev_seller": prev_name,
                "reseller_key": snapshot.seller_key,
                "reseller": snapshot.seller,
                "source_disappeared_id": int(prev["id"]),
                "source_disappeared_at": prev_at,
                "reseller_first_seen_at": first_seen,
                "gap_sec": first_seen - prev_at,
                "source_price": _to_float(prev["price"]),
                "reseller_price": snapshot.price,
                "quality": snapshot.quality,
                "identity_json": snapshot.identity_json,
                "reseller_listing_key": snapshot.n8_id,
                "reseller_last_seen_at": now,
                "reseller_disappeared_at": None,
                "verified": 1,
                "created_at": now,
            }
        )
