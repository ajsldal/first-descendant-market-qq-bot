"""无参 `/订阅` 的分步向导。

向导只负责收集文本，最后拼成一条标准 DSL，交给 dsl.parse_dsl 校验，
避免出现第二套条件结构。会话 10 分钟过期。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from .dsl import DslError, option_help, parse_dsl

SESSION_TTL = 600
SKIP_WORDS = {"跳过", "skip", "无", "-", "不填"}


@dataclass
class WizardReply:
    text: str
    dsl: str | None = None
    done: bool = False


@dataclass
class _State:
    step: int
    item: str = ""
    type_label: str = ""
    price: str = ""
    advanced: str = ""
    updated: float = 0.0


Resolver = Callable[[str], tuple[str, str]]


class Wizard:
    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver
        self._states: dict[int, _State] = {}
        # 状态表会被 asyncio.to_thread 访问，加锁保证跨线程安全
        self._lock = threading.Lock()

    def active(self, qq: int) -> bool:
        now = time.time()
        with self._lock:
            state = self._states.get(qq)
            if state is None:
                return False
            if now - state.updated > SESSION_TTL:
                self._states.pop(qq, None)
                return False
            return True

    def cancel(self, qq: int) -> bool:
        with self._lock:
            return self._states.pop(qq, None) is not None

    def purge_expired(self) -> int:
        """清理过期会话，由机器人的维护任务定期调用。"""
        now = time.time()
        with self._lock:
            expired = [
                qq for qq, state in self._states.items()
                if now - state.updated > SESSION_TTL
            ]
            for qq in expired:
                self._states.pop(qq, None)
            return len(expired)

    def start(self, qq: int) -> WizardReply:
        with self._lock:
            self._states[qq] = _State(step=1, updated=time.time())
        return WizardReply(
            "开始创建订阅（回复 /取消 退出）。\n"
            "1/4 请输入物品名或 ItemId，例如 怪物猎人、阴影的决意、211064002："
        )

    def feed(self, qq: int, text: str) -> WizardReply:
        now = time.time()
        with self._lock:
            state = self._states.get(qq)
            if state is None or now - state.updated > SESSION_TTL:
                self._states.pop(qq, None)
                return WizardReply("向导已过期，请重新发送 /订阅")
            state.updated = now

        text = text.strip()

        if state.step == 1:
            try:
                name, type_label = self._resolver(text)
            except Exception as exc:  # noqa: BLE001 — 直接把原因回给用户
                return WizardReply(f"没找到这个物品：{exc}\n请重新输入物品名或 ID：")
            state.item = name
            state.type_label = type_label
            state.step = 2
            return WizardReply(
                f"已选择：{name}（{type_label}）\n"
                "2/4 价格区间，例如 100-5000、>=100、<=5000；回复 跳过 表示不限："
            )

        if state.step == 2:
            price = "" if text.lower() in SKIP_WORDS else text
            error = _validate(state.item, f" 价格={price}" if price else "")
            if error:
                return WizardReply(f"价格不合法：{error}\n请重新输入价格，或回复 跳过：")
            state.price = price
            state.step = 3
            return WizardReply(
                "3/4 附加条件，键=值、空格分隔，例如：品质=390-400 辅助=强化大容量弹夹\n"
                f"{option_help(_type_key(state.type_label))}\n"
                "回复 跳过 表示不加："
            )

        if state.step == 3:
            advanced = "" if text.lower() in SKIP_WORDS else text
            error = _validate(
                state.item,
                (f" 价格={state.price}" if state.price else "") + (f" {advanced}" if advanced else ""),
            )
            if error:
                return WizardReply(f"附加条件不合法：{error}\n请重新输入，或回复 跳过：")
            state.advanced = advanced
            state.step = 4
            return WizardReply(
                "4/4 检查间隔，例如 10m、30m、1h；回复 跳过 使用 10 分钟默认值："
            )

        if state.step == 4:
            interval = "10m" if text.lower() in SKIP_WORDS else text
            parts = f" 间隔={interval}"
            if state.price:
                parts = f" 价格={state.price}" + parts
            if state.advanced:
                parts = f" {state.advanced}" + parts
            error = _validate(state.item, parts)
            if error:
                return WizardReply(f"间隔不合法：{error}\n请重新输入间隔，或回复 跳过：")
            dsl = f"/订阅 {state.item}{parts}"
            with self._lock:
                self._states.pop(qq, None)
            return WizardReply("条件已收集，正在创建订阅…", dsl=dsl, done=True)

        with self._lock:
            self._states.pop(qq, None)
        return WizardReply("向导状态异常，请重新发送 /订阅")


def _validate(item: str, rest: str) -> str | None:
    try:
        parse_dsl(f"{item}{rest}")
    except DslError as exc:
        return str(exc)
    return None


def _type_key(type_label: str) -> str:
    return {
        "超越武器": "transcendent",
        "模块": "ancestor",
        "继承者模块": "descendant",
        "触发器": "trigger",
    }.get(type_label, "")
