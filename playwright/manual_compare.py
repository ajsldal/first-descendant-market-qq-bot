"""用 Playwright 模拟人工在市场页面设置筛选条件，并与本项目的 API 抓取结果比对。

用法：
    python playwright/manual_compare.py --spec specs/monster_hunter.json
    python playwright/manual_compare.py --spec specs/monster_hunter.json --headed
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_v2.filters import FilterSpec  # noqa: E402
from market_v2.runner import run_spec  # noqa: E402

MARKET_URL = "https://tfd.nexon.com/zh-Hans/market"
GRADE_LABELS = {"Tier1": "普通", "Tier2": "稀有", "Tier3": "终极", "Tier4": "超越"}


def _products_response_handler(store: list[dict[str, Any]]):
    def handler(resp):  # noqa: ANN001
        url = resp.url
        if "/v2/trademarket/" not in url or "/products" not in url:
            return
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return
        store.append({"url": url, "status": resp.status, "at": time.time(), "data": data})

    return handler


def _wait_new_products(responses: list[dict[str, Any]], before: int, timeout_s: float = 15.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if len(responses) > before:
            return True
        time.sleep(0.2)
    return False


def _click_suggestion(page, name: str, timeout_ms: int) -> None:
    page.fill("#search__input", name)
    try:
        page.wait_for_selector(".autocomplete li", timeout=3000)
    except Exception:  # noqa: BLE001
        page.press("#search__input", "Enter")
        page.wait_for_selector(".autocomplete li", timeout=timeout_ms)
    suggestions = page.locator(".autocomplete li")
    if suggestions.count() == 0:
        raise RuntimeError("搜索框没有出现候选物品（可能被 Cloudflare 拦截或页面结构变化）")

    for i in range(suggestions.count()):
        text = suggestions.nth(i).inner_text()
        if name.replace(" ", "") in text.replace(" ", ""):
            suggestions.nth(i).click()
            return
    suggestions.first.click()


def _ensure_filter_panel(page) -> None:
    panel = page.locator(".filter-panel")
    if not panel.is_visible():
        btn = page.locator(".mobile-filter-btn")
        if btn.count() and btn.is_visible():
            btn.click()
            page.wait_for_timeout(500)


def _apply_price(page, spec: FilterSpec) -> None:
    common = page.locator(".filter-panel__content.common")
    if common.count() == 0:
        return
    inputs = common.locator(".price-range__input")
    if inputs.count() < 2:
        return
    if spec.price.min is not None:
        inputs.nth(0).fill(str(spec.price.min))
    if spec.price.max is not None:
        inputs.nth(1).fill(str(spec.price.max))
    if spec.price.min is not None or spec.price.max is not None:
        common.locator(".price-range__btn").first.click()
        page.wait_for_timeout(1200)


def _apply_grade(page, spec: FilterSpec) -> None:
    if not spec.grade:
        return
    common = page.locator(".filter-panel__content.common")
    dropdown = common.locator(".dropdown").first
    if dropdown.count() == 0:
        return
    dropdown.locator(".dropdown__button").click()
    dropdown.locator(".dropdown__menu").wait_for(state="visible", timeout=3000)
    label = GRADE_LABELS.get(spec.grade, "")
    options = dropdown.locator(".dropdown__menu li")
    for i in range(options.count()):
        if label and label in options.nth(i).inner_text():
            options.nth(i).click()
            page.wait_for_timeout(1200)
            return


def _apply_quality(page, spec: FilterSpec) -> None:
    if spec.quality.is_empty():
        return
    trans = page.locator(".filter-panel__content.transcend")
    if trans.count() == 0:
        return
    inputs = trans.locator(".price-range__input")
    if inputs.count() < 2:
        return
    if spec.quality.min is not None:
        inputs.nth(0).fill(str(spec.quality.min))
    if spec.quality.max is not None:
        inputs.nth(1).fill(str(spec.quality.max))
    trans.locator(".price-range__btn").first.click()
    page.wait_for_timeout(1200)


def _apply_subperks(page, spec: FilterSpec) -> None:
    if not spec.subperks:
        return
    buttons = page.locator(".filter-panel__item.sub-ability .dropdown__button")
    for idx, name in enumerate(spec.subperks[:2]):
        if idx >= buttons.count():
            break
        item = page.locator(".filter-panel__item.sub-ability").nth(idx)
        item.locator(".dropdown__button").click()
        item.locator(".dropdown__menu").wait_for(state="visible", timeout=3000)
        options = item.locator(".dropdown__menu li")
        picked = False
        for j in range(options.count()):
            if name.replace(" ", "") in options.nth(j).inner_text().replace(" ", ""):
                options.nth(j).click()
                picked = True
                break
        if not picked:
            raise RuntimeError(f"筛选面板中找不到辅助能力 {name!r}")
        page.wait_for_timeout(1000)


def _apply_advantage(page, spec: FilterSpec) -> None:
    content = page.locator(".filter-panel__content.advantage")
    if content.count() == 0:
        return

    def pick(dropdown, text: str, label: str) -> None:
        dropdown.locator(".dropdown__button").click()
        dropdown.locator(".dropdown__menu").wait_for(state="visible", timeout=3000)
        options = dropdown.locator(".dropdown__menu li")
        for i in range(options.count()):
            if text.replace(" ", "") in options.nth(i).inner_text().replace(" ", ""):
                options.nth(i).click()
                page.wait_for_timeout(900)
                return
        raise RuntimeError(f"筛选面板中找不到{label} {text!r}")

    for idx, cond in enumerate(spec.advantage[:3], start=1):
        dropdown = content.locator(f'[data-name="advantageOption{idx}"]')
        if dropdown.count() == 0:
            continue
        pick(dropdown, cond.key, "增益属性")
        if cond.grade:
            grade_dd = content.locator(f'[data-name="advantageGrade{idx}"]')
            label = GRADE_LABELS.get(cond.grade, "")
            if grade_dd.count() and label:
                pick(grade_dd, label, "增益属性品级")

    if spec.penalty is not None:
        penalty_dd = page.locator('[data-name="penaltyOption"]')
        if penalty_dd.count():
            pick(penalty_dd, spec.penalty.key, "减益属性")


def _apply_abilities(page, spec: FilterSpec) -> None:
    ranges = (spec.ability1, spec.ability2, spec.ability3)
    if all(r.is_empty() for r in ranges):
        return
    inputs = page.locator(".filter-panel__item.transcend-ability .price-range__input")
    if inputs.count() < 6:
        return
    for group, rng in enumerate(ranges):
        if rng.min is not None:
            inputs.nth(group * 2).fill(str(rng.min))
        if rng.max is not None:
            inputs.nth(group * 2 + 1).fill(str(rng.max))
    apply_btn = page.locator(".filter-panel__item.transcend-ability-apply .price-range__btn")
    if apply_btn.count():
        apply_btn.click()
        page.wait_for_timeout(1400)


def capture_ui(spec: FilterSpec, *, headed: bool, timeout_ms: int) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    responses: list[dict[str, Any]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            locale="zh-CN",
            viewport={"width": 1440, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.on("response", _products_response_handler(responses))

        page.goto(MARKET_URL, wait_until="domcontentloaded", timeout=timeout_ms * 2)
        page.wait_for_timeout(3000)
        try:
            page.wait_for_selector("#search__input", timeout=max(timeout_ms * 2, 60000))
        except Exception:  # noqa: BLE001
            shot_dir = ROOT / ".playwright"
            shot_dir.mkdir(exist_ok=True)
            page.screenshot(path=str(shot_dir / "failure_search_input.png"))
            (shot_dir / "failure_page.html").write_text(page.content(), encoding="utf-8")
            raise RuntimeError(
                "页面没有渲染出搜索框（可能是 Cloudflare 验证或网络问题）；"
                f"已保存截图到 {shot_dir / 'failure_search_input.png'}"
            ) from None

        before = len(responses)
        _click_suggestion(page, spec.item_name or "", timeout_ms)
        page.wait_for_timeout(1500)

        _ensure_filter_panel(page)
        _apply_price(page, spec)
        _apply_grade(page, spec)
        _apply_quality(page, spec)
        _apply_advantage(page, spec)
        _apply_subperks(page, spec)
        _apply_abilities(page, spec)
        page.wait_for_timeout(1500)

        shot_dir = ROOT / ".playwright"
        shot_dir.mkdir(exist_ok=True)
        page.screenshot(path=str(shot_dir / "after_filters.png"), full_page=False)

        if len(responses) <= before:
            raise RuntimeError("操作过程中没有捕获到 products 请求，筛选可能未生效")
        last = responses[-1]
        browser.close()
        return {
            "url": last["url"],
            "status": last["status"],
            "total_count": (last["data"].get("ResultData") or {}).get("totalCount"),
            "items": (last["data"].get("ResultData") or {}).get("items") or [],
            "requests_captured": len(responses),
        }


def _item_satisfies(raw: dict[str, Any], spec: FilterSpec) -> tuple[bool, list[str]]:
    problems: list[str] = []
    price = float(raw.get("n4SalePrice") or 0)
    if spec.price.min is not None and price < spec.price.min:
        problems.append(f"price {price} < {spec.price.min}")
    if spec.price.max is not None and price > spec.price.max:
        problems.append(f"price {price} > {spec.price.max}")

    data = raw.get("data") or {}
    if spec.item_type == "transcendent" or str(raw.get("strType")).lower() == "transcendent":
        quality = data.get("n4WeaponQuality")
        if spec.quality.min is not None and (quality is None or quality < spec.quality.min):
            problems.append(f"quality {quality} < {spec.quality.min}")
        if spec.quality.max is not None and (quality is None or quality > spec.quality.max):
            problems.append(f"quality {quality} > {spec.quality.max}")

        names = {sp.get("strName") for sp in (data.get("subperks") or [])}
        for name in spec.subperks:
            if name not in names:
                problems.append(f"missing subperk {name}")

        ranges = {1: spec.ability1, 2: spec.ability2, 3: spec.ability3}
        for group, rng in ranges.items():
            if rng.is_empty():
                continue
            points = [
                int(o.get("n4Point") or 0)
                for o in (data.get("options") or [])
                if int(o.get("n4Group") or 0) == group - 1
            ]
            if not points:
                problems.append(f"missing ability group {group}")
                continue
            point = max(points)
            if rng.min is not None and point < rng.min:
                problems.append(f"ability{group} {point} < {rng.min}")
            if rng.max is not None and point > rng.max:
                problems.append(f"ability{group} {point} > {rng.max}")
    else:
        options = data.get("options") or []
        if spec.grade:
            want = spec.grade
            if not any(str(o.get("strTier")) == want for o in options):
                problems.append(f"missing grade {want}")

        advantages = [o for o in options if str(o.get("strSlot")) == "Advantage"]
        for cond in spec.advantage:
            matched = [
                o for o in advantages
                if cond.key.replace(" ", "") in str(o.get("strDesc") or "").replace(" ", "")
            ]
            if not matched:
                problems.append(f"missing advantage {cond.key}")
            elif cond.grade and not any(str(o.get("strTier")) == cond.grade for o in matched):
                problems.append(f"advantage {cond.key} grade != {cond.grade}")

        if spec.penalty is not None:
            penalties = [o for o in options if str(o.get("strSlot")) == "Penalty"]
            if not any(spec.penalty.key.replace(" ", "") in str(o.get("strDesc") or "").replace(" ", "")
                       for o in penalties):
                problems.append(f"missing penalty {spec.penalty.key}")

    return (not problems, problems)


def compare(ui: dict[str, Any], outcome) -> dict[str, Any]:
    ui_items = ui["items"]
    api_items = outcome.document["products"]
    api_ids = {str(it["n8Id"]) for it in api_items}
    ui_ids = {str(it["n8Id"]) for it in ui_items}

    violations = []
    for raw in ui_items:
        ok, problems = _item_satisfies(raw, outcome.spec)
        if not ok:
            violations.append({"n8Id": str(raw.get("n8Id")), "problems": problems})

    overlap = len(ui_ids & api_ids)
    ratio = overlap / len(ui_ids) if ui_ids else 0.0

    return {
        "ui_item_count": len(ui_items),
        "api_item_count": len(api_items),
        "ui_total_count": ui.get("total_count"),
        "api_total_count_max": outcome.document["stats"].get("server_total_max"),
        "id_overlap": overlap,
        "id_overlap_ratio": round(ratio, 4),
        "filter_violations": violations,
        "ui_ids": sorted(ui_ids),
        "api_ids": sorted(api_ids),
        "passed": bool(ui_items) and not violations and ratio >= 0.6,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Playwright 手动模拟 vs API 抓取结果比对")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=30000)
    args = parser.parse_args(argv)

    spec = FilterSpec.from_dict(json.loads(Path(args.spec).read_text(encoding="utf-8")))
    spec.validate()

    print(f"[1/3] Playwright 模拟人工筛选: {spec.item_name} ...")
    ui = capture_ui(spec, headed=args.headed, timeout_ms=args.timeout_ms)
    print(f"      UI 捕获 {ui['requests_captured']} 次 products 请求，最后一次返回 {len(ui['items'])} 条，totalCount={ui['total_count']}")

    print("[2/3] 调用同一条件的 API 抓取 ...")
    outcome = run_spec(spec)
    print(f"      API 匹配 {outcome.document['stats']['matched']} 条，文件: {outcome.path}")

    print("[3/3] 比对 ...")
    report = compare(ui, outcome)
    report["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    report["ui_url"] = ui["url"]

    out_dir = ROOT / ".playwright"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "ui_last_response.json").write_text(
        json.dumps({"url": ui["url"], "total_count": ui["total_count"], "items": ui["items"]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "compare_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n=== 比对结果 ===")
    print(f"UI 条数            : {report['ui_item_count']}")
    print(f"API 条数           : {report['api_item_count']}")
    print(f"UI totalCount      : {report['ui_total_count']}")
    print(f"ID 重合            : {report['id_overlap']} / {report['ui_item_count']} ({report['id_overlap_ratio']:.0%})")
    print(f"筛选违规           : {len(report['filter_violations'])}")
    for v in report["filter_violations"][:5]:
        print(f"  - {v['n8Id']}: {', '.join(v['problems'])}")
    print(f"结论               : {'PASS' if report['passed'] else 'FAIL'}")
    print(f"报告               : {out_dir / 'compare_report.json'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
