"""倒卖利润估算：为开启了手机推送的订阅构建模糊过滤、计算最高利润。

规则（与用户确认的一致）：
- 模糊表默认：物品名/类型、品质照抄原订阅；辅助忽略；能力只保留一条（按区间最低值比较取最大），
  价格/品级忽略；排序固定 price_asc。
- 利润 = p2 - (p1 + fee + p2 * tax)，p1 为订阅命中最低价，p2 为模糊结果中第 2~(m+1) 低价。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .dsl import DslError, parse_dsl

ABILITY_KEYS = ("ability1", "ability2", "ability3")
OVERRIDE_KEYS = {
    "price", "quality", "grade", "subperks",
    "ability1", "ability2", "ability3", "sort",
}


def _range(spec: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = spec.get(key)
    if not isinstance(value, dict):
        return None
    low, high = value.get("min"), value.get("max")
    if low is None and high is None:
        return None
    return {"min": low, "max": high}


def select_ability(spec: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """选择要保留的能力：空的忽略；多个非空时按区间最低值取最大，同一区间再比上限。"""
    candidates: list[tuple[str, dict[str, Any]]] = []
    for key in ABILITY_KEYS:
        rng = _range(spec or {}, key)
        if rng is not None:
            candidates.append((key, rng))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    def score(item: tuple[str, dict[str, Any]]) -> tuple[float, float]:
        rng = item[1]
        low = rng.get("min")
        high = rng.get("max")
        low_value = float(low) if low is not None else float("-inf")
        high_value = float(high) if high is not None else low_value
        return low_value, high_value

    return max(candidates, key=score)


def build_fuzzy_spec(
    sub_spec: dict[str, Any], overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    """把订阅条件转成模糊过滤表；overrides 覆盖任意字段。"""
    source = sub_spec or {}
    item = source.get("item") if isinstance(source.get("item"), dict) else {}
    fuzzy: dict[str, Any] = {
        "item": {k: item[k] for k in ("name", "id", "type") if item.get(k)},
        "sort": "price_asc",
    }
    quality = _range(source, "quality")
    if quality is not None:
        fuzzy["quality"] = quality
    ability = select_ability(source)
    if ability is not None:
        key, rng = ability
        fuzzy[key] = rng
    for key, value in (overrides or {}).items():
        if key in OVERRIDE_KEYS and value is not None:
            fuzzy[key] = value
    if isinstance((overrides or {}).get("sort"), str):
        fuzzy["sort"] = str((overrides or {})["sort"])
    else:
        fuzzy["sort"] = "price_asc"
    return fuzzy


def parse_flip_overrides(text: str) -> dict[str, Any]:
    """解析 `价格=100-8000 能力2=150-150` 为覆盖项（不含 item）。"""
    raw = (text or "").strip()
    if not raw:
        return {}
    try:
        parsed = parse_dsl(f"物品 {raw}")
    except DslError as exc:
        raise DslError(str(exc)) from exc
    spec = dict(parsed.spec_dict)
    spec.pop("item", None)
    return {k: v for k, v in spec.items() if k in OVERRIDE_KEYS}


def overrides_to_text(overrides: dict[str, Any]) -> str:
    """把覆盖项转回可再次编辑的 DSL 文本。"""
    labels = {
        "price": "价格",
        "quality": "品质",
        "grade": "品级",
        "subperks": "辅助",
        "ability1": "能力1",
        "ability2": "能力2",
        "ability3": "能力3",
        "sort": "排序",
    }
    parts: list[str] = []
    for key, value in (overrides or {}).items():
        name = labels.get(key)
        if not name:
            continue
        if isinstance(value, dict):
            low, high = value.get("min"), value.get("max")
            if low is not None and high is not None:
                parts.append(f"{name}={low}-{high}")
            elif low is not None:
                parts.append(f"{name}>={low}")
            elif high is not None:
                parts.append(f"{name}<={high}")
        elif isinstance(value, list):
            parts.append(f"{name}=" + ",".join(str(v) for v in value))
        else:
            parts.append(f"{name}={value}")
    return " ".join(parts)


def fuzzy_fingerprint(spec: dict[str, Any]) -> str:
    raw = json.dumps(spec, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(("flip:" + raw).encode("utf-8")).hexdigest()


def price_of(item: dict[str, Any]) -> float | None:
    try:
        return float(item.get("n4SalePrice"))
    except (TypeError, ValueError):
        return None


def profit_of(cheapest: float, target: float, *, fee: float, tax_rate: float) -> float:
    """利润 = 卖出价 - (买入价 + 手续费 + 卖出价 * 税率)。"""
    return target - (cheapest + fee + target * tax_rate)


def money(value: float) -> str:
    if float(value).is_integer():
        return f"¥{int(value):,}"
    return f"¥{value:,.1f}"


def format_profit_message(
    *,
    sub_id: int,
    matched: int,
    cheapest: dict[str, Any],
    target: dict[str, Any],
    target_rank: int,
    profit: float,
    fee: float,
    tax_rate: float,
    command: str,
) -> str:
    p1 = price_of(cheapest) or 0.0
    p2 = price_of(target) or 0.0
    seller = str((cheapest.get("seller") or {}).get("strNickName") or "-")
    cost = p1 + fee + p2 * tax_rate
    tag = "可倒卖" if profit > 0 else "亏损不建议"
    lines = [
        f"订阅 #{sub_id} · 命中 {matched} 条 · {tag}",
        f"最低价 {money(p1)} · {seller}",
        f"卖出参考 {money(p2)}（市场第 {target_rank} 低价）",
        f"成本 = {p1:,.0f} + {fee:,.0f} + {p2 * tax_rate:,.0f} = {cost:,.0f}",
        f"最高倒卖利润 {money(profit)}",
    ]
    if command:
        lines.append(command)
    return "\n".join(lines)
