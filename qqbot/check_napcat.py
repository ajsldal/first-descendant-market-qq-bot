"""NapCatQQ HTTP Server 连通性与接口自检脚本。

用法::

    .\\.venv\\Scripts\\python.exe -m qqbot.check_napcat --token "你的token"
    .\\.venv\\Scripts\\python.exe -m qqbot.check_napcat ^
        --send-private 123456

token 也可以放在环境变量 NAPCAT_TOKEN，地址放在 NAPCAT_URL。
默认只做只读调用；只有显式传 --send-private / --send-group 才会发消息。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from .napcat_client import DEFAULT_URL, NapCatClient, NapCatError, text


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m"


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m"


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m"


def run_readonly_checks(client: NapCatClient) -> tuple[list[dict[str, Any]], bool]:
    """返回 (检查结果列表, 是否全部通过)。"""
    specs: list[tuple[str, dict[str, Any]]] = [
        ("get_version_info", {}),
        ("get_status", {}),
        ("get_login_info", {}),
        ("get_friend_list", {}),
        ("get_group_list", {}),
    ]
    results: list[dict[str, Any]] = []
    all_ok = True

    for action, params in specs:
        results.append(_timed(client, action, params))
        all_ok = all_ok and results[-1]["ok"]

    # 带必填参数的动作：顺带验证「参数是否真的传到了 handler」
    login = next((r for r in results if r["action"] == "get_login_info" and r["ok"]), None)
    if login and isinstance(login.get("data"), dict):
        results.append(
            _timed(client, "get_stranger_info", {"user_id": login["data"].get("user_id")})
        )
        all_ok = all_ok and results[-1]["ok"]

    return results, all_ok


def _timed(client: NapCatClient, action: str, params: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        resp = client.call(action, params)
        return {
            "action": action,
            "ok": resp.ok,
            "retcode": resp.retcode,
            "ms": round((time.perf_counter() - started) * 1000, 1),
            "data": resp.data,
            "message": resp.message or resp.wording,
        }
    except NapCatError as exc:
        return {
            "action": action,
            "ok": False,
            "retcode": None,
            "ms": round((time.perf_counter() - started) * 1000, 1),
            "error": str(exc),
        }


def _print_readonly(results: list[dict[str, Any]]) -> None:
    for item in results:
        mark = _green("[ OK ]") if item["ok"] else _red("[FAIL]")
        info = item.get("error") or f"retcode={item['retcode']}"
        if item["ok"]:
            data = item.get("data")
            if item["action"] == "get_version_info" and isinstance(data, dict):
                info = f"{data.get('app_name')} {data.get('protocol_version')} v{data.get('app_version')}"
            elif item["action"] == "get_status" and isinstance(data, dict):
                info = f"online={data.get('online')} good={data.get('good')}"
            elif item["action"] == "get_login_info" and isinstance(data, dict):
                info = f"QQ {data.get('user_id')} ({data.get('nickname')})"
            elif isinstance(data, list):
                info = f"{len(data)} 条"
            else:
                info = json.dumps(data, ensure_ascii=False)[:120]
        print(f"{mark} {item['action']:<18} {info}  ({item['ms']} ms)")


def run_diagnostics(client: NapCatClient) -> None:
    """非致命诊断：鉴权失败、未知 action 的返回形态。"""
    print("\n诊断（不影响退出码）：")

    try:
        NapCatClient(base_url=client.base_url, token="").call("get_login_info")
    except NapCatError as exc:
        print(f"  {_yellow('无 token')} -> HTTP {exc.status_code}（预期 403）")
    else:
        print(f"  {_yellow('无 token')} -> 未鉴权也成功，说明服务端未启用 token")

    try:
        resp = client.call("__no_such_action__")
        print(f"  未知 action -> retcode={resp.retcode} message={resp.message!r}")
    except NapCatError as exc:
        print(f"  未知 action -> {exc}")


def run_send_tests(
    client: NapCatClient, private_id: int | None, group_id: int | None
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    stamp = time.strftime("%H:%M:%S")
    message = [text(f"NapCat 接口自检 {stamp}（来自 Python 客户端）")]

    if private_id is not None:
        try:
            data = client.send_private_msg(private_id, message)
            results.append({"target": f"private:{private_id}", "ok": True, "data": data})
        except NapCatError as exc:
            results.append({"target": f"private:{private_id}", "ok": False, "error": str(exc)})

    if group_id is not None:
        try:
            data = client.send_group_msg(group_id, message)
            results.append({"target": f"group:{group_id}", "ok": True, "data": data})
        except NapCatError as exc:
            results.append({"target": f"group:{group_id}", "ok": False, "error": str(exc)})

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NapCat HTTP 接口自检")
    parser.add_argument("--url", default=os.getenv("NAPCAT_URL", DEFAULT_URL))
    parser.add_argument("--token", default=os.getenv("NAPCAT_TOKEN", ""))
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--send-private", type=int, default=None, metavar="QQ号")
    parser.add_argument("--send-group", type=int, default=None, metavar="群号")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = parser.parse_args(argv)

    client = NapCatClient(args.url, args.token, timeout=args.timeout)
    print(f"目标: {client.base_url}  token: {'已设置' if client.token else '未设置'}\n")

    try:
        results, all_ok = run_readonly_checks(client)
    except Exception as exc:  # noqa: BLE001 — 顶层兜底，保证有可读报错
        print(_red(f"自检失败: {exc}"))
        return 2

    sends: list[dict[str, Any]] = []
    if all_ok and (args.send_private or args.send_group):
        sends = run_send_tests(client, args.send_private, args.send_group)
        all_ok = all_ok and all(item["ok"] for item in sends)

    if args.json:
        print(
            json.dumps(
                {"url": client.base_url, "checks": results, "sends": sends, "ok": all_ok},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _print_readonly(results)
        if sends:
            print("\n消息发送：")
            for item in sends:
                mark = _green("[ OK ]") if item["ok"] else _red("[FAIL]")
                detail = item.get("error") or f"message_id={item.get('data', {}).get('message_id')}"
                print(f"{mark} {item['target']}  {detail}")
        if not sends:
            run_diagnostics(client)

    print(_green("\n全部通过") if all_ok else _red("\n存在失败项"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
