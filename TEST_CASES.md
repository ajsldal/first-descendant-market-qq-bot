# 测试用例与实测记录

> 运行环境：Windows / Python 3.10.6 / curl_cffi 0.16.3 / pytest 8.3.4 / Playwright 1.58.0（Chromium 145）
> 实测时间：2026-10-21 02:20 ~ 02:50（CST）

---

## 1. 如何运行

```powershell
cd tfd-market-v2

# 42 个单元测试（不访问网络）
.\.venv\Scripts\python.exe -m pytest -q -m "not live"

# 2 个真实 API 测试
.\.venv\Scripts\python.exe -m pytest -q -m live

# Playwright 人工模拟比对（会自动写 JSON 并对比）
.\.venv\Scripts\python.exe playwright\manual_compare.py --spec specs\monster_hunter_full.json
```

---

## 2. 单元测试（42 个）

### 2.1 物品字典解析 `tests/test_catalog.py`（8）

| 编号 | 用例 | 预期 |
|---|---|---|
| CAT-01 | `normalize_name("  统治者   决意 ")` | 归一化为 `统治者 决意`；去空格版本一致 |
| CAT-02 | 中文精确名 `怪物猎人` | 解析为 `211064002` / transcendent |
| CAT-03 | 英文名 `monster hunter` | 解析为 `211064002`（大小写不敏感） |
| CAT-04 | 模糊名 `海莉` | 唯一命中 `101000036` |
| CAT-05 | 重名 `喜悦决意`（ancestor/trigger） | 抛 `AmbiguousItemError`；指定 `type=trigger` 后解析为 `255000099` |
| CAT-06 | 按 `item_id` 解析 | 返回对应条目 |
| CAT-07 | `item_id` 与 `type` 不匹配 | 抛 `ItemNotFoundError` |
| CAT-08 | 不存在的名称 | 抛 `ItemNotFoundError` |

### 2.2 请求体构造与校验 `tests/test_filters.py`（14）

| 编号 | 用例 | 预期 |
|---|---|---|
| FIL-01 | 超越武器完整条件 | payload 的 `transcendent.quality/subperks/abilityPoint1~3` 与 spec 完全一致；无 `ancestor` 键；`odType` 正确 |
| FIL-02 | 先裔模块增益+减益+品级 | `ancestor.options` 顺序、`strSlot`、减益 `strGrade=NONE` 正确；`strGrade=TIER4` |
| FIL-03 | 触发器 | payload 不含 `ancestor` / `transcendent` 块 |
| FIL-04 | 用原始 ID 指定属性 | 直接透传 `301900007` |
| FIL-05 | 属性名不存在 | 抛 `OptionResolveError` 并给出候选 |
| FIL-06 | ancestor + quality | 抛 `FilterSpecError` |
| FIL-07 | transcendent + advantage | 抛 `FilterSpecError` |
| FIL-08 | subperks 3 个 | 抛 `FilterSpecError`（最多 2 个） |
| FIL-09 | 价格越界（1~5000） | 抛 `FilterSpecError`（下限 10） |
| FIL-10 | price.min > price.max | 抛 `FilterSpecError` |
| FIL-11 | transcendent + grade | 抛 `FilterSpecError` |
| FIL-12 | spec JSON 字符串形式 | `"advantage": ["技能冷却时间:Tier4"]`、`"ability1": "100-150"` 正确解析 |
| FIL-13 | 品级写法 `4` / `TIER2` / `""` | 归一化为 `Tier4` / `Tier2` / `None` |
| FIL-14 | 跨语言别名解析 | 主名称为英文 `Multi-Hit Conversion`、别名为 `多重打击转换` 时，中文名、英文名、ID 三种写法都能解析到 `20136:0` |

### 2.3 分页与封顶切分 `tests/test_collector.py`（6）

| 编号 | 用例 | 预期 |
|---|---|---|
| COL-01 | 3 页 20/20/5，total=45 | 45 条、3 页、无重复、按页递增 |
| COL-02 | `totalCount=null` 且空列表 | 0 条、1 页、0 个有效区间 |
| COL-03 | 区间命中封顶（测试用 cap=100） | 自动切分价格区间，叶子区间进入分页，结果无重复 |
| COL-04 | 单价格点仍封顶 | `truncated=True`，最多 `max_pages` 页 |
| COL-05 | `max_pages=3` | 只抓 3 页（60 条） |
| COL-06 | `sort=price_asc` | 输出按价格升序 |

### 2.4 导出 `tests/test_exporter.py`（5）

| 编号 | 用例 | 预期 |
|---|---|---|
| EXP-01 | 文件名 | `2026-10-21_怪物猎人.json` |
| EXP-02 | 非法字符 | `/ \ : * ? " < > \|` 替换为 `_` |
| EXP-03 | 字段归一化 | 价格转 float；`dtRegister` UTC → `+08:00` |
| EXP-04 | 文档结构 + 落盘 | 含 `item/filters/request/stats/products`；无残留 `.tmp` |
| EXP-05 | 同日重复运行 | 第二个文件追加时分秒，不覆盖第一个 |

### 2.5 端到端 `tests/test_runner.py`（2）

| 编号 | 用例 | 预期 |
|---|---|---|
| RUN-01 | `run_spec(write=False)` | 解析物品 → 构造 payload → 采集 → 生成文档；`subperks` 名称映射为 `20017:0` |
| RUN-02 | `run_spec(write=True)` | 在指定目录生成 `*_怪物猎人.json` |

### 2.6 API 解析层 `tests/test_api.py`（7）

| 编号 | 用例 | 预期 |
|---|---|---|
| API-01 | Jarvis `ItemId`（大写 D） | 正常解析 2 条 |
| API-02 | Jarvis `itemId`（小写 d） | 正常解析（真实环境两种都会出现） |
| API-03 | `isDeleted=true` | 跳过 |
| API-04 | `ResultCode=INVALID_PARAM` | 抛 `ApiError`，保留 `result_code` |
| API-05 | 正常响应 | `items` 与 `totalCount` 正确 |
| API-06 | 中文站点下拉 + 英文别名合并 | `20136:0` 同时拥有 `多重打击转换` 与 `Multi-Hit Conversion` 两个名字；英文名可解析 |
| API-07 | 英文站点（`language=en`）+ 中文输入 | 主名称为英文，中文名作为别名仍可解析 |

**实测结果：`42 passed in 0.19s`**

---

## 3. 真实 API 测试（2 个）

| 编号 | 用例 | 预期 | 实测 |
|---|---|---|---|
| LIVE-01 | `怪物猎人` / 价格 25000~25000 | 至少 1 条，且所有价格 = 25000、类型均为 transcendent | PASS（5 条） |
| LIVE-02 | `阴影的决意` / Tier4 / 价格 10~200 | 至少 1 条，且每条至少有一个 `Tier4` 属性 | PASS |

**实测结果：`2 passed in 21.11s`**

---

## 4. Playwright 人工模拟比对（3 个场景）

### 4.1 方法

`playwright/manual_compare.py`：

1. 真实 Chromium 打开市场页（1440×1000，zh-CN，headless）。
2. 在搜索框输入物品名 → 点击候选。
3. 在筛选面板设置与 spec 相同的条件：
   - 价格区间 → 「应用价格」
   - 品级下拉 → 选项
   - 品质区间 → 「应用」
   - 增益/减益属性下拉 → 选项 + 品级
   - 辅助能力 ①② 下拉 → 选项
   - 超越能力 ①②③ 区间 → 「应用」
4. 捕获页面自身发出的 `POST /v2/trademarket/cn/products` 响应。
5. 用同一 spec 跑本项目 API 抓取并写 JSON。
6. 比对：
   - UI `totalCount` vs API `server_total_max`
   - UI 首屏 20 条与 API 结果的 `n8Id` 重合率
   - UI 每条结果是否满足筛选条件（价格/品质/属性/辅助能力/能力点）

### 4.2 实测结果

| 编号 | spec | 条件 | UI totalCount | API matched | ID 重合 | 筛选违规 | 结论 |
|---|---|---|---|---|---|---|---|
| PW-01 | `specs/monster_hunter.json` | 价格 100~5000 + 品质 1~400 | **1064** | **1064** | 20 / 20（100%） | 0 | PASS |
| PW-02 | `specs/monster_hunter_full.json` | 价格 100~5000 + 品质 390~400 + 辅助能力「强化大容量弹夹」+ 超越能力①30~150 + ②③0~150 | **31** | **31** | 20 / 20（100%） | 0 | PASS |
| PW-03 | `specs/ancestor_shadow.json` | 价格 10~200 + 品级 Tier4 + 增益「技能冷却时间」Tier4 | **104** | **104** | 20 / 20（100%） | 0 | PASS |

> UI 只加载首屏 20 条（懒加载），所以「UI 条数」恒为 20；`totalCount` 与 API 完全一致，且 20 条全部出现在 API 结果中、全部满足筛选条件。

### 4.3 产物

```text
.playwright/compare_report.json    # 最近一次比对报告
.playwright/ui_last_response.json  # UI 最后一次 products 响应原文
.playwright/after_filters.png      # 筛选完成后的页面截图
output/2026-10-21_怪物猎人.json           # PW-01（1064 条）
output/2026-10-21_怪物猎人_024643.json    # PW-02（31 条，同日追加时分秒）
output/2026-10-21_阴影的决意.json          # PW-03（104 条）
```

---

## 5. 命令行冒烟测试

| 编号 | 命令 | 预期 | 实测 |
|---|---|---|---|
| CLI-01 | `run.py --spec specs\monster_hunter.json` | 生成 `output/2026-10-21_怪物猎人.json` | PASS（1060 条；数据实时变化） |
| CLI-02 | `run.py --spec ... --dry-run` | 只打印 payload，不发抓取请求 | PASS |
| CLI-03 | `run.py --list-items` | 列出 113 条字典 | PASS |
| CLI-04 | `run.py --name 阿尔米斯 --type transcendent --list-options` | 辅助能力同时显示中文、英文、ID | PASS（`20136:0 多重打击转换 \| Multi-Hit Conversion`） |
| CLI-05 | `--subperk 多重打击转换`（中文）+ `--language en` | 跨语言解析为 `strId=20136:0` | PASS |

---

## 6. 结论

- 42 个单元测试全部通过；2 个真实 API 测试通过。
- 3 个 Playwright 人工模拟场景与 API 抓取结果在 `totalCount` 上完全一致，首屏 20 条全部重合、零筛选违规。
- 说明本项目的筛选条件构造、服务端查询、分页与导出逻辑，与网页上人工操作的行为一致。
