# AstrBot 虚拟桌宠插件 🐾

参考 Steam《虚拟桌宠模拟器》的 AstrBot 桌宠插件，配合 Live2D 桌面端使用。

## 功能

- 🖥️ 内置 HTTP + WebSocket 服务（默认 `127.0.0.1:9898`），供桌面端连接
- 💬 桌宠聊天由 AstrBot 配置的 LLM 驱动，人格（system prompt）可自定义
- 🍖 状态养成：心情 / 饱食 / 清洁 / 精力 / 经验等级，随时间衰减，支持投喂、抚摸、洗澡、睡觉、戳一戳
- 📢 可把 AstrBot 各平台的回复同步推送到桌宠气泡
- ⚙️ 行为配置（自言自语台词池/频率、走速、犯困阈值、自由走动）插件统一持有，桌面端连接后自动同步、可回写，多端实时一致
- 🌐 支持桌面端连接远程服务器上的插件（CORS + token 鉴权）

## 指令

| 指令 | 说明 |
|---|---|
| `/桌宠` | 查看桌宠状态与桌面端连接数 |
| `/投喂` | 在聊天里投喂桌宠 |

## 安装

1. 在 AstrBot WebUI 插件页选择「从 GitHub 安装」，填入本仓库地址；或把本仓库 clone 到 `AstrBot/data/plugins/` 下
2. 重启 AstrBot，在插件页启用并按需修改配置
3. 桌面端见：https://github.com/iownmmiku/astrbot_plugin_desktop_pet （README 中的 desktop 说明）

## 配置项（节选）

| 配置 | 默认 | 说明 |
|---|---|---|
| `ws_host` / `ws_port` | `127.0.0.1` / `9898` | 服务监听地址；远程连接时 host 改为 `0.0.0.0` |
| `auth_token` | 空 | 连接令牌，设置后桌面端需携带相同 token |
| `pet_name` / `persona` | 桌宠 / 内置 | 桌宠名字与人格设定 |
| `push_bot_reply` | 开 | 机器人回复同步到桌宠气泡 |
| `enable_chatter` 等 | — | 自言自语/走速/犯困等行为配置，可被桌面端同步修改 |

## 桌面端

桌面端（Electron + Live2D，支持任意 `.model3.json` 模型）源码见主项目 `desktop/` 目录。
