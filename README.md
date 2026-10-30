# AstrBot 虚拟桌宠插件 0.5.0 🐾

为 Electron + Live2D 桌面端提供聊天、养成状态、人格配置和实时消息推送。此版基于用户提供的 0.3.3 源码重整，以连接稳定、失败可恢复、数据保存可靠为重点；配套重建桌面端版本为 0.8.0。

插件声明支持 AstrBot `>=4.17.0`；已核对官方 SDK 接口并完成自动化回归，**尚未在真实 AstrBot 实例和真实模型提供商上完成联调**。配套 [桌面端](https://github.com/iownmmiku/astrbot-desktop-pet) 和 [插件发行版](https://github.com/iownmmiku/astrbot_plugin_desktop_pet/releases) 分别提供源码与下载。

## 0.5.0 增量

增加请求状态查询与客户端超时协商；睡觉改为持续恢复，可用 `wake` 唤醒；相同互动默认冷却10秒并跨重启保留。睡觉与唤醒不获得经验。新增服务端配置 `action_cooldown_sec`（默认10，0关闭时间冷却）和 `sleep_energy_per_min`（默认6）。

本版未包含事件记忆系统，聊天历史仍为原有的进程内完整轮次上下文。

## 功能

- 默认在 `127.0.0.1:9898` 提供 HTTP API 和 WebSocket。
- 使用 AstrBot 当前聊天模型，或指定桌宠专用模型；支持自定义人格、指定 AstrBot 人格、AstrBot 默认人格。
- 保存心情、饱食、清洁、精力、经验和等级；支持投喂、摸头、洗澡、睡觉、戳一戳。
- 桌面端读取、修改行为配置后，插件保存并广播给所有连接端。
- 可将 AstrBot 各平台回复推送到所有已连接桌宠；由 `push_bot_reply` 控制。
- 聊天超时和模型失败返回明确错误；互动台词生成失败时，使用预置台词完成互动。
- 支持令牌鉴权、请求去重、健康检查、慢连接隔离和退出清理。

## 安装与首次连接

1. 在 AstrBot WebUI 插件页选择「从 GitHub 安装」，填入 `https://github.com/iownmmiku/astrbot_plugin_desktop_pet`。也可安装 [Release](https://github.com/iownmmiku/astrbot_plugin_desktop_pet/releases/latest) 中的插件 ZIP，或将本目录放到 `data/plugins/astrbot_plugin_desktop_pet/`，使 `main.py` 和 `metadata.yaml` 直接位于该目录内。升级前先保留原插件及其数据备份。
2. 插件依赖见 `requirements.txt`。若 AstrBot 未自动安装依赖，在 **AstrBot 使用的 Python 环境**中执行：

   ```sh
   python -m pip install -r data/plugins/astrbot_plugin_desktop_pet/requirements.txt
   ```

3. 重启 AstrBot，在插件页启用本插件；确认日志出现 `v0.5.0 服务已启动`。端口被占用时会明确启动失败。
4. 在 AstrBot 中配置可用的聊天模型。桌宠默认跟随 AstrBot 当前模型，也可设置 `provider_id`。
5. 下载并启动 [桌面端 0.8.0](https://github.com/iownmmiku/astrbot-desktop-pet/releases/tag/v0.8.0)，在连接设置填写 `127.0.0.1:9898`。若插件设置了 `auth_token`，两端填写同一令牌。
6. 测试连接、投喂，再发送一条聊天。先验证无需模型的状态和互动，再验证真实模型回复。

只安装插件不会创建桌面窗口；Live2D 渲染、窗口置顶、走动和气泡由桌面端实现。

### 远程服务器

插件配置 `ws_host: 0.0.0.0` 后，桌面端填写服务器的可达地址，并设置非空 `auth_token`。`0.0.0.0` 是监听地址，不能作为桌面端的目标地址。防火墙需允许所用端口；公网部署建议由反向代理提供 HTTPS/WSS。

插件自身提供明文 HTTP/WS，不内置 TLS。桌面端 0.8.0 在 Electron 主进程中携带令牌请求插件；旧桌面端的查询参数令牌仍兼容。

## 聊天指令

| 指令 | 功能 |
|---|---|
| `/桌宠` | 显示养成状态、等级与桌面端连接数 |
| `/投喂` | 保存投喂结果并同步桌宠，使用预置回复，不调用模型 |

## 配置

### 连接与模型

| 配置 | 默认 | 说明 |
|---|---|---|
| `ws_host` | `127.0.0.1` | 本机监听；远程使用时可设 `0.0.0.0` |
| `ws_port` | `9898` | 监听端口；需与桌面端一致 |
| `auth_token` | 空 | 配置后，HTTP 和 WebSocket 都必须通过鉴权 |
| `provider_id` | 空 | 自 0.4.0 起支持；留空跟随当前模型，也可在 WebUI 选择专用模型 |
| `llm_timeout_sec` | `45` | 自0.4.0起支持；聊天和台词生成超时秒数，建议设置范围 1–180 |
| `action_llm_timeout_sec` | `8` | 自0.4.0起支持；互动台词超时秒数，建议设置范围 1–60；超时使用预置台词 |
| `llm_max_concurrency` | `2` | 自0.4.0起支持；同时调用模型的最大数量，范围 1–8 |
| `chat_max_chars` | `4000` | 自0.4.0起支持；每次聊天允许的最大字数，范围 1–20000 |
| `history_turns` | `6` | 自0.4.0起支持；保留完整问答轮数，范围 1–30 |

聊天使用共享上下文，同一时刻只处理一条聊天；另一条并发聊天返回 `429`，不会无限排队。模型并发上限同时覆盖聊天、互动回复和台词生成。

### 人格与回复

| 配置 | 默认 | 说明 |
|---|---|---|
| `pet_name` | `桌宠` | 桌宠名字，1–128 字 |
| `persona_source` | `custom` | `custom` 自定义、`persona` 指定 AstrBot 人格、`default` AstrBot 默认人格 |
| `astrbot_persona_id` | 空 | 来源为 `persona` 时使用的人格 ID |
| `persona` | 内置桌宠人格 | 仅来源为 `custom` 时生效，最多 20000 字 |
| `llm_action_reply` | `true` | 互动是否尝试生成符合人格的台词 |
| `push_bot_reply` | `true` | 是否向所有已连接桌宠同步 AstrBot 平台回复 |

选择 AstrBot 人格时，直接使用其系统提示词和开场对话，不再追加自定义人格或桌宠聊天前缀。所选人格不可用时，聊天和台词生成返回错误；互动仍可回退到预置台词，并在结果中标记 `fallback: true`。

开场对话兼容交替字符串和 `role/content` 对象；只保留完整的用户—助手对。真实聊天历史按完整轮次截断，开场对话持续保留。成功切换人格后的下一次聊天从新历史开始；失败请求不会污染历史，也不会获得聊天经验。

### 养成与行为

| 配置 | 默认 | 说明 |
|---|---|---|
| `enable_state_decay` | `true` | 服务运行时按实际经过时间衰减养成状态 |
| `enable_chatter` | `true` | 桌面端空闲自言自语 |
| `chatter_interval_sec` | `90` | 自言自语最小间隔，5–86400 秒 |
| `chatter_lines` | 内置台词 | 每行一句；最多 100 行，每行 500 字 |
| `sleepy_lines` | 内置台词 | 犯困台词，格式和限制同上 |
| `walk_speed` | `1.5` | 桌面走动速度参数，0.2–10 |
| `sleepy_threshold` | `20` | 犯困精力阈值，0–100，允许设置为 0 |
| `enable_roam` | `true` | 桌面端是否允许自由走动 |

互动规则延续原版：投喂增加饱食，摸头增加心情但消耗精力，洗澡增加清洁，睡觉进入持续睡眠，插件运行期间逐渐恢复精力，精力满时自动醒来，也可手动唤醒。睡觉与唤醒不增加经验。相同动作默认冷却10秒，冷却保存在状态文件中。当前没有离线时长结算、物品背包或复杂经济系统。

## 数据和配置保存

- 使用 AstrBot `StarTools.get_data_dir("astrbot_plugin_desktop_pet")` 对应的数据目录，通常为 `data/plugin_data/astrbot_plugin_desktop_pet/`。
- `pet_state.json` 保存养成数值、睡眠状态与动作冷却时间；`pet_behavior.json` 保存桌面端回写的行为、名字和人格配置。
- 使用临时文件、刷新磁盘和原子替换，避免中途退出把旧文件截断。保存失败返回 `503 storage_error`，不更新内存中的对应状态或配置，也不广播“保存成功”。
- 无法解析的 JSON 会尽量保留为 `.corrupt-时间戳` 副本，随后以默认值启动；异常数值会规范化。新数据目录为空时，可从旧插件源码目录读取同名存档，不删除旧文件。
- 桌面端回写项优先于 AstrBot WebUI 中的同名配置。要恢复由 WebUI 控制，停止插件、备份 `pet_behavior.json`，再移走该覆盖文件并重启。只修改 WebUI 中已被覆盖的字段不会覆盖桌面端存档。
- 连接令牌、监听地址和模型配置不能通过桌面端 `/api/config` 改写。

**重启后保留养成数据和桌面端配置；聊天历史、状态修订号、请求去重缓存会清空。** 去重缓存仅对当前服务进程生效，最多保留 128 条成功请求、有效期 10 分钟。状态修订号不能跨重启比较。

## 协议与兼容性

保留原有 `/api/state`、`/api/config`、`/api/personas`、`/api/chat`、`/api/action`、`/api/generate-lines` 和 `/ws`。0.4.0 增加 `/api/health`、WebSocket `hello` 的完整 `config`、协议版本和状态修订号，并保留旧 `behavior` 字段；0.5.0 增加 `/api/requests/{request_id}` 请求状态查询及 `request_timeout_sec` 超时建议。

聊天和互动支持可选 `request_id`。它同时出现在 HTTP 响应和 WebSocket `speak` 中，桌面端可以去重显示，也可在当前服务进程内重试结果未知的请求。不得对 `request_id` 重用不同内容。

[PROTOCOL.md](docs/PROTOCOL.md) 记录端点、示例、错误码和重试限制。旧桌面端可继续使用旧字段；需要客户端实现新错误提示和请求去重，才能获得完整的 0.5.0 恢复能力。

## 验证与已知边界

在具有 `aiohttp` 的 Python 3.12 环境中运行：

```sh
python -m unittest discover -s tests -v
```

本次回归使用真实 HTTP/WebSocket 服务和临时目录，覆盖鉴权、坏输入、模型超时与失败、历史轮次、双端同步、请求去重、慢连接、原子保存、损坏存档、端口占用及关闭清理。AstrBot SDK 导入边界、人格管理器和模型提供商使用测试替身；这些结果不等同于真实 AstrBot 部署或真实模型联调通过。

接口依据已核对的 [AstrBot Context 源码](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/star/context.py) 和 [官方插件配置文档](https://github.com/AstrBotDevs/AstrBot/blob/master/docs/en/dev/star/guides/plugin-config.md) 适配。插件保留旧 SDK 的同步 provider 获取路径，并优先使用可用的异步接口。
