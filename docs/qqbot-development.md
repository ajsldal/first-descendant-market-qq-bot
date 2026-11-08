# QQ 机器人开发与运维文档

适用代码：`qqbot/` + `run_bot.py`，NapCat 4.x / OneBot v11。
本文同时记录当前实现、并发模型、存储设计、安全边界和二次开发方法。

---

## 1. 设计目标

- 一个 QQ 号服务多个用户，每个用户有独立的订阅条件。
- 单进程常驻：asyncio 收事件和调度，少量线程执行同步抓取。
- 相同筛选条件只抓一次，结果分发给多个订阅。
- 只取最新几页，不做全量翻页；默认“每轮全量推送”。
- 长期运行不膨胀：所有内存结构有上限，数据库有保留期。

---

## 2. 运行架构与数据流

```text
QQ ↔ NapCat ──WS(事件)──► bot.py ──命令──► SQLite
                    ▲                        │
                    │HTTP(发消息)             │到期订阅
                    │                        ▼
                 pusher.py ◄── scheduler.py ──► market.py ──HTTPS──► Nexon API
                                                │
                                         2 个抓取线程（ThreadPoolExecutor）
```

1. `bot.py` 连接 `ws://127.0.0.1:3000/`，接收 `post_type=message` 事件。
2. 命令写入 SQLite；调度器每 30 秒取一次到期订阅。
3. 按 `fingerprint` 分组，每组只调用一次 `market.py`。
4. 结果经 `pusher.py` 格式化，通过 NapCat HTTP 接口发送。
5. 订阅级异常只影响该订阅；WS 断开自动重连。

---

## 3. 目录与模块

```text
tfd-market-v2/
├─ run_bot.py              # 入口：参数/环境变量、日志、启动 QQBot
├─ qqbot/
│  ├─ bot.py               # WS 事件、命令路由、向导接入、限速、维护任务
│  ├─ scheduler.py         # 到期扫描、指纹合并、抓取、去重、推送、退避
│  ├─ market.py            # 物品解析、字典/属性缓存、只取前 N 页
│  ├─ pusher.py            # 消息格式化、分片、发送
│  ├─ dsl.py               # /订阅 DSL、订阅级选项、指纹、帮助文案
│  ├─ wizard.py            # 无参 /订阅 的四步会话
│  ├─ store.py             # SQLite：用户/订阅/seen/runs/token/采购记录
│  ├─ nexon_auth.py        # Steam 扫码登录、二维码截图、Cookie 导出
│  ├─ bark.py              # Bark 手机推送：Key 加密、精确命令模板、冷却/限速
│  ├─ flip.py              # 倒卖利润：模糊过滤表、利润公式、消息模板
│  ├─ trade.py             # 已登录的 chat / chat/buy / lounge 写操作
│  ├─ napcat_client.py     # HTTP 动作调用（参数平铺）
│  └─ check_napcat.py      # 接口自检脚本
├─ market_v2/              # 原有市场抓取库（被 market.py 复用）
├─ tests/test_qqbot_core.py
├─ tests/test_qqbot_trade.py   # 写操作/令牌/采购记录
├─ tests/test_qqbot_login.py   # 代理解析/二维码放大/Cookie 解析
├─ tests/test_qqbot_login_commands.py  # 导入/取消命令
├─ tests/test_qqbot_browse.py  # 筛选→序号→私聊/休闲室 两阶段流程
├─ data/qqbot.db           # 运行时生成
└─ logs/qqbot.log          # 运行时生成
```

---

## 4. 用户识别与群聊

### 4.1 用户识别

- 每个事件里 `event.user_id` 是**发送者 QQ**，`event.self_id` 是机器人自己。
- 订阅表的主键是 `subscriptions.id`，归属字段是 `qq`；所有读/删/改操作都带 `qq = ?` 条件。
- 同一个人私聊和群聊共用同一套订阅（都按发送者 QQ 识别）。
- 机器人自己发的消息按 `user_id == self_id` 忽略。
- `message_id` 进入 500 条的去重队列，NapCat 重复推送时不会重复执行。

### 4.2 群聊支持

`bot.py` 的 `handle_event` 同时接受 `private` 和 `group`：

- 群消息必须带 `/`、`#`、`！` 前缀；可以同时 `@机器人`，但**纯 @ 不会触发回复**。
- 回复只使用 `reply` 引用原消息，**不再自动 `at` 发送者**。
- `BotSettings.group_whitelist` 非空时，只处理白名单群；通过 `QQBOT_GROUPS=123,456` 或 `--groups` 配置。
- 群聊中创建的是**个人订阅**，不是群共享订阅；群里任何人都可以创建自己的订阅。
- **定时输出只走私聊**：订阅推送、图片卡片、失败通知、倒卖利润都只发到订阅者私聊，绝不发群；群聊执行 `/立即查` 时，“正在检查/检查完成”也只私聊发送，群里不出现定时结果。
- 在群聊创建订阅时，确认消息会提示“推送结果只会私聊发送，不会发到本群”。

好友与订阅约束（2026-11）：

- `/订阅`、`/我的订阅`、`/设置`、`/暂停`、`/恢复`、`/删除`、`/立即查`、`/模糊` 要求发送者与机器人是好友；`/查价`、`/物品`、`/选项` 等群聊查询命令不受此限制。
- `FriendDirectory` 每 10 分钟刷新一次好友列表；调度器发现订阅所有者已不在好友列表时自动暂停该订阅（重新加好友后需手动 `/恢复`）。
- NapCat 的 `send_private_msg`/`send_group_msg` 现在会检查 `retcode/status`，失败按传输失败处理并进入退避，不再误记为成功。
- 订阅连续 10 次投递（推送）失败会自动暂停，避免被拉黑/删除机器人后永久空转。

### 4.3 @ 回环防护

群里同时有 QQ管家或其他机器人时，最容易出现“你 @ 我、我 @ 你”的死循环。当前有三层防护：

1. **回复不 @ 人**：群回复只带 `reply`（引用原消息）+ `text`。这样对方不会因为被 @ 而再次自动回复。
2. **纯 @ 不响应**：只有带 `/` 等前缀的命令才会执行；@机器人 后说“你好”不会得到任何回复。
3. **循环熔断**：同一 `(群, QQ)` 在 60 秒内触发 8 次回复后，静音该成员 10 分钟，并在日志里记录警告。
4. **黑名单**：`QQBOT_IGNORE_QQ=QQ号1,QQ号2` 或 `--ignore-qq`，永远不响应指定 QQ（适合直接屏蔽 QQ管家）。

> 如果对方机器人会对“所有消息”而非“@消息”自动回复，最后的兜底就是把它加入 `QQBOT_IGNORE_QQ` 或群白名单之外。

---

## 5. 并发模型

### 5.1 多用户同时走向导

- 事件处理和调度都在**一个 asyncio 事件循环**里，天然没有多线程竞争命令状态。
- 每个 QQ 有一把 `asyncio.Lock`，同一个用户的消息串行处理；不同用户互不阻塞。
- 向导状态 `Wizard._states` 以 `qq` 为键，每个用户一份，10 分钟过期。
- `Wizard` 内部加了 `threading.Lock`，因为 `feed()` 会通过 `asyncio.to_thread` 执行；过期会话由维护任务每 10 分钟清理。
- 结论：两个或更多用户同时走向导时，各自维护独立状态，不会串话。

### 5.2 跨线程边界

| 共享对象 | 保护方式 |
|---|---|
| SQLite 连接 | `Store._lock` + `busy_timeout` |
| 市场字典/属性缓存 | `MarketService._cache_lock` / `_catalog_lock` |
| 抓取结果缓存 | 只在事件循环内访问 |
| 向导状态 | `Wizard._lock` |
| `MarketClient` | `threading.local()`，每个线程一个实例 |
| 锁表/限速表 | 只在事件循环内访问，维护任务定期清理 |
| 命令触发的市场请求 | `asyncio.Semaphore(2)` 限制并发 |

`MarketClient` 自带 `_last_request_at`、`language`，不是线程安全的，所以**不能跨线程共享**；`market.py` 用线程本地实例解决。

### 5.3 已知并发边界

- 调度器和 `/立即查` 可能同时抓同一个指纹，产生一次重复请求；结果正确，只是多一次网络开销。
- 多个线程同时首次解析同一物品时，属性列表可能重复拉取一次，然后写入缓存。
- 这两处是有意的简单取舍，不影响正确性。

---

## 6. 缓存、比较算法与数据格式

### 6.1 三层缓存

| 缓存 | 位置 | 键 | TTL | 内容 |
|---|---|---|---|---|
| 抓取结果 | `Scheduler._cache` | 筛选指纹 | 60 秒 | `FetchResult`（最多 60 条原始商品） |
| 物品字典 | `MarketService._catalog` | 全局 | 24 小时 | 113 条 `CatalogItem` |
| 属性/辅助能力 | `MarketService._options` | `(类型, ItemId)` | 12 小时 | `OptionLists` |

- 抓取结果缓存每 10 分钟由维护任务清理：超过 10 分钟或超过 200 条即删除。
- 字典与属性缓存在内存中，不落盘；进程重启后重新拉取。
- **原始商品数据不持久化**，只在一次抓取/推送期间存在于内存。

### 6.2 三个哈希/集合，各司其职

1. **筛选指纹 `fingerprint`**
   `SHA1(ItemId + 类型 + 价格 + 品级 + 品质 + 辅助 + 能力1~3 + 增益 + 减益 + 排序)`
   只用于“相同条件只抓一次”，不参与结果比较。

2. **已见集合 `seen`**
   表 `seen(sub_id, item_id, first_seen_at)`，按订阅隔离。
   `Store.seen_new()` 用一条 `SELECT ... IN (...)` + `INSERT OR IGNORE` 返回首次出现的 `n8Id`。
   作用：
   - 给结果打 `[新]`；
   - 用户设置 `仅新=1` 时，只推首次出现的挂单。

3. **快照哈希 `last_hash`**
   `SHA1(本次结果全部 n8Id 排序后拼接)`，存在 `subscriptions.last_hash`。
   仅用于判断“与上轮相同”，在消息头追加提示；不做价格/属性 diff。

### 6.3 比较语义

- 默认（`only_new=0`）：每轮把命中的挂单全部推送，`seen` 只负责标记 `[新]`。
- `only_new=1`：只推送 `seen_new()` 返回的新 ID。
- 不比较价格变化、不比较属性变化；需要“降价提醒”要单独扩展，见 9.4。

### 6.4 商品数据格式

`market.py` 返回 `FetchResult`：

```python
@dataclass
class FetchResult:
    item: CatalogItem                 # 物品元数据（中文名、类型、ItemId）
    resolved: dict                    # 解析后的筛选条件（调试用）
    items: list[dict]                 # 服务端原始商品 JSON
    requests: int                     # 本轮请求数
```

`items` 中单条商品（节选真实结构）：

```json
{
  "n8Id": "898484701287498754",
  "n4SalePrice": 600.0,
  "dtRegister": "2026-10-20T18:31:14.017",
  "seller": {"strNickName": "Heretic1971#4459", "n4MasteryRank": 37},
  "data": {
    "n4WeaponQuality": 400,
    "subperks": [{"strName": "巨魔强化弹头 I"}],
    "options": [{"strDesc": "枪械攻击力\t58.2%[x]", "n4Point": "101", "n4Group": 0}]
  }
}
```

- `n8Id` 是 18 位字符串，超过 JS 安全整数，**必须按字符串处理**。
- `dtRegister` 是 UTC，推送时已转为 CST（`+8`）。
- 不同物品类型的 `data` 字段不同，`pusher.py` 按类型取摘要。

---

## 7. 存储设计

SQLite 文件：`data/qqbot.db`，WAL 模式。

```sql
CREATE TABLE users (
    qq         INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL
);

CREATE TABLE subscriptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    qq           INTEGER NOT NULL,            -- 归属用户
    spec_json    TEXT    NOT NULL,            -- FilterSpec JSON
    fingerprint  TEXT    NOT NULL,            -- 共享抓取用
    interval_sec INTEGER NOT NULL DEFAULT 600,
    next_run_at  INTEGER NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    only_new     INTEGER NOT NULL DEFAULT 0,
    max_push     INTEGER NOT NULL DEFAULT 20,
    quiet_hours  TEXT    NOT NULL DEFAULT '',
    first_push   INTEGER NOT NULL DEFAULT 1,
    bark_enabled INTEGER NOT NULL DEFAULT 0,  -- 该订阅是否推送手机
    bark_sound   TEXT    NOT NULL DEFAULT '', -- 空 = 继承用户默认铃声
    bark_last_at INTEGER NOT NULL DEFAULT 0,  -- 冷却计时
    flip_overrides_json TEXT NOT NULL DEFAULT '', -- 倒卖模糊过滤覆盖项
    fail_count   INTEGER NOT NULL DEFAULT 0,
    last_run_at  INTEGER,
    last_count   INTEGER,
    last_hash    TEXT,
    created_at   INTEGER NOT NULL
);

CREATE TABLE seen (
    sub_id        INTEGER NOT NULL,
    item_id       TEXT    NOT NULL,           -- n8Id
    first_seen_at INTEGER NOT NULL,
    PRIMARY KEY (sub_id, item_id)
);

CREATE TABLE runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_id      INTEGER NOT NULL,
    started_at  INTEGER NOT NULL,
    finished_at INTEGER NOT NULL,
    matched     INTEGER NOT NULL,
    pushed      INTEGER NOT NULL,
    new_count   INTEGER NOT NULL,
    requests    INTEGER NOT NULL,
    error       TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE tokens (
    qq              INTEGER PRIMARY KEY,          -- 一个 QQ 一份登录态
    cookies_json    TEXT    NOT NULL,             -- 仅 nexon.com Cookie，无密码
    user_agent      TEXT    NOT NULL DEFAULT '',
    platform        TEXT    NOT NULL DEFAULT '',
    method          TEXT    NOT NULL DEFAULT '',   -- steam_qr/manual_browser/cookie_import
    status          TEXT    NOT NULL DEFAULT 'unknown', -- valid/invalid/unknown
    last_error      TEXT    NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    last_checked_at INTEGER
);

CREATE TABLE user_bark (
    qq             INTEGER PRIMARY KEY,
    server         TEXT    NOT NULL DEFAULT 'https://api.day.app',
    device_key_enc BLOB    NOT NULL,              -- DPAPI 密文，不含明文 Key
    sound          TEXT    NOT NULL DEFAULT 'alarm',
    enabled        INTEGER NOT NULL DEFAULT 1,
    last_sent_at   INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT    NOT NULL DEFAULT '',
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE TABLE purchases (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    qq             INTEGER NOT NULL,
    mode           TEXT    NOT NULL,              -- chat / lounge
    action         TEXT    NOT NULL,              -- prepare / buy
    item_name      TEXT    NOT NULL DEFAULT '',
    item_type      TEXT    NOT NULL DEFAULT '',
    item_id        TEXT    NOT NULL DEFAULT '',
    n8_id          TEXT    NOT NULL DEFAULT '',
    price          REAL,
    seller         TEXT    NOT NULL DEFAULT '',
    enc            TEXT    NOT NULL DEFAULT '',   -- 确认凭证，仅内部使用
    status         TEXT    NOT NULL DEFAULT 'prepared', -- prepared/success/failed
    result_code    TEXT    NOT NULL DEFAULT '',
    result_message TEXT    NOT NULL DEFAULT '',
    raw_json       TEXT    NOT NULL DEFAULT '',
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);
```

`spec_json` 示例（也是 `market_v2.filters.FilterSpec` 的输入）：

```json
{
  "item": {"name": "怪物猎人", "id": "211064002", "type": "transcendent"},
  "price": {"min": 100, "max": 5000},
  "quality": {"min": 390, "max": 400},
  "subperks": ["强化大容量弹夹"],
  "ability1": {"min": 30, "max": 150},
  "sort": "latest"
}
```

### 7.1 为什么原始商品不落库

- 抓取结果只用于当轮推送，落库会迅速放大磁盘（每用户每轮几十条）。
- 只保留 `seen` 的 ID，足够做“新上架”判断。
- 需要历史数据时，建议新增 `snapshots` 表或按天写 JSON，而不是改现有表（见 9.5）。

### 7.2 保留期与清理

| 数据 | 清理策略 |
|---|---|
| `seen` | 30 天前删除，启动时 + 每 10 分钟维护 |
| `runs` | 30 天前删除，每 10 分钟维护 |
| `purchases` | 90 天前删除，每 10 分钟维护 |
| `tokens` | 不自动删除；`/登出` 或重新登录覆盖 |
| 抓取缓存 | 10 分钟或 200 条上限 |
| 向导会话 | 10 分钟 TTL，每 10 分钟清理 |
| 日志 | 5MB × 3 个滚动文件 |

SQLite 删除后文件不会自动缩小；需要回收空间时，可在停机时执行 `VACUUM`。

### 7.3 登录 token 与采购记录

- `tokens` 按 `qq` 一主键；`method` 记录来源（Steam 扫码 / 有头浏览器 / Cookie 导入）。
- 只保存 `nexon.com` Cookie，不保存浏览器状态；Nexon 不支持续期，登录态过期只能重新登录。
- 登录成功后先调 `POST /v2/trademarket/chat`（`n8TMID=1` 的探测请求）校验，
  只有不是 `NO_LOGIN/INVALID_SESSION` 才标记 `valid`。
- 一次失败的重新登录不会覆盖已有的 `valid` token。
- `purchases` 记录每次 `/求购`、`/购买`、`/组队` 的 item/n8Id/seller/enc/ResultCode；
  `enc` 只用于 `/确认`，不会在聊天或日志里输出。
- `TradeBrowseSession` 只存在内存（5 分钟 TTL）：保存筛选结果、当前选中商品、购买方式和第 1 步的 `enc`；
  用户回复序号 → 选商品，回复 1/2 → 选购买方式，第 1 步成功后再回复 1 确认、0 取消。
- 两对端点：私聊购买 = `/chat` → `/chat/buy`；休闲室购买 = `/lounge` → `/lounge/buy`。
  `/chat`/`/lounge` 只打开官网弹框，第二步才真正发送游戏内私聊 / 添加休闲室，机器人不会混用两组。
- 第 1 步记录 `chat_prepare`/`lounge_prepare`，第 2 步更新同一条记录状态；
  `/确认 <id>` 可在会话过期后补做第 2 步。私聊购买会用 `seller.isAccessState` 预检卖家在线。

登录入口：

| 命令 | 实现 | 说明 |
|---|---|---|
| `/登录` | `QRLoginSession` | Steam 二维码；需代理访问 steamcommunity.com |
| `/登录 自动` | `QRLoginSession` + `SteamVault` | 后台保存的 Steam 账号自动填密码；Guard/验证码保留浏览器转人工 |
| `/浏览器登录` | `ManualLoginSession` | 本机有头 Chromium 打开 Nexon 登录选择页，用户手动完成任意平台登录 |
| `/导入登录` | `parse_cookie_payload` | 支持 Cookie 头、扁平 JSON、cookies.json、storage_state.json（只取其中 Cookie）；可多段拼接 |

Steam 自动登录：
- 凭据用 Windows DPAPI（当前用户作用域）加密保存到 `data/secrets/steam_vault.bin`，不写数据库/日志；换 Windows 用户或换机器后需重新录入。
- 仅 `steam_auto_allowed_qq` 白名单内的 QQ 可触发；空列表等于关闭。
- 流程：Nexon 选择页 → Steam 弹窗 → 若显示二维码则切到账号密码表单 → 自动填写并提交；验证码完成后自动点击 OpenID 授权页的「登录」，等待 Nexon 带 `web_ticket` 跳回 `/main` 并写入 `_ifwt_guest_2392`，**不要主动跳裸 `/main`**。
- 出现 Steam Guard / 邮箱验证码 / CAPTCHA 时不关闭浏览器，QQ 提示人工完成；`steam_auto_guard_timeout` 默认 1800 秒，只有拿到 `_if_game_auth + _ifwt_guest_2392` 后才保存登录态并关闭浏览器。
- 连续失败/密码错误不会自动重试；可发送 `/取消登录` 立即关闭窗口。
- 自动登录期间禁用 `--login-debug` 截图，避免密码页进入 `logs/login_audit/`。

登录命令白名单（后台「系统设置 → 登录白名单 QQ」）：

- `login_allowed_qq` 控制全部登录命令：`/登录`、`/登录 自动`、`/浏览器登录`、`/导入登录`、`/完成导入`、`/取消登录`、`/登录状态`、`/检查登录`；空列表等于禁止所有登录。
- `/登出` 只删除本地登录态，不受白名单限制，避免用户无法清理自己的 Cookie。
- `steam_auto_allowed_qq` 仍单独限制 `/登录 自动`，是更小一层白名单。
- 非白名单用户发送登录命令时回复「登录功能仅对白名单 QQ 开放，如需使用请联系管理员。」

好友申请自动处理（后台「系统设置 → 好友申请」）：

- WS 收到 `post_type=request`、`request_type=friend` 后调用 `set_friend_add_request(flag, approve=true)`。
- `friend_auto_accept_enabled` 控制自动同意；`friend_accept_max_per_min`（默认 5）做每分钟限流；已封禁/`ignore_qq` 的申请人不处理。
- `friend_welcome_enabled` 控制通过后是否发送欢迎语、`/帮助` 文字与使用图；图片失败自动只发文字，发送失败会隔 2 秒重试一次。
- 同一 `flag` 在 10 分钟内只处理一次，避免重连重放导致重复欢迎。

登录态有效期（实测 2026-10）：

- `_if_game_auth` 是服务端会话，市场页/主站都**无法延长**它的生命周期；
  实测登录后约 4~5 小时失效，之后 `/chat` 返回 `INVALID_SESSION`，只能重新登录。
- 用旧 Cookie 打开市场页只会拿到访客 `_ifwt_guest_2392`，看起来"Cookie 齐全"但实际未登录，
  因此本项目**不提供** `/续期登录` 或定时续期，避免给出"续期成功"的假象。
- 检测到失效时，`/检查登录` 与交易命令会提示重新 `/登录`。

登录代理（后台「系统设置 → 登录代理」可开关并改地址）：

| 设置项 | 作用 | 默认 |
|---|---|---|
| `login_proxy_enabled` / `login_proxy_url` | `/登录`、`/登录 自动`、`/浏览器登录` 的浏览器 | 启用，地址空时回退 `--login-proxy` |

手机推送（Bark）：

- 每用户一份配置：`user_bark(qq, server, device_key_enc, sound, enabled)`；
  Key 用 Windows DPAPI（当前用户作用域）加密，DB 被拷走也拿不到明文。
- 订阅级 `bark_enabled` + `bark_sound`（空 = 继承用户默认，默认 `alarm`）。
- 触发：订阅开关开启 且 本轮 `matched > 0`，同时受全局开关、订阅冷却
  （`bark_cooldown_sec`，默认 120 秒）、用户每分钟上限（默认 3）约束；**不遵守** `quiet_hours`。
- 内容只发文字：标题「物品名 命中 N 条」、副标题「订阅 #x · 新 M 条 · 最低 ¥P」，
  正文是该商品精确条件的 `/购买 ...` 命令，并写入 `copy`（下一条通知用 `id=sub-<id>` 覆盖旧通知）。
- 发送：`POST {server}/push`，`device_key` 放 JSON body，5 秒超时、失败重试 1 次、
  `asyncio.to_thread` 执行；每 tick 最多 5 条、全局限并发，失败只记 `user_bark.last_error`。
- 安全：后台 `GET` 只回 `****末四位`；审计只记 `configured/changed`，不落 Key；
  QQ 命令仅私聊，写入后提示撤回消息。

铃声（`sound`）：

- 直接填铃声名，**不要加 `.caf` 等后缀**（实测带后缀反而不响），例如 `alarm`、`minuet`、`chime`。
- 自导入的铃声按 Bark App 里显示的名称填写（`.caf`、≤30 秒）。
- 名称不存在时 Bark 静默回退默认提示音，接口仍返回 200，只能靠手机确认。

倒卖利润（仅 `bark_enabled=1` 的订阅）：

- 触发：本轮命中 1~3 条；命中 ≥4 条直接跳过。每轮的利润消息受 `flip_cooldown_sec`（默认 600 秒）
  和签名去重限制（签名 = 最低价 n8Id + 卖出参考 n8Id + 利润）。
- 模糊过滤表默认：物品名/类型、品质照抄原订阅；辅助忽略；能力只保留一条
  （三个区间按**最低值**比较取最大，空的忽略）；价格/品级忽略；排序固定 `price_asc`。
- 自定义：`/模糊 <id> 价格=100-8000 能力2=143-150`、`/模糊 <id> 清除`；后台订阅弹窗同名字段。
- 抓取：用独立指纹做一次强制抓取（等价 `/立即查`，`force=True` 绕过缓存）。
- 计算：`利润 = p2 - (p1 + fee + p2 * tax)`；p1 为订阅命中最低价，p2 为模糊结果第 2~(m+1) 低价，
  取最大。`fee`/`tax` 可在后台配置（默认 500 / 10%）。
- 输出：私聊该 QQ 用户，含最低价卖家、卖出参考排名、成本明细、最高利润，并附该商品精确 `/购买` 命令。

订阅间隔下限已降到 **1 分钟**（`dsl.MIN_INTERVAL=60`）；快速轮询会显著增加 Nexon 请求，
建议只给开了手机推送的订阅设 1 分钟。

### 7.4 订阅抓取分析（仅超越武器）

只统计 `config.json` 白名单内用户的**定时订阅抓取**，不记录 `/立即查`、`/查价`、`/购买`；
只处理 `transcendent`，模块/触发器/继承者完全跳过。不新增 QQ 命令，也不发送分析消息。

配置：

```json
{
  "analytics_enabled": true,
  "analytics_whitelist_qq": [123456789],
  "analytics_runs_retention_days": 30
}
```

白名单为空时关闭记录（fail closed）。非白名单订阅仍照常抓取、推送。

表：

| 表 | 说明 |
|---|---|
| `crawl_runs` | 每轮抓取审计；`complete=0` 时不判消失，保留 30 天 |
| `sub_listings` | 每条订阅的挂单状态（内部按 n8Id 去重）与最新身份快照 |
| `disappeared_history` | 只追加的消失快照；含 `identity_key` / `seller_key`，永久保留 |
| `item_sellers` | 每个商品身份下的卖家汇总：角色、首次/最近上架、消失时间、挂单数、价格区间 |
| `seller_flips` | 已验证的导狗（倒卖）记录：原始卖家、上一手、导狗、消失与上架时间、价格变化 |

商品身份规则：

- 身份 = `n4WeaponQuality` + `data.subperks` 中 `isFixed=0` 的 2 条辅助词条 + 3 组 `n4Group` 最大 `n4Point`。
- 五项必须齐全且完全一致，才产生相同 `identity_key`；少一条或数值不同都视为不同商品。
- 固定辅助词条不落库、不参与身份。
- `n8Id` 只作为**挂单实例 ID**，不参与任何商品比对；订阅推送的 `seen` 与交易 `n8TMID` 仍按 n8Id。

卖家身份与导狗判定：

- 卖家键 `seller_key` = 昵称末尾 `#数字`（如 `Gutala#3157 → 3157`），改名不影响识别；`item_sellers.seller_name` 保留最近一次昵称用于展示。
- 原始卖家 = 同一 `identity_key` 里 `first_seen_at` 最早的卖家。
- 导狗 = 同一身份下、卖家键不同的后续挂单，并且同时满足：
  1. 原始卖家在该身份下已有消失记录；
  2. 导狗上架时间 `reseller_first_seen_at` 晚于该商品最后一次消失时间；
  3. 记录 `prev_seller`（消失记录里的上一手，链式转手时可能是中间商）。
- 判定只在新挂单 / 复现挂单时执行，每件最多 2 次索引查询；已存在的 `(identity_key, reseller_listing_key)` 直接跳过。
- 只有 `complete=1` 的完整抓取才会把上一轮 `active` 中缺席的挂单记为消失，抓取不完整不产生消失记录。

常用视图：

```sql
-- 同一商品（身份相同）出现多个卖家的分组
SELECT * FROM v_identity_groups WHERE sellers >= 2;

-- 每种商品的消失数量
SELECT * FROM v_disappeared_by_item;

-- 导狗记录（按商品或导狗筛选）
SELECT * FROM seller_flips WHERE item_id = '211064002' ORDER BY reseller_first_seen_at DESC;
```

旧库迁移（会先备份到 `data/backup/`，迁移期间需停止 bot 与 admin）：

```powershell
.\.venv\Scripts\python.exe -m qqbot.analytics_migrate --dry-run
.\.venv\Scripts\python.exe -m qqbot.analytics_migrate
```

迁移会重建 `sub_listings` / `disappeared_history`（去掉 n8Id 比对列与 raw 大字段）、删除 `reappear_checks` 及旧视图、回填 `item_sellers` 与 `seller_flips`，最后 `VACUUM`。

---

## 8. 命令与配置

### 8.1 命令

```text
/帮助
/订阅 怪物猎人 价格=100-5000 品质=390-400 辅助=弹夹 能力1=30-150 间隔=10m
/订阅                       # 分步向导
/我的订阅
/设置 1 间隔=30m 条数=20 仅新=0 静默=23:00-08:00
/立即查 1
/选项 怪物猎人
/物品 怪物
/删除 1
/暂停 1
/恢复 1
/取消                       # 退出向导

/登录                       # 仅私聊：Steam 手机 App 扫码
/登录 自动                  # 仅白名单 QQ：用后台保存的 Steam 账号自动登录
/浏览器登录                  # 本机有头浏览器手动登录
/导入登录                   # 粘贴 Cookie / cookies.json / storage_state
/完成导入                   # 多段粘贴后收尾
/登录状态
/检查登录
/登出
/bark                       # 手机推送：状态 / 设置 Key / 铃声 / 测试 / 开关
/bark 订阅 3 开              # 开启某条订阅的手机推送
/模糊 3 价格=100-8000 能力2=143-150   # 倒卖模糊过滤（不带参数查看，/模糊 3 清除 恢复默认）
/购买 怪物猎人 价格=100-5000   # 筛选→序号→1私聊购买/2休闲室购买→确认
/求购 怪物猎人 价格=100-5000   # 私聊购买（/chat → /chat/buy）
/休闲室 怪物猎人               # 休闲室购买（/lounge → /lounge/buy）
/组队 怪物猎人                 # /休闲室 的别名
/选择 3                        # 也可直接回复序号
/确认 3                        # 确认第 1 步打开的私聊框/休闲室框
/采购记录 10
```

- `/帮助` 会同时发送 `docs/design/qqbot-command-guide.png` 使用图（首次压缩到 1600 宽并缓存 base64，图片缺失/发送失败时退回纯文字）。
- `/查价`（别名 `/查询`）的结果默认渲染成订阅同款商品卡，渲染或发送失败自动退回原来的文字结果。

### 8.2 DSL 键

| 键（别名） | 适用 | 范围 |
|---|---|---|
| `价格` `p` | 全部 | 10~25000；`100-5000`、`>=100`、`<=5000`、`500` |
| `品质` `q` | 超越武器 | 1~400 |
| `辅助` `sp` | 超越武器 | 最多 2 个，逗号分隔 |
| `能力1/2/3` `a1..a3` | 超越武器 | 0~150 |
| `品级` `g` | 模块/触发器 | Tier1~Tier4 |
| `增益` `adv` | 模块 | 最多 3 条，`名称[:品级]` |
| `减益` `pen` | 模块 | 1 条 |
| `类型` `t` | 全部 | 超越 / 模块 / 继承者 / 触发器 |
| `排序` `sort` | 全部 | latest / price_asc / price_desc |
| `间隔` `every` | 订阅级 | 5m~24h |
| `条数` `limit` | 订阅级 | 1~50 |
| `仅新` `new` | 订阅级 | 1/0 |
| `静默` `quiet` | 订阅级 | `23:00-08:00` |
| `首次` `first` | 订阅级 | push / base |

### 8.3 配置

| 项 | 环境变量 | 参数 | 默认 |
|---|---|---|---|
| NapCat HTTP | `NAPCAT_URL` | `--url` | `http://127.0.0.1:3000` |
| WebSocket | `NAPCAT_WS_URL` | `--ws-url` | 由 HTTP 地址推导，`/` 路径 |
| Token | `NAPCAT_TOKEN` | `--token` | 空（必填） |
| 数据库 | `QQBOT_DB` | `--db` | `data/qqbot.db` |
| tick | — | `--tick` | 30 秒 |
| 抓取线程 | — | `--workers` | 2 |
| 单轮页数 | — | `--max-pages` | 3 |
| 群白名单 | `QQBOT_GROUPS` | `--groups` | 空 = 所有群 |
| 忽略 QQ | `QQBOT_IGNORE_QQ` | `--ignore-qq` | 空 = 不屏蔽 |
| 登录代理 | `TFD_LOGIN_PROXY` | `--login-proxy` | 空 = 直连（仅 Steam 需要代理）；后台 `login_proxy_url` 优先 |
| 登录超时 | `TFD_LOGIN_TIMEOUT` | `--login-timeout` | 300 秒 |
| 登录直连绕过 | `TFD_LOGIN_BYPASS` | `--login-bypass/--no-login-bypass` | 开启：所有 nexon.com 直连 |
| 登录诊断日志 | `TFD_LOGIN_DEBUG` | `--login-debug` | 关闭 |

---

## 9. 二次开发指南

### 9.1 新增一个命令

1. 在 `bot.py::_handle_command` 加分支。
2. 写一个 `async def _xxx(...)`，内部用 `_reply()` 回复。
3. 需要市场数据时用 `async with self._market_gate:` + `asyncio.to_thread(self.market.xxx, ...)`。
4. 更新 `dsl.help_text()`。
5. 在 `tests/test_qqbot_core.py` 加纯逻辑测试。

注意：所有用户输入都经过 `_strip_prefix` 和 800 字长度限制；不要直接拼 SQL、不要 `eval`。

### 9.2 新增筛选字段

以“新增 `等级=...`”为例：

1. `qqbot/dsl.py`：`KEY_ALIASES` 加别名，`parse_dsl` 里写入 `spec_dict`。
2. `market_v2/filters.py`：`FilterSpec` 加字段，`validate()`/`validate_for_type()` 校验，`build_payload()` 写进请求体。
3. `qqbot/dsl.py::fingerprint_for`：把这个字段加入指纹，否则不同条件会被错误合并。
4. `describe_spec()`、`help_text()`、`/选项` 提示同步更新。
5. 测试：DSL 解析、越界、指纹差异、payload 断言。

### 9.3 新增推送渠道

`Pusher` 目前只依赖 `NapCatClient`。扩展时定义一个协议：

```python
class Notifier(Protocol):
    async def send_private(self, qq: int, content: str) -> None: ...
    async def send_group(self, group_id: int, content: str) -> None: ...
```

`Scheduler` 只依赖 `push()` 和 `send_private()`；新增邮件/Webhook 时实现同样的方法即可，不要改调度逻辑。

### 9.4 群级共享订阅 / 降价提醒

- **群级订阅**：新增 `group_subscriptions(group_id, spec_json, creator_qq, ...)`，调度仍然按 `fingerprint` 合并；`seen` 的主键改为 `(group_sub_id, item_id)`。需要同时定义群管理员权限。
- **降价提醒**：新增 `prices(sub_id, item_id, last_price, updated_at)`，每轮对比 `n4SalePrice`；注意这要求持续跟踪已见商品，不能只取第一页，否则会漏掉不在首页的挂单。建议单独做成低频任务（如每小时全量一次）。

### 9.5 持久化原始结果

需要历史数据时，建议：

- 新增 `snapshots(sub_id, item_id, price, data_json, seen_at)`，按 30 天清理；或
- 每小时/每天把 `FetchResult.items` 写成 `output/qqbot/YYYY-MM-DD_物品.json`，复用现有 exporter 风格。

不要在每个订阅每轮都写文件，IO 会先于内存成为瓶颈。

---

## 10. 长期运行与内存

### 10.1 有上限的结构

| 结构 | 上限 | 清理 |
|---|---|---|
| `_seen_messages` | 500 条 | deque 自动淘汰 |
| `_locks` / `_lock_used` / `_last_command` | 按活跃用户 | 维护任务清理 1 小时未用 |
| `Wizard._states` | 按活跃会话 | 10 分钟 TTL + 维护任务 |
| `Scheduler._cache` | 200 条 | 10 分钟 TTL + 维护任务 |
| `MarketService._catalog` | 1 份（113 条） | 24 小时刷新 |
| `MarketService._options` | 物品数 × 2 | 12 小时刷新 |
| `MarketService._clients` | 线程数 | 进程退出时关闭 |
| `seen` / `runs` | 磁盘 | 30 天保留 |

### 10.2 异常隔离

- WS 断开：指数退避重连，最长 30 秒。
- 单条事件异常：记录日志，不打断连接。
- 单次 tick 异常：记录日志，下一轮继续。
- 单个订阅异常：只给该订阅退避，连续 3 次失败通知用户。
- 推送失败：`record_failure`，不会把失败当成功。
- 日志滚动：5MB × 3，避免磁盘写满。

### 10.3 仍需注意

- SQLite 文件删除记录后不自动收缩，长期运行建议每季度停机 `VACUUM`。
- `MarketService._clients` 会随线程数增长，但线程数受 executor 上限约束；不要把 `workers` 调得很大。
- 调度器和 `/立即查` 可能重复抓同一指纹一次，属于可接受开销。
- 建议用系统服务（NSSM / 计划任务）拉起 `run_bot.py`，并监控 `logs/qqbot.log`。

---

## 11. 安全边界

### 11.1 已做到

- **SQL 注入**：所有值使用 `?` 参数化；`update_options` 的动态列名来自白名单。
- **命令注入 / 代码执行**：没有 `eval`、`exec`、`subprocess`、`os.system`；DSL 只解析成结构化字段。
- **路径穿越**：没有用户可控的文件路径；数据库和日志路径固定。
- **消息注入**：发送的都是 `text` 消息段，不拼 HTML/CQ 代码。
- **越权访问**：`get/delete/update` 都带 `qq = ?`；不能操作别人的订阅。
- **资源滥用**：每人订阅上限默认 5 个（`config.json` 的 `max_subscriptions` 可调）；命令 1 秒限速、`/选项` 3 秒；输入最长 800 字；市场命令并发 2。
- **群滥用**：可选 `QQBOT_GROUPS` 白名单；群消息必须带前缀或 @。
- **密钥**：NapCat token 只从环境变量/参数读取，不写入数据库和日志。
- **Nexon 登录态**：只保存扫码/浏览器/导入得到的 Cookie，不保存浏览器状态，
  不接触密码；二维码仅私聊发送，默认 300 秒超时；`enc` 确认凭证只落库，不对外输出。
- **Cookie 导入**：只接受私聊；内容不写日志、不回显；多段缓冲 5 分钟过期；导入成功后提示撤回消息。
- **隐私**：不提供好友列表、手机号等查询命令；日志只记录错误，不记录完整消息。

### 11.2 尚未实现，部署前建议补

- 群管理员校验：目前群内任何成员都能创建自己的订阅；如需限制，增加 `admins` 表或群主判断。
- 好友校验：非好友私聊是否可达取决于 QQ 设置；如需只服务好友，调用 `get_friend_list` 做缓存校验。
- 全局配额：目前只限制单用户订阅数（`config.json` 的 `max_subscriptions`），没有全站订阅上限；用户量大时可加 `MAX_TOTAL_SUBSCRIPTIONS`。
- 审计：可把命令与推送结果写入单独审计表，便于排查滥用。
- 敏感词/内容审核：机器人只输出市场数据，风险较低；如果后续支持自由文本，需要加审核。

---

## 12. 运维与故障排查

### 12.1 启动

```powershell
$env:NAPCAT_TOKEN = "<token>"
.\.venv\Scripts\python.exe run_bot.py
```

### 12.2 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| HTTP 403 `token verify failed` | token 不对 | 用 `onebot11_<QQ>.json → network.httpServers[0].token` |
| WS 连不上 | 未开 `enableWebsocket`、端口不对 | 配置改 true 并重启 NapCat |
| 私聊发命令没反应 | 缺 `/` 前缀 | 用 `/帮助` |
| 群聊没反应 | 未 @、缺前缀、群不在白名单 | 检查三种条件 |
| 和 QQ管家/其他机器人反复 @ | 对方被 @ 后自动回复 | 回复已不 @ 人；把对方 QQ 加到 `QQBOT_IGNORE_QQ` |
| `Schema compilation error` | HTTP 动作套了 `params` | 本客户端已平铺，检查自写脚本 |
| 同一批挂单每轮都推 | 默认每轮全量 | `/设置 1 仅新=1` |
| 推送太多条 | `max_push` 太大 | `/设置 1 条数=10` |
| 检查失败连续 3 次 | 网络/接口异常 | 看 `logs/qqbot.log`，程序会退避重试 |
| 启动提示“已有实例在运行” | 已经有进程持有 `data/qqbot.db.lock` | 停掉旧进程，或确认没有第二个实例 |

### 12.3 备份

停机后复制 `data/qqbot.db` 即可（同时复制 `-wal`、`-shm` 更稳妥）。

---

## 13. 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_qqbot_core.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

当前覆盖：

- DSL 解析、区间、别名、错误提示、指纹稳定性；
- SQLite CRUD、归属校验、`seen_new`、保留期清理；
- 调度器同指纹合并、仅新过滤、首轮基线、静默时段、缓存清理；
- 消息格式化、`[新]` 标记、超长分片、两位小数时间；
- 向导流程、过期清理、群白名单解析；
- 重复订阅识别、推送失败不丢 seen、单实例文件锁。

---

## 14. 崩溃与重启行为

### 14.1 持久化的状态

以下数据在 SQLite 中，进程崩溃/被强杀后仍然存在：

- 用户与订阅（`users`、`subscriptions`）；
- 订阅级设置（间隔、条数、仅新、静默、首次）；
- 已见商品（`seen`）；
- 运行记录（`runs`）、`last_hash`、`fail_count`。

`add_subscription` 是一条 INSERT，`record_success` 把订阅状态和 `runs` 放在同一个事务里；崩溃时要么整体提交，要么整体回滚，**不会出现半条订阅**。WAL 文件在下次打开时由 SQLite 自动恢复。

### 14.2 不会持久化的状态

- **进行中的向导会话**：`Wizard._states` 只在内存里。崩溃后向导丢失，用户需要重新 `/订阅`。
- **消息去重队列**：`_seen_messages` 只在内存里；重启后 NapCat 不会重放旧消息，所以不影响。
- **抓取缓存**：重启后重新拉取。
- **限速记录**：重启后清零。

向导丢失不会留下脏数据，也不会产生“写了一半的订阅”；只是用户需要重新走一遍流程。重启后用户如果继续发纯文本，私聊会被忽略（因为不在向导中），群聊只有 @机器人 才会提示 `/帮助`。

### 14.3 重启后可能出现的现象

| 现象 | 原因 | 是否需要处理 |
|---|---|---|
| 用户继续输入向导内容没反应 | 向导状态已丢失 | 重新 `/订阅` |
| 刚创建的订阅没有收到确认消息 | 崩溃发生在 INSERT 之后、回复之前 | `/我的订阅` 能看到；重试相同条件会被去重拦截 |
| 重启后立刻收到一轮推送 | 到期订阅重新执行 | 正常；默认每轮全量，可能重复上一轮内容 |
| 只推新模式重复推了同一批 | 崩溃发生在发送成功、seen 落库之前 | 正常的 at-least-once 语义，不会丢消息 |
| 启动提示“已有实例在运行” | 旧进程还活着，或锁文件被占用 | 停掉旧进程；锁文件本身可以保留 |

### 14.4 已做的防护

- **单实例锁**：`run_bot.py` 启动时锁住 `data/qqbot.db.lock`；第二个实例直接退出（退出码 3），避免两个调度器同时推送、同时写库。进程崩溃时操作系统自动释放锁。
- **重复订阅拦截**：创建前按 `(qq, fingerprint)` 查找，已存在则提示订阅编号，不会因用户重试产生两份。
- **seen 后写**：调度器先推送，成功后才把 ID 写入 `seen`。推送失败或进程崩溃时，下一轮仍会重试，不会永久丢掉“新上架”。
- **事务边界**：订阅状态与运行记录同事务；设置修改单条 UPDATE。
- **失败退避**：接口/推送失败会指数退避，不会在崩溃重启后立刻高频重试。
- **启动清理**：启动时清理 30 天前的 `seen`；维护任务每 10 分钟清理向导、缓存、`runs`。
- **DB 容错**：WAL + `busy_timeout=5000`；锁冲突会等待而不是立即报错。

### 14.5 仍然存在的语义

- 推送是 **at-least-once**：可能重复，但不会因为崩溃而静默丢失。
- 调度是 **到期即执行**：没有“断点续跑”，重启后未完成的订阅会重新抓一次。
- 不会自动恢复向导进度；如果希望做到，需要把 `_State` 序列化到 SQLite（可作为后续扩展）。

---

## 15. 商品图片卡片

实现文件：[qqbot/card.py](../qqbot/card.py)。三套版式样图：
[官网风](../output/样图A_官网风.png)、[紧凑列表](../output/样图B_紧凑列表.png)、
[混合风格](../output/样图C_混合风格.png)。线上默认使用 `hybrid`（混合风格）。

### 15.1 官网是怎么索引图片的

用 Playwright 抓了官网市场页的真实 DOM，结论很明确：

```html
<div class="image">
  <img class="module-bg transcendent" src=".../img/market/bg--transcend.png">
  <img class="module-icon transcendent" src="https://nxsvc.inface.nexon.com/meta-binary/de66c142...">
</div>
```

- 官网的 `module-icon` 用的就是商品接口里的 `strBinaryUrl`，**没有第二套图片索引**。
- 图片是**按物品**共享的：同一物品的所有挂单都是同一张 1024×304 渲染图。
- Jarvis 物品字典里没有图片字段，所以图片 URL 只能从 `products` 结果里取。
- 结论：预取方案 = 对 113 个物品各发一次 `pageSize=1` 的最小查询，取 `strBinaryUrl` 下载，建立 `ItemId → 本地文件` 索引。不需要 Playwright 逐商品截图。
- 不采用“Playwright 截图官网商品卡”：单商品 1~3 秒、浏览器占 200~400MB，还要应对页面改版，不适合多人使用。

### 15.2 本地图片索引

- 索引文件：`data/assets/index.json`
- 图片目录：`data/assets/items/<ItemId>_<sha1(url)[:12]>.png`

```json
{
  "211064002": {
    "url": "https://nxsvc.inface.nexon.com/meta-binary/de66c142...",
    "path": "data/assets/items/211064002_202e5622998c.png",
    "type": "transcendent",
    "name": "怪物猎人",
    "size": 156125
  }
}
```

- 全量 113 个物品已预取完成，共约 **4MB**；角色/模块图十几 KB，武器渲染图 150~200KB。
- 渲染时 `card.py` 先查这个索引，直接读本地文件；只有索引缺失或官方 URL 变化时才下载并更新索引。
- 数据库仍然只存订阅条件和 `seen`，不存图片、不存二进制。

命令：

```powershell
# 补齐缺失图片（已有索引会跳过）
.\.venv\Scripts\python.exe -m qqbot.assets --fetch

# 查看本地索引状态
.\.venv\Scripts\python.exe -m qqbot.assets --status
```

建议在物品字典更新后（新武器/新模块上线）执行一次 `--fetch`；日常运行不需要联网取图。

官网 UI 素材（背景渐变、子弹图标）也放在 `data/assets/ui/`：

- `bg--transcend.png`（140×190，超越武器红黑渐变底）
- `ic-transcend.png`、`icon_bullet--GeneralRounds.png`

这些素材只用于把物品图渲染成官网那种“红黑渐变卡片”的图块，缺失时 `card.py` 会自动用代码生成渐变兜底，不影响出图。

### 15.3 渲染与缓存

- 输出：`data/render/<sha1(版式+物品+指纹+n8Id列表+新ID+条数)>.png`。
- 缓存命中直接复用；相同筛选条件的多个用户天然共享同一张图。
- 版式通过 `render_card(..., style=...)` 选择：

| style | 观感 | 一条高度 | 适合场景 |
| --- | --- | --- | --- |
| `official` | 仿官网：白底信息卡、物品图、品质条、辅助能力、超越能力分段条，数值按能力组上色并右对齐 | 约 900px | 条数少（≤3~5 条）时最像官网、信息最全 |
| `compact` | 深色一行一条：大号价格 + 三个能力点色块 + 辅助能力名称 + 两行数值摘要 | 约 152px | 条数多，快速横向比价 |
| `hybrid` | 深色官网风头部 + 中型条目：价格、能力色块、辅助能力（每条词条的全部属性数值）、每组关键数值 | 约 204px | 默认推荐，兼顾观感与信息密度 |

- 所有版式共用同一份解析结果：`strClassName/strBulletName`（枪械信息）、`subperks`（辅助能力）、
  `options`（按 `n4Group` 分成能力 1/2/3 三组，`n4Point` 为点数），能力组配色固定为橙 / 紫 / 绿。
- 数值处理：去掉描述尾部的 `(143pt)`，点数单独用大号字显示；`枪械伤害量ｘ954%` 统一转成 `×`。
- 失败降级：图片下载或渲染失败时，继续发原来的文字，不影响推送。

### 15.4 发送方式

- `base64://<...>`：不需要改 NapCat 配置，适合当前环境；官网风 5 条约 515KB，编码后约 690KB。
- 或把 NapCat 的 `enableLocalFile2Url` 设为 `true`，发 `file:///E:/...png`。
- 一条消息可以同时带 `text` 和 `image` 段：文字写“本轮 N 条 / 新 M 条”，图片放详情。

### 15.5 实测性能（本机）

- 全量 113 个物品图片首次预取约 1~3 分钟，落盘约 4MB；之后渲染完全走本地，不再请求官网。
- 市场抓取 40 条约 6~7 秒（网络占主导）。
- 物品图首次下载约 150~200KB，之后走本地缓存。
- 同一组 5 条怪物猎人数据（品质 400、能力1 143-150）实测：
  - `official`：900×4831，513KB；
  - `compact`：840×926，127KB；
  - `hybrid`：880×1256，184KB。
- 三种版式单张渲染均在 0.1~0.3 秒内（官网风最长）。
- 缓存命中约 0.002 秒。
- 单张渲染内存约 10MB；建议独立 1~2 个渲染线程，加队列和超时。
- 100 个**不同条件**的订阅每 10 分钟各渲染一张，CPU 约占单核 8%；相同条件的用户共享图片，实际更低。
- 渲染会排在抓取之后、发送之前，最多让推送晚 1 秒左右，不会阻塞 WS 事件处理。

### 15.6 接入位置与开关

已在 `qqbot/pusher.py::Pusher.push` 中接入，默认开启：

- 渲染在 `asyncio.to_thread` 里执行，不阻塞 WS 事件循环；图片取 `data/render` 缓存。
- 发送一条消息：`[text(标题行), image("base64://...")]`。文字只保留
  `format_result` 首行（物品名、本轮命中、新上架、与上轮相同），明细全部交给图片。
- 图片内容只包含本轮实际要推送的挂单（受 `条数` 上限与 `仅新` 过滤影响）。
- 渲染或发送失败时自动退回原来的纯文字分片，不影响订阅记录与 `seen`。
- 开关：
  - 命令行：`--image` / `--no-image`；`--image-style official|compact|hybrid`（默认 `hybrid`）。
  - 环境变量：`QQBOT_IMAGE=1/0`、`QQBOT_IMAGE_STYLE=hybrid`。
- 维护任务每 10 分钟清理一次渲染缓存：超过 24 小时，或超过 200 张且已超过 1 小时的文件会被删除。

---

## 12. 后台管理服务（admin）

独立进程，默认 `127.0.0.1:8090`（`config.json` 的 `admin_host` / `admin_port` 可改），只做 HTTP + 数据库；唯一的外部请求是定时抓取 Nexon 成交统计（`curl_cffi`），不导入 Playwright、不跑登录流程。

### 12.1 进程与数据

| 项 | 说明 |
|---|---|
| 入口 | `python -m admin`（uvicorn，单 worker） |
| 前端 | `web-admin/dist`，Vue 3 + Element Plus，构建后由 FastAPI 托管；开发用 5173 + `/api` 代理 |
| `data/admin.db` | `admin_users`、`admin_sessions`、`admin_audit`、`trade_stats_items`、`trade_stats_points`、`trade_stats_runs` |
| `data/qqbot.db` | `blocked_users`、`user_permissions`、`system_settings`（bot 读取） |

### 12.2 并发策略

- 两边都启用 WAL；admin 写连接 `BEGIN IMMEDIATE` + 短事务 + `busy_timeout=10000`，遇 `locked` 最多重试 5 次（50~800ms）。
- admin 读使用 `mode=ro` + `PRAGMA query_only=ON` 独立连接，列表强制分页，禁止无 `LIMIT` 全表扫描。
- 不在线执行 `VACUUM`；需要时停机窗口处理。备份用 SQLite Backup API，不直接复制 `.db`。
- admin 只低频写权限/设置，bot 写订阅运行记录；当前规模下不需要 PostgreSQL。

### 12.3 认证与安全

- Argon2id 密码哈希；会话 token 只存 SHA-256 哈希；HttpOnly + SameSite=Strict Cookie；写请求要求 `X-CSRF-Token`。
- 30 分钟空闲 / 8 小时绝对过期；登录失败 5 次/5 分钟限速。
- 默认只监听 `127.0.0.1`；远程访问需 TLS + TOTP + 防火墙。
- 所有写操作写 `admin_audit`（before/after JSON、IP、管理员）。
- API 永不返回 `tokens.cookies_json` 和 `enc`。

### 12.4 API

| 路径 | 说明 |
|---|---|
| `POST /api/auth/login` | 登录 |
| `GET /api/auth/me` | 当前管理员 |
| `POST /api/auth/change-password` | 修改密码并踢出其他会话 |
| `GET /api/users` | 用户列表（订阅数、登录态、封禁、权限） |
| `PUT /api/users/{qq}/permissions` | 按用户订阅配额/功能开关 |
| `POST /api/users/{qq}/block` / `DELETE` | 封禁/解封 |
| `GET/PUT /api/settings` | 全局订阅上限、分析开关/白名单/保留天数、Steam 自动登录开关/白名单/等待时长、登录命令白名单、登录代理、Bark 全局开关/冷却/级别/分组/循环/限速、倒卖手续费/税率/冷却、成交统计开关/间隔 |
| `GET/PUT/DELETE /api/users/{qq}/bark` | 该用户的 Bark 配置（Key 只回末四位） |
| `POST /api/users/{qq}/bark/toggle` | 开关该用户的手机推送 |
| `POST /api/users/{qq}/bark/test` | 发送一条测试推送（admin 进程直发） |
| `GET /api/steam/status` | Steam 凭据状态（不返回用户名/密码） |
| `PUT /api/steam/credentials` | 录入 Steam 凭据（仅 super_admin，DPAPI 加密） |
| `DELETE /api/steam/credentials` | 清空 Steam 凭据（仅 super_admin） |
| `GET/PATCH /api/subscriptions` | 订阅列表/修改参数（含 `bark_enabled`、`bark_sound`、`flip_overrides`） |
| `POST /api/subscriptions/{id}/pause|resume` | 暂停/恢复 |
| `DELETE /api/subscriptions/{id}` | 删除订阅（同时清理 seen） |
| `GET /api/analytics/disappeared` | 消失历史（商品 / identity_key 筛选） |
| `GET /api/analytics/identity-groups` | 同一商品分组（按挂单数倒序，`min_sellers` 默认 2） |
| `GET /api/analytics/identity-detail` | 商品详情：品质、能力、辅助词条、挂单历史、卖家、导狗记录 |
| `GET /api/analytics/group-sellers` | 某身份的卖家列表与角色 |
| `GET /api/analytics/flips` | 导狗（倒卖）记录，可按商品 / 导狗筛选 |
| `GET /api/trade-stats/items` | 超越武器列表（点数、24h 笔数/均价、最近时间点、抓取错误） |
| `GET /api/trade-stats/status` | 抓取开关、间隔、最近一轮汇总 |
| `GET /api/trade-stats/{item_id}` | 单件近 2 天按小时点集（CST 时间文本） |
| `POST /api/trade-stats/refresh` | 立即抓取一轮（写审计 `trade_stats.refresh`） |
| `GET /api/audit` | 审计日志 |
| `GET /api/dashboard` | 统计、DB 大小、最近审计 |

### 12.5 成交统计（超越武器）

- 数据源 `GET https://tfd-api.nexon.com/api/v2/trademarket/transcendent/{itemId}/2/statis`：匿名可读，但 Nexon WAF 会按 TLS 指纹拦截（`chrome133a` 返回 403）；`admin/trade_stats.py` 依次尝试 `safari17_0` → `edge101` → `chrome133a`。
- 只抓物品字典中 `item_type == transcendent` 的 12 件；每件之间间隔 0.2 秒。
- `days=2` 只返回近 48 小时里**有成交的小时**，时间按 CST（UTC+8），`average_price` 是字符串。
- 入库增量：`trade_stats_points` 主键 `(item_id, bucket_at)` + `INSERT OR IGNORE`，重复小时跳过；历史点永久保留，不做清理。
- 调度：admin 启动 60 秒后首轮，之后每 `trade_stats_interval_hours`（默认 12，范围 1~168）小时一轮；`trade_stats_enabled` 关闭后跳过自动轮次，手动「立即抓取」仍可用。进程内 `asyncio.Lock` 保证同一时刻只有一轮在跑。
- 页面「成交统计」：左侧 12 件超越武器（24h 笔数/均价、最近时间点、抓取失败标记），右侧 ECharts 双轴图——灰色柱为成交笔数（右轴），蓝色折线为平均价格（左轴），图例顶部居中，时间标签放在图表下方。
- 物品字典拉取失败时回退 `trade_stats_items` 里的历史条目，避免列表被清空。
- 每轮结果写入 `trade_stats_runs`（件数、新增点数、失败数、错误摘要），`/api/trade-stats/status` 返回最近一轮。

### 12.6 bot 侧生效方式

`qqbot/permissions.py` 的 `PermissionService` 每 30 秒从 DB 读取：

- `blocked_users`：命令和定时推送都跳过；
- `user_permissions`：`allow_subscribe` / `allow_login` / `allow_trade` / `allow_price_query`、用户级 `max_subscriptions`；
- `system_settings`：全局订阅上限、分析开关、白名单、`crawl_runs` 保留天数、登录命令白名单、好友申请自动同意/欢迎/限流。

优先级：DB 设置 > `config.json` 默认值。admin 服务首次启动会把 config 的值种入 `system_settings`，之后通过后台修改即可，无需重启 bot。

### 12.7 启动

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-admin.txt
.\.venv\Scripts\python.exe -m admin
# 前端
cd web-admin; npm install; npm run build
```

首次启动自动创建 `admin`，随机密码只打印一次；可用
`python -m admin.manage reset-password admin` 重置。


