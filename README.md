# TFD 市场 v2 条件筛选抓取器

独立于老项目（`tfd-market-scraper`）的新实现。只调用 Nexon 交易市场的 **v2 只读接口**，把「服务端筛选后的挂单」直接导出为 JSON。

- 只读、无需登录（匿名即可）。
- 筛选在**服务端**执行：价格、品级、增益/减益属性、品质、辅助能力、超越能力点数。
- 自动处理 `totalCount` 封顶 10000 的情况（按价格区间切分）。
- 文件名格式：`YYYY-MM-DD_商品名.json`（同一天重复运行会追加时分秒避免覆盖）。

---

## 1. 安装

需要 **Python 3.10+**。Windows 下用自带的 `venv` 即可：

```powershell
git clone <仓库地址> tfd-market-v2
cd tfd-market-v2

# 1) 创建虚拟环境
python -m venv .venv

# 2) 安装依赖
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3) 需要「人工模拟比对」时再装浏览器（可选）
.\.venv\Scripts\python.exe -m playwright install chromium
```

依赖：`curl_cffi`（TLS 指纹）、`pytest`（测试）、`playwright`（人工模拟比对）。

> **接口令牌**：物品名解析依赖 Nexon Jarvis 内容接口。代码内置了官网前端公开的
> 应用级令牌（所有访客共用，不是个人凭据），如遇 401 说明该令牌已被轮换，
> 请从 `tfd.nexon.com` 的前端 JS 中获取新值，或设置环境变量
> `TFD_JARVIS_TOKEN` 覆盖。

---

## 2. 快速开始

### 2.1 用 spec 文件（推荐）

```powershell
.\.venv\Scripts\python.exe run.py --spec specs\monster_hunter.json
```

`specs/monster_hunter.json`：

```json
{
  "item": { "name": "怪物猎人", "type": "transcendent" },
  "price": { "min": 100, "max": 5000 },
  "quality": { "min": 1, "max": 400 },
  "sort": "latest",
  "language": "cn",
  "output_dir": "output"
}
```

### 2.2 用命令行参数

```powershell
# 超越武器：价格 + 品质 + 辅助能力 + 三组超越能力
.\.venv\Scripts\python.exe run.py --name 怪物猎人 --type transcendent `
  --price-min 100 --price-max 5000 `
  --quality-min 390 --quality-max 400 `
  --subperk 强化大容量弹夹 `
  --ability1 30-150 --ability2 0-150 --ability3 0-150

# 先裔模块：品级 + 增益属性 + 价格
.\.venv\Scripts\python.exe run.py --name 阴影的决意 `
  --price-min 10 --price-max 200 `
  --grade Tier4 --advantage 技能冷却时间:Tier4
```

### 2.3 辅助命令

```powershell
# 查看 Jarvis 物品字典（113 条）
.\.venv\Scripts\python.exe run.py --list-items

# 查看某个物品可选的属性/辅助能力（同时显示中文名、英文名和 ID）
.\.venv\Scripts\python.exe run.py --name 阿尔米斯 --type transcendent --list-options

# 只打印将要发送的请求体，不抓取
.\.venv\Scripts\python.exe run.py --spec specs\monster_hunter.json --dry-run
```

`--list-options` 输出示例：

```text
物品: 阿尔米斯 (transcendent, id=211064001)

辅助能力 (--subperk):
  20056:1      多重打击强化 I | Multi-Hit Enhancement I
  20136:0      多重打击转换 | Multi-Hit Conversion
  ...
```

> `--subperk` / `--advantage` / `--penalty` 同时接受**中文名、英文名、以及服务端 ID**。程序会按 ID 合并中英文两套文案，所以中文站点和英文站点可以混用。

---

## 3. 支持的筛选条件

### 3.1 通用

| 字段 | 说明 | 有效范围 |
|---|---|---|
| `price.min` / `price.max` | 价格区间（卡利弗） | 10 ~ 25000 |
| `sort` | `latest` / `price_desc` / `price_asc` | — |
| `language` | 文案语言 | `cn`（默认）/`tw`/`en`/`ko`/`ja` 等 |
| `grade` | 模块品级 `Tier1`~`Tier4` | ancestor/descendant/trigger |

> `grade` 的服务端语义：**该卡至少有一条属性的等级 ≥ 所选等级**；`Tier1` 等价于不过滤。

### 3.2 超越武器（transcendent）

```json
{
  "quality": { "min": 1, "max": 400 },
  "subperks": ["强化大容量弹夹", "强化弹头 I"],
  "ability1": { "min": 30, "max": 150 },
  "ability2": { "min": 0, "max": 150 },
  "ability3": { "min": 0, "max": 150 }
}
```

| 字段 | 说明 | 有效范围 |
|---|---|---|
| `quality` | 品质 | 1 ~ 400 |
| `subperks` | 辅助能力名称或 ID，**最多 2 个**；支持中文名、英文名 | 来自 `SUBPERK` 筛选器 |
| `ability1` | 超越能力①（`n4Group=0`）点数区间 | 0 ~ 150 |
| `ability2` | 超越能力②（`n4Group=1`） | 0 ~ 150 |
| `ability3` | 超越能力③（`n4Group=2`） | 0 ~ 150 |

### 3.3 先裔/继承者模块（ancestor / descendant）

```json
{
  "grade": "Tier4",
  "advantage": [
    { "name": "技能冷却时间", "grade": "Tier4" },
    { "name": "技能资源消耗量", "grade": "" }
  ],
  "penalty": { "name": "最大护盾" }
}
```

| 字段 | 说明 |
|---|---|
| `advantage` | 增益属性，最多 3 条；`grade` 为空表示不限品级；名称支持中英文 |
| `penalty` | 减益属性，最多 1 条；名称支持中英文；服务端 `strGrade` 恒为 `NONE` |

### 3.4 触发器（trigger）

只支持 `price`、`grade`、`sort`（触发器没有属性筛选面板）。

---

## 4. 输出

### 4.1 命名

```text
output/2026-10-21_怪物猎人.json
output/2026-10-21_阴影的决意.json
```

- 基础名 = `生成日期_商品名.json`（日期取本地时区 CST）。
- 同一天重复运行同一物品时，追加时分秒：`2026-10-21_怪物猎人_024643.json`。
- 非法文件名字符会被替换为 `_`。

### 4.2 结构

```json
{
  "schema_version": 1,
  "generated_at": "2026-10-21T02:46:43.123456+08:00",
  "generated_date": "2026-10-21",
  "item": { "name": "怪物猎人", "item_id": "211064002", "item_type": "transcendent" },
  "filters": {
    "price": { "min": 100, "max": 5000 },
    "quality": { "min": 390, "max": 400 },
    "subperks": [{ "id": "20017:0", "name": "强化大容量弹夹" }],
    "abilities": [{ "group": 1, "min": 30, "max": 150 }]
  },
  "request": {
    "endpoint": "POST https://tfd-api.nexon.com/api/v2/trademarket/cn/products",
    "payload": { "...": "实际发送的请求体（不含分页变化）" }
  },
  "stats": {
    "pages_fetched": 2,
    "price_slices": 1,
    "requests": 2,
    "truncated": false,
    "server_total_max": 31,
    "server_totals": [31],
    "matched": 31
  },
  "products": [
    {
      "n8Id": "898484701287406594",
      "item_name": "怪物猎人",
      "item_type": "transcendent",
      "price": 600.0,
      "register_time_utc": "2026-10-20T18:17:23.057",
      "register_time_cst": "2026-10-21T02:17:23.057000+08:00",
      "seller": { "strNickName": "xxx#1234", "isAccessState": 1, "n4MasteryRank": 37 },
      "data": { "n4WeaponQuality": 400, "subperks": [], "options": [] }
    }
  ]
}
```

说明：

- `n8Id` 保持字符串（18 位，超出 JS 安全整数）。
- `price` 为数值；`register_time_utc` 是服务端原值（UTC，无后缀），`register_time_cst` 已 +8 小时。
- `data` 保留服务端原始结构（不同物品类型字段不同），便于二次分析。
- `stats.truncated = true` 表示某个价格单点仍有 ≥10000 条、无法继续切分，结果可能不完整。

---

## 5. 关键实现细节

1. **服务端筛选**：请求体直接带 `strGrade`、`ancestor.options`、`transcendent.quality/subperks/abilityPoint1~3`，服务端返回匹配结果，不做本地过滤。
2. **物品解析**：`item.name` 通过 Jarvis 字典（113 条）解析为 `(moduleType, ItemId)`；支持中/英/日/韩等名称，重名时需用 `item.type` 或 `item.id` 指定。
3. **分页**：每页 20 条，最多 500 页；`totalCount` 为 `null` 视为 0。
4. **10000 封顶**：当某个价格区间的 `totalCount >= 10000` 时，对半切分价格区间递归抓取，最后按 `n8Id` 去重。
5. **属性名 → ID（中英文映射）**：`advantage`/`penalty` 走 `ITEMOPTION` 筛选器（`subType="{type}:{itemId}"`），`subperks` 走 `SUBPERK`（`subType="{itemId}"`）。程序会同时请求当前语言、`cn`、`en` 三套文案，按 ID 合并为「主名称 + 别名」，所以传中文名或英文名都能解析到同一个 `strId`。
6. **Jarvis 字段兼容**：不同边缘节点会返回 `ItemId` 或 `itemId`，代码按小写键统一读取。
7. **TLS 指纹**：默认 `impersonate=chrome133a`，不手写 `Sec-Ch-Ua*`。

---

## 6. Playwright 人工模拟比对

```powershell
.\.venv\Scripts\python.exe playwright\manual_compare.py --spec specs\monster_hunter_full.json
# 需要看到浏览器窗口时加 --headed
```

脚本会：

1. 打开 `https://tfd.nexon.com/zh-Hans/market`，搜索并选中物品；
2. 在筛选面板里设置价格、品级、品质、辅助能力、超越能力（与 spec 一致）；
3. 捕获页面自身发出的 `POST /v2/trademarket/cn/products` 响应；
4. 用本项目对同一条件抓取一次，写 JSON；
5. 比对：UI 的 `totalCount` vs API 的 `server_total_max`、UI 首屏 20 条与 API 结果的 ID 重合率、以及每条 UI 结果是否满足筛选条件。

产物：

```text
.playwright/after_filters.png      # 筛选后的页面截图
.playwright/ui_last_response.json # UI 最后一次 products 响应
.playwright/compare_report.json    # 比对报告
```

判定：UI 非空、无筛选违规、ID 重合率 ≥ 60% 即 PASS。

---

## 7. 已知边界

- `descendant` 类型的挂单是「该角色可装备模块」的重复视图，抓商品时建议不要与 `ancestor` 重复采集；但它的 `statis` 是独立汇总指标。
- 平台筛选（`strPlatform`）在新 API 中已失效，本项目不提供。
- 价格上界超过 25000 会被服务端钳制；下界超过 25000 会返回空。
- 单价格点 ≥10000 条时无法再切分，只能取前 500 页并标记 `truncated`。
- `statis` 接口未包含在本项目（只做挂单筛选与导出）。

---

## 8. 目录结构

```text
tfd-market-v2/
├─ market_v2/
│  ├─ api.py          # v2 HTTP 客户端（products/filter/Jarvis）
│  ├─ catalog.py      # 物品名解析
│  ├─ filters.py      # spec 校验 + 请求体构造 + 名称→ID
│  ├─ collector.py    # 分页 + 10000 封顶价格切分 + 去重
│  ├─ exporter.py     # JSON 命名与写入
│  ├─ runner.py       # 端到端流程
│  └─ cli.py          # 命令行
├─ specs/             # 示例筛选条件
├─ tests/             # 39 个单元测试 + 2 个真实 API 测试
├─ playwright/        # 人工模拟比对脚本
├─ output/            # 生成的 JSON
├─ qqbot/             # NapCat HTTP 客户端 + 接口自检
├─ docs/              # NapCat 接口使用文档
├─ run.py             # 入口
├─ requirements.txt
└─ TEST_CASES.md      # 测试用例与实测记录
```

---

## 9. QQ 机器人（NapCat / OneBot v11）

NapCat HTTP Server 默认地址 `http://127.0.0.1:3000`。安装、版本要求与 OneBot 配置见 [9.1](#91-napcat-安装与配置)。

### 9.1 NapCat 安装与配置

本项目在 **NapCat Shell 构建 52230**（Windows 一键包 `NapCat.52230.Shell`）上开发与验证；
其它 Shell 版本只要 OneBot v11 接口一致也能用，HTTP 调用格式见
[docs/napcat-http-api.md](docs/napcat-http-api.md)。

**1) 安装**：从 NapCat 官方发布页下载 Windows Shell 一键包，解压后运行 `napcat.quick.bat`。
该脚本实际执行 `NapCatWinBootMain.exe <QQ号>`，首次启动需要扫码登录；登录过一次后，
把脚本里的 QQ 号改成自己的即可快速启动。

**2) OneBot 配置**：编辑
`<NapCat目录>\versions\<QQ版本>-<构建号>\resources\app\napcat\config\onebot11_<QQ号>.json`，
或在 NapCat WebUI（默认 `http://127.0.0.1:6099`）里修改网络配置：

```json
{
  "network": {
    "httpServers": [
      {
        "enable": true,
        "name": "TFD 机器人",
        "host": "127.0.0.1",
        "port": 3000,
        "enableCors": false,
        "enableWebsocket": true,
        "messagePostFormat": "array",
        "token": "<TOKEN>"
      }
    ]
  },
  "enableLocalFile2Url": false
}
```

| 字段 | 要求 |
|---|---|
| `enable` | `true`，否则不监听 |
| `host` / `port` | 默认 `127.0.0.1:3000`，需与 `--url` 一致 |
| `enableWebsocket` | 必须 `true`：事件走 WS，发消息走 HTTP，二者共用 3000 端口 |
| `messagePostFormat` | `array`，代码按消息段数组解析 |
| `token` | 与启动时传入的 `NAPCAT_TOKEN` 完全一致 |

**3) 重启 NapCat 并自检**：

```powershell
$env:NAPCAT_TOKEN = "<TOKEN>"
.\.venv\Scripts\python.exe -m qqbot.check_napcat
.\.venv\Scripts\python.exe -m qqbot.check_napcat --send-private <QQ号>
```

- 客户端封装：[qqbot/napcat_client.py](qqbot/napcat_client.py)
- 自检脚本：[qqbot/check_napcat.py](qqbot/check_napcat.py)
- 接口文档与实测经验：[docs/napcat-http-api.md](docs/napcat-http-api.md)

> 注意：NapCat 4.x 起要求参数**平铺在 body 顶层**，不能套 `{"action","params"}`。

### 9.2 配置

机器人、后台与定时任务的可调参数都在根目录 `config.json`。仓库只提供模板，首次运行先复制一份：

```powershell
Copy-Item config.example.json config.json
```

`config.json` 已加入 `.gitignore`，不会被提交；请勿把真实 QQ 号、代理地址、Bark Key 写进模板。

常用项：

| 键 | 说明 |
|---|---|
| `max_subscriptions` | 每个用户最多订阅数 |
| `analytics_whitelist_qq` | 订阅分析白名单 QQ，空列表 = 关闭记录 |
| `login_allowed_qq` | 允许使用登录命令的 QQ，空列表 = 禁止所有人 |
| `steam_auto_allowed_qq` | 允许 Steam 自动登录的 QQ |
| `login_proxy_enabled` / `login_proxy_url` | 登录浏览器代理开关与地址 |
| `admin_host` / `admin_port` | 后台监听地址与端口（默认 `127.0.0.1:8090`） |
| `bark_*` | 手机推送默认参数（冷却、铃声等级等） |
| `flip_*` | 倒卖利润计算参数（手续费、税率、冷却） |

### 9.3 启动市场推送机器人

NapCat 的 `httpServers[0]` 打开 `enableWebsocket: true` 后，WebSocket 与 HTTP
共用 `3000` 端口：事件走 `ws://127.0.0.1:3000/`，发消息仍走 HTTP。

```powershell
$env:NAPCAT_TOKEN = "<token>"
.\.venv\Scripts\python.exe run_bot.py

# 可选参数
.\.venv\Scripts\python.exe run_bot.py --tick 30 --workers 2 --max-pages 3 --db data/qqbot.db

# 图片卡片默认开启（hybrid 混合风），可切换版式或退回纯文字
.\.venv\Scripts\python.exe run_bot.py --image-style official
.\.venv\Scripts\python.exe run_bot.py --image-style compact
.\.venv\Scripts\python.exe run_bot.py --no-image

# Steam 扫码登录：国内直连 steamcommunity.com 会超时，需给登录浏览器配代理
.\.venv\Scripts\python.exe run_bot.py --login-proxy http://127.0.0.1:7897
# 排查登录问题：输出 [audit] 导航/Cookie/API 预检日志
.\.venv\Scripts\python.exe run_bot.py --login-proxy http://127.0.0.1:7897 --login-debug
# 或设置环境变量
$env:TFD_LOGIN_PROXY = "http://127.0.0.1:7897"
```

QQ 私聊命令：

```text
/订阅 怪物猎人 价格=100-5000 品质=390-400 辅助=强化大容量弹夹 能力1=30-150 间隔=10m
/订阅                       # 分步向导
/我的订阅
/设置 1 间隔=30m 条数=20 仅新=0
/立即查 1
/选项 怪物猎人              # 查看可选属性/辅助能力
/物品 怪物
/删除 1                     # /暂停 1、/恢复 1

/登录                       # Steam 手机 App 扫码（务必私聊）
/浏览器登录                  # 机器人主机弹出浏览器，手动登录
/导入登录                   # 粘贴 Cookie/cookies.json/storage_state
/完成导入                   # 分多条粘贴后收尾
/登录状态 /检查登录
/登出
/bark                       # 手机推送（Bark）：状态 / 设置 Key / 铃声 / 测试 / 开关
/bark 订阅 3 开              # 开启某条订阅的手机推送
/模糊 3 价格=100-8000 能力2=143-150   # 倒卖模糊过滤；不带参数查看，/模糊 3 清除 恢复默认
/购买 怪物猎人 价格=100-5000 # 筛选→序号→1私聊购买/2休闲室购买→确认
/求购 怪物猎人 价格=100-5000 # 私聊购买（/chat → /chat/buy）
/休闲室 怪物猎人              # 休闲室购买（/lounge → /lounge/buy）
/组队 怪物猎人                # /休闲室 的别名
/选择 3                      # 也可直接回复图片里的序号
/确认 3                      # 确认第 1 步打开的私聊框/休闲室框
/采购记录 10
```

- 调度：每 30 秒 tick，相同筛选条件只抓一次、分发给多个用户。
- 抓取：默认只取前 3 页（`sort=latest`），不做全量翻页。
- 推送：默认每轮全量，`[新]` 仅作标记；每人订阅上限默认 5 个（`config.json` 的 `max_subscriptions` 可调）、间隔 5 分钟~24 小时。
- 订阅分析：仅统计 `config.json` 白名单 QQ 的定时抓取、仅超越武器；消失历史写入 `disappeared_history`，卖家与导狗（倒卖）记录写入 `item_sellers` / `seller_flips`，供后台读取。
- `/帮助` 会附带用户使用图（`docs/design/qqbot-command-guide.png`）；`/查价` 结果默认发订阅同款商品卡，失败自动退回文字。
- 商品卡：默认发“标题文字 + 商品图片”，图片为 hybrid 风（价格、品质、辅助能力数值、能力点数）；渲染失败自动退回纯文字。
- 图片素材：已预取 113 个物品图到 `data/assets/`（约 4MB），索引 `data/assets/index.json`；
  更新物品图用 `.\.venv\Scripts\python.exe -m qqbot.assets --fetch`。
- 群聊：群里用 `/命令`（可同时 @机器人）；纯 @ 不回复；可用 `QQBOT_GROUPS` 限制群、`QQBOT_IGNORE_QQ` 屏蔽 QQ管家/其他机器人。
- 定时输出只走私聊：订阅推送/图片卡/失败通知/倒卖利润不会发群；群聊执行 `/立即查` 的状态与结果也只私聊发送。
- 订阅命令（`/订阅`、`/我的订阅`、`/设置`、`/暂停`、`/恢复`、`/删除`、`/立即查`、`/模糊`）要求好友关系；`/查价`、`/物品`、`/选项` 等查询命令不受限制。非好友订阅会被自动暂停，连续 10 次投递失败也会自动暂停。
- 单实例：启动时锁 `data/qqbot.db.lock`，重复启动会直接退出（退出码 3）。
- 数据：`data/qqbot.db`（SQLite，WAL），日志 `logs/qqbot.log`。
- 二次开发、并发模型、存储设计与安全说明：[docs/qqbot-development.md](docs/qqbot-development.md)

### 9.4 登录与采购（v2 写操作）

登录提供 **3 种方式**，按环境选择：

| 方式 | 命令 | 适用场景 |
|---|---|---|
| Steam 扫码 | `/登录` | 用户有 Steam 手机 App，机器人能访问 steamcommunity.com（需代理） |
| 有头浏览器 | `/浏览器登录` | 机器人主机有桌面，操作者能直接看到浏览器窗口 |
| Cookie 导入 | `/导入登录` | 远程用户；扫码/浏览器都失败时的兜底 |

> 登录态有效期实测约 4~5 小时（`_if_game_auth` 是服务端会话，页面刷新无法延长）。
> 过期后只能重新登录，本项目不提供续期命令。

**Steam 扫码**：

1. 用户私聊发送 `/登录`；
2. 机器人在独立线程启动 Playwright，打开 Nexon 登录页并点击 Steam；
3. 截取 Steam 弹窗里的二维码（放大 + 白边）私聊发给用户；
4. 用户用 Steam 手机 App 扫码并在手机上确认；
5. 机器人等待回调，拿到 `_if_game_auth`、`_ifwt_guest_2392`、`A2SK` 等 Cookie；
6. 保存后立刻用 `POST /api/v2/trademarket/chat`（探测用的假 n8TMID）校验会话。

**有头浏览器**（旧 `login_helper.py` 方式）：

- `/浏览器登录` 会在机器人所在电脑弹出 Chromium，并直接打开 **Nexon 登录选择页**；
- 用户自己选 Nexon ID / Steam / Xbox / PSN 完成登录；
- 浏览器检测到 `_if_game_auth` + `_ifwt_guest_2392` 后自动保存并关闭；
- 仅适合能操作机器人主机的用户；看不到窗口时改用 `/导入登录`。

> 实测：自动化浏览器里的市场页商品列表可能被 Cloudflare 的 API 预检挑战拦成空，
> 页面看起来“没登录”，但登录回调已经写入 `_if_game_auth` / `_ifwt_guest_2392`；
> 机器人用 `curl_cffi` 直连交易 API，令牌本身是有效的（实测校验返回 `NO_COUNT`，即会话有效）。

**Cookie 导入**（旧 `auth_cookies.json` 方式）：

- `/导入登录` 后直接粘贴，支持：
  - Cookie 字符串：`_if_game_auth=...; _ifwt_guest_2392=...; A2SK=...`
  - 旧 `auth_cookies.json`（扁平字典）
  - 旧 `cookies.json`（`{"cookies":[...]}`）
  - Playwright `storage_state.json`（只取其中 Cookie）
- 内容较长可以分多条发送，最后发 `/完成导入`；5 分钟无操作自动失效。
- 也可以一次性发送：`/导入登录 <Cookie 内容>`。
- 导入成功后请撤回包含 Cookie 的消息。

存储与安全：

- 每个 QQ 用户一行，存在 `data/qqbot.db` 的 `tokens` 表，记录 `method` 与 Cookie；
- 只存 Cookie，不存密码与浏览器状态；Cookie 不会出现在日志或聊天里；
- Cookie 等同于登录凭据，请勿把 `data/qqbot.db` 分享给他人；`/登出` 会立即删除；
- 登录会话最多同时 2 个；扫码默认 300 秒、浏览器登录至少 600 秒，可 `/取消登录`。

**手机推送（Bark）**：

- 每个用户维护自己的 Bark Key：私聊 `/bark 设置 https://api.day.app/你的Key`，
  Key 用 Windows DPAPI 加密后存 `user_bark`，后台只显示末四位；写入后请撤回含 Key 的消息。
- 铃声分两层：用户默认（`/bark 铃声 alarm`）+ 每个订阅可单独覆盖（`/设置 3 铃声=minuet`）；
  直接填铃声名，不要加 `.caf` 后缀（实测带后缀反而不响）。
- 订阅级开关：后台订阅列表的「Bark」列，或 `/bark 订阅 3 开`、`/bark 订阅 3 关`。
- 触发条件：该订阅开关开启且本轮命中 > 0；同一订阅默认 120 秒冷却、每用户每分钟最多 3 条，
  不受静默时段限制（可在后台调整冷却与上限）。
- 通知内容只有文字：标题「物品名 命中 N 条」，副标题「订阅 #x · 新 M 条 · 最低 ¥P」，
  正文是该商品精确条件的 `/购买 ...` 命令，可直接复制到 QQ 执行；同一订阅新通知覆盖旧通知。

**倒卖利润（仅手机订阅）**：

- 订阅命中 1~3 条时，用「模糊过滤表」强制抓一次（等价 `/立即查`），按价格升序取第 2~(m+1) 低
  作为卖出参考，计算 `利润 = p2 - (p1 + 500 + p2 × 10%)`，取最高值私聊给你；命中 ≥4 条跳过。
- 模糊表默认：物品名/类型、品质与原订阅一致；辅助忽略；能力只留一条（三个区间按最低值取最大，
  空的忽略）；价格/品级忽略。
- 自定义：`/模糊 3 价格=100-8000 能力2=143-150`，或后台订阅编辑里的「模糊过滤（倒卖）」。
- 同一订阅默认 10 分钟 1 条，内容不变不重发；手续费、税率、冷却都可在后台调整。
- 订阅间隔最小已降到 **1 分钟**，建议只给手机订阅设 1 分钟，避免 Nexon 请求过多。

采购接口（两对两步端点）：

| 命令 | 服务端调用 | 交互 |
|---|---|---|
| `/购买` | `/chat`→`/chat/buy` 或 `/lounge`→`/lounge/buy` | 选商品后选 1 私聊购买 / 2 休闲室购买；第 1 步返回 `enc` 后再确认 |
| `/求购` | `/chat`→`/chat/buy` | 直接走私聊购买 |
| `/休闲室` | `/lounge`→`/lounge/buy` | 直接走休闲室购买 |
| `/组队` | 同上 | `/休闲室` 的别名 |
| `/确认 <id>` | 第二步 | 确认第 1 步打开的私聊框/休闲室框 |

- **私聊购买**：`/chat` 在官网打开私聊框，**不会立刻发游戏内私聊**；`/chat/buy` 才是确认并发送。
- **休闲室购买**：`/lounge` 打开“添加信息到休闲室”框；`/lounge/buy` 才确认把卖家休闲室添加到游戏中。
- 所以这是 **2 对端点 = 2 种购买方式**，机器人不会把 `/chat` 和 `/lounge/buy` 混用。
- `/购买` / `/求购` / `/休闲室` 先按筛选条件抓取第一页，渲染**带 1~N 序号**的图片 + 文字列表；
  用户回复序号或 `/选择 N` 选定商品，再选择购买方式。
- 私聊购买会用列表里的 `seller.isAccessState` 预检卖家在线；休闲室购买不预检卖家在线。
- 两个第 1 步都要求：已登录 + 买家游戏 App 已启动并进入主角色。
  失败会按 `NO_LOGIN`、`INVALID_SESSION`、`NO_PRIMARY`、`NO_ACCESS`、`NO_ACCESS_SELLER`、
  `RETRY_LATER`、`NO_ITEM`、`NO_COUNT`、`MATCHED_USERINFO`、`RESTRICT_*` 给出不同提示。
- 第 1 步成功后，回复 `1` 确认执行第 2 步、回复 `0` 取消；也可以稍后发送 `/确认 <id>`。
  取消会把该记录标记为“已取消”。
- 默认按价格从低到高；显式写 `排序=最新` 等则按用户排序。official 版式最多显示 5 条，其他版式 10 条。
- 30 秒本地冷却只限制第 1 步；第 2 步不额外拦截，由服务端决定。
- 采购记录在 `purchases` 表，保留 90 天；第 1 步记录 `chat_prepare`/`lounge_prepare`，
  第 2 步更新同一条记录状态；`enc` 不在聊天里展示。
- **登录代理**：`steamcommunity.com` 在国内直连会超时；`--login-proxy`/`TFD_LOGIN_PROXY`
  只让 **Steam 域名**走代理。所有 `nexon.com`（含 `signin`、`tfd`、`tfd-api`、`jarvis-api`）
  默认直连，避免代理节点 SSL 断开或 403；`--no-login-bypass` 可关闭该直连绕过。
- **登录诊断**：`--login-debug` 会输出 `[audit]` 日志（导航、Cookie 名称/长度、tfd-api/jarvis 响应、
  代理预检状态），日志在 `logs/qqbot.log`；本地审计脚本产物在 `logs/login_audit/`。

---

## 后台管理（仅本机）

后台是独立进程，默认只监听 `127.0.0.1:8090`（可在 `config.json` 的 `admin_host` / `admin_port` 修改），不跑 Playwright，避免和 QQ bot 抢资源；唯一的外部请求是定时抓取 Nexon 成交统计。

启动：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-admin.txt
.\.venv\Scripts\python.exe -m admin
```

打开 `http://127.0.0.1:8090`。

- 首次启动会自动创建管理员 `admin`，随机密码只打印在控制台和 `logs/admin.log` 一次。
- 也可以先设置环境变量 `ADMIN_INITIAL_USERNAME` / `ADMIN_INITIAL_PASSWORD`，或在启动后重置：
  `python -m admin.manage reset-password admin`
- 管理员凭据、会话、审计放在独立的 `data/admin.db`；bot 数据仍在 `data/qqbot.db`。
- 权限/设置写入 `qqbot.db` 的 `blocked_users`、`user_permissions`、`system_settings`，bot 30 秒内自动生效，无需重启。
- 后台永不返回 `tokens` 的 Cookie / `enc`。

前端（Vue 3 + Element Plus）：

```powershell
cd web-admin
npm install
npm run build      # 产物 web-admin/dist 由后台直接托管
# 开发模式：npm run dev  → http://127.0.0.1:5173，/api 代理到 8090
```

可管理：用户封禁、按用户订阅配额与功能开关、订阅暂停/恢复/删除/参数、全局订阅上限、订阅分析白名单/开关/保留天数、分析数据（消失历史 / 同一商品卖家分组 / 导狗记录）、好友申请自动同意/欢迎、审计日志。

**成交统计**：后台「成交统计」页按官方图表的样式展示 12 件超越武器近 2 天的按小时成交（灰色柱=成交笔数，蓝色折线=平均价格，时间标签在图表下方）。admin 每 12 小时增量抓取一次（可在系统设置调整间隔或关闭），数据写入 `data/admin.db` 并永久保留，也可在页面上点「立即抓取」手动刷新。

**Steam 自动登录**：在「系统设置」页录入 Steam 账号密码，凭据用 Windows DPAPI 加密保存在 `data/secrets/steam_vault.bin`，后台只显示“已配置/未配置”，不会回显账号密码。仅 `steam_auto_allowed_qq` 白名单 QQ 可发送 `/登录 自动`；遇到 Steam Guard/邮箱验证码时浏览器保持打开，手动完成后才保存登录态。

**登录白名单**：`login_allowed_qq` 控制全部登录命令（`/登录`、`/登录 自动`、`/浏览器登录`、`/导入登录`、`/登录状态`、`/检查登录`），空列表等于禁止所有人登录；`/登出` 不受限制，方便用户随时清除自己的 Cookie。登录会保存 Nexon 登录态，请勿转发二维码或 Cookie。

**好友申请**：默认自动同意（排除封禁/忽略 QQ，每分钟最多 5 个），通过后自动发送欢迎语、`/帮助` 文字与使用图；后台「系统设置 → 好友申请」可关闭或调整上限。

---

## 免责声明

- 本项目为个人学习与自用工具，**与 NEXON / Nexon Games 无任何关联**，未获官方授权或认可。
- 所有市场数据均来自 Nexon 公开接口，数据与游戏素材版权归原权利方所有。
- 请遵守 Nexon 服务条款与当地法律法规，控制请求频率，不要用于商业转售、批量倒卖或任何滥用行为。
- 使用本项目产生的账号风险、数据风险与法律责任由使用者自行承担。
- 项目涉及 QQ 机器人、Steam 登录自动化与 Cookie 保存：请勿在他人设备上运行，也不要分享二维码、Cookie 或登录态。

## 许可证

本项目采用 [MIT License](LICENSE)。

## 贡献

欢迎 Issue 与 Pull Request。提交前请先跑测试：

```powershell
# 默认只跑单元测试，不访问真实接口
.\.venv\Scripts\python.exe -m pytest -q -m "not live"

# 需要真实接口时单独执行
.\.venv\Scripts\python.exe -m pytest -q -m live
```

请勿在提交中包含 `config.json`、`data/`、`logs/`、任何 Token / Cookie / 真实 QQ 号。


