# 桌宠联动协议 — 插件 0.5.1 / 桌面端 0.8.1

本文件以配套插件的 `main.py` 为准，描述 HTTP、WebSocket、错误和持久化语义。协议版本保持 `1`。旧端点和主要返回字段保留，旧客户端可以忽略 0.5.1 新增的冷却同步字段；完整增补见第 9 节。

协议回归已通过真实 HTTP/WebSocket 和文件操作验证；测试中的 AstrBot、人格管理器、模型提供商使用替身。**真实 AstrBot 实例和真实 LLM 尚未联调，不应将这些测试描述为完整部署验证。**

## 1. 服务、鉴权与连接

默认根地址：`http://127.0.0.1:9898`；WebSocket：`ws://127.0.0.1:9898/ws`。

插件直接提供 HTTP/WS；HTTPS/WSS 由部署方的反向代理提供。使用路径前缀代理时，需将前缀去掉后转发至下列插件路径。

`auth_token` 非空时，所有 HTTP 端点和 WebSocket 握手都需要以下任一种凭证：

```http
X-Pet-Token: your-token
```

或：

```http
Authorization: Bearer your-token
```

兼容旧端的 `?token=your-token` 查询参数也受支持。桌面端 0.8.1 的网络请求由 Electron 主进程完成，使用请求头携带令牌。不要把令牌写进诊断文本或保存的分享 URL。

`OPTIONS` 预检无需令牌，返回 `204`。普通 HTTP 响应带 `Cache-Control: no-store` 和 CORS 头；允许的请求头包括 `Content-Type`、`X-Pet-Token`、`Authorization`。令牌为空时不要求鉴权；远程部署应配置令牌。

POST 请求使用 `Content-Type: application/json`，请求体必须是 JSON 对象。请求体上限为 64 KiB；数组、字符串、`null`、损坏 JSON 均被拒绝。无效输入不会部分应用。

## 2. HTTP 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 服务版本、协议版本、能力及请求超时建议 |
| GET | `/api/requests/{request_id}?kind=chat` | 查询原聊天或互动请求；kind 可为 chat / action |
| GET | `/api/state` | 当前养成状态、名字、版本及连接数 |
| GET | `/api/config` | 当前合并后行为及人格配置 |
| POST | `/api/config` | 校验、保存指定配置字段并广播 |
| GET | `/api/personas` | AstrBot 可选人格列表 |
| POST | `/api/chat` | 发送聊天并更新养成状态 |
| POST | `/api/action` | 执行互动并更新养成状态 |
| POST | `/api/generate-lines` | 生成台词；不会自动保存到台词池 |
| GET / Upgrade | `/ws` | 接收实时状态、说话和配置事件 |

### 健康检查

```json
{
  "ok": true,
  "plugin_version": "0.5.1",
  "protocol_version": 1,
  "clients": 1,
  "request_timeout_sec": 60,
  "capabilities": ["request_id", "request_status", "full_config", "state_revision", "sleep_wake", "interactions"]
}
```

`ok` 表示 HTTP 服务能够响应，不表示模型已配置或模型服务可用。旧插件可能没有此端点，兼容客户端可退回读取 `/api/state`。

### 请求恢复与超时

`health`、`hello` 和 GET `/api/config` 返回 `request_timeout_sec`；客户端据此调整等待时间，包含模型执行与广播余量，最大支持210秒。超时不会自动创建另一条聊天请求。

GET `/api/requests/{request_id}?kind=chat|action` 返回 `status: pending|completed|unknown`。完成时附 `result`（原HTTP成功结果）。查询本身不调用模型、不加经验。客户端用同一ID合并HTTP、WS和恢复结果的气泡，避免重复展示。

缓存仍只在插件内存中：重启、过期或逐出后为unknown，不能承诺跨重启去重。用户手动重试时使用原ID与原文；客户端切换服务器后清理旧待重试请求。

### 读取状态

```json
{
  "pet_name": "桌宠",
  "state": {
    "mood": 80,
    "satiety": 80,
    "cleanliness": 80,
    "energy": 80,
    "exp": 0,
    "level": 1
  },
  "plugin_version": "0.5.1",
  "protocol_version": 1,
  "revision": 0,
  "clients": 1
}
```

四项养成数值范围为 0–100，可带一位小数。`exp` 为当前等级内的经验，升级所需经验为 `level * 100`；等级最低为 1。

`revision` 在当前插件进程每次成功提交养成状态后递增。HTTP 响应与 WS 事件可能交错，客户端在同一连接期间应忽略低于已接收修订号的状态。**重连收到 `hello` 时必须重新建立修订号基线；插件重启后修订号重置为 0。**

### 聊天

请求：

```json
{"text":"今天也陪我工作吧。","request_id":"c0e7aabb-7e21-4db5-9552-chat-001"}
```

成功返回：

```json
{
  "reply": "好呀，我在这里陪你。",
  "state": {"mood":81,"satiety":80,"cleanliness":80,"energy":80,"exp":3,"level":1},
  "revision": 1,
  "request_id": "c0e7aabb-7e21-4db5-9552-chat-001"
}
```

`text` 必须是非空字符串；默认最多 4000 字，可由 `chat_max_chars` 调整。前后空白会去掉。成功会增加 3 经验和 1 心情，并向所有桌面端依次发出 `state`、`speak`。

所有客户端共享同一桌宠及同一聊天上下文。上一条聊天未完成时，新的不同聊天返回 `429 busy`。聊天没有无界等待队列，也不会自动换模型或在一次请求内重复调用模型。

### 互动

请求：

```json
{"action":"feed","request_id":"feed-001"}
```

返回包含 `reply`、`state`、`revision`、`request_id` 和 `fallback`：

```json
{
  "reply": "唔姆唔姆……好好吃！",
  "state": {"mood":85,"satiety":100,"cleanliness":80,"energy":80,"exp":5,"level":1},
  "revision": 1,
  "fallback": false,
  "request_id": "feed-001"
}
```

| action | 对应操作 | 状态变化 | 经验 |
|---|---|---|---|
| `feed` | 投喂 | 饱食 +25、心情 +5 | +5 |
| `clean` | 洗澡 | 清洁 +30、心情 +5 | +5 |
| `play` | 摸头 / 陪玩 | 心情 +20、精力 −10 | +8 |
| `sleep` | 睡觉 | 进入持续睡眠 | 0 |
| `wake` | 唤醒 | 结束睡眠 | 0 |
| `poke` | 戳一戳 | 心情 +3 | +2 |

状态会限幅到 0–100。睡觉进入持续状态：`is_sleeping: true`，`sleep_started_at` 为 Unix 秒时间。默认每分钟恢复6点精力，满100时自动醒来；`wake` 可手动唤醒。睡觉和唤醒均为0经验。只计算插件运行时间，不补算关机时长。相同动作默认冷却10秒，重启后剩余冷却仍有效；冷却中返回429 `action_cooldown`，不会重复加状态或经验。

启用 `llm_action_reply` 时，插件最多等待 `action_llm_timeout_sec` 生成台词。模型忙、超时、异常、人格不可用或返回空文本时，仍使用预置台词完成互动，返回 `200` 和 `fallback: true`。主动关闭模型互动回复时，使用预置台词但 `fallback: false`，因为没有发生生成失败。

保存状态失败则返回错误，不发出成功说话或状态广播。

### 读取和修改配置

GET 返回扁平对象，字段如下；未请求的字段仍会返回：

```json
{
  "enable_chatter": true,
  "chatter_lines": ["你在忙什么呢？"],
  "chatter_interval_sec": 90,
  "sleepy_lines": ["好困……"],
  "walk_speed": 1.5,
  "sleepy_threshold": 20,
  "enable_roam": true,
  "persona_source": "custom",
  "astrbot_persona_id": "",
  "persona": "你是一只住在用户电脑桌面上的可爱桌宠。",
  "pet_name": "桌宠",
  "llm_action_reply": true
}
```

POST 是部分字段更新：

```json
{"enable_chatter":false,"sleepy_threshold":0,"chatter_lines":["我在这里哦。"]}
```

成功后返回完整配置，并广播 `config`。`chatter_lines` 和 `sleepy_lines` 可提交字符串数组或按换行分隔的文本；响应统一使用字符串数组。

| 字段 | 校验规则 |
|---|---|
| `enable_chatter`、`enable_roam`、`llm_action_reply` | 必须为 JSON 布尔值，不接受 `"false"` 或 `0` |
| `chatter_interval_sec` | 整数，5–86400 |
| `walk_speed` | 有限数字，0.2–10 |
| `sleepy_threshold` | 整数，0–100 |
| 两种台词池 | 最多 100 个非空行，每行最多 500 字；允许空台词池 |
| `persona_source` | `custom`、`persona`、`default` 三选一 |
| `pet_name` | 去除前后空白后非空，最多 128 字 |
| `astrbot_persona_id` | 字符串，最多 128 字 |
| `persona` | 字符串，最多 20000 字 |

任何已知字段无效时，整次更新返回 `400`，其他有效字段也不会写入。未知字段为兼容目的被忽略。监听地址、令牌、模型 ID、超时等服务端配置不在回写白名单中，只能通过 AstrBot 插件配置修改。

写入覆盖文件成功后才替换内存配置并广播。磁盘失败返回 `503 storage_error`；GET 仍返回旧配置。桌面端覆盖项优先于 WebUI 中的同名项，并持续到覆盖文件被备份移走或相应键被人工移除后重启。

### 人格列表

```json
{"personas":[{"id":"assistant","name":"assistant"}]}
```

列表读取失败返回错误，不以空列表伪装成功。选择配置通过 `POST /api/config` 更新：

```json
{"persona_source":"persona","astrbot_persona_id":"assistant"}
```

保存配置时只校验字段格式；实际是否能找到该人格，在下一次聊天或生成台词时验证。

### 生成台词

请求：

```json
{"kind":"chatter","count":8}
```

`kind` 为 `chatter` 或 `sleepy`；默认 `chatter`。`count` 为 1–20 的整数，默认 8，不接受字符串数值或布尔值。

返回：

```json
{"kind":"chatter","lines":["工作辛苦啦。","我陪你休息一会儿。"]}
```

实际条数可少于请求条数，每条最多返回 500 字。此接口只生成，不自动更新配置，也不广播；客户端审阅后可另行 POST 对应台词池到 `/api/config`。

## 3. WebSocket 消息

服务器最多接收 32 个同时连接的桌面客户端。每个客户端入站帧限制 8 KiB。服务使用 30 秒底层心跳；客户端也可发送应用层 `ping`。

连接后服务先发出：

```json
{
  "type":"hello",
  "pet_name":"桌宠",
  "state":{"mood":80,"satiety":80,"cleanliness":80,"energy":80,"exp":0,"level":1},
  "behavior":{"enable_chatter":true,"chatter_lines":[],"chatter_interval_sec":90,"sleepy_lines":[],"walk_speed":1.5,"sleepy_threshold":20,"enable_roam":true},
  "config":{"enable_chatter":true,"chatter_lines":[],"chatter_interval_sec":90,"sleepy_lines":[],"walk_speed":1.5,"sleepy_threshold":20,"enable_roam":true,"persona_source":"custom","astrbot_persona_id":"","persona":"桌宠人格","pet_name":"桌宠","llm_action_reply":true},
  "plugin_version":"0.5.1",
  "protocol_version":1,
  "revision":0,
  "ts":1793318400.0
}
```

`ts` 是服务端 Unix 时间秒，仅作信息展示。示例台词和提示词已缩短，不代表实际默认内容。

其他消息：

```json
{"type":"state","data":{"mood":81,"satiety":80,"cleanliness":80,"energy":80,"exp":3,"level":1},"revision":1}
```

```json
{"type":"speak","text":"你好呀。","source":"chat","request_id":"chat-001"}
```

```json
{"type":"config","data":{"enable_chatter":false,"chatter_lines":[],"chatter_interval_sec":90,"sleepy_lines":[],"walk_speed":1.5,"sleepy_threshold":20,"enable_roam":true,"persona_source":"custom","astrbot_persona_id":"","persona":"桌宠人格","pet_name":"桌宠","llm_action_reply":true}}
```

```json
{"type":"ping"}
```

```json
{"type":"pong"}
```

`speak.source` 为 `chat`、互动动作名或 `bot`。`bot` 来自 AstrBot 平台回复转发；`/投喂` 指令的说话来源为 `feed`。这些服务端主动消息也带独立的 `request_id`。

入站 WS 只处理应用层 `ping`；聊天、互动和配置回写均使用 HTTP。未知消息和无法解析的文本帧被忽略。

广播为每个客户端并发发送，单次发送限时 2 秒；失败或过慢的连接被移出并限时关闭。因此单个慢端不会挡住其他端收消息，但 HTTP 操作的广播阶段可能增加少量等待。断线后客户端应重新建立连接，以新的 `hello` 为状态和配置基线。

## 4. request_id、去重与重试

`POST /api/chat` 和 `POST /api/action` 可携带 `request_id`，为 1–128 字的非空字符串。客户端应在**第一次发送之前**生成标识，并在重试时原样保留整个 JSON 请求内容。

成功响应和对应 WS `speak` 使用同一个标识。发起端可以先接收任一通道的回复，随后忽略同标识的重复说话；不要简单地同时把 HTTP `reply` 和 WS `speak` 都显示一次。

服务端去重键为 `(请求路径, request_id)`，内容比较忽略对象键顺序：

| 情况 | 行为 |
|---|---|
| 首次请求 | 执行一次，成功后缓存原始响应 |
| 相同 ID、相同内容、先前已成功 | 返回缓存的 `200` 响应，不再次调用模型、加经验或广播 |
| 相同 ID、相同内容、仍在处理中 | `409 request_pending`，不创建第二次执行 |
| 相同 ID、不同内容 | `409 request_conflict` |
| 先前失败 | 不缓存失败结果，允许用同一请求重试 |
| 未提供 ID | 服务端生成 ID 并回传；无法可靠地恢复最初响应已丢失的请求 |

缓存仅在内存中，成功条目最多存活 10 分钟；最多容纳 128 条请求记录，容量不足时提前逐出已完成记录。同时在处理的聊天/互动请求上限为 32，超出返回 `429`。**服务重启、记录过期或被逐出后，相同 ID 可以再次执行。** 这不是跨重启的“恰好一次”保证。

缓存响应包含当时的状态快照，可能比当前状态旧；客户端应使用 `revision` 或重新 GET `/api/state` 同步，不应将重放的旧状态覆盖更高修订号状态。

请求在模型已经调用后仍可能因为磁盘失败而返回错误。去重机制不会回滚模型供应商已完成的调用。客户端不应对所有失败无限自动重试；先展示错误原因，再决定是否重试。

## 5. 错误响应

常规错误使用 JSON：

```json
{"error":"上一条聊天还在回复中，请稍后再试。","code":"busy"}
```

中文说明可调整，客户端应按 HTTP 状态和 `code` 判断。服务端不把供应商异常正文、令牌或内部路径直接返回客户端。

| HTTP | code | 含义 |
|---|---|---|
| 400 | `invalid_request` | 无效 JSON 对象、字段类型、范围或动作 |
| 401 | `unauthorized` | 令牌未提供或不匹配；WS 握手也适用 |
| 409 | `request_pending` | 同标识请求仍在执行 |
| 409 | `request_conflict` | 同标识被用于不同内容 |
| 409 | `already_sleeping` / `already_awake` | 睡眠状态已符合请求，无需再次执行 |
| 429 | `action_cooldown` | 相同动作冷却中或同动作仍在执行 |
| 413 | `http_error` | 请求体超过限制 |
| 429 | `busy` | 聊天、模型并发、请求数或 WS 连接数达到上限 |
| 502 | `llm_failed` | 模型调用失败 |
| 502 | `llm_empty` | 模型未返回可用文本 |
| 502 | `persona_unavailable` | 人格列表读取失败 |
| 503 | `llm_unavailable` | 未配置可用聊天模型 |
| 503 | `persona_unavailable` | 所选聊天人格不可用 |
| 503 | `storage_error` | 状态或配置未能保存 |
| 503 | `shutting_down` | 服务正在关闭 |
| 504 | `llm_timeout` | 聊天或台词生成超过配置时限 |
| 500 | `internal_error` | 未预期内部错误，查看 AstrBot 日志 |
| 404 / 405 等 | `http_error` | 未知端点或方法等 HTTP 错误 |

互动的模型异常通常由预置台词回退吸收，返回 `200` 和 `fallback: true`；这不代表模型调用成功。WS 建立连接后的网络错误通过连接关闭处理，不能再返回 HTTP JSON 错误。

## 6. 人格、历史与保存边界

- `custom`：系统提示词使用自定义 `persona`；聊天用户文本前带“用户在桌面上对桌宠说”的语境前缀。
- `persona` / `default`：使用 AstrBot 人格原文和开场对话，忽略自定义 `persona`，不添加上述聊天前缀。人格不存在时不悄悄改用自定义人格。
- 开场对话支持交替字符串和成对的 `{"role":"user|assistant","content":"..."}`。只取完整用户—助手对，不将字典转换成字符串发给模型。
- 开场对话与真实聊天历史分开；每次调用保留开场，真实历史最多保留 `history_turns` 个完整问答轮次。
- 人格来源、选定人格、提示词、开场对话、名字或配置的专用模型 ID 改变后，下一次成功聊天建立新的历史。
- 聊天失败不追加用户孤立消息；状态保存失败时也恢复此次聊天前的历史。
- 状态和桌面端覆盖配置是磁盘存档；聊天历史、状态修订号、去重缓存、连接列表均不落盘；睡眠状态和动作冷却随养成状态落盘。
- 状态衰减只在插件服务运行期间计算；停止服务的离线时长不会在下次启动补扣。

## 7. 服务端配置

这些配置由 AstrBot WebUI 持有，不经 `/api/config` 暴露或回写：

| 配置 | 默认 | 用途 |
|---|---|---|
| `provider_id` | 空 | 专用聊天模型；空值使用当前模型 |
| `llm_timeout_sec` | 45 | 聊天和台词生成最长等待秒数 |
| `action_llm_timeout_sec` | 8 | 互动模型台词最长等待秒数 |
| `llm_max_concurrency` | 2 | 同时调用模型的上限 |
| `chat_max_chars` | 4000 | 输入聊天长度上限 |
| `history_turns` | 6 | 保留的真实聊天轮数 |
| `action_cooldown_sec` | 10 | 同动作冷却秒数；0关闭时间冷却，仍限制同动作并发 |
| `sleep_energy_per_min` | 6 | 持续睡眠每分钟恢复精力；范围0.1–60 |

## 8. 旧端兼容说明

- 旧版 HTTP 路径、聊天 `text`、互动 `action` 和已有返回字段继续可用。
- `hello.behavior` 保留；新客户端优先读取 `hello.config`，旧客户端可继续使用 `behavior` 并另行 GET `/api/config`。
- 新字段 `protocol_version`、`revision`、`request_id`、`fallback` 可被旧客户端忽略。
- 配置现在严格校验，过去被静默转换的 `"false"`、数字字符串、越界值会返回 `400`。
- 聊天失败现在返回错误状态，而不是以 `200` 包装失败台词；旧客户端应升级错误显示逻辑。
- 旧客户端不提供 `request_id` 时仍可聊天和互动，但不具备可靠的重试去重能力。
- 桌面端 0.8.1 是基于既有联动协议的重建版本；接口兼容说明不代表所有旧版桌面端都已完成实测。

<a id="interactions-051"></a>

## 9. 0.5.1 增补：互动冷却同步

**本节随插件 0.5.1 发行，配套桌面端为 0.8.1。** 协议版本仍为 `1`，旧接口保持兼容。客户端应检查 `capabilities` 中是否包含 `interactions`，并兼容字段缺失的旧端，而不是仅依据版本字符串判断能力。

### 冷却快照

服务端提供以下 `interactions` 对象：

```json
{
  "server_time": 1793400000.25,
  "revision": 12,
  "cooldowns": {
    "feed": 9.75,
    "clean": 0,
    "play": 0,
    "sleep": 0,
    "wake": 0,
    "poke": 0
  }
}
```

| 字段 | 含义 |
|---|---|
| `server_time` | 生成快照时的服务端 Unix 时间，单位为秒，可带小数 |
| `revision` | 快照对应的养成状态修订号，作用域为当前插件进程；重启后重置 |
| `cooldowns` | 各动作在生成快照时剩余的冷却秒数；非负数字，最多保留三位小数；`0` 表示没有剩余时间冷却 |

冷却按动作分别计算，并与养成数据一起保存。冷却快照只描述时间限制；`sleep` / `wake` 是否适用仍需结合 `state.is_sleeping`。同一动作仍在执行时也可能返回 `action_cooldown`，即使此前快照中的时间冷却为零。

### HTTP 与 hello 中的字段

| 位置 | 0.5.1 增补语义 |
|---|---|
| `GET /api/health` | `capabilities` 包含 `interactions`，用于能力检测 |
| `GET /api/state` | 顶层增加当前 `interactions`，并提供 `capabilities` |
| WS `hello` | 顶层增加当前 `interactions` 和 `capabilities`，用于首次连接或重连同步 |
| `POST /api/action` 成功响应 | 保留 `action` 动作名，并附该次成功提交后的 `interactions`；响应仍包含 `reply`、`state`、`revision`、`request_id` 等原字段 |
| `GET /api/requests/{request_id}?kind=chat\|action` | 无论 `status` 是 `pending`、`completed` 还是 `unknown`，外层 `interactions` 都是查询时的当前快照 |

成功请求的缓存内容保持历史语义。查询已完成请求时，`result` 是原始成功响应，其中的 `result.interactions` 如果存在，表示当时的冷却；查询响应外层的 `interactions` 表示现在的冷却。示例省略了缓存结果中的其他字段：

```json
{
  "status": "completed",
  "request_id": "feed-001",
  "interactions": {
    "server_time": 1793400060.25,
    "revision": 12,
    "cooldowns": {"feed":0,"clean":0,"play":0,"sleep":0,"wake":0,"poke":0}
  },
  "result": {
    "action": "feed",
    "reply": "唔姆唔姆……好好吃！",
    "request_id": "feed-001",
    "revision": 12,
    "interactions": {
      "server_time": 1793400000.25,
      "revision": 12,
      "cooldowns": {"feed":9.75,"clean":0,"play":0,"sleep":0,"wake":0,"poke":0}
    }
  }
}
```

客户端应先应用外层当前快照，再处理缓存结果；不能把缓存中的旧剩余秒数当成新冷却重新开始计时。重复 POST 同一成功请求仍返回原缓存结果，不额外发放收益，也不刷新缓存中的快照。请求缓存仍仅在进程内有效，重启后查询可能返回 `unknown`。

### WebSocket 广播与顺序

互动保存成功后，除原来的 `state` 和 `speak` 外，还向所有连接端广播：

```json
{
  "type": "interactions",
  "revision": 12,
  "data": {
    "server_time": 1793400000.25,
    "revision": 12,
    "cooldowns": {"feed":9.75,"clean":0,"play":0,"sleep":0,"wake":0,"poke":0}
  }
}
```

顶层 `revision` 与 `data.revision` 一致。写入失败不广播成功冷却，也不启动新的冷却。旧客户端可以忽略不认识的消息类型。

客户端应为冷却维护独立的接收基线：先比较冷却快照的 `revision`，相同修订号再比较 `server_time`，拒绝更旧或完全重复的快照。完全重复的 HTTP/WS 结果不应刷新本机倒计时起点。聊天和状态衰减也会提高养成状态修订号，因此不能仅因全局 `stateRevision` 更高，就丢弃尚未接收过的有效冷却快照。重连收到 `hello` 后，应重新建立基线，允许服务重启后的低修订号。

### 冷却错误

`429 action_cooldown` 保留原来的 `error` 和 `code`，额外提供建议等待秒数：

```json
{
  "error": "这个互动正在冷却，请约 8 秒后再试。",
  "code": "action_cooldown",
  "retry_after_sec": 8
}
```

`retry_after_sec` 至少为 1。它用于显示等待提示，不保证正在执行的动作会在该时间内完成，也不要求客户端自动重发。其他错误不必带此字段。

### 桌面端的本地接收时间

**`received_at_ms` 是桌面 core 添加的本机字段，不属于服务端协议。** 插件不会发送或保存此字段。桌面 core 接受一份新快照后，附上本机 `Date.now()`，单位为毫秒，用于本地倒计时，避免直接混用两台机器的时钟：

```js
const elapsed = Math.max(0, (Date.now() - interactions.received_at_ms) / 1000);
const remaining = Math.max(0, interactions.cooldowns[action] - elapsed);
```

桌面 `snapshot.interactions`、`interactions` 更新事件、`action()` / `getState()` 返回值中的 `interactions`，以及 `requestStatus()` 返回值外层的 `interactions`，均使用 core 已接收并过滤过的当前快照，带 `received_at_ms`。`requestStatus().result` 保持历史缓存原义；控制台恢复冷却时应读取外层 `interactions`，而不是直接应用 `result.interactions`。

服务端缺少该增补时，桌面端仍可发起原有互动并处理错误；不应仅根据未知冷却状态永久禁用动作。
