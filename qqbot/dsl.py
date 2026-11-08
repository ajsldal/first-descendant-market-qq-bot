"""把用户输入的 `/订阅 ...` 解析为 FilterSpec + 订阅参数。

格式：``/订阅 物品名 键=值 键=值 ...``
- 第一个 ``键=值`` 之前的内容整体作为物品名（可含空格）。
- 区间写法：``100-5000``、``>=100``、``<=5000``、``500``。
- 多值用逗号分隔：``辅助=弹夹,弹头``。

内部不发明新结构，最终仍交给 market_v2.filters.FilterSpec。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from market_v2.filters import FilterSpec
from market_v2.models import CatalogItem

from .store import Subscription

ITEM_TYPE_LABELS = {
    "transcendent": "超越武器",
    "ancestor": "模块",
    "descendant": "继承者模块",
    "trigger": "触发器",
}

TYPE_ALIASES = {
    "超越": "transcendent",
    "超越武器": "transcendent",
    "武器": "transcendent",
    "transcendent": "transcendent",
    "transcend": "transcendent",
    "模块": "ancestor",
    "先裔": "ancestor",
    "ancestor": "ancestor",
    "继承者": "descendant",
    "descendant": "descendant",
    "触发器": "trigger",
    "trigger": "trigger",
}

SORT_ALIASES = {
    "latest": "latest",
    "最新": "latest",
    "date": "latest",
    "price_asc": "price_asc",
    "最低": "price_asc",
    "低价": "price_asc",
    "price_desc": "price_desc",
    "最高": "price_desc",
    "高价": "price_desc",
}

KEY_ALIASES = {
    "类型": "type", "t": "type", "itemtype": "type", "item_type": "type",
    "价格": "price", "p": "price",
    "品质": "quality", "q": "quality",
    "品级": "grade", "g": "grade",
    "辅助": "subperks", "sp": "subperks", "subperk": "subperks", "subperks": "subperks",
    "增益": "advantage", "adv": "advantage", "advantage": "advantage",
    "减益": "penalty", "pen": "penalty", "penalty": "penalty",
    "能力1": "ability1", "a1": "ability1", "ability1": "ability1",
    "能力2": "ability2", "a2": "ability2", "ability2": "ability2",
    "能力3": "ability3", "a3": "ability3", "ability3": "ability3",
    "排序": "sort", "sort": "sort",
    "间隔": "interval", "every": "interval", "interval": "interval",
    "条数": "limit", "limit": "limit", "max": "limit",
    "仅新": "only_new", "new": "only_new", "only_new": "only_new",
    "静默": "quiet", "quiet": "quiet",
    "首次": "first", "first": "first",
    "铃声": "sound", "sound": "sound", "bark铃声": "sound",
}

SUB_KEYS = {"interval", "limit", "only_new", "quiet", "first", "sound"}

_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
_GE_RE = re.compile(r"^>=\s*(\d+)$")
_LE_RE = re.compile(r"^<=\s*(\d+)$")
_SINGLE_RE = re.compile(r"^(\d+)$")
_DURATION_RE = re.compile(r"^(\d+)\s*(s|m|h|秒|分|时|小时)?$")
_QUIET_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")

MIN_INTERVAL = 60
MAX_INTERVAL = 86400
MAX_PUSH_LIMIT = 50


class DslError(ValueError):
    """用户输入格式错误，message 可直接回复给用户。"""


@dataclass
class SubOptions:
    interval_sec: int = 600
    max_push: int = 20
    only_new: bool = False
    quiet_hours: str = ""
    first_push: bool = True
    bark_sound: str = ""


@dataclass
class ParsedSubscription:
    spec: FilterSpec
    spec_dict: dict[str, Any]
    options: SubOptions


def parse_dsl(text: str) -> ParsedSubscription:
    """解析 `/订阅` 后面的参数。"""
    text = (text or "").strip()
    if not text:
        raise DslError("用法：/订阅 物品名 价格=100-5000 品质=390-400 ...")

    tokens = text.split()
    key_index = next((i for i, token in enumerate(tokens) if "=" in token), len(tokens))

    name = " ".join(tokens[:key_index]).strip().strip('"“”')
    if not name:
        raise DslError("缺少物品名：/订阅 怪物猎人 价格=100-5000")

    raw: dict[str, str] = {}
    for token in tokens[key_index:]:
        key, sep, value = token.partition("=")
        if not sep or not value:
            raise DslError(f"参数 {token!r} 不是 键=值 形式")
        canonical = _key_of(token)
        if canonical is None:
            raise DslError(f"不认识的选项 {key!r}；输入 /帮助 查看可用选项")
        if canonical in raw:
            raise DslError(f"选项 {key!r} 重复了")
        raw[canonical] = value.strip()

    item: dict[str, Any] = {"name": name}
    if "type" in raw:
        item_type = TYPE_ALIASES.get(raw["type"].strip().lower())
        if item_type is None:
            raise DslError("类型只能是：超越武器 / 模块 / 继承者 / 触发器")
        item["type"] = item_type

    spec_dict: dict[str, Any] = {"item": item}

    for src, dst in (("price", "price"), ("quality", "quality")):
        if src in raw:
            spec_dict[dst] = _range(raw[src], dst)

    for key in ("ability1", "ability2", "ability3"):
        if key in raw:
            spec_dict[key] = _range(raw[key], key)

    if "grade" in raw:
        spec_dict["grade"] = raw["grade"]
    if "sort" in raw:
        sort = SORT_ALIASES.get(raw["sort"].strip().lower())
        if sort is None:
            raise DslError("排序只能是：latest（最新）/ price_asc（低价）/ price_desc（高价）")
        spec_dict["sort"] = sort
    if "subperks" in raw:
        spec_dict["subperks"] = _split_list(raw["subperks"])
    if "advantage" in raw:
        entries = []
        for entry in _split_list(raw["advantage"]):
            name_part, _, grade = entry.partition(":")
            if not name_part.strip():
                raise DslError("增益属性不能为空")
            entries.append({"name": name_part.strip(), "grade": grade.strip()})
        spec_dict["advantage"] = entries
    if "penalty" in raw:
        spec_dict["penalty"] = raw["penalty"].strip()

    spec = FilterSpec.from_dict(spec_dict)
    try:
        spec.validate()
    except Exception as exc:  # FilterSpecError 等
        raise DslError(str(exc)) from exc

    options = SubOptions()
    if "interval" in raw:
        options.interval_sec = _duration(raw["interval"])
    if "limit" in raw:
        options.max_push = _limit(raw["limit"])
    if "only_new" in raw:
        options.only_new = _bool(raw["only_new"], "仅新")
    if "quiet" in raw:
        options.quiet_hours = normalize_quiet(raw["quiet"])
    if "first" in raw:
        value = raw["first"].strip().lower()
        if value not in ("push", "推送", "base", "基线"):
            raise DslError("首次只能是 push（推送当前结果）或 base（只记基线）")
        options.first_push = value in ("push", "推送")
    if "sound" in raw:
        options.bark_sound = raw["sound"].strip()

    return ParsedSubscription(spec=spec, spec_dict=spec_dict, options=options)


def parse_settings(text: str) -> dict[str, Any]:
    """解析 `/设置 <id> 间隔=10m 条数=5 仅新=1 静默=23:00-08:00` 的参数部分。"""
    fields: dict[str, Any] = {}
    for token in (text or "").split():
        key, sep, value = token.partition("=")
        if not sep:
            raise DslError(f"参数 {token!r} 不是 键=值 形式")
        canonical = _key_of(token)
        if canonical not in SUB_KEYS:
            raise DslError(f"{key!r} 不是订阅级选项；可设置：间隔、条数、仅新、静默、首次、铃声")
        if canonical == "interval":
            fields["interval_sec"] = _duration(value)
        elif canonical == "limit":
            fields["max_push"] = _limit(value)
        elif canonical == "only_new":
            fields["only_new"] = int(_bool(value, "仅新"))
        elif canonical == "quiet":
            fields["quiet_hours"] = normalize_quiet(value)
        elif canonical == "first":
            v = value.strip().lower()
            if v not in ("push", "推送", "base", "基线"):
                raise DslError("首次只能是 push 或 base")
            fields["first_push"] = int(v in ("push", "推送"))
        elif canonical == "sound":
            fields["bark_sound"] = value.strip()
    return fields


def fingerprint_for(spec: FilterSpec, item: CatalogItem) -> str:
    """筛选条件指纹：条件相同的订阅只抓一次。"""
    payload = {
        "item_id": item.item_id,
        "item_type": item.item_type,
        "sort": spec.sort,
        "price": [spec.price.min, spec.price.max],
        "grade": spec.grade,
        "quality": [spec.quality.min, spec.quality.max],
        "subperks": sorted(spec.subperks),
        "abilities": [
            [rng.min, rng.max] for rng in (spec.ability1, spec.ability2, spec.ability3)
        ],
        "advantage": sorted(
            (cond.key, cond.grade or "") for cond in spec.advantage
        ),
        "penalty": (spec.penalty.key, spec.penalty.grade or "") if spec.penalty else None,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def describe_spec(spec: FilterSpec, *, include_sort: bool = True) -> str:
    parts: list[str] = []

    def rng(label: str, r) -> None:
        if r.is_empty():
            return
        if r.min is not None and r.max is not None:
            if r.min == r.max:
                parts.append(f"{label} {r.min}")
            else:
                parts.append(f"{label} {r.min}-{r.max}")
        elif r.min is not None:
            parts.append(f"{label} ≥{r.min}")
        else:
            parts.append(f"{label} ≤{r.max}")

    rng("价格", spec.price)
    if spec.grade:
        parts.append(f"品级 {spec.grade}")
    rng("品质", spec.quality)
    if spec.subperks:
        parts.append("辅助 " + ",".join(spec.subperks))
    if spec.advantage:
        parts.append(
            "增益 " + ",".join(
                f"{c.key}:{c.grade}" if c.grade else c.key for c in spec.advantage
            )
        )
    if spec.penalty:
        parts.append(f"减益 {spec.penalty.key}")
    for idx, r in enumerate((spec.ability1, spec.ability2, spec.ability3), start=1):
        rng(f"能力{idx}", r)
    if include_sort and spec.sort != "latest":
        parts.append(f"排序 {spec.sort}")
    return " | ".join(parts) if parts else "无附加筛选"


def describe_subscription(sub: Subscription) -> str:
    try:
        spec = FilterSpec.from_dict(sub.spec)
    except Exception:  # noqa: BLE001 — 历史数据损坏时仍能列出
        return f"#{sub.id} 数据异常"
    item = sub.spec.get("item") or {}
    name = item.get("name") or item.get("id") or "?"
    state = "运行中" if sub.enabled else "已暂停"
    interval = _format_duration(sub.interval_sec)
    flags = "仅新" if sub.only_new else "全量"
    bits = [
        f"#{sub.id} {name}（{state}）",
        describe_spec(spec),
        f"每 {interval} · 最多 {sub.max_push} 条 · {flags}",
    ]
    if sub.quiet_hours:
        bits.append(f"静默 {sub.quiet_hours}")
    if sub.bark_enabled:
        bits.append("Bark 开" + (f"（{sub.bark_sound}）" if sub.bark_sound else ""))
    bits.append(
        "下次 " + datetime.fromtimestamp(sub.next_run_at).strftime("%m-%d %H:%M")
    )
    bits.append(f"上次命中 {sub.last_count if sub.last_count is not None else '-'} 条")
    return " ｜ ".join(bits)


def help_text() -> str:
    return (
        "可用命令：\n"
        "/订阅 物品 键=值 ...  新建订阅（不带参数进入向导）\n"
        "/我的订阅            查看自己的订阅\n"
        "/删除 <id>           删除订阅\n"
        "/暂停 <id> /恢复 <id>\n"
        "/立即查 <id>         立刻检查一次\n"
        "/设置 <id> 间隔=1m    修改订阅级选项（最小 1 分钟）\n"
        "/模糊 <id> [键=值]   设置倒卖利润的模糊过滤（不带参数查看）\n"
        "/选项 物品           查看该物品可填的属性/辅助能力\n"
        "/物品 关键词         搜索物品名或 ID\n"
        "/查价 物品 [参数...]  查最低价前 3 条；只写物品名查看该类型模板\n\n"
        "登录与采购（需私聊）：\n"
        "/登录                Steam 手机 App 扫码绑定账号\n"
        "/登录 自动           用后台保存的 Steam 账号自动登录（验证码需手动完成）\n"
        "/浏览器登录          在机器人电脑弹出浏览器，手动登录\n"
        "/导入登录            粘贴 Cookie/cookies.json/storage_state\n"
        "/完成导入            分多条粘贴后收尾\n"
        "/登录状态 /检查登录  查看或校验登录态\n"
        "/登出                删除登录态\n"
        "/bark                手机推送：查看状态；设置 Key/铃声/开关/测试\n"
        "/购买 物品 键=值 ...  筛选→序号→选 1私聊购买 / 2休闲室购买→确认\n"
        "/求购 物品 键=值 ...  私聊购买（/chat → /chat/buy）\n"
        "/休闲室 物品 键=值 ... 休闲室购买（/lounge → /lounge/buy）\n"
        "/组队 物品 键=值 ...  /休闲室 的别名\n"
        "/选择 <序号>         选择图片里的商品（也可直接回复数字）\n"
        "/确认 <id>           确认第 1 步打开的私聊框/休闲室框\n"
        "/采购记录 [N]        查看最近采购记录\n\n"
        "常用键：价格=100-5000 品质=390-400 品级=Tier4 辅助=弹夹 "
        "增益=技能冷却时间:Tier4 减益=最大护盾 能力1=30-150 间隔=1m 条数=20\n"
        "示例：/订阅 怪物猎人 价格=100-5000 品质=390-400 能力1=30-150\n"
        "示例：/购买 怪物猎人 价格=100-5000"
    )


def option_help(item_type: str) -> str:
    common = "通用：类型、价格、排序"
    if item_type == "transcendent":
        return common + "\n超越武器：品质(1-400)、辅助(≤2)、能力1~3(0-150)"
    if item_type in ("ancestor", "descendant"):
        return common + "\n模块：品级(Tier1~Tier4)、增益(≤3，格式 名称[:品级])、减益(1)"
    return common + "\n触发器：只支持价格、品级、排序"


# 每种物品类型对应的查询模板；所有参数都可省略
QUERY_TEMPLATES = {
    "transcendent": (
        "/查价 {name} 价格=100-5000 品质=390-400 "
        "辅助=弹夹,弹头 能力1=30-150 能力2=0-150 能力3=0-150"
    ),
    "ancestor": (
        "/查价 {name} 价格=100-5000 品级=Tier4 "
        "增益=技能冷却时间:Tier4,技能资源消耗量 减益=最大护盾"
    ),
    "descendant": (
        "/查价 {name} 价格=100-5000 品级=Tier4 "
        "增益=技能冷却时间:Tier4,技能资源消耗量 减益=最大护盾"
    ),
    "trigger": "/查价 {name} 价格=100-5000 品级=Tier4",
}


def query_template(item_type: str, name: str = "") -> str:
    """按物品类型返回 /查价 模板（名称已知时直接填入示例）。"""
    label = ITEM_TYPE_LABELS.get(item_type, item_type)
    template = QUERY_TEMPLATES.get(item_type, "/查价 {name} 价格=100-5000")
    lines = [
        f"{label}价格查询模板：",
        template.format(name=name or "物品名"),
        "参数全部可省略（只写物品名即查最低价）；"
        "只支持模板里的键，排序固定最低价，仅返回前 3 条。",
    ]
    return "\n".join(lines)


def query_template_overview() -> str:
    return (
        "价格查询用法：/查价 物品名 [参数...]\n"
        "只写物品名会返回该物品的查询模板；带参数才真正查询。\n\n"
        "超越武器：/查价 怪物猎人 价格=100-5000 品质=390-400 辅助=弹夹 能力1=30-150\n"
        "模块/继承者：/查价 阴影的决意 价格=100-5000 品级=Tier4 "
        "增益=技能冷却时间:Tier4 减益=最大护盾\n"
        "触发器：/查价 <触发器> 价格=100-5000 品级=Tier4\n\n"
        "参数可全部省略；默认按最低价排序，仅展示前 3 条，结果不落库。"
    )


# ── 内部工具 ──────────────────────────────────────────────


def _key_of(token: str) -> str | None:
    key = token.partition("=")[0].strip().lower()
    return KEY_ALIASES.get(key)


def _range(value: str, label: str) -> dict[str, int | None]:
    text = value.strip()
    match = _RANGE_RE.match(text)
    if match:
        lo, hi = int(match.group(1)), int(match.group(2))
        if lo > hi:
            raise DslError(f"{label} 下限不能大于上限")
        return {"min": lo, "max": hi}
    match = _GE_RE.match(text)
    if match:
        return {"min": int(match.group(1)), "max": None}
    match = _LE_RE.match(text)
    if match:
        return {"min": None, "max": int(match.group(1))}
    match = _SINGLE_RE.match(text)
    if match:
        v = int(match.group(1))
        return {"min": v, "max": v}
    raise DslError(f"{label} 格式应为 100-5000、>=100、<=5000 或单个数字")


def _split_list(value: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,，、]", value) if x.strip()]


def _duration(value: str) -> int:
    match = _DURATION_RE.match(value.strip().lower())
    if not match:
        raise DslError("间隔格式应为 10m、90s、1h")
    number = int(match.group(1))
    unit = match.group(2) or "s"
    seconds = number * {"s": 1, "秒": 1, "m": 60, "分": 60, "h": 3600, "时": 3600, "小时": 3600}[unit]
    if not MIN_INTERVAL <= seconds <= MAX_INTERVAL:
        raise DslError(f"间隔范围为 1 分钟 ~ 24 小时，收到 {value}")
    return seconds


def _limit(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise DslError("条数必须是整数") from exc
    if not 1 <= number <= MAX_PUSH_LIMIT:
        raise DslError(f"条数范围为 1~{MAX_PUSH_LIMIT}")
    return number


def _bool(value: str, label: str) -> bool:
    v = value.strip().lower()
    if v in ("1", "true", "yes", "是", "开"):
        return True
    if v in ("0", "false", "no", "否", "关"):
        return False
    raise DslError(f"{label} 只能是 1 或 0")


def normalize_quiet(value: str) -> str:
    text = value.strip()
    if not text or text in ("0", "关", "无"):
        return ""
    match = _QUIET_RE.match(text)
    if not match:
        raise DslError("静默时段格式应为 23:00-08:00")
    h1, m1, h2, m2 = map(int, match.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise DslError("静默时段不是合法时间")
    if (h1, m1) == (h2, m2):
        return ""
    return f"{h1:02d}:{m1:02d}-{h2:02d}:{m2:02d}"


def _format_duration(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}小时"
    if seconds % 60 == 0:
        return f"{seconds // 60}分钟"
    return f"{seconds}秒"


def format_duration(seconds: int) -> str:
    return _format_duration(seconds)
