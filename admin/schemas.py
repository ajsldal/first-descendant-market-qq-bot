"""后台 API 请求模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


class PermissionsUpdate(BaseModel):
    max_subscriptions: int | None = None
    allow_subscribe: bool | None = None
    allow_login: bool | None = None
    allow_trade: bool | None = None
    allow_price_query: bool | None = None
    note: str | None = Field(default=None, max_length=500)

    @field_validator("max_subscriptions")
    @classmethod
    def _positive_quota(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("max_subscriptions 必须 >= 1")
        return value


class BlockRequest(BaseModel):
    reason: str = Field(default="", max_length=500)
    until: int | None = Field(
        default=None, description="封禁到期时间戳；不传表示永久"
    )


class SettingsUpdate(BaseModel):
    analytics_enabled: bool | None = None
    analytics_whitelist_qq: list[int] | None = None
    analytics_runs_retention_days: int | None = None
    max_subscriptions: int | None = None
    steam_auto_login_enabled: bool | None = None
    steam_auto_allowed_qq: list[int] | None = None
    steam_auto_guard_timeout: int | None = None
    login_allowed_qq: list[int] | None = None
    friend_auto_accept_enabled: bool | None = None
    friend_welcome_enabled: bool | None = None
    friend_accept_max_per_min: int | None = None
    login_proxy_enabled: bool | None = None
    login_proxy_url: str | None = Field(default=None, max_length=200)
    bark_enabled: bool | None = None
    bark_cooldown_sec: int | None = None
    bark_level: str | None = None
    bark_group: str | None = Field(default=None, max_length=40)
    bark_call: bool | None = None
    bark_max_per_min: int | None = None
    flip_enabled: bool | None = None
    flip_fee: float | None = None
    flip_tax_rate: float | None = None
    flip_cooldown_sec: int | None = None
    trade_stats_enabled: bool | None = None
    trade_stats_interval_hours: int | None = None

    @field_validator("trade_stats_interval_hours")
    @classmethod
    def _trade_stats_interval(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 168:
            raise ValueError("成交统计间隔必须在 1~168 小时")
        return value

    @field_validator("friend_accept_max_per_min")
    @classmethod
    def _friend_accept_limit(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 30:
            raise ValueError("每分钟好友通过数必须在 1~30")
        return value

    @field_validator("flip_fee")
    @classmethod
    def _flip_fee(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 100000:
            raise ValueError("手续费必须在 0~100000")
        return value

    @field_validator("flip_tax_rate")
    @classmethod
    def _flip_tax(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 0.5:
            raise ValueError("税率必须在 0~0.5")
        return value

    @field_validator("flip_cooldown_sec")
    @classmethod
    def _flip_cooldown(cls, value: int | None) -> int | None:
        if value is not None and not 10 <= value <= 3600:
            raise ValueError("利润消息冷却必须在 10~3600 秒")
        return value

    @field_validator("bark_level")
    @classmethod
    def _bark_level(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in ("active", "timeSensitive", "passive", "critical"):
            raise ValueError("bark_level 只能是 active/timeSensitive/passive/critical")
        return value

    @field_validator("bark_cooldown_sec")
    @classmethod
    def _bark_cooldown(cls, value: int | None) -> int | None:
        if value is not None and not 10 <= value <= 3600:
            raise ValueError("冷却时间必须在 10~3600 秒")
        return value

    @field_validator("bark_max_per_min")
    @classmethod
    def _bark_rate(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 30:
            raise ValueError("每分钟上限必须在 1~30")
        return value

    @field_validator("login_proxy_url")
    @classmethod
    def _valid_proxy_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return ""
        if not text.startswith(("http://", "https://", "socks5://")):
            raise ValueError("代理地址必须以 http://、https:// 或 socks5:// 开头")
        return text

    @field_validator("analytics_whitelist_qq", "steam_auto_allowed_qq")
    @classmethod
    def _valid_qq(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        return [int(item) for item in value if int(item) > 0]

    @field_validator(
        "analytics_runs_retention_days", "max_subscriptions", "steam_auto_guard_timeout"
    )
    @classmethod
    def _positive(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("必须 >= 1")
        return value


class SteamCredentialsUpdate(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class BarkUpdate(BaseModel):
    target: str | None = Field(default=None, max_length=300, description="完整 Bark 地址或 Key；不改 Key 时留空")
    sound: str | None = Field(default=None, max_length=60)
    enabled: bool | None = None


class BarkToggle(BaseModel):
    enabled: bool


class SubscriptionUpdate(BaseModel):
    enabled: bool | None = None
    interval_sec: int | None = Field(default=None, ge=60, le=86400)
    max_push: int | None = Field(default=None, ge=1, le=50)
    only_new: bool | None = None
    quiet_hours: str | None = Field(default=None, max_length=11)
    first_push: bool | None = None
    next_run_at: int | None = Field(default=None, ge=0)
    bark_enabled: bool | None = None
    bark_sound: str | None = Field(default=None, max_length=60)
    flip_overrides: str | None = Field(default=None, max_length=500)
