"""Steam 凭据的本地加密存储（Windows DPAPI）。

明文只包含 Steam 用户名/密码；落盘前用当前 Windows 用户作用域的 DPAPI 加密。
换 Windows 用户或换机器后无法解密，需要重新录入。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wintypes
import getpass
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "data" / "secrets" / "steam_vault.bin"
ENTROPY = b"tfd-market-v2/steam-vault/v1"
DESCRIPTION = "tfd-market-v2 steam credentials"


class VaultError(RuntimeError):
    """凭据无法读取或解密；message 可直接展示。"""


@dataclass(frozen=True)
class SteamCredentials:
    username: str
    password: str


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _crypt32():
    if os.name != "nt":
        raise VaultError("Steam 凭据加密仅支持 Windows。")
    return ctypes.windll.crypt32, ctypes.windll.kernel32


def _protect(plaintext: bytes) -> bytes:
    crypt32, kernel32 = _crypt32()
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    data_in, _buf_in = _blob(plaintext)
    entropy, _buf_entropy = _blob(ENTROPY)
    data_out = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(data_in),
        DESCRIPTION,
        ctypes.byref(entropy),
        None,
        None,
        0,
        ctypes.byref(data_out),
    ):
        raise VaultError(f"Windows DPAPI 加密失败：{ctypes.WinError()}")
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        kernel32.LocalFree(data_out.pbData)


def _unprotect(ciphertext: bytes) -> bytes:
    crypt32, kernel32 = _crypt32()
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    data_in, _buf_in = _blob(ciphertext)
    entropy, _buf_entropy = _blob(ENTROPY)
    data_out = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(data_in),
        None,
        ctypes.byref(entropy),
        None,
        None,
        0,
        ctypes.byref(data_out),
    ):
        raise VaultError(
            "Steam 凭据解密失败：可能换了 Windows 用户/机器，或凭据文件损坏。"
        )
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        kernel32.LocalFree(data_out.pbData)


def _restrict_acl(path: Path) -> None:
    if os.name != "nt":
        return
    user = (os.environ.get("USERNAME") or "").strip()
    if not user:
        return
    domain = (os.environ.get("USERDOMAIN") or "").strip()
    who = f"{domain}\\{user}" if domain else user
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{who}:F"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:  # noqa: BLE001 — ACL 收紧失败不影响加解密
        pass


class SteamVault:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or os.getenv("TFD_STEAM_VAULT") or DEFAULT_PATH)

    def save(self, username: str, password: str) -> None:
        username = (username or "").strip()
        if not username or not password:
            raise VaultError("Steam 用户名和密码都不能为空。")
        payload = json.dumps(
            {"username": username, "password": password, "saved_at": int(time.time())},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        ciphertext = _protect(payload)
        now = int(time.time())
        wrapper: dict[str, Any] = {
            "version": 1,
            "created_at": now,
            "updated_at": now,
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(wrapper, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self.path)
        _restrict_acl(self.path)

    def load(self) -> SteamCredentials | None:
        if not self.path.exists():
            return None
        try:
            wrapper = json.loads(self.path.read_text(encoding="utf-8"))
            ciphertext = base64.b64decode(str(wrapper["ciphertext"]))
            raw = _unprotect(ciphertext)
            data = json.loads(raw.decode("utf-8"))
        except VaultError:
            raise
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise VaultError(f"Steam 凭据文件无法读取：{exc}") from exc
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        if not username or not password:
            raise VaultError("Steam 凭据文件内容不完整。")
        return SteamCredentials(username=username, password=password)

    def clear(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise VaultError(f"删除凭据失败：{exc}") from exc

    def status(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"configured": False, "readable": False, "updated_at": None}
        updated_at = None
        try:
            wrapper = json.loads(self.path.read_text(encoding="utf-8"))
            updated_at = int(wrapper.get("updated_at") or 0) or None
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        readable = False
        error = ""
        try:
            readable = self.load() is not None
        except VaultError as exc:
            error = str(exc)
        return {
            "configured": True,
            "readable": readable,
            "updated_at": updated_at,
            "error": error,
        }


default_vault = SteamVault()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Steam 凭据加密存储")
    parser.add_argument("--path", default="")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="查看是否已配置")
    set_cmd = sub.add_parser("set", help="录入 Steam 用户名/密码")
    set_cmd.add_argument("--username", default="")
    sub.add_parser("clear", help="删除本地凭据")
    args = parser.parse_args(argv)
    vault = SteamVault(args.path or None)
    if args.command == "status":
        print(json.dumps(vault.status(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "set":
        username = (args.username or input("Steam 用户名：")).strip()
        password = getpass.getpass("Steam 密码：")
        confirm = getpass.getpass("再次输入密码：")
        if password != confirm:
            print("两次密码不一致。", file=sys.stderr)
            return 2
        vault.save(username, password)
        print("已加密保存。")
        return 0
    if args.command == "clear":
        print("已删除。" if vault.clear() else "本来就没有配置。")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
