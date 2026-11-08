"""商品图片资产预取器：把全量物品图片下载到本地并建立 ItemId 索引。

官网没有单独的图片索引表——商品图 URL（strBinaryUrl）就来自 products 接口，
图片按物品共享（同一物品的所有挂单用同一张渲染图）。Jarvis 物品字典不含
strBinaryUrl，所以这里对每个物品发一次最小查询（pageSize=1）取出 strBinaryUrl，
下载成 PNG，并写入 data/assets/index.json：

    { "<ItemId>": { "url": "...", "path": "data/assets/items/<ItemId>_<hash>.png",
                    "type": "transcendent", "name": "怪物猎人", "size": 156125 } }

用法：
    .\\.venv\\Scripts\\python.exe -m qqbot.assets --fetch
    .\\.venv\\Scripts\\python.exe -m qqbot.assets --status
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

from curl_cffi import requests
from PIL import Image, UnidentifiedImageError

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_v2.api import MarketClient  # noqa: E402

ASSET_DIR = ROOT / "data" / "assets" / "items"
INDEX_PATH = ROOT / "data" / "assets" / "index.json"
MAX_BYTES = 4 * 1024 * 1024
PAGE_SIZE = 1
_LOCK = threading.Lock()


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def icon_path(
    item_id: str,
    url: str | None = None,
    *,
    item_type: str = "",
    name: str = "",
) -> Path | None:
    """给渲染器用：优先本地索引；URL 变化或缺失时才下载并更新索引。"""
    index = _load_index()
    entry = index.get(item_id)
    if entry:
        path = _resolve(str(entry.get("path") or ""))
        if path.exists() and (not url or entry.get("url") == url):
            return path
    if not url:
        return None

    with _LOCK:
        index = _load_index()
        entry = index.get(item_id)
        if entry:
            path = _resolve(str(entry.get("path") or ""))
            if path.exists() and entry.get("url") == url:
                return path
        path = _download(url, item_id)
        if path is None:
            return None
        index[item_id] = {
            "url": url,
            "path": path.relative_to(ROOT).as_posix(),
            "type": item_type,
            "name": name,
            "size": path.stat().st_size,
        }
        _save_index(index)
        return path


def _load_index() -> dict[str, dict[str, Any]]:
    if INDEX_PATH.exists():
        try:
            return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_index(index: dict[str, dict[str, Any]]) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(INDEX_PATH)


def _item_image_url(client: MarketClient, item_type: str, item_id: str) -> str | None:
    """对单物品发一次最小 products 查询，取首条的 strBinaryUrl。"""
    payload = {
        "now": None, "maintenance": None, "restrict": None, "access": None,
        "pageInfo": {"pageNumber": 1, "pageSize": PAGE_SIZE},
        "strLanguage": "cn", "strItemType": item_type, "strItemid": item_id,
        "strGrade": None, "n4SalePriceMin": None, "n4SalePriceMax": None,
        "odType": 1,
    }
    page = client.query_products(payload)
    if page.items:
        url = str(page.items[0].get("strBinaryUrl") or "")
        if url:
            return url
    return None


def _download(url: str, item_id: str) -> Path | None:
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    path = ASSET_DIR / f"{item_id}_{key}.png"
    if path.exists() and path.stat().st_size > 0:
        return path
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    try:
        resp = requests.get(url, impersonate="chrome133a", timeout=25)
        if resp.status_code != 200 or not resp.content or len(resp.content) > MAX_BYTES:
            return None
        # 校验确为图片
        try:
            Image.open(io.BytesIO(resp.content)).verify()
        except (UnidentifiedImageError, OSError):
            return None
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(resp.content)
        tmp.replace(path)
        return path
    except Exception as exc:  # noqa: BLE001
        print(f"  下载失败 {item_id}: {exc}", file=sys.stderr)
        return None


def fetch_all() -> tuple[int, int]:
    client = MarketClient(language="cn")
    index = _load_index()
    missing: list[dict[str, Any]] = []
    try:
        catalog = client.fetch_catalog()
    finally:
        client.close()

    for item in catalog:
        item_id = item.item_id
        existing = index.get(item_id)
        if existing and existing.get("path"):
            stored = Path(str(existing["path"]))
            if not stored.is_absolute():
                stored = ROOT / stored
            if stored.exists():
                continue
        missing.append({"id": item_id, "type": item.item_type, "name": item.name("cn")})

    print(f"物品字典共 {len(catalog)} 个，待抓 {len(missing)} 个")
    if not missing:
        print("全部物品图已在本地，无需更新")
        return 0, len(catalog)

    client = MarketClient(language="cn")
    ok = 0
    try:
        for i, entry in enumerate(missing, 1):
            item_id, item_type, name = entry["id"], entry["type"], entry["name"]
            url = _item_image_url(client, item_type, item_id)
            if not url:
                print(f"  [{i}/{len(missing)}] {name}({item_id}) 无图片 URL")
                continue
            path = _download(url, item_id)
            if path is None:
                print(f"  [{i}/{len(missing)}] {name}({item_id}) 下载失败")
                continue
            index[item_id] = {
                "url": url,
                "path": path.relative_to(ROOT).as_posix(),
                "type": item_type,
                "name": name,
                "size": path.stat().st_size,
            }
            ok += 1
            if i % 20 == 0 or i == len(missing):
                _save_index(index)
                print(f"  ...已处理 {i}/{len(missing)}，成功 {ok}")
            time.sleep(0.1)  # 节流
    finally:
        client.close()
    _save_index(index)
    print(f"完成：新下载 {ok}，本地总计 {len(index)}")
    return ok, len(index)


def status() -> dict[str, int]:
    index = _load_index()
    present = 0
    for value in index.values():
        path = Path(str(value.get("path") or ""))
        if not path.is_absolute():
            path = ROOT / path
        if path.exists():
            present += 1
    return {"indexed": len(index), "on_disk": present, "missing": max(0, len(index) - present)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="商品图片资产预取器")
    parser.add_argument("--fetch", action="store_true", help="补齐缺失的物品图")
    parser.add_argument("--status", action="store_true", help="查看本地索引状态")
    args = parser.parse_args(argv)

    if args.status or not args.fetch:
        stats = status()
        print(stats)
        return 0
    if args.fetch:
        ok, total = fetch_all()
        print(f"本次新增 {ok}，索引总 {total}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())