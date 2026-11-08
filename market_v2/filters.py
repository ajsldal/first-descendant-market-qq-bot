from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .catalog import compact_name, normalize_name
from .errors import FilterSpecError, OptionResolveError
from .models import (
    MAX_ABILITY_POINT,
    MAX_PRICE,
    MAX_QUALITY,
    MIN_ABILITY_POINT,
    MIN_PRICE,
    MIN_QUALITY,
    SORT_ALIASES,
    CatalogItem,
    OptionEntry,
)


@dataclass
class IntRange:
    min: int | None = None
    max: int | None = None

    def is_empty(self) -> bool:
        return self.min is None and self.max is None

    @classmethod
    def from_value(cls, value: Any) -> "IntRange":
        if value is None:
            return cls()
        if isinstance(value, dict):
            return cls(min=_to_int(value.get("min")), max=_to_int(value.get("max")))
        if isinstance(value, str) and "-" in value:
            lo, _, hi = value.partition("-")
            return cls(min=_to_int(lo), max=_to_int(hi))
        raise FilterSpecError(f"无法解析区间: {value!r}")


@dataclass
class OptionCondition:
    key: str
    grade: str | None = None


@dataclass
class FilterSpec:
    item_name: str | None = None
    item_id: str | None = None
    item_type: str | None = None
    language: str = "cn"
    sort: str = "latest"
    price: IntRange = field(default_factory=IntRange)
    grade: str | None = None
    advantage: list[OptionCondition] = field(default_factory=list)
    penalty: OptionCondition | None = None
    quality: IntRange = field(default_factory=IntRange)
    subperks: list[str] = field(default_factory=list)
    ability1: IntRange = field(default_factory=IntRange)
    ability2: IntRange = field(default_factory=IntRange)
    ability3: IntRange = field(default_factory=IntRange)
    output_dir: str = "output"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FilterSpec":
        if not isinstance(data, dict):
            raise FilterSpecError("spec 必须是 JSON 对象")

        item = data.get("item")
        item_name: str | None = None
        item_id: str | None = None
        item_type: str | None = None
        if isinstance(item, str):
            item_name = item
        elif isinstance(item, dict):
            item_name = item.get("name")
            item_id = str(item["id"]) if item.get("id") is not None else None
            item_type = item.get("type")
        else:
            item_name = data.get("name")
            item_id = str(data["item_id"]) if data.get("item_id") is not None else None
            item_type = data.get("type") or data.get("item_type")

        spec = cls(
            item_name=item_name,
            item_id=item_id,
            item_type=str(item_type).lower() if item_type else None,
            language=str(data.get("language") or "cn"),
            sort=str(data.get("sort") or "latest"),
            price=IntRange.from_value(data.get("price")),
            grade=_normalize_grade(data.get("grade")),
            quality=IntRange.from_value(data.get("quality")),
            subperks=list(data.get("subperks") or []),
            ability1=IntRange.from_value(data.get("ability1")),
            ability2=IntRange.from_value(data.get("ability2")),
            ability3=IntRange.from_value(data.get("ability3")),
            output_dir=str(data.get("output_dir") or "output"),
        )

        for entry in data.get("advantage") or []:
            if isinstance(entry, str):
                name, _, grade = entry.partition(":")
                spec.advantage.append(OptionCondition(name.strip(), _normalize_grade(grade)))
            elif isinstance(entry, dict):
                spec.advantage.append(
                    OptionCondition(str(entry.get("name") or entry.get("id") or ""), _normalize_grade(entry.get("grade")))
                )
            else:
                raise FilterSpecError(f"advantage 条目格式错误: {entry!r}")

        penalty = data.get("penalty")
        if isinstance(penalty, str):
            spec.penalty = OptionCondition(penalty.strip())
        elif isinstance(penalty, dict):
            spec.penalty = OptionCondition(
                str(penalty.get("name") or penalty.get("id") or ""),
                _normalize_grade(penalty.get("grade")),
            )

        return spec

    # ── 校验 ────────────────────────────────────────────────

    def validate(self) -> None:
        if not self.item_name and not self.item_id:
            raise FilterSpecError("必须提供 item.name 或 item.id")
        if self.item_type and self.item_type not in ("ancestor", "trigger", "transcendent", "descendant"):
            raise FilterSpecError(f"不支持的 item.type: {self.item_type!r}")
        if self.sort not in SORT_ALIASES:
            raise FilterSpecError(f"不支持的 sort: {self.sort!r}（latest/price_desc/price_asc）")

        self._validate_range(self.price, MIN_PRICE, MAX_PRICE, "price")
        if self.price.min is not None and self.price.max is not None and self.price.min > self.price.max:
            raise FilterSpecError("price.min 不能大于 price.max")

        if len(self.subperks) > 2:
            raise FilterSpecError("subperks 最多 2 个")
        if len(self.advantage) > 3:
            raise FilterSpecError("advantage 最多 3 条")

    @staticmethod
    def _validate_range(rng: IntRange, lo: int, hi: int, label: str) -> None:
        for value in (rng.min, rng.max):
            if value is not None and not (lo <= value <= hi):
                raise FilterSpecError(f"{label} 取值范围为 {lo}~{hi}，收到 {value}")
        if rng.min is not None and rng.max is not None and rng.min > rng.max:
            raise FilterSpecError(f"{label}.min 不能大于 {label}.max")

    def validate_for_type(self, item_type: str) -> None:
        transcendent_only = self.quality.is_empty() is False or bool(self.subperks) or any(
            not r.is_empty() for r in (self.ability1, self.ability2, self.ability3)
        )
        module_only = bool(self.advantage) or self.penalty is not None

        if transcendent_only and item_type != "transcendent":
            raise FilterSpecError("quality/subperks/ability 只能用于 transcendent 物品")
        if module_only and item_type not in ("ancestor", "descendant"):
            raise FilterSpecError("advantage/penalty 只能用于 ancestor/descendant 物品")
        if self.grade and item_type == "transcendent":
            raise FilterSpecError("grade 不能用于 transcendent 物品（请用 quality）")

        if item_type == "transcendent":
            self._validate_range(self.quality, MIN_QUALITY, MAX_QUALITY, "quality")
        if item_type == "transcendent":
            for idx, rng in enumerate((self.ability1, self.ability2, self.ability3), start=1):
                self._validate_range(rng, MIN_ABILITY_POINT, MAX_ABILITY_POINT, f"ability{idx}")


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise FilterSpecError(f"无法解析为整数: {value!r}") from exc


def _normalize_grade(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    upper = s.upper()
    if upper in ("TIER1", "TIER2", "TIER3", "TIER4"):
        return f"Tier{upper[-1]}"
    if upper in ("1", "2", "3", "4"):
        return f"Tier{upper}"
    if upper == "NONE":
        return "NONE"
    raise FilterSpecError(f"品级只能是 Tier1~Tier4，收到 {value!r}")


def resolve_entry(entries: list[OptionEntry], key: str, kind: str) -> OptionEntry:
    """把名称或 ID 解析为服务端选项 ID（支持中文名/英文名/ID）。"""
    key = (key or "").strip()
    if not key:
        raise OptionResolveError(f"{kind} 不能为空")
    for entry in entries:
        if entry.id == key:
            return entry
    norm = normalize_name(key)
    norm_compact = compact_name(key)
    exact = [
        e for e in entries
        if any(normalize_name(n) == norm or compact_name(n) == norm_compact for n in e.all_names())
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise OptionResolveError(
            f"{kind} {key!r} 匹配到多个 ID: " + ", ".join(f"{e.value}={e.id}" for e in exact[:8])
        )
    fuzzy = [
        e for e in entries
        if any(norm in normalize_name(n) or norm_compact in compact_name(n) for n in e.all_names())
    ]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) > 1:
        raise OptionResolveError(
            f"{kind} {key!r} 模糊匹配到多个 ID: " + ", ".join(f"{e.value}={e.id}" for e in fuzzy[:8])
        )
    available = ", ".join(f"{e.value}({e.id})" for e in entries[:12])
    raise OptionResolveError(f"{kind} 中没有 {key!r}；可选值示例: {available}")


def build_payload(
    spec: FilterSpec,
    item: CatalogItem,
    option_lists: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """构造 products 请求体，并返回可读的已解析筛选条件。"""
    item_type = item.item_type
    spec.validate_for_type(item_type)

    payload: dict[str, Any] = {
        "now": None,
        "maintenance": None,
        "restrict": None,
        "access": None,
        "pageInfo": {"pageNumber": 1, "pageSize": 20},
        "strLanguage": spec.language,
        "strItemType": item_type,
        "strItemid": item.item_id,
        "strGrade": spec.grade.upper() if spec.grade else None,
        "n4SalePriceMin": spec.price.min,
        "n4SalePriceMax": spec.price.max,
        "odType": SORT_ALIASES[spec.sort],
    }

    resolved: dict[str, Any] = {
        "item": {
            "name": item.name(spec.language),
            "item_id": item.item_id,
            "item_type": item_type,
        },
        "price": {"min": spec.price.min, "max": spec.price.max},
        "grade": spec.grade,
        "sort": spec.sort,
        "language": spec.language,
        "advantage": [],
        "penalty": None,
        "quality": {"min": spec.quality.min, "max": spec.quality.max},
        "subperks": [],
        "abilities": [],
    }

    if item_type in ("ancestor", "descendant"):
        options: list[dict[str, str]] = []
        for cond in spec.advantage:
            entry = resolve_entry(option_lists.advantage, cond.key, "增益属性")
            options.append(
                {
                    "strId": entry.id,
                    "strGrade": cond.grade or "",
                    "strSlot": "ADVANTAGE",
                }
            )
            resolved["advantage"].append(
                {"id": entry.id, "name": entry.value, "grade": cond.grade or ""}
            )
        if spec.penalty is not None:
            entry = resolve_entry(option_lists.penalty, spec.penalty.key, "减益属性")
            options.append({"strId": entry.id, "strGrade": "NONE", "strSlot": "PENALTY"})
            resolved["penalty"] = {"id": entry.id, "name": entry.value}
        payload["ancestor"] = {"options": options}

    elif item_type == "transcendent":
        subperks: list[dict[str, str]] = []
        for key in spec.subperks:
            entry = resolve_entry(option_lists.subperks, key, "辅助能力")
            subperks.append({"strId": entry.id})
            resolved["subperks"].append({"id": entry.id, "name": entry.value})

        payload["transcendent"] = {
            "quality": {"n4ValueMin": spec.quality.min, "n4ValueMax": spec.quality.max},
            "subperks": subperks,
            "abilityPoint1": {"n4ValueMin": spec.ability1.min, "n4ValueMax": spec.ability1.max},
            "abilityPoint2": {"n4ValueMin": spec.ability2.min, "n4ValueMax": spec.ability2.max},
            "abilityPoint3": {"n4ValueMin": spec.ability3.min, "n4ValueMax": spec.ability3.max},
        }
        for idx, rng in enumerate((spec.ability1, spec.ability2, spec.ability3), start=1):
            if not rng.is_empty():
                resolved["abilities"].append({"group": idx, "min": rng.min, "max": rng.max})

    return payload, resolved
