"""临时脚本：渲染成交统计折线图样张（不接入机器人）。"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from curl_cffi.requests import Session
from PIL import Image, ImageDraw

from market_v2.models import CatalogItem
from qqbot.card import DARK_BG, GOLD, GROUP_DARK, _font, _tile

W, H, S = 1240, 820, 2
BG = DARK_BG
PANEL = (21, 26, 33)
BORDER = (39, 46, 57)
TEXT = (236, 239, 244)
MUTED = (139, 148, 162)
PURPLE = GROUP_DARK[1]
GREEN = GROUP_DARK[2]
GOLD = GOLD
OUT = Path("docs/stats_samples")


def font(size: int, bold: bool = False):
    return _font(size * S, bold)


def num(value: float) -> str:
    return f"{value:,.0f}"


def kfmt(value: float) -> str:
    if value >= 10000:
        return f"{value / 10000:.1f}万"
    return f"{value:,.0f}"


def dashed(draw: ImageDraw.ImageDraw, a, b, fill, width, dash=10, gap=8):
    ax, ay = a
    bx, by = b
    length = math.hypot(bx - ax, by - ay)
    if length <= 0:
        return
    ux, uy = (bx - ax) / length, (by - ay) / length
    pos = 0.0
    while pos < length:
        end = min(pos + dash, length)
        draw.line(
            [(ax + ux * pos, ay + uy * pos), (ax + ux * end, ay + uy * end)],
            fill=fill,
            width=width,
        )
        pos = end + gap


def fetch(days: int, item_id: str = "211064002") -> list[dict]:
    s = Session(impersonate="chrome133a")
    url = (
        f"https://tfd-api.nexon.com/api/v2/trademarket/"
        f"transcendent/{item_id}/{days}/statis"
    )
    response = s.get(url, timeout=30)
    response.raise_for_status()
    data = response.json()
    return data.get("ResultData") or []


def render(days: int, item: CatalogItem, points: list[dict], target: Path) -> Image.Image:
    img = Image.new("RGB", (W * S, H * S), BG)
    draw = ImageDraw.Draw(img)

    parsed = []
    for point in points:
        stamp = datetime.fromisoformat(str(point.get("date")))
        price = float(point.get("average_price") or 0)
        count = int(point.get("trade_count") or 0)
        parsed.append((stamp, price, count))
    if not parsed:
        raise SystemExit("no points")

    turnover = [p * c for _, p, c in parsed]
    total_turnover = sum(turnover)
    total_trades = sum(c for _, _, c in parsed)
    avg_price = total_turnover / total_trades if total_trades else 0
    peak = max(turnover)
    peak_index = turnover.index(peak)
    peak_count = max(c for _, _, c in parsed)
    nominal = 3600 if days == 2 else 86400
    unit = "按小时" if days == 2 else "按天"
    xfmt = "%m-%d %H:%M" if days == 2 else "%m-%d"

    # 背景与头部
    draw.rounded_rectangle(
        (28 * S, 160 * S, (W - 28) * S, 770 * S),
        radius=18 * S,
        fill=PANEL,
        outline=BORDER,
        width=1 * S,
    )
    icon = _tile(item, None, (96 * S, 96 * S), label=False)
    img.paste(icon, (28 * S, 26 * S), icon)
    draw.text((142 * S, 30 * S), item.name("cn"), font=font(34, True), fill=TEXT, anchor="la")
    draw.rounded_rectangle(
        (142 * S, 80 * S, 228 * S, 104 * S), radius=5 * S, fill=(52, 42, 18)
    )
    draw.text((152 * S, 82 * S), "超越武器", font=font(14, True), fill=GOLD, anchor="la")
    draw.text((240 * S, 82 * S), f"ID {item.item_id}", font=font(13), fill=MUTED, anchor="la")
    draw.text(
        (142 * S, 116 * S),
        f"{days} 天 · {unit} · CST",
        font=font(15, True),
        fill=(205, 210, 220),
        anchor="la",
    )
    first, last = parsed[0][0], parsed[-1][0]
    draw.text(
        (142 * S, 138 * S),
        f"{first:%Y-%m-%d %H:%M} ~ {last:%Y-%m-%d %H:%M}",
        font=font(12),
        fill=MUTED,
        anchor="la",
    )

    # KPI 卡片
    kpis = [
        ("交易总额", num(total_turnover), "卡利弗", GOLD),
        ("成交笔数", num(total_trades), f"{len(parsed)} 个时间点", GREEN),
        ("加权均价", num(avg_price), "卡利弗 / 笔", PURPLE),
    ]
    box_w, box_h, gap = 176, 94, 12
    start_x = W - 28 - box_w * 3 - gap * 2
    for i, (label, value, unit_text, color) in enumerate(kpis):
        x = start_x + i * (box_w + gap)
        draw.rounded_rectangle(
            (x * S, 26 * S, (x + box_w) * S, (26 + box_h) * S),
            radius=12 * S,
            fill=(17, 21, 27),
            outline=BORDER,
            width=1 * S,
        )
        draw.rectangle((x * S, 34 * S, (x + 3) * S, (26 + box_h - 8) * S), fill=color)
        draw.text(((x + 16) * S, 38 * S), label, font=font(12), fill=MUTED, anchor="la")
        draw.text(((x + 16) * S, 56 * S), value, font=font(23, True), fill=TEXT, anchor="la")
        draw.text(((x + 16) * S, 92 * S), unit_text, font=font(11), fill=MUTED, anchor="la")

    # 图例
    legend_y = 184
    lx = 744
    draw.line((lx * S, legend_y * S, (lx + 28) * S, legend_y * S), fill=GOLD, width=4 * S)
    draw.ellipse(
        ((lx + 10) * S, (legend_y - 4) * S, (lx + 18) * S, (legend_y + 4) * S), fill=GOLD
    )
    draw.text(((lx + 36) * S, (legend_y - 8) * S), "成交总额", font=font(13), fill=TEXT, anchor="la")
    lx += 112
    dashed(draw, (lx * S, legend_y * S), ((lx + 28) * S, legend_y * S), PURPLE, 3 * S, 6 * S, 5 * S)
    draw.text(((lx + 36) * S, (legend_y - 8) * S), "平均价", font=font(13), fill=TEXT, anchor="la")
    lx += 96
    draw.rectangle((lx * S, (legend_y - 5) * S, (lx + 12) * S, (legend_y + 5) * S), fill=GREEN)
    draw.text(((lx + 20) * S, (legend_y - 8) * S), "成交笔数", font=font(13), fill=TEXT, anchor="la")

    draw.text((48 * S, 176 * S), "成交总额 / 平均价", font=font(15, True), fill=(205, 210, 220), anchor="la")

    # 坐标区
    left, right = 96, W - 108
    top, bottom = 218, 596
    lower_top, lower_bottom = 626, 714
    plot_w = right - left
    plot_h = bottom - top
    min_ts = parsed[0][0].timestamp()
    max_ts = parsed[-1][0].timestamp()
    span = max(max_ts - min_ts, 1)

    def x_at(stamp: datetime) -> float:
        return left + (stamp.timestamp() - min_ts) / span * plot_w

    max_turn = max(turnover) * 1.12 or 1
    min_price = min(p for _, p, _ in parsed) * 0.94
    max_price = max(p for _, p, _ in parsed) * 1.06
    if max_price <= min_price:
        max_price = min_price + 1

    def y_turn(value: float) -> float:
        return bottom - value / max_turn * plot_h

    def y_price(value: float) -> float:
        return bottom - (value - min_price) / (max_price - min_price) * plot_h

    # 网格与左侧刻度
    for i in range(5):
        value = max_turn * i / 4
        y = y_turn(value)
        draw.line((left * S, y * S, right * S, y * S), fill=(34, 40, 49), width=1 * S)
        draw.text(((left - 10) * S, y * S), kfmt(value), font=font(11), fill=MUTED, anchor="rm")
    # 右侧价格刻度
    for i in range(4):
        value = min_price + (max_price - min_price) * i / 3
        y = y_price(value)
        draw.text(((right + 10) * S, y * S), f"{value:,.0f}", font=font(11), fill=MUTED, anchor="lm")
    draw.text(((right + 10) * S, (top - 16) * S), "均价", font=font(11, True), fill=PURPLE, anchor="lm")

    # X 轴刻度
    tick_count = 6
    tick_indices = sorted({round(i * (len(parsed) - 1) / (tick_count - 1)) for i in range(tick_count)})
    for idx in tick_indices:
        stamp = parsed[idx][0]
        x = x_at(stamp)
        draw.line((x * S, top * S, x * S, lower_bottom * S), fill=(30, 36, 44), width=1 * S)
        draw.text((x * S, (lower_bottom + 12) * S), stamp.strftime(xfmt), font=font(11), fill=MUTED, anchor="ma")

    # 成交总额面积 + 线
    area = Image.new("RGBA", img.size, (0, 0, 0, 0))
    adraw = ImageDraw.Draw(area)
    polygon = [(x_at(parsed[0][0]) * S, bottom * S)]
    for i, (stamp, _, _) in enumerate(parsed):
        polygon.append((x_at(stamp) * S, y_turn(turnover[i]) * S))
    polygon.append((x_at(parsed[-1][0]) * S, bottom * S))
    adraw.polygon(polygon, fill=(226, 172, 44, 46))
    img = Image.alpha_composite(img.convert("RGBA"), area).convert("RGB")
    draw = ImageDraw.Draw(img)

    for i in range(len(parsed) - 1):
        a, b = parsed[i], parsed[i + 1]
        gap = b[0].timestamp() - a[0].timestamp() > nominal * 1.5
        points = [
            (x_at(a[0]) * S, y_turn(turnover[i]) * S),
            (x_at(b[0]) * S, y_turn(turnover[i + 1]) * S),
        ]
        if gap:
            dashed(draw, points[0], points[1], GOLD, 3 * S, 8 * S, 6 * S)
        else:
            draw.line(points, fill=GOLD, width=3 * S)
    for i, (stamp, _, _) in enumerate(parsed):
        x, y = x_at(stamp), y_turn(turnover[i])
        draw.ellipse(
            ((x - 3.2) * S, (y - 3.2) * S, (x + 3.2) * S, (y + 3.2) * S), fill=GOLD
        )

    # 平均价线
    for i in range(len(parsed) - 1):
        a, b = parsed[i], parsed[i + 1]
        points = [
            (x_at(a[0]) * S, y_price(a[1]) * S),
            (x_at(b[0]) * S, y_price(b[1]) * S),
        ]
        dashed(draw, points[0], points[1], PURPLE, 2 * S, 6 * S, 5 * S)
    for stamp, price, _ in parsed:
        x, y = x_at(stamp), y_price(price)
        draw.ellipse(((x - 2.6) * S, (y - 2.6) * S, (x + 2.6) * S, (y + 2.6) * S), fill=PURPLE)

    # 最新点与峰值标注
    lx0, ly0 = x_at(parsed[-1][0]), y_turn(turnover[-1])
    draw.ellipse(
        ((lx0 - 6) * S, (ly0 - 6) * S, (lx0 + 6) * S, (ly0 + 6) * S),
        outline=(255, 255, 255),
        width=2 * S,
    )
    draw.text(
        ((lx0 - 8) * S, (ly0 - 14) * S),
        f"最新 {num(turnover[-1])}",
        font=font(11, True),
        fill=TEXT,
        anchor="rb",
    )
    if peak_index != len(parsed) - 1:
        px, py = x_at(parsed[peak_index][0]), y_turn(peak)
        draw.ellipse(
            ((px - 6) * S, (py - 6) * S, (px + 6) * S, (py + 6) * S),
            outline=GOLD,
            width=2 * S,
        )
        if px - left < 90:
            draw.text(
                ((px + 8) * S, (py - 10) * S),
                f"峰值 {num(peak)}",
                font=font(11, True),
                fill=GOLD,
                anchor="lb",
            )
        else:
            draw.text(
                (px * S, (py - 12) * S),
                f"峰值 {num(peak)}",
                font=font(11, True),
                fill=GOLD,
                anchor="mb",
            )

    # 成交笔数柱
    draw.text((48 * S, (lower_top - 4) * S), "成交笔数", font=font(13, True), fill=GREEN, anchor="la")
    max_count = max(c for _, _, c in parsed) * 1.18 or 1
    bar_area_h = lower_bottom - lower_top
    bar_w = min(26, max(5, plot_w / len(parsed) * 0.48))
    for stamp, _, count in parsed:
        x = x_at(stamp)
        h = count / max_count * bar_area_h
        draw.rounded_rectangle(
            ((x - bar_w / 2) * S, (lower_bottom - h) * S, (x + bar_w / 2) * S, lower_bottom * S),
            radius=2 * S,
            fill=GREEN,
        )
    draw.line((left * S, lower_bottom * S, right * S, lower_bottom * S), fill=(48, 56, 68), width=1 * S)
    draw.text(((left - 10) * S, lower_top * S), kfmt(max_count), font=font(11), fill=MUTED, anchor="rm")
    draw.text(((left - 10) * S, lower_bottom * S), "0", font=font(11), fill=MUTED, anchor="rm")

    # 页脚
    draw.text(
        (28 * S, 790 * S),
        "数据源：Nexon 成交统计 API · 仅统计有成交的时间段",
        font=font(11),
        fill=MUTED,
        anchor="la",
    )
    draw.text(
        ((W - 28) * S, 790 * S),
        f"成交总额 = 平均价 × 成交量 · 生成 {datetime.now():%Y-%m-%d %H:%M} CST",
        font=font(11),
        fill=MUTED,
        anchor="ra",
    )

    img = img.resize((W, H), Image.LANCZOS)
    target.parent.mkdir(parents=True, exist_ok=True)
    img.save(target)
    return img


def main() -> None:
    item = CatalogItem(item_id="211064002", item_type="transcendent", names={"cn": "怪物猎人"})
    for days in (2, 7, 90):
        points = fetch(days)
        target = OUT / f"stats_{days}d_211064002.png"
        render(days, item, points, target)
        print(days, len(points), target)


if __name__ == "__main__":
    main()
