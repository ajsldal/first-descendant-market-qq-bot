from __future__ import annotations


class MarketError(Exception):
    """本项目的所有可预期错误的基类。"""


class ApiError(MarketError):
    """服务端返回非 SUCCESS。"""

    def __init__(self, result_code: str, message: str = "", http_status: int = 200) -> None:
        self.result_code = result_code
        self.result_message = message
        self.http_status = http_status
        super().__init__(f"{result_code}: {message}" if message else result_code)


class HttpError(MarketError):
    """HTTP 层错误（403/404/5xx 等）。"""

    def __init__(self, status_code: int, body: str = "") -> None:
        self.status_code = status_code
        self.body = body[:300]
        super().__init__(f"HTTP {status_code}: {self.body}")


class ItemNotFoundError(MarketError):
    """在 Jarvis 字典里找不到该物品。"""


class AmbiguousItemError(MarketError):
    """物品名匹配到多个条目（例如同名模块分属不同类型）。"""


class FilterSpecError(MarketError):
    """筛选条件本身不合法（类型与条件不匹配、数值越界等）。"""


class OptionResolveError(MarketError):
    """属性/辅助能力名称无法映射到服务端 ID。"""
