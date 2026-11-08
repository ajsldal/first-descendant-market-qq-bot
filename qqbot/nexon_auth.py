"""Nexon 账号登录：Steam 扫码、有头浏览器手动登录、Cookie 导入、静默续期。

Steam 扫码链路（2026-10 实测）：
1. 打开 https://signin.nexon.com/auth-selector?gid=2392 ...
2. 点 Steam → 弹窗 https://steamcommunity.com/openid/login?...
3. 弹窗里点 “Show me a QR code” → 二维码
4. 用户用 Steam 手机 App 扫码并确认
5. 回调 signin.nexon.com/pages/account/sso/callback/ → 回到 tfd.nexon.com
6. 浏览器获得 _if_game_auth / _ifwt_guest_2392 等 Cookie

重要：steamcommunity.com 在国内直连会超时，登录浏览器必须走代理。
交易 API（tfd-api.nexon.com）走代理会 403，所以代理只用于登录/续期浏览器。
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from PIL import Image

logger = logging.getLogger(__name__)

GAME_ID = "2392"
PLATFORM = "arena"
MARKET_URL = "https://tfd.nexon.com/zh-Hans/market"
MAIN_URL = "https://tfd.nexon.com/main"
AUTH_URL = (
    "https://signin.nexon.com/auth-selector?gid=2392"
    "&support_platforms=nexon%2Csteam%2Cxbox%2Cpsn"
    "&redirect_uri=https%3A%2F%2Ftfd.nexon.com%2Fmain"
)
STEAM_OPENID_URL = (
    "https://steamcommunity.com/openid/login"
    "?openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
    "&openid.mode=checkid_setup"
    "&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0"
    "&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
    "&openid.realm=https%3A%2F%2Fsignin.nexon.com"
    "&openid.return_to=https%3A%2F%2Fsignin.nexon.com%2Fpages%2Faccount%2Fsso%2Fcallback%2F"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
# 登录代理只用于 Steam（国内直连 steamcommunity.com 会超时）。
# Nexon 全系域名直连：代理节点对 signin/tfd-api 不稳定，实测会 SSL 断开或 403；
# 直连时 signin 选择页、市场页与 API 都可达。
TFD_DIRECT_HOSTS = (
    "nexon.com,*.nexon.com,"
    "challenges.cloudflare.com,*.challenges.cloudflare.com"
)
REGION_ERROR_MARKERS = (
    "region currently not supported",
    "目前尚未支援的地區",
    "지원하지 않는 지역",
    "113001",
    "not_supported_region",
)


class LoginError(RuntimeError):
    """登录流程可预期失败，message 可直接回复给用户。"""


class LoginTimeout(LoginError):
    pass


class LoginCancelled(LoginError):
    pass


class CookieImportError(LoginError):
    """用户粘贴的 Cookie 文本无法解析或不含登录 Cookie。"""


@dataclass
class LoginCookies:
    cookies: dict[str, str]
    user_agent: str
    platform: str
    complete: bool
    method: str = "steam_qr"

    @property
    def has_core(self) -> bool:
        return has_login_cookies(self.cookies)


@dataclass
class ParsedCookiePayload:
    cookies: dict[str, str]
    source: str


def has_login_cookies(cookies: dict[str, str]) -> bool:
    """运行时登录态：必须同时有游戏认证和 guest web token。"""
    return "_if_game_auth" in cookies and "_ifwt_guest_2392" in cookies


def has_importable_login_cookies(cookies: dict[str, str]) -> bool:
    """导入时放宽到 _ifwt，最终仍会用 /chat 校验。"""
    if "_if_game_auth" not in cookies:
        return False
    return "_ifwt_guest_2392" in cookies or "_ifwt" in cookies


def _playwright_proxy(
    value: str | None, bypass: str | None = None
) -> dict[str, str] | None:
    """把 http://user:pass@host:port 转成 Playwright 的 proxy 字典。"""
    if not value or value.strip().lower() in ("none", "off", "0"):
        return None
    raw = value.strip()
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    if not parsed.hostname or not parsed.port:
        raise LoginError(f"代理地址不合法：{value}（示例 http://127.0.0.1:7897）")
    proxy = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    if bypass:
        proxy["bypass"] = bypass
    return proxy


def _expand_qr(png: bytes) -> bytes:
    """把 Steam 的 165px 二维码放大到 ~600px 并补白边，方便 QQ 图片扫描。"""
    try:
        image = Image.open(io.BytesIO(png)).convert("RGB")
    except Exception:  # noqa: BLE001 — 截图异常时原样返回
        return png
    longest = max(image.size)
    scale = max(1, round(600 / longest))
    if scale > 1:
        image = image.resize(
            (image.width * scale, image.height * scale), Image.Resampling.NEAREST
        )
    border = max(24, image.width // 12)
    canvas = Image.new("RGB", (image.width + border * 2, image.height + border * 2), "white")
    canvas.paste(image, (border, border))
    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


class QRLoginSession:
    """一次登录会话。run() 是同步阻塞调用，请放在独立线程执行。

    manual=False：Steam 二维码登录；
    manual=True ：在机器人主机弹出有头浏览器，用户自己完成任意登录。
    """

    def __init__(
        self,
        *,
        on_qr: Callable[[bytes, bool], None] | None = None,
        on_ready: Callable[[], None] | None = None,
        proxy: str | None = None,
        proxy_bypass: str | None = TFD_DIRECT_HOSTS,
        timeout_s: int = 300,
        headless: bool = True,
        manual: bool = False,
        debug: bool = False,
        steam_credentials: tuple[str, str] | None = None,
        on_notice: Callable[[str], None] | None = None,
    ) -> None:
        self.on_qr = on_qr
        self.on_ready = on_ready
        self.proxy = proxy
        self.proxy_bypass = proxy_bypass
        self.timeout_s = max(60, int(timeout_s))
        self.headless = headless
        self.manual = manual
        self.debug = debug
        self.steam_credentials = steam_credentials
        self.on_notice = on_notice
        self._cancelled = False
        self._debug_pages: set[int] = set()
        self._last_api: dict[str, Any] = {}

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> LoginCookies:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - 环境问题
            raise LoginError("缺少 playwright，请先安装依赖") from exc

        proxy = _playwright_proxy(self.proxy, self.proxy_bypass)
        logger.info(
            "启动%s登录（代理=%s，直连=%s）",
            (
                "Steam 自动"
                if self.steam_credentials
                else ("有头浏览器" if self.manual else " Steam 扫码")
            ),
            self.proxy or "无",
            self.proxy_bypass or "无",
        )
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(
                    headless=self.headless and not self.manual and not self.steam_credentials,
                    args=["--disable-blink-features=AutomationControlled"],
                )
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                if "Executable doesn't exist" in message or "playwright install" in message:
                    raise LoginError(
                        "Playwright Chromium 未安装，请运行："
                        ".\\\\.venv\\\\Scripts\\\\python.exe -m playwright install chromium"
                    ) from exc
                raise LoginError(f"启动登录浏览器失败：{message[:200]}") from exc
            context = browser.new_context(
                locale=(
                    "zh-CN" if (self.manual or self.steam_credentials) else "en-US"
                ),
                viewport={"width": 1280, "height": 1000},
                device_scale_factor=2,
                proxy=proxy,
            )
            context.add_cookies(
                [
                    {"name": "_ifplatform", "value": PLATFORM, "domain": ".nexon.com", "path": "/"},
                    {"name": "_ifgid", "value": GAME_ID, "domain": ".nexon.com", "path": "/"},
                    {"name": "_iflocale", "value": "zh-CN", "domain": ".nexon.com", "path": "/"},
                ]
            )
            if self.debug:
                context.on("page", lambda page: self._attach_page_debug(page))
                logger.info(
                    "[audit] context 已创建 proxy=%s bypass=%s",
                    self.proxy or "无",
                    self.proxy_bypass or "无",
                )
            try:
                return self._flow(context)
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass

    # ── 主流程 ──────────────────────────────────────────────

    def _flow(self, context) -> LoginCookies:  # noqa: ANN001
        page = context.new_page()
        if self.steam_credentials:
            return self._auto_flow(context, page)
        if self.manual:
            # 用 Nexon 登录选择页而不是市场页：市场页的登录按钮依赖 API，
            # 容易受 Cloudflare 挑战影响；登录选择页可稳定打开 Steam/Nexon ID 等入口。
            try:
                page.goto(AUTH_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:  # noqa: BLE001
                raise LoginError(
                    f"无法打开 Nexon 登录选择页：{exc}\n"
                    "如果配置了代理，可尝试去掉 --login-proxy 后重试，或改用 /导入登录。"
                ) from exc
            page.wait_for_timeout(3000)
            if self.debug:
                logger.info(
                    "[audit] 登录选择页 url=%s cookies=%s",
                    page.url[:200],
                    self._cookie_summary(context),
                )
                self._shot(page, "manual_login_selector")
                try:
                    info = page.evaluate(
                        """() => {
                          const buttons=[...document.querySelectorAll('button,a,[role=button]')]
                            .map(e=>({t:(e.innerText||'').trim().slice(0,40),
                                      cls:(e.className||'').toString().slice(0,60),
                                      aria:e.getAttribute('aria-label')||''}))
                            .filter(x=>x.t)
                            .slice(0,20);
                          return {
                            title: document.title,
                            body: document.body.innerText.slice(0,300),
                            buttons,
                          };
                        }"""
                    )
                    logger.info(
                        "[audit] DOM %s",
                        json.dumps(info, ensure_ascii=False)[:1500],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.info("[audit] DOM eval failed: %s", exc)
            if self.on_ready:
                self.on_ready()
            return self._wait(context, page, None)

        try:
            page.goto(AUTH_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            raise LoginError(
                "无法打开 Nexon 登录页。若在国内网络环境，请先给登录浏览器配置代理。"
                f"（{exc}）"
            ) from exc
        page.wait_for_timeout(4000)
        if self.debug:
            logger.info(
                "[audit] 登录选择页 url=%s cookies=%s",
                page.url[:200],
                self._cookie_summary(context),
            )
            self._shot(page, "auth_selector")

        steam = self._open_steam(page)
        try:
            steam.wait_for_load_state("domcontentloaded", timeout=60000)
        except Exception:  # noqa: BLE001
            pass
        steam.wait_for_timeout(3000)
        self._check_region(steam)
        self._ensure_qr(steam)

        first_png = self._capture_qr(steam)
        if not first_png:
            raise LoginError("没有找到 Steam 二维码，登录页结构可能已变化。")
        if self.on_qr:
            self.on_qr(first_png, False)
        if self.debug:
            logger.info("[audit] Steam 二维码已截取 popup=%s", steam.url[:200])
            self._shot(steam, "steam_qr")

        return self._wait(context, page, steam)

    def _open_steam(self, page):  # noqa: ANN001
        """点击 Steam 按钮并返回弹窗页；失败时回退到直接打开 OpenID 地址。"""
        try:
            button = page.locator("button:has-text('Steam')").first
            if not button.count():
                raise LoginError("登录页没有找到 Steam 按钮")
            with page.expect_popup(timeout=20000) as popup_info:
                button.click(timeout=8000)
            return popup_info.value
        except LoginError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Steam 弹窗未打开，回退到 OpenID 直连：%s", exc)
            steam = page.context.new_page()
            try:
                steam.goto(STEAM_OPENID_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as exc2:  # noqa: BLE001
                raise LoginError(
                    "无法访问 steamcommunity.com。请确认登录代理可用"
                    "（例如 --login-proxy http://127.0.0.1:7897）。"
                ) from exc2
            return steam

    @staticmethod
    def _check_region(page) -> None:  # noqa: ANN001
        try:
            text = page.inner_text("body")[:2000].lower()
        except Exception:  # noqa: BLE001
            return
        if any(marker.lower() in text for marker in REGION_ERROR_MARKERS):
            raise LoginError(
                "Nexon 拒绝了当前登录地区（113001）。请给登录浏览器配置代理后重试。"
            )

    @staticmethod
    def _ensure_qr(page) -> None:  # noqa: ANN001
        qr = page.locator('img[src^="blob:"]')
        if qr.count() and qr.last.is_visible():
            return
        for selector in (
            "text=Show me a QR code",
            "text=Show me a QR-code",
            "text=Sign in with QR",
            "text=QR code",
            "text=QRコード",
            "text=QR 코드",
            "text=二维码",
        ):
            try:
                link = page.locator(selector).first
                if link.count() and link.is_visible():
                    link.click(timeout=5000)
                    break
            except Exception:  # noqa: BLE001
                continue
        try:
            page.wait_for_selector('img[src^="blob:"]:visible', timeout=20000)
        except Exception as exc:  # noqa: BLE001
            raise LoginError("Steam 登录页没有显示二维码。") from exc
        # 二维码刚出现时上方还有一层模糊 + 转圈的加载态，等它消失后再截图
        page.wait_for_timeout(1500)

    @staticmethod
    def _qr_element(page):  # noqa: ANN001
        return page.locator('img[src^="blob:"]').last

    def _capture_qr(self, page) -> bytes | None:  # noqa: ANN001
        try:
            element = self._qr_element(page)
            element.scroll_into_view_if_needed()
            png = element.screenshot()
            # 加载态截图会带模糊背景与转圈，体积明显偏大；重试拿清晰版本
            for _ in range(3):
                if len(png) < 20000:
                    break
                page.wait_for_timeout(1000)
                png = element.screenshot()
        except Exception as exc:  # noqa: BLE001
            logger.warning("二维码截图失败：%s", exc)
            return None
        return _expand_qr(png)

    def _notice(self, message: str) -> None:
        if self.on_notice is None:
            return
        try:
            self.on_notice(message)
        except Exception:  # noqa: BLE001 — 通知失败不能影响登录
            logger.exception("登录通知发送失败")

    def _auto_flow(self, context, page) -> LoginCookies:  # noqa: ANN001
        """Nexon → Steam → 自动填账号密码；需要 Guard/验证码时保留窗口转人工。"""
        try:
            page.goto(AUTH_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            raise LoginError(f"无法打开 Nexon 登录选择页：{exc}") from exc
        page.wait_for_timeout(3000)
        if self.on_ready:
            self.on_ready()

        steam = self._open_steam(page)
        try:
            steam.wait_for_load_state("domcontentloaded", timeout=60000)
        except Exception:  # noqa: BLE001
            pass
        try:
            steam.wait_for_timeout(3000)
        except Exception as exc:  # noqa: BLE001
            raise LoginError("登录浏览器已被关闭。") from exc
        self._check_region(steam)

        username, password = self.steam_credentials or ("", "")
        status = self._steam_password_login(steam, username, password)
        logger.info("Steam 自动填写状态：%s", status)
        if status == "closed":
            raise LoginError("登录浏览器已被关闭。")
        messages = {
            "filled": "已自动填写 Steam 账号并提交，正在等待登录结果。",
            "guard": "需要 Steam Guard / 邮箱验证码，请在浏览器里完成验证；完成后会自动保存登录态。",
            "form_missing": "没有找到 Steam 密码输入框，请在浏览器里手动完成登录。",
            "bad_credentials": "Steam 提示账号或密码错误，请在浏览器里手动重试。",
            "challenge": "Steam 要求额外验证，请在浏览器里手动完成。",
            "unknown": "自动填写未确认成功，请在浏览器里检查并手动完成。",
        }
        if status in messages:
            self._notice(messages[status])
        return self._wait(context, page, steam)

    @staticmethod
    def _visible(loc, timeout: int = 1000) -> bool:  # noqa: ANN001
        try:
            return bool(loc.count()) and loc.is_visible(timeout=timeout)
        except Exception:  # noqa: BLE001
            return False

    def _click_first(self, page, selectors: list[str], timeout: int = 6000) -> bool:  # noqa: ANN001
        for selector in selectors:
            loc = page.locator(selector).first
            if not self._visible(loc):
                continue
            try:
                if not loc.is_enabled():
                    continue
            except Exception:  # noqa: BLE001
                pass
            try:
                loc.click(timeout=timeout)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _find_steam_form(self, steam, timeout_s: int = 25):  # noqa: ANN001
        """慢网速下持续等待 Steam 表单；必要时点“Sign in”切到密码登录。"""
        deadline = time.monotonic() + timeout_s
        started = time.monotonic()
        switched = False
        while time.monotonic() < deadline:
            user_input = None
            for selector in (
                'input[name="username"]',
                'input[name="userName"]',
                'input[autocomplete="username"]',
                'input[type="text"]',
                'input[type="email"]',
            ):
                loc = steam.locator(selector).first
                if self._visible(loc, timeout=500):
                    user_input = loc
                    break
            pass_input = None
            for selector in ('input[name="password"]', 'input[type="password"]'):
                loc = steam.locator(selector).first
                if self._visible(loc, timeout=500):
                    pass_input = loc
                    break
            if user_input is not None and pass_input is not None:
                return user_input, pass_input

            if (
                not switched
                and time.monotonic() - started >= 3
            ):
                switched = self._click_first(
                    steam,
                    [
                        "button:has-text('Sign in with account name')",
                        "a:has-text('Sign in with account name')",
                        "a:has-text('Sign in')",
                        "button:has-text('Sign in')",
                        "a:has-text('登录')",
                        "button:has-text('登录')",
                        "button:has-text('密码')",
                        "a:has-text('密码')",
                        "#loginLink",
                    ],
                )
                if switched:
                    logger.info("已切换 Steam 到账号密码登录")
                    try:
                        steam.wait_for_timeout(1500)
                    except Exception:  # noqa: BLE001
                        return None
            time.sleep(0.8)
        return None

    def _wait_login_outcome(self, steam, timeout_s: int = 30) -> str:  # noqa: ANN001
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            text = ""
            try:
                text = (steam.inner_text("body") or "")[:6000]
            except Exception:  # noqa: BLE001
                pass
            lower = text.lower()
            guard_markers = (
                "steam guard",
                "verification code",
                "验证码",
                "guard code",
                "authenticator",
                "确认您的身份",
                "发送验证码",
            )
            if any(marker.lower() in lower for marker in guard_markers):
                return "guard"
            bad_markers = (
                "incorrect",
                "wrong password",
                "invalid password",
                "密码错误",
                "账号或密码",
                "too many login",
                "try again later",
            )
            if any(marker in lower for marker in bad_markers):
                return "bad_credentials"
            challenge_markers = (
                "captcha",
                "verify you are human",
                "人机验证",
                "too many attempts",
            )
            if any(marker in lower for marker in challenge_markers):
                return "challenge"
            try:
                if not self._visible(
                    steam.locator('input[type="password"]').first, timeout=300
                ):
                    return "filled"
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0)
        return "filled"

    def _steam_password_login(self, steam, username: str, password: str) -> str:  # noqa: ANN001
        """在 Steam 页面填写账号密码；只返回状态，不记录任何输入内容。"""
        if not username or not password:
            return "form_missing"

        for attempt in range(4):
            form = self._find_steam_form(steam, timeout_s=25)
            if form is None:
                if attempt < 3:
                    try:
                        steam.wait_for_timeout(1500)
                    except Exception:  # noqa: BLE001
                        return "closed"
                    continue
                return "form_missing"
            user_input, pass_input = form
            logger.info("已找到 Steam 登录表单（第 %s 次尝试）", attempt + 1)
            try:
                user_input.click(timeout=3000)
                user_input.fill("", timeout=3000)
                user_input.fill(username, timeout=5000)
                pass_input.click(timeout=3000)
                pass_input.fill("", timeout=3000)
                pass_input.fill(password, timeout=5000)
            except Exception as exc:  # noqa: BLE001
                if "closed" in str(exc).lower():
                    return "closed"
                if attempt < 3:
                    try:
                        steam.wait_for_timeout(1500)
                    except Exception:  # noqa: BLE001
                        return "closed"
                    continue
                return "form_missing"

            clicked = self._click_first(
                steam,
                [
                    "button[type='submit']",
                    "#login_btn_signin",
                    "button:has-text('Sign in')",
                    "button:has-text('登录')",
                    "input[type='submit']",
                ],
            )
            logger.info("已填写 Steam 账号密码，提交=%s", clicked)
            if not clicked:
                try:
                    pass_input.press("Enter", timeout=3000)
                except Exception as exc:  # noqa: BLE001
                    if "closed" in str(exc).lower():
                        return "closed"
                    if attempt < 3:
                        try:
                            steam.wait_for_timeout(1500)
                        except Exception:  # noqa: BLE001
                            return "closed"
                        continue
                    return "unknown"
            return self._wait_login_outcome(steam, timeout_s=45)
        return "form_missing"

    @staticmethod
    def _auth_cookie_names(context) -> dict[str, int]:  # noqa: ANN001
        wanted = ("_if_game_auth", "_ifwt_guest_2392", "_ifwt", "A2SK", "PCID", "AWSALB")
        result: dict[str, int] = {}
        try:
            for cookie in context.cookies():
                name = str(cookie.get("name") or "")
                if name in wanted:
                    result[name] = len(str(cookie.get("value") or ""))
        except Exception:  # noqa: BLE001
            pass
        return result

    def _wait(self, context, page, steam) -> LoginCookies:  # noqa: ANN001
        deadline = time.monotonic() + self.timeout_s
        last_src = self._qr_src(steam) if steam is not None else ""
        finalize_started = False
        callback_seen = False
        callback_seen_at = 0.0
        consent_clicked = False
        last_emit = time.monotonic()
        last_debug = 0.0

        while time.monotonic() < deadline:
            if self._cancelled:
                raise LoginCancelled("登录已取消。")
            try:
                if page.is_closed():
                    raise LoginError("登录浏览器已被关闭。")
            except LoginError:
                raise
            except Exception:  # noqa: BLE001
                pass

            cookies = self._cookie_dict(context)
            if has_login_cookies(cookies):
                # 让回调流程把剩余 Cookie（A2SK/PCID 等）写完
                page.wait_for_timeout(1200)
                if self.debug:
                    logger.info("[audit] 检测到登录 Cookie：%s", self._cookie_summary(context))
                    if not self.steam_credentials:
                        self._shot(page, "logged_in")
                return self._finish(context)

            steam_closed = False
            steam_url = ""
            if steam is not None:
                try:
                    if steam.is_closed():
                        steam_closed = True
                    else:
                        steam_url = steam.url
                except Exception:  # noqa: BLE001
                    steam_closed = True
                if "nexon.com" in steam_url and not callback_seen:
                    callback_seen = True
                    callback_seen_at = time.monotonic()
                    logger.info(
                        "登录回调已出现，等待回调完成后再访问主站 cookies=%s",
                        self._auth_cookie_names(context),
                    )

            # Steam 邮箱/手机验证码完成后，会出现 OpenID 授权页，自动点“登录”。
            if (
                self.steam_credentials
                and not consent_clicked
                and steam is not None
                and not steam_closed
            ):
                try:
                    page_text = (steam.inner_text("body") or "")[:3000]
                    is_consent = (
                        "openid/loginform" in steam_url
                        or "使用您的 Steam 帐户登录到" in page_text
                        or "Sign in through STEAM" in page_text
                    )
                    if is_consent:
                        clicked = self._click_first(
                            steam,
                            [
                                "button:has-text('登录')",
                                "button:has-text('Sign In')",
                                "button:has-text('Sign in')",
                                "input[type='submit']",
                            ],
                            timeout=4000,
                        )
                        if clicked:
                            consent_clicked = True
                            logger.info("已自动点击 Steam OpenID 授权页的登录按钮")
                except Exception:  # noqa: BLE001
                    pass

            # 自动登录：不要自己跳 /main 或 /market。
            # Steam 授权成功后，Nexon 会带 web_ticket 跳到 tfd.nexon.com/.../main，
            # 由这个页面写入 _ifwt_guest_2392；提前跳裸 /main 会丢掉 web_ticket。
            if self.steam_credentials:
                if "web_ticket" in steam_url:
                    logger.info("检测到 Nexon web_ticket 回调，等待页面写入登录 Cookie")
                time.sleep(1.5)
                continue

            should_finalize = steam_closed or "_ifwt" in cookies
            # 回调页可能还需要较长时间写 `_ifwt_guest_2392`；自动登录多等一会儿，
            # 避免像旧逻辑那样过早跳主站/市场导致 NO_LOGIN。
            callback_grace = 45 if self.steam_credentials else 10
            if (
                not should_finalize
                and callback_seen
                and time.monotonic() - callback_seen_at >= callback_grace
            ):
                should_finalize = True
            if should_finalize and not finalize_started:
                finalize_started = True
                logger.info("登录回调已完成，访问主站完成 Nexon 登录态")
                try:
                    page.goto(MAIN_URL, wait_until="domcontentloaded", timeout=60000)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("打开主站失败：%s", exc)
                for _ in range(15):
                    page.wait_for_timeout(1000)
                    cookies = self._cookie_dict(context)
                    if has_login_cookies(cookies):
                        break
                logger.info("访问主站后 cookies=%s", self._auth_cookie_names(context))
                if not has_login_cookies(cookies):
                    try:
                        page.goto(MARKET_URL, wait_until="domcontentloaded", timeout=60000)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("打开市场页失败：%s", exc)
                    for _ in range(15):
                        page.wait_for_timeout(1000)
                        cookies = self._cookie_dict(context)
                        if has_login_cookies(cookies):
                            break
                    logger.info("访问市场页后 cookies=%s", self._auth_cookie_names(context))
                if has_login_cookies(cookies):
                    page.wait_for_timeout(1200)
                    if self.debug:
                        logger.info(
                            "[audit] 最终化后检测到登录 Cookie：%s",
                            self._cookie_summary(context),
                        )
                        if not self.steam_credentials:
                            self._shot(page, "logged_in")
                    return self._finish(context)

            if self.debug and time.monotonic() - last_debug >= 5:
                last_debug = time.monotonic()
                logger.info(
                    "[audit] page=%s popups=%s core=%s callback=%s finalize=%s last_api=%s cookies=%s",
                    page.url[:180],
                    len(context.pages),
                    has_login_cookies(cookies),
                    callback_seen,
                    finalize_started,
                    self._last_api or "-",
                    self._cookie_summary(context),
                )
                if self.manual and not self.steam_credentials:
                    self._shot(page, "manual_wait")

            # 二维码自动刷新时，把新码推给用户
            if (
                steam is not None
                and not steam_closed
                and not callback_seen
                and time.monotonic() - last_emit > 20
            ):
                src = self._qr_src(steam)
                if src and src != last_src:
                    png = self._capture_qr(steam)
                    if png and self.on_qr:
                        try:
                            self.on_qr(png, True)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("二维码刷新推送失败：%s", exc)
                    last_src = src
                    last_emit = time.monotonic()

            time.sleep(1.5)

        partial = self._cookie_dict(context)
        if self.debug:
            logger.info(
                "[audit] 登录超时 page=%s cookies=%s last_api=%s",
                page.url[:200],
                self._cookie_summary(context),
                self._last_api or "-",
            )
            if not self.steam_credentials:
                self._shot(page, "timeout")
        hint = "已拿到部分 Cookie，但缺少 _ifwt_guest_2392" if "_ifwt" in partial else "没有拿到登录 Cookie"
        if self.steam_credentials:
            raise LoginTimeout(
                f"Steam 自动登录等待超时（{hint}）。验证码完成后才会保存登录态；"
                "请重新发送 /登录 自动，并在浏览器打开期间完成验证。"
            )
        if self.manual:
            raise LoginTimeout(f"浏览器登录超时（{hint}）。请重新发送 /浏览器登录。")
        raise LoginTimeout(f"登录超时（{hint}）。请重新发送 /登录 获取新二维码。")

    # ── 结果与工具 ──────────────────────────────────────────

    @staticmethod
    def _cookie_dict(context) -> dict[str, str]:  # noqa: ANN001
        values: dict[str, str] = {}
        try:
            cookies = context.cookies()
        except Exception:  # noqa: BLE001
            return values
        for cookie in cookies:
            if "nexon.com" not in str(cookie.get("domain", "")):
                continue
            if cookie.get("value"):
                values[str(cookie["name"])] = str(cookie["value"])
        return values

    @staticmethod
    def _qr_src(page) -> str:  # noqa: ANN001
        try:
            value = QRLoginSession._qr_element(page).get_attribute("src")
            return value or ""
        except Exception:  # noqa: BLE001
            return ""

    def _finish(self, context) -> LoginCookies:  # noqa: ANN001
        cookies = self._cookie_dict(context)
        user_agent = USER_AGENT
        try:
            for page in context.pages:
                user_agent = str(page.evaluate("navigator.userAgent") or USER_AGENT)
                break
        except Exception:  # noqa: BLE001
            pass
        return LoginCookies(
            cookies=cookies,
            user_agent=user_agent,
            platform=PLATFORM,
            complete=has_login_cookies(cookies),
            method="manual_browser" if self.manual else "steam_qr",
        )

    # ── 审计日志 ────────────────────────────────────────────

    @staticmethod
    def _is_audit_url(url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host in (
            "signin.nexon.com",
            "tfd.nexon.com",
            "tfd-api.nexon.com",
            "jarvis-api.nexon.com",
            "challenges.cloudflare.com",
        ):
            return True
        if host.endswith(".tfd.nexon.com") or host.endswith(".jarvis-api.nexon.com"):
            return True
        if host == "steamcommunity.com" and parsed.path.startswith("/openid"):
            return True
        return False

    def _attach_page_debug(self, page) -> None:  # noqa: ANN001
        key = id(page)
        if key in self._debug_pages:
            return
        self._debug_pages.add(key)
        logger.info("[audit] attach page id=%s url=%s", key, (page.url or "")[:200])

        def on_nav(frame):  # noqa: ANN001
            if frame == page.main_frame:
                logger.info("[audit] nav %s", frame.url[:300])

        page.on("framenavigated", on_nav)
        page.on(
            "requestfailed",
            lambda req: logger.info(
                "[audit] failed %s %s %s",
                req.resource_type,
                req.url[:280],
                req.failure,
            )
            if self._is_audit_url(req.url)
            else None,
        )
        page.on(
            "console",
            lambda msg: logger.info(
                "[audit] console %s %s", msg.type, msg.text[:300]
            )
            if msg.type in ("error", "warning")
            and any(k in msg.text for k in ("nexon", "CORS", "challenge", "403"))
            else None,
        )
        page.on(
            "pageerror",
            lambda exc: logger.info("[audit] pageerror %s", str(exc)[:300]),
        )
        page.on(
            "dialog",
            lambda dialog: logger.info("[audit] dialog %s", dialog.message[:200]),
        )
        page.on(
            "request",
            lambda req: logger.info(
                "[audit] req %s %s %s", req.method, req.resource_type, req.url[:260]
            )
            if self._is_audit_url(req.url)
            else None,
        )
        page.on(
            "response",
            lambda resp: self._log_response(resp)
            if self._is_audit_url(resp.url)
            else None,
        )

    def _log_response(self, resp) -> None:  # noqa: ANN001
        url = resp.url
        method = resp.request.method
        headers = resp.headers
        summary = {
            k: headers.get(k)
            for k in (
                "access-control-allow-origin",
                "access-control-allow-methods",
                "access-control-allow-headers",
                "content-type",
                "cf-ray",
                "cf-mitigated",
                "server",
            )
            if headers.get(k)
        }
        if "tfd-api.nexon.com" in url or "jarvis-api.nexon.com" in url:
            body = ""
            try:
                if "json" in str(headers.get("content-type", "")).lower():
                    data = resp.json()
                    if isinstance(data, dict):
                        result = data.get("ResultData")
                        total = None
                        items = None
                        if isinstance(result, dict):
                            total = result.get("totalCount")
                            raw_items = result.get("items")
                            items = len(raw_items) if isinstance(raw_items, list) else None
                        body = (
                            f"code={data.get('ResultCode')} total={total} items={items}"
                        )
                        self._last_api = {
                            "status": resp.status,
                            "code": data.get("ResultCode"),
                            "total": total,
                            "items": items,
                        }
                else:
                    body = resp.text()[:200].replace("\n", " ")
            except Exception:  # noqa: BLE001
                body = "(body unavailable)"
            logger.info(
                "[audit] resp %s %s %s summary=%s headers=%s body=%s",
                resp.status,
                method,
                url[:240],
                body,
                summary,
                "" if resp.status < 400 else resp.text()[:200].replace("\n", " "),
            )
            return
        logger.info("[audit] resp %s %s %s headers=%s", resp.status, method, url[:240], summary)

    def _cookie_summary(self, context) -> str:  # noqa: ANN001
        items: list[str] = []
        try:
            for cookie in context.cookies():
                domain = str(cookie.get("domain", ""))
                if "nexon.com" not in domain:
                    continue
                items.append(
                    f"{cookie.get('name')}@{domain}:{len(str(cookie.get('value', '')))}"
                )
        except Exception as exc:  # noqa: BLE001
            return f"ERR {exc}"
        return ",".join(items)

    def _shot(self, page, name: str) -> None:  # noqa: ANN001
        if not self.debug:
            return
        try:
            directory = Path("logs/login_audit")
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / f"{name}.png"), full_page=True)
            logger.info("[audit] screenshot %s", directory / f"{name}.png")
        except Exception as exc:  # noqa: BLE001
            logger.info("[audit] screenshot failed %s: %s", name, exc)


class ManualLoginSession(QRLoginSession):
    """在机器人主机弹出有头浏览器，用户自己完成 Nexon/Steam/Xbox/PSN 登录。"""

    def __init__(
        self,
        *,
        on_ready: Callable[[], None] | None = None,
        proxy: str | None = None,
        proxy_bypass: str | None = TFD_DIRECT_HOSTS,
        timeout_s: int = 600,
        debug: bool = False,
    ) -> None:
        super().__init__(
            on_ready=on_ready,
            proxy=proxy,
            proxy_bypass=proxy_bypass,
            timeout_s=timeout_s,
            headless=False,
            manual=True,
            debug=debug,
        )


# ── Cookie 文本导入 ──────────────────────────────────────

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")


def _strip_fence(text: str) -> str:
    value = (text or "").strip()
    value = _FENCE_RE.sub("", value).strip()
    if value.lower().startswith("cookie:"):
        value = value.split(":", 1)[1].strip()
    return value


def _normalize_same_site(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip().lower()
    mapping = {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no_restriction": "None",
        "unspecified": None,
        "": None,
    }
    return mapping.get(text)


def _normalize_cookie_entries(raw: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return entries
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = item.get("value")
        if not name or value is None:
            continue
        entry: dict[str, Any] = {
            "name": name,
            "value": str(value),
            "domain": str(item.get("domain") or ".nexon.com"),
            "path": str(item.get("path") or "/"),
        }
        expires = item.get("expires")
        try:
            expires_num = float(expires) if expires is not None else -1
        except (TypeError, ValueError):
            expires_num = -1
        entry["expires"] = expires_num if expires_num > 0 else -1
        entry["httpOnly"] = bool(item.get("httpOnly", False))
        entry["secure"] = bool(item.get("secure", False))
        same_site = _normalize_same_site(item.get("sameSite"))
        if same_site:
            entry["sameSite"] = same_site
            if same_site == "None":
                entry["secure"] = True
        entries.append(entry)
    return entries


def _cookies_from_entries(entries: list[dict[str, Any]]) -> dict[str, str]:
    return {str(e["name"]): str(e["value"]) for e in entries if e.get("name")}


def _parse_cookie_header(text: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in re.split(r"[;\n\r]+", text):
        item = part.strip()
        if not item or "=" not in item:
            continue
        name, _, value = item.partition("=")
        name = name.strip()
        value = value.strip().strip('"')
        if name:
            cookies[name] = value
    return cookies


def _from_json(data: Any) -> tuple[dict[str, str], str]:
    if isinstance(data, list):
        return _cookies_from_entries(_normalize_cookie_entries(data)), "cookie_list"

    if not isinstance(data, dict):
        raise CookieImportError("JSON 必须是对象或 Cookie 数组。")

    if "name" in data and "value" in data:
        return _cookies_from_entries(_normalize_cookie_entries([data])), "cookie_object"

    if "cookies" in data:
        raw_cookies = data.get("cookies")
        if isinstance(raw_cookies, dict):
            return (
                {str(k): str(v) for k, v in raw_cookies.items() if v is not None},
                "cookies_dict",
            )
        return (
            _cookies_from_entries(_normalize_cookie_entries(raw_cookies)),
            "storage_state" if "origins" in data else "cookies_json",
        )

    # 形如 {"_if_game_auth": "...", "_ifwt_guest_2392": "..."}
    flat: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, (str, int, float, bool)) and value is not None:
            flat[str(key)] = str(value)
    if flat:
        return flat, "flat_json"

    raise CookieImportError("JSON 里没有找到 Cookie。")


def parse_cookie_payload(text: str) -> ParsedCookiePayload:
    """解析用户粘贴的 Cookie JSON / cookies.json / storage_state.json / Cookie 头。"""
    raw = _strip_fence(text)
    if not raw:
        raise CookieImportError("内容为空。")

    if raw.startswith("{") or raw.startswith("["):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CookieImportError(f"JSON 不完整或格式错误：{exc.msg}") from exc
        cookies, source = _from_json(data)
    elif "=" in raw:
        cookies = _parse_cookie_header(raw)
        source = "cookie_header"
    else:
        raise CookieImportError(
            "无法识别的格式。请粘贴 Cookie JSON、cookies.json、storage_state.json "
            "或 `_if_game_auth=...; _ifwt_guest_2392=...` 这样的 Cookie 字符串。"
        )

    if not cookies:
        raise CookieImportError("没有解析出任何 Cookie。")
    if not has_importable_login_cookies(cookies):
        missing = []
        if "_if_game_auth" not in cookies:
            missing.append("_if_game_auth")
        if "_ifwt_guest_2392" not in cookies and "_ifwt" not in cookies:
            missing.append("_ifwt_guest_2392")
        raise CookieImportError(
            "缺少登录 Cookie：" + "、".join(missing) + "。请在已登录的浏览器里重新导出。"
        )
    return ParsedCookiePayload(cookies=cookies, source=source)
