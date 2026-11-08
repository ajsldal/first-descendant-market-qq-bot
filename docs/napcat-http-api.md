# NapCatQQ HTTP 接口使用文档（OneBot v11）

> 环境：NapCat **4.18.41**（`NapCat.Onebot` / `protocol_version=v11`）、QQ 9.9.33-52230、Windows。
> HTTP Server：`http://127.0.0.1:3000`，登录账号 `3964200143`。
> 本文所有「实测」结论均来自本机真实调用，脚本见 [qqbot/check_napcat.py](../qqbot/check_napcat.py)。

---

## 1. 配置文件位置

NapCat 的 OneBot 配置在 QQ 安装目录的 `resources/app/napcat/config/` 下：

```text
E:\NapCat.Shell.Windows.OneKey\NapCat.52230.Shell\versions\9.9.33-52230\resources\app\napcat\config\
└─ onebot11_<QQ号>.json
```

与 HTTP 服务相关的字段：

```json
{
  "network": {
    "httpServers": [
      {
        "enable": true,
        "name": "第一后裔机器人",
        "host": "127.0.0.1",
        "port": 3000,
        "enableCors": false,
        "enableWebsocket": false,
        "messagePostFormat": "array",
        "token": "********",
        "debug": true
      }
    ],
    "httpClients": [],
    "websocketServers": [],
    "websocketClients": []
  },
  "enableLocalFile2Url": false,
  "parseMultMsg": false,
  "timeout": { "baseTimeout": 10000 }
}
```

要点：

- `token` 就是 HTTP 调用的鉴权口令，改完配置需重启 NapCat。
- `httpServers[0].enableWebsocket: true` 时，**同一个 3000 端口同时提供 WebSocket**：
  `ws://127.0.0.1:3000/`（`/ws` 也可用），握手同样带 `Authorization: Bearer <token>`。
  事件从这里推送；WS 上的动作调用仍是经典 `{"action","params"}`，而 HTTP 动作要平铺参数。
- `messagePostFormat: "array"` 表示**事件上报**里的 `message` 是消息段数组（不是 CQ 字符串）。
- `httpClients` 才是「HTTP 事件上报」配置；`httpServers` 只负责接收动作调用。
  使用同端口 WS 时不需要再配 `httpClients`。
- `timeout.baseTimeout = 10000` 是 NapCat 等待 QQ 接口的超时（毫秒）。

---

## 2. 鉴权

两种方式，任选其一：

```http
POST /get_login_info HTTP/1.1
Host: 127.0.0.1:3000
Content-Type: application/json
Authorization: Bearer <token>
```

```text
GET http://127.0.0.1:3000/get_status?access_token=<token>
```

- 不带 token 或 token 错误：**HTTP 403**，响应体为 `{"message":"token verify failed!"}`。
- 403 是 HTTP 层错误，不是 OneBot 的 `retcode`，客户端要单独处理。

---

## 3. 请求格式（本版本最大的坑）

### 3.1 正确格式：参数平铺

```http
POST http://127.0.0.1:3000/send_private_msg
Content-Type: application/json
Authorization: Bearer <token>

{"user_id": 3964200143, "message": "hello", "echo": "req-1"}
```

即：**参数直接放在 JSON 顶层**，`echo` 用于回显匹配请求。`GET /{action}?参数=值` 同样可用。

### 3.2 错误格式：OneBot v11 经典包装

```json
{"action": "send_private_msg", "params": {"user_id": 3964200143, "message": "hi"}}
```

实测结果：

| 场景 | 返回 |
|---|---|
| `POST /send_private_msg` + 包装格式 | `retcode=200`，`status=failed`，`message` 为 NapCat 内部 `TypeError: Cannot read properties of undefined (reading 'type')` |
| `POST /get_stranger_info` + 包装格式 | `retcode=400`，`Schema compilation error: Expected required property` |
| `POST /get_login_info` + 包装格式 | 恰好成功（该动作没有参数）——**容易被误判为「格式没问题」** |

> 结论：参数被忽略时，无参接口全部正常、有参接口报奇怪的错。看到 `Schema compilation error` 或 NapCat 的 JS 堆栈，先检查是不是套了 `params`。

### 3.3 `POST /` 不是动作入口

```text
POST http://127.0.0.1:3000/  →  {"status":"ok","retcode":0,"message":"NapCat4 Is Running"}
```

它只是健康检查，**不会执行 action**。动作必须 `POST /{action}` 或 `GET /{action}`。

---

## 4. 响应格式

```json
{
  "status": "ok",
  "retcode": 0,
  "data": { "user_id": 3964200143, "nickname": "99" },
  "message": "",
  "wording": "",
  "echo": "py-1",
  "stream": "normal-action"
}
```

- `status == "ok" && retcode == 0` 才算成功。
- 失败时 `status="failed"`，`retcode` 常见值：

| retcode | 含义 | 备注 |
|---|---|---|
| 0 | 成功 | |
| 200 | 不支持的 API / NapCat 内部异常 | `message` 里常是 JS 堆栈，如 `不支持的Api __no_such_action__` |
| 400 | 参数校验失败 | `Schema compilation error: Expected required property` |
| HTTP 403 | token 校验失败 | 非 JSON 协议错误 |

- HTTP 200 不代表业务成功，必须判断 `retcode`，并保留 `message` 方便排查。

---

## 5. 实测通过的接口

| action | 参数 | 结果 | 耗时 |
|---|---|---|---|
| `get_version_info` | 无 | `NapCat.Onebot / v11 / 4.18.41` | 17 ms |
| `get_status` | 无 | `online=true, good=true` | 13 ms |
| `get_login_info` | 无 | `3964200143 (99)` | 12 ms |
| `get_friend_list` | 无 | 2 条 | 238 ms |
| `get_group_list` | 无 | 0 条（该账号无群） | 4 ms |
| `get_stranger_info` | `user_id` | 返回 `uid/uin/nick/...` | 224 ms |
| `can_send_image` | 无 | `{"yes": true}` | — |
| `send_private_msg` | `user_id`, `message` | `{"message_id": 1045085215}` | — |
| `send_msg` | `message_type`, `user_id`, `message` | 返回 `message_id` | — |
| `get_msg` | `message_id` | 返回完整消息对象（`sender`、`raw_message`、`message` 数组等） | — |
| `delete_msg` | `message_id` | `retcode=0`，撤回成功 | — |
| `send_private_msg`（无 token） | — | HTTP 403 | — |
| 未知 action | — | `retcode=200`，`不支持的Api ...` | — |

`get_msg` 的消息对象（节选）：

```json
{
  "self_id": 3964200143,
  "user_id": 3964200143,
  "time": 1792662720,
  "message_id": 1045085215,
  "message_type": "private",
  "sender": {"user_id": 3964200143, "nickname": "99", "card": ""},
  "raw_message": "NapCat 接口自检 17:52:00（来自 Python 客户端）",
  "message": [{"type": "text", "data": {"text": "NapCat 接口自检 17:52:00（来自 Python 客户端）"}}]
}
```

> 群相关接口（`send_group_msg`、`get_group_member_list` 等）本账号暂无群可测，接口存在但未实测。

---

## 6. 常用接口速查

**账号 / 系统**

| action | 说明 |
|---|---|
| `get_login_info` | 当前登录号 |
| `get_status` / `get_version_info` | 运行状态 / 版本 |
| `get_stranger_info` | 陌生人资料 |
| `get_friend_list` | 好友列表 |
| `can_send_image` / `can_send_record` | 能力检查 |
| `clean_cache` | 清理缓存 |

**消息**

| action | 主要参数 |
|---|---|
| `send_private_msg` | `user_id`, `message` |
| `send_group_msg` | `group_id`, `message` |
| `send_msg` | `message_type`(`private`/`group`), `user_id` 或 `group_id`, `message` |
| `get_msg` | `message_id` |
| `delete_msg` | `message_id` |
| `send_like` | `user_id`, `times`（注意频率） |
| `get_group_msg_history` | `group_id`, `message_seq`, `count` |
| `send_group_forward_msg` / `send_private_forward_msg` | 合并转发 |

**群管理**：`set_group_kick`、`set_group_ban`、`set_group_whole_ban`、`set_group_admin`、`set_group_card`、`set_group_name`、`set_group_leave`、`set_group_special_title`、`_:get_group_notice` 等。

**媒体 / 文件**：`get_image`、`get_record`、`upload_group_file`、`upload_private_file`、`get_group_file_url`、`download_file`。

**NapCat 扩展**：`send_poke`、`friend_poke`、`group_poke`、`set_msg_emoji_like`、`get_recent_contact`、`translate_en2zh`、`get_ai_record`。

完整列表见 [官方兼容表](https://napneko.github.io/develop/api) 与 [Apifox 用例](https://napcat.apifox.cn)。

---

## 7. 消息段格式

`message` 支持 **CQ 字符串** 或 **消息段数组**（推荐数组，避免转义问题）：

```json
[
  {"type": "at", "data": {"qq": "123456789"}},
  {"type": "text", "data": {"text": " 你好"}},
  {"type": "image", "data": {"file": "https://example.com/a.png"}},
  {"type": "reply", "data": {"id": "1045085215"}}
]
```

注意事项：

- `at` 的 `qq`、`reply` 的 `id`、`message_id` 在 JSON 里用**字符串**更稳妥；`user_id`/`group_id` 用数字。
- `@全体成员`：`{"type": "at", "data": {"qq": "all"}}`。
- 图片支持 URL、`base64://...`；本地路径需要 `enableLocalFile2Url: true`，否则发送失败。
- 事件里的 `message` 因 `messagePostFormat=array` 也是数组，可直接遍历 `type/data`。

---

## 8. Python 调用示例

项目内已封装：[qqbot/napcat_client.py](../qqbot/napcat_client.py)，仅用标准库：

```python
import os
from qqbot.napcat_client import NapCatClient, at, text

bot = NapCatClient(
    base_url=os.getenv("NAPCAT_URL", "http://127.0.0.1:3000"),
    token=os.getenv("NAPCAT_TOKEN", ""),
)

print(bot.get_login_info())
print(bot.get_group_list())

# 群消息：@某人 + 文本
bot.send_group_msg(123456789, [at(123456789), text(" 市场查询结果……")])

# 私聊
bot.send_private_msg(123456789, "hello")

# 任意接口
resp = bot.call("get_stranger_info", {"user_id": 123456789})
if not resp.ok:
    print(resp.retcode, resp.message)
```

原生写法（不依赖本项目封装）：

```python
import json
import urllib.request

req = urllib.request.Request(
    "http://127.0.0.1:3000/send_private_msg",
    data=json.dumps({"user_id": 123, "message": "hi"}).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer <token>",
    },
    method="POST",
)
with urllib.request.urlopen(req, timeout=10) as resp:
    print(json.loads(resp.read()))
```

---

## 9. 自检脚本

```powershell
# token 走环境变量（推荐）
$env:NAPCAT_TOKEN = "<token>"
.\.venv\Scripts\python.exe -m qqbot.check_napcat

# 或直接传参；--send-private 会真的发一条消息
.\.venv\Scripts\python.exe -m qqbot.check_napcat `
  --token "<token>" --send-private 3964200143

# 机器可读报告
.\.venv\Scripts\python.exe -m qqbot.check_napcat --token "<token>" --json
```

实测输出：

```text
目标: http://127.0.0.1:3000  token: 已设置

[ OK ] get_version_info   NapCat.Onebot v11 v4.18.41  (17.5 ms)
[ OK ] get_status         online=True good=True  (12.5 ms)
[ OK ] get_login_info     QQ 3964200143 (99)  (11.9 ms)
[ OK ] get_friend_list    2 条  (238.3 ms)
[ OK ] get_group_list     0 条  (4.3 ms)
[ OK ] get_stranger_info  {"uid": "...", "uin": "3964200143", ...}  (223.5 ms)

消息发送：
[ OK ] private:3964200143  message_id=1045085215

全部通过
```

- 退出码：`0` 全部通过，`1` 有失败项，`2` 连接/异常。
- 不传 `--send-private` / `--send-group` 时只做只读检查，并额外做两个非致命诊断（无 token 的 403、未知 action 的返回）。
- `get_stranger_info` 专门用来验证「参数是否真的传到了 handler」，能提前暴露平铺/包装格式错误。

---

## 10. 经验总结

1. **403 先查 token**：配置里 `httpServers[0].token` 非空时，任何请求都要带 `Authorization: Bearer` 或 `?access_token=`。
2. **参数平铺，不要套 `params`**：这是 4.x HTTP Server 与经典 OneBot v11 文档最大的差异。包装格式下无参接口正常、有参接口报 `TypeError` / `Schema compilation error`，极具迷惑性。
3. **`POST /` 只是健康检查**，返回 `NapCat4 Is Running`；动作必须走 `/{action}`。
4. **HTTP 200 ≠ 成功**：必须检查 `status` 和 `retcode`；`retcode=200` 既可能是「API 不存在」，也可能是 NapCat 内部异常。
5. **保留 `echo`**：并发调用时用自增 ID 匹配响应；本项目的封装会自动带上 `py-N`。
6. **自测优先发给自己**：用 `send_private_msg` 发给登录号本身，验证成功后立刻 `delete_msg` 撤回，避免打扰好友。
7. **先跑自检再写业务**：NapCat/QQ 升级后接口行为可能变化（本机 4.18.41 已与旧文档不同），升级后先跑 `qqbot.check_napcat`。
8. **事件与调用是两套配置**：`httpServers` 只收动作；要接收消息事件，需配置 `httpClients`（POST 上报到你的服务）或 `websocketServers`/`websocketClients`。
9. **token 不要写进代码/文档**：用 `NAPCAT_TOKEN` 环境变量；token 泄露等于任何人可操作该 QQ 号。
10. **发送限速**：`send_like`、群发、图片上传都有频率/大小限制；批量任务要加间隔与重试，失败时记录 `retcode` 与 `message`。
11. **数据要脱敏**：`get_friend_list` 会返回手机号、生日等隐私字段，落盘/日志前先裁剪。
12. **超时设置**：NapCat 默认 10s（`timeout.baseTimeout`），客户端超时建议略大于它；长任务（上传文件、OCR）单独调大。

---

## 11. 下一步：机器人开发建议

1. **事件接入**：在 `onebot11_<QQ>.json` 的 `network.httpClients` 增加 `{"url": "http://127.0.0.1:8080/onebot", "secret": "", "enable": true}`，用 FastAPI/aiohttp 接收 POST 事件；或使用正向/反向 WebSocket 长连接。
2. **消息路由**：按 `post_type`（`message`/`notice`/`request`/`meta_event`）、`message_type`（`private`/`group`）分发；忽略 `self_id == sender.user_id` 的自身消息，防止自触发。
3. **业务接入 TFD 市场**：复用 `market_v2` 的 `collector`/`filters`，命令示例：
   - `/market 怪物猎人 100-5000`
   - `/market 阴影的决意 Tier4 技能冷却时间`
   把 `products` 摘要成文本或图片回复，注意单条消息长度。
4. **配置与部署**：把 URL、token、白名单群号放环境变量或本地配置文件；NapCat 与本项目同机运行时保持 `127.0.0.1` 绑定，不要暴露到公网。

---

## 12. 参考

- NapCat 官方 API 兼容表：<https://napneko.github.io/develop/api>
- NapCat 全部接口用例：<https://napcat.apifox.cn>
- 本项目客户端：[qqbot/napcat_client.py](../qqbot/napcat_client.py)
- 本项目自检脚本：[qqbot/check_napcat.py](../qqbot/check_napcat.py)
