from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from .api import MarketClient
from .catalog import resolve_item
from .filters import FilterSpec, IntRange, OptionCondition
from .models import ITEM_TYPES
from .runner import run_spec


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tfd-market-filter",
        description="按条件调用 Nexon TFD 市场 v2 接口，把符合条件的挂单写入 JSON。",
    )
    p.add_argument("--spec", help="筛选条件 JSON 文件（与命令行参数二选一）")
    p.add_argument("--name", help="物品名称，例如 怪物猎人")
    p.add_argument("--item-id", help="物品 ItemId，例如 211064002")
    p.add_argument("--type", choices=list(ITEM_TYPES) + ["transcend"], help="物品类型")
    p.add_argument("--language", default=None, help="文案语言（默认 cn）")

    p.add_argument("--price-min", type=int, help="最低价格（10~25000）")
    p.add_argument("--price-max", type=int, help="最高价格（10~25000）")
    p.add_argument("--sort", choices=["latest", "price_desc", "price_asc"], default=None)

    p.add_argument("--grade", help="模块品级 Tier1~Tier4（ancestor/descendant/trigger）")
    p.add_argument("--advantage", action="append", default=[], metavar="名称[:品级]",
                   help="增益属性，可重复最多 3 次，例如 --advantage 技能冷却时间:Tier4")
    p.add_argument("--penalty", metavar="名称", help="减益属性")

    p.add_argument("--quality-min", type=int, help="超越武器品质下限（1~400）")
    p.add_argument("--quality-max", type=int, help="超越武器品质上限（1~400）")
    p.add_argument("--subperk", action="append", default=[], metavar="名称或ID",
                   help="辅助能力，可重复最多 2 次")
    for idx in (1, 2, 3):
        p.add_argument(f"--ability{idx}", metavar="MIN-MAX",
                       help=f"超越能力 {idx} 的点数区间，例如 100-150")

    p.add_argument("--output-dir", default=None, help="JSON 输出目录（默认 output）")
    p.add_argument("--list-items", action="store_true", help="列出 Jarvis 物品字典后退出")
    p.add_argument("--list-options", action="store_true",
                   help="列出该物品可选的属性/辅助能力（含中英文名与 ID）后退出")
    p.add_argument("--dry-run", action="store_true", help="只打印请求体，不抓取")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _parse_range(value: str | None, label: str) -> IntRange:
    if not value:
        return IntRange()
    lo, _, hi = value.partition("-")
    try:
        return IntRange(min=int(lo) if lo != "" else None, max=int(hi) if hi != "" else None)
    except ValueError as exc:
        raise SystemExit(f"{label} 格式应为 MIN-MAX，收到 {value!r}") from exc


def _spec_from_args(args: argparse.Namespace) -> FilterSpec:
    if args.spec:
        path = Path(args.spec)
        if not path.exists():
            raise SystemExit(f"spec 文件不存在: {path}")
        spec = FilterSpec.from_dict(json.loads(path.read_text(encoding="utf-8")))
        # 命令行覆盖（仅在显式提供时）
        if args.name:
            spec.item_name = args.name
        if args.item_id:
            spec.item_id = args.item_id
        if args.type:
            spec.item_type = args.type
        if args.language:
            spec.language = args.language
        if args.sort:
            spec.sort = args.sort
        if args.price_min is not None:
            spec.price.min = args.price_min
        if args.price_max is not None:
            spec.price.max = args.price_max
        if args.output_dir:
            spec.output_dir = args.output_dir
        return spec

    spec = FilterSpec(
        item_name=args.name,
        item_id=args.item_id,
        item_type=args.type,
        language=args.language or "cn",
        sort=args.sort or "latest",
        price=IntRange(args.price_min, args.price_max),
        grade=args.grade,
        quality=IntRange(args.quality_min, args.quality_max),
        subperks=list(args.subperk),
        ability1=_parse_range(args.ability1, "--ability1"),
        ability2=_parse_range(args.ability2, "--ability2"),
        ability3=_parse_range(args.ability3, "--ability3"),
        output_dir=args.output_dir or "output",
    )
    for entry in args.advantage:
        name, _, grade = entry.partition(":")
        spec.advantage.append(OptionCondition(name.strip(), grade.strip() or None))
    if args.penalty:
        spec.penalty = OptionCondition(args.penalty.strip())
    return spec


def _print_catalog(client: MarketClient) -> None:
    catalog = client.fetch_catalog()
    for it in sorted(catalog, key=lambda x: (x.item_type, x.item_id)):
        print(f"{it.item_type:<13} {it.item_id:<12} {it.name('cn')}")
    print(f"\n共 {len(catalog)} 条")


def _print_options(item, lists) -> None:
    print(f"物品: {item.name('cn')} ({item.item_type}, id={item.item_id})\n")

    def dump(title: str, entries) -> None:
        if not entries:
            return
        print(f"{title}:")
        for entry in entries:
            names = " | ".join(entry.all_names())
            print(f"  {entry.id:<12} {names}")
        print()

    dump("增益属性 (--advantage)", lists.advantage)
    dump("减益属性 (--penalty)", lists.penalty)
    dump("辅助能力 (--subperk)", lists.subperks)
    dump("品级 (--grade)", lists.grades)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    client = MarketClient(language=args.language or "cn")
    try:
        if args.list_items:
            _print_catalog(client)
            return 0

        spec = _spec_from_args(args)
        client.language = spec.language

        if args.list_options:
            spec.validate()
            catalog = client.fetch_catalog()
            item = resolve_item(catalog, name=spec.item_name, item_id=spec.item_id, item_type=spec.item_type)
            lists = client.fetch_option_lists(item.item_type, item.item_id)
            _print_options(item, lists)
            return 0

        if args.dry_run:
            from .filters import build_payload

            catalog = client.fetch_catalog()
            item = resolve_item(catalog, name=spec.item_name, item_id=spec.item_id, item_type=spec.item_type)
            spec.validate()
            lists = client.fetch_option_lists(item.item_type, item.item_id)
            payload, resolved = build_payload(spec, item, lists)
            print(json.dumps({"item": resolved["item"], "payload": payload}, ensure_ascii=False, indent=2))
            return 0

        outcome = run_spec(spec, client)
    finally:
        client.close()

    doc = outcome.document
    print("\n=== 完成 ===")
    print(f"物品: {doc['item']['name']} ({doc['item']['item_type']}, id={doc['item']['item_id']})")
    print(f"匹配: {doc['stats']['matched']} 条")
    print(f"页数: {doc['stats']['pages_fetched']} | 价格切分: {doc['stats']['price_slices']} | 截断: {doc['stats']['truncated']}")
    print(f"文件: {outcome.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
