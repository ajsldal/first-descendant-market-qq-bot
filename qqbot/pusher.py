"""结果格式化与消息推送。

推送语义为「每轮全量」：命中的挂单全部列出，`[新]` 只作为标记，
不代表只推新。超过上限时列前 N 条并给出剩余数量。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .card import RENDER_DIR, STYLES, render_card
from .dsl import ITEM_TYPE_LABELS
from .market import FetchResult
from .napcat_client import NapCatClient, image, text
from .store import Subscription

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
MAX_CHARS = 1500


def _fmt_price(raw: dict[str, Any]) -> str:
    try:
        price = float(raw.get("n4SalePrice") or 0)
    except (TypeError, ValueError):
        return "价格?"
    return f"{price:,.0f}"


def _fmt_time(raw: dict[str, Any]) -> str:
    value = str(raw.get("dtRegister") or raw.get("register_time_utc") or "")
    if not value:
        return ""
    try:
        # Python 3.10 的 fromisoformat 只接受 3/6 位小数，服务端会返回 .72 这种
        normalized = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], value)
        normalized = normalized.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CST).strftime("%m-%d %H:%M")
    except ValueError:
        return value[:16]


def _fmt_seller(raw: dict[str, Any]) -> str:
    seller = raw.get("seller") or {}
    name = str(seller.get("strNickName") or "")
    return name[:22]


def _short_desc(desc: str) -> str:
    """把 '技能冷却时间 [-16.1~-4.3]%' 压成 '技能冷却时间'。"""
    first = desc.split("\t")[0]
    first = re.split(r"\s*[\[［]", first)[0]
    return first.strip()[:18]


def _fmt_detail(raw: dict[str, Any], item_type: str) -> str:
    data = raw.get("data") or {}
    parts: list[str] = []

    if item_type == "transcendent":
        quality = data.get("n4WeaponQuality")
        if quality is not None:
            parts.append(f"品质{quality}")
        subperks = [
            str(sp.get("strName") or "") for sp in (data.get("subperks") or [])
            if sp.get("strName")
        ]
        if subperks:
            parts.append("/".join(subperks[:2]))
    elif item_type in ("ancestor", "descendant"):
        options = [
            opt for opt in (data.get("options") or [])
            if str(opt.get("strSlot") or "").lower() == "advantage"
        ]
        if options:
            shown = []
            for opt in options[:3]:
                name = _short_desc(str(opt.get("strDesc") or ""))
                tier = str(opt.get("strTier") or "")
                shown.append(f"{name} {tier}".strip())
            parts.append("/".join(shown))
        socket_name = data.get("strSocketName")
        if socket_name:
            parts.append(f"宝石{socket_name}")

    return " | ".join(p for p in parts if p)


def format_item_summary(raw: dict[str, Any], item_type: str) -> str:
    """一行商品摘要：价格 | 明细 | 卖家 | 上架时间（不含 [新] 标记）。"""
    bits = [f"{_fmt_price(raw)} 卡利弗"]
    detail = _fmt_detail(raw, item_type)
    if detail:
        bits.append(detail)
    seller = _fmt_seller(raw)
    if seller:
        bits.append(f"卖家 {seller}")
    stamp = _fmt_time(raw)
    if stamp:
        bits.append(stamp)
    return " | ".join(bits)


def _fmt_item(raw: dict[str, Any], item_type: str, is_new: bool) -> str:
    line = "· " + format_item_summary(raw, item_type)
    return ("[新] " + line) if is_new else line


def format_result(
    sub: Subscription,
    result: FetchResult,
    new_ids: Iterable[str],
    *,
    items: list[dict[str, Any]] | None = None,
    same_as_last: bool = False,
) -> list[str]:
    """生成一条或多条消息文本（超长时自动分片）。"""
    new_set = {str(x) for x in new_ids}
    item = result.item
    matched = result.items if items is None else items
    shown = matched[: sub.max_push]

    title = item.name("cn")
    bits = [f"【{title}】{ITEM_TYPE_LABELS.get(item.item_type, item.item_type)}"]
    bits.append(f"本轮命中 {len(matched)} 条")
    if sub.only_new:
        bits.append(f"新上架 {len(new_set)} 条")
    if len(matched) > len(shown):
        bits.append(f"仅列前 {len(shown)} 条")
    header = " | ".join(bits)
    if same_as_last:
        header += "（与上轮相同）"

    lines = [header]
    for raw in shown:
        lines.append(_fmt_item(raw, item.item_type, str(raw.get("n8Id") or "") in new_set))
    if len(matched) > len(shown):
        lines.append(f"… 其余 {len(matched) - len(shown)} 条未列出")

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > MAX_CHARS:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


class Pusher:
    def __init__(
        self,
        client: NapCatClient,
        *,
        chunk_delay: float = 0.4,
        image_enabled: bool = True,
        image_style: str = "hybrid",
        render_dir: Path | None = None,
    ) -> None:
        if image_style not in STYLES:
            raise ValueError(f"未知图片版式 {image_style!r}，可选：{', '.join(STYLES)}")
        self._client = client
        self._chunk_delay = chunk_delay
        self._image_enabled = image_enabled
        self._image_style = image_style
        self._render_dir = render_dir

    async def send_private(self, qq: int, content: str) -> None:
        await asyncio.to_thread(
            self._client.send_private_msg, qq, [text(content)]
        )

    async def push(self, sub: Subscription, result: FetchResult, new_ids: Iterable[str],
                   *, items: list[dict[str, Any]] | None = None,
                   same_as_last: bool = False) -> int:
        """定时/自动推送只走私聊，绝不向群聊发送。"""
        matched = result.items if items is None else items
        if not matched:
            return 0
        new_set = {str(x) for x in new_ids}
        chunks = format_result(
            sub, result, new_ids, items=matched, same_as_last=same_as_last
        )
        shown = matched[: sub.max_push]

        if self._image_enabled:
            try:
                await self._push_image(sub, result, shown, new_set, chunks[0])
                return min(len(matched), sub.max_push)
            except Exception as exc:  # noqa: BLE001 — 图片失败必须退回文字
                logger.warning("订阅 #%s 图片推送失败，退回文字：%s", sub.id, exc)

        for index, chunk in enumerate(chunks):
            if index:
                await asyncio.sleep(self._chunk_delay)
            await self.send_private(sub.qq, chunk)
        return min(len(matched), sub.max_push)

    async def _push_image(
        self,
        sub: Subscription,
        result: FetchResult,
        shown: list[dict[str, Any]],
        new_set: set[str],
        caption: str,
    ) -> None:
        """渲染图片并与简短说明一起发送。"""
        snapshot = FetchResult(
            item=result.item,
            resolved=result.resolved,
            items=shown,
            requests=result.requests,
        )
        path = await asyncio.to_thread(
            render_card,
            snapshot,
            sub,
            new_set,
            style=self._image_style,
            out_dir=self._render_dir,
            max_rows=sub.max_push,
        )
        data = await asyncio.to_thread(path.read_bytes)
        # 说明只用 format_result 的首行（标题 + 本轮条数），明细交给图片
        header = caption.split("\n", 1)[0]
        segments = [text(header), image("base64://" + base64.b64encode(data).decode("ascii"))]
        await asyncio.to_thread(self._client.send_private_msg, sub.qq, segments)
        logger.info(
            "订阅 #%s 图片推送 %s 条（%s，%sKB）",
            sub.id,
            len(shown),
            self._image_style,
            len(data) // 1024,
        )

    @staticmethod
    def prune_render(max_entries: int = 200, max_age: int = 86400) -> int:
        """清理渲染缓存，避免 data/render 长期运行膨胀。"""
        directory = RENDER_DIR
        if not directory.exists():
            return 0
        try:
            files = sorted(
                (p for p in directory.glob("*.png") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return 0
        now = time.time()
        removed = 0
        for index, path in enumerate(files):
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if age > max_age or (index >= max_entries and age > 3600):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed
