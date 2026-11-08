"""把一轮市场结果渲染成商品图片（Pillow）。

支持三种版式，用 ``style`` 选择：

- ``official``：仿官网详情页。每条挂单一张白色信息卡，物品图 + 名称/价格，
  下面按「物品信息 / 辅助能力 / 超越能力」分区，数值右对齐并用颜色区分能力组。
- ``compact``：深色紧凑列表。一条一行，价格与三组能力点数用大号字突出。
- ``hybrid``：深色官网风头部 + 中等密度条目，兼顾观感与一屏信息量。

物品图来自接口里的 ``strBinaryUrl``，按 URL 哈希缓存到 data/assets/items/；
数据库不存图片，只存文字和订阅；同一批结果按内容哈希缓存渲染图。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .assets import icon_path
from .dsl import ITEM_TYPE_LABELS, describe_spec
from .market import FetchResult
from .store import Subscription

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
RENDER_DIR = ROOT / "data" / "render"
UI_DIR = ROOT / "data" / "assets" / "ui"

CST = timezone(timedelta(hours=8))
STYLES = ("official", "compact", "hybrid")
STYLE_LABELS = {"official": "官网风", "compact": "紧凑列表", "hybrid": "混合"}

# ── 浅色（官网）配色 ──────────────────────────────────────
PAGE_BG = (242, 243, 245)
CARD_BG = (255, 255, 255)
CARD_BORDER = (228, 231, 236)
INK = (17, 19, 24)
INK_SOFT = (58, 65, 76)
MUTED = (118, 126, 138)
FAINT = (158, 165, 177)
ORANGE = (226, 129, 13)
BLUE = (47, 125, 225)
GREEN = (29, 155, 80)
RED = (224, 66, 72)
GAUGE_BG = (232, 234, 238)
GOLD = (226, 172, 44)

# 能力 1/2/3 组（n4Group 0/1/2）的配色：橙、紫、绿
GROUP_LIGHT = (ORANGE, (138, 88, 232), GREEN)
GROUP_DARK = ((242, 201, 76), (176, 122, 250), (52, 206, 124))

# 词条品级配色：T1 蓝、T2 绿、T3 紫、T4 金
TIER_LIGHT = (BLUE, GREEN, (138, 88, 232), GOLD)
TIER_DARK = ((120, 170, 255), (52, 206, 124), (176, 122, 250), (242, 201, 76))
RED_SOFT = (232, 96, 96)
CYAN = (96, 200, 214)

# ── 深色（紧凑）配色 ──────────────────────────────────────
DARK_BG = (11, 14, 19)
DARK_PANEL = (21, 26, 33)
DARK_PANEL_ALT = (17, 21, 27)
DARK_BORDER = (38, 45, 57)
DARK_TEXT = (232, 235, 241)
DARK_MUTED = (143, 152, 167)
DARK_FAINT = (108, 117, 132)
DARK_BAR = (44, 51, 63)
VALUE_ORANGE = (233, 166, 63)

FONT_REGULAR = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REGULAR
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", size)


def _truncate(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    text = str(text or "")
    if max_width <= 0:
        return ""
    if draw.textlength(text, font=font) <= max_width:
        return text
    text = text.rstrip()
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _put(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    text: Any,
    font,
    fill,
    *,
    right: float | None = None,
    max_width: int | None = None,
) -> float:
    """写一行文字；right 表示右对齐边界。返回文字实际宽度。"""
    text = str(text)
    if max_width is not None:
        text = _truncate(draw, text, font, max_width)
    width = draw.textlength(text, font=font)
    if right is not None:
        x = right - width
    draw.text((x, y), text, font=font, fill=fill)
    return width


def _center_text(
    draw: ImageDraw.ImageDraw, cx: float, y: float, text: str, font, fill
) -> None:
    width = draw.textlength(text, font=font)
    draw.text((cx - width / 2, y), text, font=font, fill=fill)


def _blend(color, bg, alpha: float):
    """把 color 以 alpha 混到 bg 上。"""
    return tuple(int(c * alpha + b * (1 - alpha)) for c, b in zip(color, bg))


def _number_badge(
    draw: ImageDraw.ImageDraw, x: int, y: int, number: int
) -> None:
    """在列表左侧画序号徽标，用于“回复序号选择商品”。"""
    width, height = 26, 26
    draw.rounded_rectangle(
        (x, y, x + width, y + height), radius=7, fill=GOLD
    )
    _center_text(
        draw, x + width / 2, y + 4, str(number), _font(12, True), (20, 15, 5)
    )


def _parse_time(value: str) -> str:
    if not value:
        return ""
    try:
        normalized = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], value)
        dt = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CST).strftime("%m-%d %H:%M")
    except ValueError:
        return value[:16]


def _price(raw: dict[str, Any]) -> float:
    try:
        return float(raw.get("n4SalePrice") or 0)
    except (TypeError, ValueError):
        return 0.0


def _seller(raw: dict[str, Any]) -> str:
    return str((raw.get("seller") or {}).get("strNickName") or "")


def _rank(raw: dict[str, Any]) -> int | None:
    try:
        value = int((raw.get("seller") or {}).get("n4MasteryRank"))
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def _clean_value(value: str) -> str:
    value = re.sub(r"\s*\(\d+\s*pt\)\s*$", "", (value or "").strip(), flags=re.IGNORECASE)
    return value.replace("ｘ", "×").strip()


def _split_desc(desc: str) -> tuple[str, str, str]:
    """``连环爆炸\\n范围\\t半径14.44米 (143pt)`` → (标题, 标签, 数值)。"""
    text = (desc or "").replace("\r", "")
    head, sep, tail = text.partition("\n")
    if sep:
        label, tab, value = tail.partition("\t")
        if not tab:
            label, value = "", tail
        return head.strip(), label.strip(), _clean_value(value)
    if "\t" in head:
        label, _, value = head.partition("\t")
        return "", label.strip(), _clean_value(value)
    return head.strip(), "", ""


_TIER_DIGITS = re.compile(r"\d+")


def _tier_short(tier: str) -> str:
    """``Tier2`` → ``T2``；无法识别时原样返回。"""
    match = _TIER_DIGITS.search(str(tier or ""))
    return f"T{match.group()}" if match else str(tier or "")


def _tier_index(tier: str) -> int:
    match = _TIER_DIGITS.search(str(tier or ""))
    return int(match.group()) if match else 0


def _best_tier(rows: Iterable[Any]) -> str:
    values = [row.tier for row in rows if row.tier]
    if not values:
        return ""
    return max(values, key=_tier_index)


def _roll_text(raw_roll: str) -> str:
    """``22.8%[+]`` → ``22.8%+``，``95.9%[x]`` → ``95.9%×``。"""
    text = (raw_roll or "").strip()
    return text.replace("[+]", "+").replace("[x]", "×").replace("[-]", "-")


def _roll_join(tail: str) -> str:
    """同一词条的两档数值（接口用字面量 ``\\s`` 分隔）用 ``/`` 并列。"""
    parts = [part for part in re.split(r"\\s", tail or "") if part.strip()]
    if not parts:
        return ""
    return " / ".join(_roll_text(part) for part in parts)


def _range_text(raw_range: str) -> str:
    text = (raw_range or "").strip()
    return re.sub(
        r"^(-?\d+(?:\.\d+)?)~(-?\d+(?:\.\d+)?)$", r"\1 ~ \2", text
    )


def _module_affix(desc: str, tier: str) -> _Affix:
    """``技能暴击倍率 [46.1~174.1]%\\t95.9%[x]\\s101.3%[x]`` → 增益词条。"""
    head, _, tail = (desc or "").partition("\t")
    label = re.split(r"\s*[\[［]", head)[0].strip()
    match = re.search(r"[\[［]([^\]］]+)[\]］]", head)
    return _Affix(
        tier=str(tier or "").strip(),
        label=label,
        value=_roll_join(tail),
        range_text=_range_text(match.group(1) if match else ""),
    )


def _trigger_effect(desc: str, tier: str) -> _TriggerEffect:
    """``4号技能应用冷却时间减少率(25.0 ~ 100.0)\\t96%`` → 触发器效果。"""
    # 触发器接口把分隔符写成字面量 ``\t``，不是真正的制表符
    parts = re.split(r"\t|\\t", desc or "", maxsplit=1)
    head = parts[0]
    value = parts[1].strip() if len(parts) > 1 else ""
    match = re.match(r"^(.*?)(?:\(([^()]*)\))?$", head.strip())
    label = match.group(1).strip() if match else head.strip()
    return _TriggerEffect(
        tier=str(tier or "").strip(),
        label=label,
        value=value,
        range_text=_range_text(match.group(2) if match else ""),
    )


# ── 商品结构 ──────────────────────────────────────────────


@dataclass
class _SubPerk:
    name: str
    fixed: bool
    options: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class _Option:
    title: str
    label: str
    value: str


@dataclass
class _Group:
    index: int
    point: int
    title: str
    options: list[_Option] = field(default_factory=list)


@dataclass
class _Affix:
    """模块增益/减益词条：品级 + 名称 + 两档数值。"""

    tier: str
    label: str
    value: str
    range_text: str = ""


@dataclass
class _TriggerEffect:
    """触发器效果：品级 + 名称 + 数值（可能带取值范围）。"""

    tier: str
    label: str
    value: str
    range_text: str = ""


@dataclass
class _Listing:
    price: float
    seller: str
    rank: int | None
    time_text: str
    is_new: bool
    quality: int | None
    class_name: str
    bullet_name: str
    perk_name: str
    subperks: list[_SubPerk]
    groups: list[_Group]
    total_pt: int
    listing_id: str
    advantages: list[_Affix] = field(default_factory=list)
    penalties: list[_Affix] = field(default_factory=list)
    socket_name: str = ""
    trigger_effects: list[_TriggerEffect] = field(default_factory=list)
    index: int = 0


def _build_listing(
    raw: dict[str, Any], new_set: set[str], item_type: str, index: int = 0
) -> _Listing:
    data = raw.get("data") or {}
    subperks: list[_SubPerk] = []
    for sp in data.get("subperks") or []:
        name = str(sp.get("strName") or "").strip()
        if not name:
            continue
        options = []
        for opt in sp.get("options") or []:
            _, label, value = _split_desc(str(opt.get("strDesc") or ""))
            if label or value:
                options.append((label or "效果", value))
        subperks.append(_SubPerk(name=name, fixed=bool(sp.get("isFixed")), options=options))

    groups: dict[int, _Group] = {}
    advantages: list[_Affix] = []
    penalties: list[_Affix] = []
    trigger_effects: list[_TriggerEffect] = []
    for opt in data.get("options") or []:
        desc = str(opt.get("strDesc") or "")
        tier = str(opt.get("strTier") or "")
        slot = str(opt.get("strSlot") or "").strip().lower()
        if slot:
            affix = _module_affix(desc, tier)
            if not affix.label:
                continue
            (penalties if slot == "penalty" else advantages).append(affix)
            continue
        if item_type == "trigger":
            trigger_effects.append(_trigger_effect(desc, tier))
            continue
        try:
            point = int(opt.get("n4Point") or 0)
            group_index = int(opt.get("n4Group") or 0)
        except (TypeError, ValueError):
            continue
        title, label, value = _split_desc(desc)
        group = groups.setdefault(
            group_index, _Group(index=group_index, point=0, title=title)
        )
        group.point = max(group.point, point)
        if not group.title:
            group.title = title
        group.options.append(_Option(title=title, label=label, value=value))

    ordered = [groups[k] for k in sorted(groups)]
    total_pt = sum(g.point for g in ordered)
    quality = data.get("n4WeaponQuality")
    try:
        quality = int(quality) if quality is not None else None
    except (TypeError, ValueError):
        quality = None

    return _Listing(
        price=_price(raw),
        seller=_seller(raw),
        rank=_rank(raw),
        time_text=_parse_time(str(raw.get("dtRegister") or "")),
        is_new=str(raw.get("n8Id") or "") in new_set,
        quality=quality,
        class_name=str(data.get("strClassName") or ""),
        bullet_name=str(data.get("strBulletName") or ""),
        perk_name=str(data.get("strPerkName") or ""),
        subperks=subperks,
        groups=ordered,
        total_pt=total_pt,
        listing_id=str(raw.get("n8Id") or ""),
        advantages=advantages,
        penalties=penalties,
        socket_name=str(data.get("strSocketName") or "").strip(),
        trigger_effects=trigger_effects,
        index=index,
    )


# ── 物品图与背景 ──────────────────────────────────────────

_BG_CACHE: dict[tuple[str, tuple[int, int]], Image.Image] = {}


def _load_ui(name: str) -> Image.Image | None:
    path = UI_DIR / name
    if not path.exists():
        return None
    try:
        return Image.open(path).convert("RGBA")
    except OSError:
        return None


def _tile_bg(item_type: str, size: tuple[int, int]) -> Image.Image:
    key = (item_type, size)
    cached = _BG_CACHE.get(key)
    if cached is not None:
        return cached.copy()
    width, height = size
    base: Image.Image | None = None
    if item_type == "transcendent":
        raw = _load_ui("bg--transcend.png")
        if raw is not None:
            base = ImageOps.fit(raw, size, Image.LANCZOS)
    if base is None:
        colors = {
            "ancestor": ((18, 26, 34), (9, 13, 18)),
            "descendant": ((20, 22, 40), (10, 11, 20)),
            "trigger": ((28, 22, 32), (12, 10, 15)),
        }.get(item_type, ((18, 22, 28), (9, 11, 15)))
        base = Image.new("RGBA", size)
        draw = ImageDraw.Draw(base)
        for y in range(height):
            t = y / max(1, height - 1)
            draw.line(
                (0, y, width, y),
                fill=tuple(int(a + (b - a) * t) for a, b in zip(colors[0], colors[1])) + (255,),
            )
    _BG_CACHE[key] = base
    return base.copy()


def _load_icon(
    url: str | None, item_id: str, item_type: str = "", name: str = ""
) -> Image.Image | None:
    path = icon_path(item_id, url, item_type=item_type, name=name)
    if path is None:
        return None
    try:
        return Image.open(path).convert("RGBA")
    except OSError:
        return None


def _tile(
    item: Any,
    icon_url: str | None,
    size: tuple[int, int] = (132, 178),
    *,
    label: bool = True,
) -> Image.Image:
    """官网风格的物品图块：渐变底 + 武器渲染图 + 底部名称。"""
    width, height = size
    tile = _tile_bg(item.item_type, size)
    icon = _load_icon(icon_url, item.item_id, item.item_type, item.name("cn"))
    if icon is not None:
        fitted = ImageOps.contain(icon, (width - 16, height - 62), Image.LANCZOS)
        x = (width - fitted.width) // 2
        y = int(height * 0.44 - fitted.height / 2)
        tile.alpha_composite(fitted, (max(0, x), max(6, y)))

    if label:
        shade = Image.new("RGBA", size, (0, 0, 0, 0))
        shade_draw = ImageDraw.Draw(shade)
        band = 54
        for i in range(band):
            alpha = int(196 * (i / max(1, band - 1)) ** 1.7)
            shade_draw.line((0, height - band + i, width, height - band + i), fill=(0, 0, 0, alpha))
        tile = Image.alpha_composite(tile, shade)

        draw = ImageDraw.Draw(tile)
        text = _truncate(draw, item.name("cn"), _font(11, True), width - 16)
        draw.text((8, height - 21), text, font=_font(11, True), fill=(255, 255, 255, 236))

    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=5, fill=255)
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    out.paste(tile, (0, 0), mask)
    ImageDraw.Draw(out).rounded_rectangle(
        (0, 0, width - 1, height - 1), radius=5, outline=(255, 255, 255, 46), width=1
    )
    return out


# ── 公共数据 ──────────────────────────────────────────────


def _filter_text(sub: Subscription) -> str:
    try:
        from market_v2.filters import FilterSpec

        return describe_spec(FilterSpec.from_dict(sub.spec))
    except Exception:  # noqa: BLE001 — 历史数据损坏也要能出图
        return ""


def _quality_text(listing: _Listing) -> str:
    return str(listing.quality) if listing.quality is not None else "-"


def _weapon_text(listing: _Listing) -> str:
    parts = [x for x in (listing.class_name, listing.bullet_name) if x]
    return " · ".join(parts)


def _short_value(value: str) -> str:
    return value.replace("枪械伤害量", "伤害").replace("额外攻击力", "攻击力")


def _perk_summary(listing: _Listing, *, with_value: bool = True, limit: int = 3) -> str:
    parts: list[str] = []
    for perk in listing.subperks[:limit]:
        text = perk.name
        if with_value and perk.options:
            text += f" {perk.options[0][1]}"
        parts.append(text)
    return " · ".join(parts)


def _perk_detail(listing: _Listing, limit: int = 3) -> list[tuple[str, list[tuple[str, str]]]]:
    """(词条名, [(属性名, 数值)])，用于混合版展示具体数值。"""
    rows: list[tuple[str, list[tuple[str, str]]]] = []
    for perk in listing.subperks[:limit]:
        name = perk.name + ("（固定）" if perk.fixed else "")
        options = [(_short_label(label), value) for label, value in perk.options[:2]]
        rows.append((name, options))
    return rows


_LABEL_SHORT = (
    ("对巨魔攻击时获得额外攻击力", "对巨魔额外攻击力"),
    ("属性状态效果的触发概率", "触发概率"),
    ("枪械暴击倍率", "暴击倍率"),
    ("枪械暴击率", "暴击率"),
    ("枪械攻击力", "攻击力"),
)


def _short_label(label: str) -> str:
    for full, short in _LABEL_SHORT:
        if label == full:
            return short
    return label.replace("枪械", "") or label


def _group_lines(group: _Group) -> list[tuple[str, str, str]]:
    """(类型, 左文, 右值)：head/subhead/row。"""
    lines: list[tuple[str, str, str]] = [("head", group.title, f"{group.point}pt")]
    seen = {group.title}
    for opt in group.options:
        if opt.title and opt.title not in seen:
            lines.append(("subhead", opt.title, ""))
            seen.add(opt.title)
        lines.append(("row", opt.label or opt.title, opt.value))
    return lines


def _perk_lines(perk: _SubPerk) -> list[tuple[str, str, str]]:
    head = perk.name + ("（固定）" if perk.fixed else "")
    lines: list[tuple[str, str, str]] = [("head", head, "")]
    for label, value in perk.options:
        lines.append(("row", label, value))
    return lines


def _tier_color(tier: str, *, dark: bool) -> tuple[int, int, int]:
    index = _tier_index(tier)
    if index <= 0:
        return DARK_MUTED if dark else MUTED
    palette = TIER_DARK if dark else TIER_LIGHT
    return palette[min(index, 4) - 1]


def _tier_badge(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    tier: str,
    *,
    dark: bool = True,
    text: str | None = None,
    color: tuple[int, int, int] | None = None,
) -> int:
    """画品级小徽标（T1~T4，或减益用「减」）。"""
    color = color or _tier_color(tier, dark=dark)
    bg = DARK_BG if dark else CARD_BG
    width, height = 26, 18
    draw.rounded_rectangle(
        (x, y, x + width, y + height),
        radius=5,
        fill=_blend(color, bg, 0.22),
        outline=_blend(color, bg, 0.6),
        width=1,
    )
    _center_text(
        draw, x + width / 2, y + 2,
        text if text is not None else _tier_short(tier),
        _font(10, True), color,
    )
    return width


def _chip(
    draw: ImageDraw.ImageDraw,
    right: int,
    y: int,
    text: str,
    color: tuple[int, int, int],
    *,
    dark: bool = True,
    height: int = 24,
) -> int:
    """右上角圆角标签（宝石等）。"""
    font = _font(11, True)
    width = int(draw.textlength(text, font=font)) + 20
    bg = DARK_BG if dark else CARD_BG
    draw.rounded_rectangle(
        (right - width, y, right, y + height),
        radius=8,
        fill=_blend(color, bg, 0.16),
        outline=_blend(color, bg, 0.55),
        width=1,
    )
    _center_text(draw, right - width / 2, y + (height - 16) / 2, text, font, color)
    return width


def _affix_value(row: _Affix) -> str:
    return row.value or row.range_text


def _trigger_value(row: _TriggerEffect) -> str:
    return row.value or row.range_text


def _render_key(
    sub: Subscription,
    result: FetchResult,
    new_set: set[str],
    rows,
    style: str,
    limit: int,
    numbering: bool = False,
) -> str:
    ids = [str(raw.get("n8Id") or "") for raw in rows]
    raw = "|".join(
        [
            "card-v9",
            style,
            result.item.item_id,
            sub.fingerprint,
            ",".join(ids),
            ",".join(sorted(new_set)),
            str(limit),
            "n" if numbering else "",
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


# ── 版式 A：官网风 ────────────────────────────────────────


def _official_content(
    listing: _Listing,
    item: Any,
    tile: Image.Image,
    title_font_size: int = 28,
) -> Image.Image:
    width = 900 - 52
    canvas = Image.new("RGBA", (width, 1700), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    x0, right = 18, width - 18
    y = 20

    canvas.paste(tile, (x0, y), tile)
    tx = x0 + tile.width + 22

    _put(draw, tx, y + 2, f"添加于 {listing.time_text}", _font(13), MUTED, max_width=right - tx)
    _put(draw, tx, y + 24, item.name("cn"), _font(title_font_size, True), INK, max_width=right - tx - 60)

    type_label = ITEM_TYPE_LABELS.get(item.item_type, item.item_type)
    meta = type_label + (f" · 等级 {listing.rank}" if listing.rank else "")
    _put(draw, tx, y + 68, meta, _font(14), INK_SOFT, max_width=right - tx)

    price_text = f"{listing.price:,.0f}"
    price_width = _put(draw, tx, y + 92, price_text, _font(32, True), INK)
    _put(draw, tx + price_width + 8, y + 110, "卡利弗", _font(14), MUTED)
    if listing.seller:
        _put(draw, tx, y + 144, listing.seller, _font(13), MUTED, max_width=right - tx)
    if listing.listing_id:
        _put(draw, tx, y + 163, f"挂单 {listing.listing_id}", _font(11), FAINT, max_width=right - tx)

    if listing.is_new:
        badge_w, badge_h = 44, 24
        bx = right - badge_w
        draw.rounded_rectangle((bx, y + 24, bx + badge_w, y + 24 + badge_h), radius=12, fill=RED)
        _center_text(draw, bx + badge_w / 2, y + 28, "新", _font(12, True), (255, 255, 255))

    y += tile.height + 18

    if item.item_type == "transcendent":
        _put(draw, x0, y, "物品信息", _font(17, True), INK)
        y += 27
        _put(draw, x0, y + 3, "品质", _font(14), MUTED)
        _put(draw, x0 + 82, y - 5, _quality_text(listing), _font(23, True), INK)
        if listing.quality is not None:
            bar_x, bar_w = x0 + 160, right - x0 - 160
            bar_y = y + 12
            draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + 8), radius=4, fill=GAUGE_BG)
            filled = max(8, int(bar_w * min(max(listing.quality / 400, 0), 1)))
            draw.rounded_rectangle((bar_x, bar_y, bar_x + filled, bar_y + 8), radius=4, fill=GOLD)
        y += 34

        weapon = _weapon_text(listing) or "-"
        _put(draw, x0, y + 1, "枪械信息", _font(14), MUTED)
        _put(draw, x0 + 82, y - 1, weapon, _font(15), INK_SOFT, max_width=right - x0 - 82)
        y += 30
    elif listing.trigger_effects:
        _put(draw, x0, y, "触发器效果", _font(17, True), INK)
        y += 27
        for effect in listing.trigger_effects[:4]:
            _tier_badge(draw, x0 + 4, y + 1, effect.tier, dark=False)
            label_width = _put(
                draw, x0 + 38, y,
                effect.label, _font(14), INK_SOFT,
                max_width=right - x0 - 38 - 210,
            )
            if effect.range_text:
                _put(
                    draw, x0 + 38 + label_width + 8, y + 1,
                    f"({effect.range_text})", _font(12), FAINT,
                    max_width=right - x0 - 38 - label_width - 100,
                )
            value = _trigger_value(effect)
            if value:
                _put(draw, right, y, value, _font(15, True), ORANGE, right=right)
            y += 26
        y += 4
    elif listing.advantages or listing.penalties:
        _put(draw, x0, y, "模块属性", _font(17, True), INK)
        y += 27

        best = _best_tier(listing.advantages)
        _put(draw, x0 + 4, y + 3, "品质", _font(14), MUTED)
        if best:
            _put(
                draw, x0 + 82, y - 5, _tier_short(best), _font(23, True),
                _tier_color(best, dark=False),
            )
        if listing.socket_name:
            _chip(draw, right, y - 4, f"宝石 · {listing.socket_name}", CYAN, dark=False)
        y += 34

        for idx, affix in enumerate(listing.advantages[:4]):
            if idx == 0:
                _put(draw, x0 + 4, y + 3, "增益", _font(14), MUTED)
            _tier_badge(draw, x0 + 50, y + 2, affix.tier, dark=False)
            _put(
                draw, x0 + 84, y,
                affix.label, _font(14), INK_SOFT,
                max_width=right - x0 - 84 - 210,
            )
            value = _affix_value(affix)
            if value:
                _put(draw, right, y, value, _font(14, True), ORANGE, right=right)
            y += 24

        for idx, affix in enumerate(listing.penalties[:2]):
            if idx == 0:
                _put(draw, x0 + 4, y + 3, "减益", _font(14), RED)
            _tier_badge(draw, x0 + 50, y + 2, "", dark=False, text="减", color=RED_SOFT)
            _put(
                draw, x0 + 84, y,
                affix.label, _font(14), INK_SOFT,
                max_width=right - x0 - 84 - 210,
            )
            value = _affix_value(affix)
            if value:
                _put(draw, right, y, value, _font(14, True), RED, right=right)
            y += 24
        y += 4

    if listing.subperks:
        draw.line((x0, y + 6, right, y + 6), fill=CARD_BORDER, width=1)
        y += 18
        _put(draw, x0, y, "辅助能力", _font(17, True), INK)
        y += 27
        for perk in listing.subperks:
            lines = _perk_lines(perk)
            for kind, left, value in lines:
                if kind == "head":
                    _put(draw, x0, y, left, _font(15, True), INK)
                    y += 25
                else:
                    _put(draw, x0 + 16, y, left, _font(14), INK_SOFT, max_width=right - x0 - 250)
                    if value:
                        _put(draw, right, y, value, _font(14, True), ORANGE, right=right)
                    y += 23

    if listing.groups:
        draw.line((x0, y + 6, right, y + 6), fill=CARD_BORDER, width=1)
        y += 18
        _put(draw, x0, y, "超越能力", _font(17, True), INK)
        y += 27
        if listing.perk_name:
            _put(draw, x0, y, listing.perk_name, _font(15, True), INK)
        if listing.total_pt:
            _put(draw, right, y, f"共 {listing.total_pt}pt", _font(13), MUTED, right=right)
        y += 26

        bar_x, bar_w = x0, right - x0
        bar_h = 10
        draw.rounded_rectangle((bar_x, y, bar_x + bar_w, y + bar_h), radius=5, fill=GAUGE_BG)
        if listing.total_pt > 0:
            seg_x = bar_x
            for group in listing.groups:
                seg_w = int(bar_w * group.point / listing.total_pt)
                if seg_w > 0:
                    color = GROUP_LIGHT[group.index % len(GROUP_LIGHT)]
                    draw.rounded_rectangle(
                        (seg_x, y, seg_x + max(4, seg_w), y + bar_h), radius=5, fill=color
                    )
                    seg_x += seg_w
        y += bar_h + 20

        for group in listing.groups:
            color = GROUP_LIGHT[group.index % len(GROUP_LIGHT)]
            for kind, left, value in _group_lines(group):
                if kind == "head":
                    _put(draw, x0, y, left, _font(15, True), color)
                    _put(draw, right, y, value, _font(15, True), color, right=right)
                    y += 26
                elif kind == "subhead":
                    _put(draw, x0 + 4, y, left, _font(13, True), color)
                    y += 23
                else:
                    _put(draw, x0 + 16, y, left, _font(14), INK_SOFT, max_width=right - x0 - 260)
                    if value:
                        _put(draw, right, y, value, _font(15, True), color, right=right)
                    y += 23
            y += 10

    return canvas.crop((0, 0, width, min(1700, y + 12)))


def _render_official(
    result: FetchResult,
    sub: Subscription,
    rows: list[dict[str, Any]],
    new_set: set[str],
    target: Path,
    numbering: bool = False,
) -> Image.Image:
    width, pad, header_h, footer_h = 900, 26, 214, 42
    item = result.item
    icon_url = next((str(r.get("strBinaryUrl")) for r in rows if r.get("strBinaryUrl")), None)
    big_tile = _tile(item, icon_url, (118, 158))
    tile = _tile(item, icon_url, (140, 188))

    listings = [
        _build_listing(
            raw, new_set, item.item_type, (i + 1) if numbering else 0
        )
        for i, raw in enumerate(rows)
    ]
    contents = [_official_content(listing, item, tile) for listing in listings]
    panel_gap = 16
    total = header_h + sum(img.height + 28 + panel_gap for img in contents) - panel_gap + footer_h

    page = Image.new("RGB", (width, total), PAGE_BG)
    draw = ImageDraw.Draw(page)
    draw.rectangle((0, 0, width, 4), fill=GOLD)

    page.paste(big_tile, (pad, 26), big_tile)
    tx = pad + big_tile.width + 24
    _put(draw, tx, 32, item.name("cn"), _font(34, True), INK, max_width=width - pad - tx)
    type_label = ITEM_TYPE_LABELS.get(item.item_type, item.item_type)
    _put(draw, tx, 80, f"{type_label} · ID {item.item_id}", _font(13), MUTED)
    filter_text = _filter_text(sub) or "无附加筛选"
    _put(draw, tx, 104, f"筛选：{filter_text}", _font(13), INK_SOFT, max_width=width - pad - tx)

    new_count = sum(1 for listing in listings if listing.is_new)
    count_text = f"共 {len(result.items)} 条"
    if len(result.items) > len(rows):
        count_text += f" · 仅列前 {len(rows)} 条"
    if new_count:
        count_text += f" · 新 {new_count} 条"
    _put(draw, tx, 128, count_text, _font(14, True), ORANGE if new_count else INK_SOFT)

    py = header_h
    for index, content in enumerate(contents, start=1):
        panel_bottom = py + content.height + 28
        draw.rounded_rectangle(
            (pad, py, width - pad, panel_bottom),
            radius=10,
            fill=CARD_BG,
            outline=CARD_BORDER,
            width=1,
        )
        page.paste(content, (pad, py + 14), content)
        if numbering:
            _number_badge(draw, 1, py + 16, index)
        py = panel_bottom + panel_gap

    _put(
        draw,
        pad,
        total - footer_h + 10,
        f"TFD 市场 · 数据来自 Nexon 公开接口 · 生成 {datetime.now(CST).strftime('%m-%d %H:%M')}",
        _font(11),
        FAINT,
    )
    page.save(target, "PNG", compress_level=6)
    return page


# ── 版式 B：紧凑列表 ──────────────────────────────────────


def _draw_compact_module(
    draw: ImageDraw.ImageDraw,
    listing: _Listing,
    mx: int,
    right: int,
    top: int,
) -> None:
    best = _best_tier(listing.advantages)
    _put(draw, mx, top + 12, "品质", _font(11), DARK_MUTED)
    _put(draw, mx + 34, top + 6, _tier_short(best) or "-",
         _font(19, True), _tier_color(best, dark=True))
    if listing.socket_name:
        _chip(draw, right, top + 8, f"宝石 · {listing.socket_name}", CYAN)
    draw.line((mx, top + 34, right, top + 34), fill=DARK_BORDER, width=1)

    y = top + 40
    for idx, affix in enumerate(listing.advantages[:3]):
        if idx == 0:
            _put(draw, mx, y + 2, "增益", _font(10), DARK_MUTED)
        _tier_badge(draw, mx + 30, y + 1, affix.tier)
        _put(
            draw, mx + 62, y,
            affix.label, _font(10), DARK_TEXT,
            max_width=right - mx - 62 - 130,
        )
        value = _affix_value(affix)
        if value:
            _put(draw, right, y, value, _font(10, True), VALUE_ORANGE, right=right)
        y += 22

    for affix in listing.penalties[:1]:
        _put(draw, mx, y + 2, "减益", _font(10), RED_SOFT)
        _tier_badge(draw, mx + 30, y + 1, "", text="减", color=RED_SOFT)
        _put(
            draw, mx + 62, y,
            affix.label, _font(10), DARK_MUTED,
            max_width=right - mx - 62 - 130,
        )
        value = _affix_value(affix)
        if value:
            _put(draw, right, y, value, _font(10, True), RED_SOFT, right=right)
        y += 22


def _draw_compact_trigger(
    draw: ImageDraw.ImageDraw,
    listing: _Listing,
    mx: int,
    right: int,
    top: int,
) -> None:
    best = _best_tier(listing.trigger_effects)
    _put(draw, mx, top + 12, "品质", _font(11), DARK_MUTED)
    _put(draw, mx + 34, top + 6, _tier_short(best) or "-",
         _font(19, True), _tier_color(best, dark=True))
    draw.line((mx, top + 34, right, top + 34), fill=DARK_BORDER, width=1)

    y = top + 40
    for effect in listing.trigger_effects[:2]:
        _tier_badge(draw, mx, y + 1, effect.tier)
        label_width = _put(
            draw, mx + 32, y,
            effect.label, _font(10), DARK_TEXT,
            max_width=right - mx - 32 - 150,
        )
        if effect.range_text:
            _put(
                draw, mx + 32 + label_width + 8, y + 1,
                f"({effect.range_text})", _font(9), DARK_FAINT,
                max_width=right - mx - 32 - label_width - 100,
            )
        value = _trigger_value(effect)
        if value:
            _put(draw, right, y - 1, value, _font(12, True), VALUE_ORANGE, right=right)
        y += 26


def _render_compact(
    result: FetchResult,
    sub: Subscription,
    rows: list[dict[str, Any]],
    new_set: set[str],
    target: Path,
    numbering: bool = False,
) -> Image.Image:
    width, pad, header_h, row_h, footer_h = 840, 24, 128, 152, 38
    total = header_h + len(rows) * row_h + footer_h
    page = Image.new("RGB", (width, total), DARK_BG)
    draw = ImageDraw.Draw(page)
    draw.rectangle((0, 0, width, 3), fill=GROUP_DARK[0])

    item = result.item
    listings = [
        _build_listing(
            raw, new_set, item.item_type, (i + 1) if numbering else 0
        )
        for i, raw in enumerate(rows)
    ]
    icon_url = next((str(r.get("strBinaryUrl")) for r in rows if r.get("strBinaryUrl")), None)
    icon = _tile(item, icon_url, (84, 84), label=False)
    page.paste(icon, (pad, 22), icon)

    tx = pad + 104
    _put(draw, tx, 26, item.name("cn"), _font(30, True), DARK_TEXT, max_width=width - pad - tx - 220)
    type_label = ITEM_TYPE_LABELS.get(item.item_type, item.item_type)
    _put(draw, tx, 70, f"{type_label} · ID {item.item_id}", _font(13), DARK_MUTED)
    _put(draw, tx, 90, f"筛选：{_filter_text(sub) or '无附加筛选'}", _font(13), DARK_MUTED, max_width=width - pad - tx - 220)

    new_count = sum(1 for listing in listings if listing.is_new)
    count_text = f"共 {len(result.items)} 条"
    if len(result.items) > len(rows):
        count_text += f" · 仅列前 {len(rows)} 条"
    if new_count:
        count_text += f" · 新 {new_count} 条"
    _put(draw, tx, 108, count_text, _font(15, True), GROUP_DARK[0] if new_count else DARK_MUTED)
    latest = listings[0].time_text if listings else ""
    if latest:
        _put(draw, width - pad, 108, f"最新 {latest}", _font(13), DARK_MUTED, right=width - pad)

    body_font = _font(12)
    caption = _font(11)
    price_font = _font(30, True)
    point_font = _font(24, True)
    chip_w, chip_h, chip_gap = 88, 58, 8
    chips_total = 3 * chip_w + 2 * chip_gap
    chips_start = width - pad - 16 - chips_total
    mid_x = pad + 216
    mid_right = chips_start - 16

    for i, listing in enumerate(listings):
        top = header_h + i * row_h + 6
        bottom = top + row_h - 8
        draw.rounded_rectangle(
            (pad, top, width - pad, bottom),
            radius=12,
            fill=DARK_PANEL if i % 2 == 0 else DARK_PANEL_ALT,
            outline=DARK_BORDER,
            width=1,
        )
        if numbering:
            _number_badge(draw, 0, top + 10, listing.index)
        if listing.is_new:
            draw.rounded_rectangle((pad, top + 12, pad + 4, bottom - 12), radius=2, fill=GROUP_DARK[1])

        price_text = f"{listing.price:,.0f}"
        price_width = _put(draw, pad + 18, top + 12, price_text, price_font, GROUP_DARK[0])
        _put(draw, pad + 18 + price_width + 7, top + 32, "卡利弗", caption, DARK_MUTED)
        if listing.is_new:
            _put(draw, pad + 18 + price_width + 46, top + 26, "新", _font(12, True), GROUP_DARK[1])
        _put(draw, pad + 18, top + 58, listing.time_text, caption, DARK_MUTED)
        if listing.seller:
            _put(draw, pad + 18, top + 76, listing.seller, caption, DARK_FAINT, max_width=170)

        draw.line((pad + 198, top + 16, pad + 198, bottom - 16), fill=DARK_BORDER, width=1)

        if item.item_type == "transcendent":
            _put(draw, mid_x, top + 16, "品质", caption, DARK_MUTED)
            _put(draw, mid_x + 34, top + 9, _quality_text(listing), _font(19, True), DARK_TEXT)
            weapon = _weapon_text(listing)
            if weapon:
                _put(draw, mid_x, top + 42, weapon, body_font, DARK_MUTED, max_width=mid_right - mid_x)
            if listing.quality is not None:
                bar_x, bar_w = mid_x, mid_right - mid_x
                bar_y = top + 64
                draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + 5), radius=3, fill=DARK_BAR)
                filled = max(4, int(bar_w * min(max(listing.quality / 400, 0), 1)))
                draw.rounded_rectangle((bar_x, bar_y, bar_x + filled, bar_y + 5), radius=3, fill=GROUP_DARK[0])

            perks = _perk_summary(listing, with_value=False)
            if perks:
                label_width = _put(draw, mid_x, top + 78, "辅助 ", _font(10), DARK_MUTED)
                _put(
                    draw,
                    mid_x + label_width,
                    top + 77,
                    perks,
                    _font(10, True),
                    DARK_TEXT,
                    max_width=mid_right - mid_x - label_width,
                )

            groups = listing.groups[:3]
            if groups:
                first = groups[0]
                color = GROUP_DARK[first.index % len(GROUP_DARK)]
                title_width = _put(draw, mid_x, top + 98, f"{first.title} ", _font(10, True), color)
                parts = " · ".join(_short_value(opt.value) for opt in first.options[:2] if opt.value)
                _put(
                    draw,
                    mid_x + title_width,
                    top + 98,
                    parts,
                    _font(10),
                    DARK_FAINT,
                    max_width=mid_right - mid_x - title_width,
                )
                x = mid_x
                for group in groups[1:]:
                    color = GROUP_DARK[group.index % len(GROUP_DARK)]
                    tw = _put(draw, x, top + 115, f"{group.title} ", _font(10, True), color)
                    value = _short_value(group.options[0].value) if group.options else ""
                    w = _put(draw, x + tw, top + 115, value, _font(10), DARK_FAINT)
                    x += tw + w + 14

            for idx, group in enumerate(listing.groups[:3]):
                color = GROUP_DARK[group.index % len(GROUP_DARK)]
                cx = chips_start + idx * (chip_w + chip_gap)
                draw.rounded_rectangle(
                    (cx, top + 18, cx + chip_w, top + 18 + chip_h),
                    radius=10,
                    fill=_blend(color, DARK_BG, 0.16),
                    outline=_blend(color, DARK_BG, 0.55),
                    width=1,
                )
                _center_text(draw, cx + chip_w / 2, top + 22, f"{group.point}", point_font, color)
                label = _truncate(draw, group.title, _font(10), chip_w - 12)
                _center_text(draw, cx + chip_w / 2, top + 54, label, _font(10), DARK_MUTED)
        elif item.item_type == "trigger":
            _draw_compact_trigger(draw, listing, mid_x, width - pad - 16, top)
        else:
            _draw_compact_module(draw, listing, mid_x, width - pad - 16, top)

    _put(
        draw,
        pad,
        total - footer_h + 10,
        f"TFD 市场 · 数据来自 Nexon 公开接口 · 生成 {datetime.now(CST).strftime('%m-%d %H:%M')}",
        _font(11),
        DARK_FAINT,
    )
    page.save(target, "PNG", compress_level=6)
    return page


# ── 版式 C：混合 ──────────────────────────────────────────


def _hero_header(width: int, height: int, item_type: str) -> Image.Image:
    raw = _load_ui("bg--transcend.png") if item_type == "transcendent" else None
    if raw is not None:
        base = ImageOps.fit(raw, (width, height), Image.LANCZOS)
    else:
        base = Image.new("RGBA", (width, height), (14, 17, 24, 255))
        glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.ellipse((-220, -260, 320, 200), fill=(120, 12, 24, 255))
        gd.ellipse((width - 300, -180, width + 120, 160), fill=(46, 28, 74, 255))
        base = Image.alpha_composite(base, glow.filter(ImageFilter.GaussianBlur(90)))
    overlay = Image.new("RGBA", (width, height), (10, 12, 17, 120))
    return Image.alpha_composite(base, overlay).convert("RGB")


def _draw_hybrid_module(
    draw: ImageDraw.ImageDraw,
    listing: _Listing,
    mx: int,
    right: int,
    top: int,
) -> None:
    best = _best_tier(listing.advantages)
    _put(draw, mx, top + 14, "品质", _font(11), DARK_MUTED)
    _put(draw, mx + 34, top + 8, _tier_short(best) or "-",
         _font(18, True), _tier_color(best, dark=True))
    if listing.socket_name:
        _chip(draw, right, top + 8, f"宝石 · {listing.socket_name}", CYAN)

    draw.line((mx, top + 40, right, top + 40), fill=DARK_BORDER, width=1)

    y = top + 50
    for idx, affix in enumerate(listing.advantages[:3]):
        if idx == 0:
            _put(draw, mx, y + 2, "增益", _font(11), DARK_MUTED)
        _tier_badge(draw, mx + 44, y + 2, affix.tier)
        _put(
            draw, mx + 78, y,
            affix.label, _font(12), DARK_TEXT,
            max_width=right - mx - 78 - 170,
        )
        value = _affix_value(affix)
        if value:
            _put(draw, right, y, value, _font(12, True), VALUE_ORANGE, right=right)
        y += 25

    for affix in listing.penalties[:1]:
        _put(draw, mx, y + 2, "减益", _font(11), RED_SOFT)
        _tier_badge(draw, mx + 44, y + 2, "", text="减", color=RED_SOFT)
        _put(
            draw, mx + 78, y,
            affix.label, _font(12), DARK_MUTED,
            max_width=right - mx - 78 - 170,
        )
        value = _affix_value(affix)
        if value:
            _put(draw, right, y, value, _font(12, True), RED_SOFT, right=right)
        y += 25


def _draw_hybrid_trigger(
    draw: ImageDraw.ImageDraw,
    listing: _Listing,
    mx: int,
    right: int,
    top: int,
) -> None:
    best = _best_tier(listing.trigger_effects)
    _put(draw, mx, top + 14, "品质", _font(11), DARK_MUTED)
    _put(draw, mx + 34, top + 8, _tier_short(best) or "-",
         _font(18, True), _tier_color(best, dark=True))

    draw.line((mx, top + 40, right, top + 40), fill=DARK_BORDER, width=1)

    y = top + 52
    for idx, effect in enumerate(listing.trigger_effects[:2]):
        if idx == 0:
            _put(draw, mx, y + 2, "效果", _font(11), DARK_MUTED)
        _tier_badge(draw, mx + 44, y + 2, effect.tier)
        label_width = _put(
            draw, mx + 78, y,
            effect.label, _font(12), DARK_TEXT,
            max_width=right - mx - 78 - 220,
        )
        if effect.range_text:
            _put(
                draw, mx + 78 + label_width + 8, y + 1,
                f"({effect.range_text})", _font(10), DARK_FAINT,
                max_width=right - mx - 78 - label_width - 110,
            )
        value = _trigger_value(effect)
        if value:
            _put(draw, right, y - 1, value, _font(14, True), VALUE_ORANGE, right=right)
        y += 30


def _render_hybrid(
    result: FetchResult,
    sub: Subscription,
    rows: list[dict[str, Any]],
    new_set: set[str],
    target: Path,
    numbering: bool = False,
) -> Image.Image:
    width, pad, header_h, row_h, footer_h = 880, 26, 196, 204, 40
    total = header_h + len(rows) * row_h + footer_h
    page = Image.new("RGB", (width, total), DARK_BG)
    page.paste(_hero_header(width, header_h, result.item.item_type), (0, 0))
    draw = ImageDraw.Draw(page)

    item = result.item
    listings = [
        _build_listing(
            raw, new_set, item.item_type, (i + 1) if numbering else 0
        )
        for i, raw in enumerate(rows)
    ]
    icon_url = next((str(r.get("strBinaryUrl")) for r in rows if r.get("strBinaryUrl")), None)
    tile = _tile(item, icon_url, (96, 128))
    page.paste(tile, (pad + 6, 30), tile)

    tx = pad + 6 + 96 + 24
    _put(draw, tx, 34, item.name("cn"), _font(32, True), (255, 255, 255), max_width=width - pad - tx - 200)
    type_label = ITEM_TYPE_LABELS.get(item.item_type, item.item_type)
    _put(draw, tx, 80, f"{type_label} · ID {item.item_id}", _font(13), (196, 200, 208))
    _put(draw, tx, 104, f"筛选：{_filter_text(sub) or '无附加筛选'}", _font(13), (178, 184, 194), max_width=width - pad - tx - 160)

    new_count = sum(1 for listing in listings if listing.is_new)
    count_text = f"共 {len(result.items)} 条"
    if len(result.items) > len(rows):
        count_text += f" · 仅列前 {len(rows)} 条"
    if new_count:
        count_text += f" · 新 {new_count} 条"
    _put(draw, tx, 132, count_text, _font(15, True), GROUP_DARK[0] if new_count else (210, 214, 222))
    latest = listings[0].time_text if listings else ""
    if latest:
        _put(draw, width - pad, 132, f"最新 {latest}", _font(12), (190, 196, 206), right=width - pad)

    draw.rectangle((0, header_h - 3, width, header_h), fill=GROUP_DARK[0])

    point_font = _font(22, True)
    chip_w, chip_h, chip_gap = 94, 44, 8
    chips_total = 3 * chip_w + 2 * chip_gap
    chips_start = width - pad - 16 - chips_total

    for i, listing in enumerate(listings):
        top = header_h + i * row_h + 8
        bottom = top + row_h - 14
        draw.rounded_rectangle(
            (pad, top, width - pad, bottom),
            radius=12,
            fill=DARK_PANEL if i % 2 == 0 else DARK_PANEL_ALT,
            outline=DARK_BORDER,
            width=1,
        )
        if numbering:
            _number_badge(draw, 0, top + 12, listing.index)

        price_text = f"{listing.price:,.0f}"
        price_width = _put(draw, pad + 18, top + 16, price_text, _font(28, True), GROUP_DARK[0])
        _put(draw, pad + 18 + price_width + 7, top + 34, "卡利弗", _font(11), DARK_MUTED)
        if listing.is_new:
            draw.rounded_rectangle((pad + 18, top + 60, pad + 54, top + 82), radius=11, fill=RED)
            _center_text(draw, pad + 36, top + 63, "新", _font(12, True), (255, 255, 255))
        _put(draw, pad + 18, top + 90, listing.time_text, _font(11), DARK_MUTED)
        if listing.seller:
            _put(draw, pad + 18, top + 108, listing.seller, _font(11), DARK_FAINT, max_width=190)

        draw.line((pad + 208, top + 16, pad + 208, bottom - 16), fill=DARK_BORDER, width=1)

        mx = pad + 226
        if item.item_type == "transcendent":
            _put(draw, mx, top + 14, "品质", _font(11), DARK_MUTED)
            _put(draw, mx + 34, top + 8, _quality_text(listing), _font(18, True), DARK_TEXT)
            weapon = _weapon_text(listing)
            if weapon:
                _put(draw, mx + 108, top + 14, f"· {weapon}", _font(12), DARK_MUTED, max_width=chips_start - mx - 120)

            for idx, group in enumerate(listing.groups[:3]):
                color = GROUP_DARK[group.index % len(GROUP_DARK)]
                cx = chips_start + idx * (chip_w + chip_gap)
                draw.rounded_rectangle(
                    (cx, top + 10, cx + chip_w, top + 10 + chip_h),
                    radius=9,
                    fill=_blend(color, DARK_BG, 0.18),
                    outline=_blend(color, DARK_BG, 0.55),
                    width=1,
                )
                _center_text(draw, cx + chip_w / 2, top + 13, f"{group.point}", point_font, color)
                label = _truncate(draw, group.title, _font(10), chip_w - 10)
                _center_text(draw, cx + chip_w / 2, top + 38, label, _font(10), DARK_MUTED)

            line_y = top + 64
            line_right = width - pad - 16
            perks = _perk_detail(listing)
            if perks:
                name_x = mx + 36
                _put(draw, mx, line_y, "辅助", _font(11), DARK_MUTED)
                for name, options in perks:
                    name_width = _put(
                        draw,
                        name_x,
                        line_y,
                        name,
                        _font(11, True),
                        DARK_TEXT,
                        max_width=line_right - name_x - 80,
                    )
                    x = name_x + name_width + 12
                    for idx, (label, value) in enumerate(options):
                        if x >= line_right - 30:
                            break
                        if idx:
                            x += _put(draw, x, line_y, "·", _font(10), DARK_FAINT) + 10
                        if x >= line_right - 30:
                            break
                        label_width = _put(
                            draw,
                            x,
                            line_y + 1,
                            f"{label} ",
                            _font(10),
                            DARK_MUTED,
                            max_width=max(0, line_right - x - 60),
                        )
                        x += label_width
                        x += _put(draw, x, line_y - 1, value, _font(11, True), VALUE_ORANGE) + 12
                    line_y += 18
                line_y += 4
            groups = listing.groups[:3]
            if groups:
                first = groups[0]
                color = GROUP_DARK[first.index % len(GROUP_DARK)]
                title_width = _put(draw, mx, line_y, f"{first.title} ", _font(11, True), color)
                parts = " · ".join(_short_value(opt.value) for opt in first.options[:2] if opt.value)
                _put(
                    draw,
                    mx + title_width,
                    line_y,
                    parts,
                    _font(11),
                    DARK_FAINT,
                    max_width=line_right - mx - title_width,
                )
            half = (line_right - mx) // 2
            for idx, group in enumerate(groups[1:], start=1):
                color = GROUP_DARK[group.index % len(GROUP_DARK)]
                x = mx + (idx - 1) * half
                title_width = _put(draw, x, line_y + 22, f"{group.title} ", _font(11, True), color)
                parts = " · ".join(_short_value(opt.value) for opt in group.options[:1] if opt.value)
                _put(
                    draw,
                    x + title_width,
                    line_y + 22,
                    parts,
                    _font(11),
                    DARK_FAINT,
                    max_width=half - title_width - 8,
                )

            effect = _effect_text(listing)
            if effect:
                _put(draw, mx, line_y + 44, effect, _font(11), DARK_MUTED, max_width=line_right - mx)
        elif item.item_type == "trigger":
            _draw_hybrid_trigger(draw, listing, mx, width - pad - 16, top)
        else:
            _draw_hybrid_module(draw, listing, mx, width - pad - 16, top)

    _put(
        draw,
        pad,
        total - footer_h + 12,
        f"TFD 市场 · 数据来自 Nexon 公开接口 · 生成 {datetime.now(CST).strftime('%m-%d %H:%M')}",
        _font(11),
        DARK_FAINT,
    )
    page.save(target, "PNG", compress_level=6)
    return page


def _effect_text(listing: _Listing) -> str:
    for group in listing.groups:
        for opt in group.options:
            if opt.title == "装配时的应用效果" and opt.value:
                return f"装配效果 {opt.label} {opt.value}".strip()
    return ""


# ── 入口 ──────────────────────────────────────────────────


def render_card(
    result: FetchResult,
    sub: Subscription,
    new_ids: Iterable[str],
    *,
    style: str = "official",
    out_dir: Path | None = None,
    max_rows: int | None = None,
    numbering: bool = False,
) -> Path:
    """渲染并返回 PNG 路径。相同内容 + 版式命中缓存时直接复用。"""
    if style not in STYLES:
        raise ValueError(f"未知版式 {style!r}，可选：{', '.join(STYLES)}")

    new_set = {str(x) for x in new_ids}
    limit = max_rows or sub.max_push
    rows = result.items[:limit]

    key = _render_key(sub, result, new_set, rows, style, limit, numbering)
    directory = out_dir or RENDER_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{key}.png"
    if path.exists():
        return path

    renderer = {
        "official": _render_official,
        "compact": _render_compact,
        "hybrid": _render_hybrid,
    }[style]
    renderer(result, sub, rows, new_set, path, numbering)
    logger.info(
        "渲染商品卡[%s] %s（%s 条，%s）",
        style,
        path.name,
        len(rows),
        f"{path.stat().st_size // 1024}KB",
    )
    return path
