"""AstrBot 虚拟桌宠插件。

与 Live2D 桌面端（Electron）联动：
- 内置 HTTP + WebSocket 服务，桌面端连接后：
  - 在桌面端与桌宠聊天（由 AstrBot 配置的 LLM 驱动，人格可配置）
  - 同步 AstrBot 各平台的回复到桌宠气泡（可开关）
  - 桌宠状态（心情/饱食/清洁/精力/经验等级）保存于插件侧，支持投喂/抚摸/清洁/睡觉
"""

import asyncio
import json
import os
import time
from typing import Any

from aiohttp import WSMsgType, web

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

DEFAULT_STATE = {
    "mood": 80,        # 心情
    "satiety": 80,     # 饱食
    "cleanliness": 80, # 清洁
    "energy": 80,      # 精力
    "exp": 0,          # 经验
    "level": 1,
}

DECAY_PER_MIN = {  # 启用状态下降时，每分钟衰减
    "mood": 0.2,
    "satiety": 0.5,
    "cleanliness": 0.2,
    "energy": 0.3,
}

STATE_FILE = "pet_state.json"
CONFIG_FILE = "pet_behavior.json"

# 行为配置键（同步给桌面端，可被桌面端回写覆盖）
BEHAVIOR_KEYS = (
    "enable_chatter",
    "chatter_lines",
    "chatter_interval_sec",
    "sleepy_lines",
    "walk_speed",
    "sleepy_threshold",
    "enable_roam",
)

# 人格/对话配置键（允许桌面端通过 /api/config 回写覆盖）
PERSONA_KEYS = (
    "persona_source",
    "astrbot_persona_id",
    "persona",
    "pet_name",
    "llm_action_reply",
)

OVERRIDE_KEYS = BEHAVIOR_KEYS + PERSONA_KEYS

# 互动场景描述（用于 LLM 生成互动回复）
ACTION_SCENARIOS = {
    "feed": "主人刚刚给你喂了好吃的。",
    "clean": "主人刚刚给你洗了个澡，你现在干干净净、香喷喷的。",
    "play": "主人刚刚摸了摸你的头，陪你玩了一会儿。",
    "sleep": "主人催你去睡觉了。",
    "poke": "主人戳了戳你。",
}


def _split_lines(text: str) -> list[str]:
    return [ln.strip() for ln in str(text).splitlines() if ln.strip()]


def _data_dir() -> str:
    try:
        from astrbot.api.star import StarTools

        return str(StarTools.get_data_dir("astrbot_plugin_desktop_pet"))
    except Exception:
        return os.path.dirname(os.path.abspath(__file__))


@register(
    "astrbot_plugin_desktop_pet",
    "you",
    "虚拟桌宠：Live2D 桌面端联动，聊天/投喂/状态养成",
    "v0.3.0",
)
class DesktopPetPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.state: dict[str, Any] = dict(DEFAULT_STATE)
        self._clients: set[web.WebSocketResponse] = set()
        self._runner: web.AppRunner | None = None
        self._decay_task: asyncio.Task | None = None
        self._history: list[dict] = []  # 桌宠端对话上下文
        self._chat_lock = asyncio.Lock()
        self._behavior_overrides: dict = {}
        self._load_state()
        self._load_behavior_overrides()

    # ---------------- 行为配置（自动同步给桌面端） ----------------

    def get_behavior(self) -> dict:
        """合并 AstrBot 插件配置与桌面端回写的覆盖项，返回给桌面端的行为配置。"""
        cfg = {}
        for k in BEHAVIOR_KEYS:
            cfg[k] = self._behavior_overrides.get(k, self.config.get(k))
        cfg["chatter_lines"] = _split_lines(cfg.get("chatter_lines") or "")
        cfg["sleepy_lines"] = _split_lines(cfg.get("sleepy_lines") or "")
        # 使用显式 None 判断，避免用户将阈值/速度设置为 0 时被错误回退；异常值也不能拖垮 API
        try:
            cfg["chatter_interval_sec"] = max(5, int(90 if cfg.get("chatter_interval_sec") is None else cfg.get("chatter_interval_sec")))
        except (TypeError, ValueError):
            cfg["chatter_interval_sec"] = 90
        try:
            cfg["walk_speed"] = max(0.2, min(10.0, float(1.5 if cfg.get("walk_speed") is None else cfg.get("walk_speed"))))
        except (TypeError, ValueError):
            cfg["walk_speed"] = 1.5
        try:
            cfg["sleepy_threshold"] = max(0, min(100, int(20 if cfg.get("sleepy_threshold") is None else cfg.get("sleepy_threshold"))))
        except (TypeError, ValueError):
            cfg["sleepy_threshold"] = 20
        cfg["enable_chatter"] = bool(cfg.get("enable_chatter", True))
        cfg["enable_roam"] = bool(cfg.get("enable_roam", True))
        return cfg

    def _cfg(self, key: str, default=None):
        """读取配置：桌面端回写的覆盖项优先于 AstrBot 插件配置。"""
        if key in self._behavior_overrides:
            return self._behavior_overrides[key]
        return self.config.get(key, default)

    def _config_path(self) -> str:
        return os.path.join(_data_dir(), CONFIG_FILE)

    def _load_behavior_overrides(self):
        try:
            with open(self._config_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            self._behavior_overrides = {k: data[k] for k in OVERRIDE_KEYS if k in data}
        except Exception:
            pass

    def _save_behavior_overrides(self):
        try:
            os.makedirs(os.path.dirname(self._config_path()), exist_ok=True)
            with open(self._config_path(), "w", encoding="utf-8") as f:
                json.dump(self._behavior_overrides, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[桌宠] 行为配置保存失败: {e}")

    # ---------------- 生命周期 ----------------

    async def initialize(self):
        host = str(self.config.get("ws_host", "127.0.0.1"))
        port = int(self.config.get("ws_port", 9898))

        @web.middleware
        async def cors_mw(request, handler):
            # 允许桌面端从任意来源（含远程服务器部署时）访问 API
            if request.method == "OPTIONS":
                resp = web.Response()
            else:
                try:
                    resp = await handler(request)
                except web.HTTPException as e:
                    resp = e
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Pet-Token"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            return resp

        app = web.Application(middlewares=[cors_mw])
        app.router.add_get("/api/state", self._http_state)
        app.router.add_get("/api/config", self._http_get_config)
        app.router.add_post("/api/config", self._http_set_config)
        app.router.add_get("/api/personas", self._http_personas)
        app.router.add_post("/api/action", self._http_action)
        app.router.add_post("/api/chat", self._http_chat)
        app.router.add_get("/ws", self._http_ws)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        try:
            await site.start()
        except OSError as e:
            logger.error(f"[桌宠] 服务启动失败 {host}:{port} -> {e}")
            return
        logger.info(f"[桌宠] 服务已启动: http://{host}:{port} (WS: /ws)")

        if self.config.get("enable_state_decay", True):
            self._decay_task = asyncio.create_task(self._decay_loop())

    async def terminate(self):
        if self._decay_task:
            self._decay_task.cancel()
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass
        if self._runner:
            await self._runner.cleanup()
        self._save_state()

    # ---------------- 状态存取 ----------------

    def _state_path(self) -> str:
        return os.path.join(_data_dir(), STATE_FILE)

    def _load_state(self):
        try:
            with open(self._state_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            self.state.update({k: data.get(k, v) for k, v in DEFAULT_STATE.items()})
        except Exception:
            pass

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self._state_path()), exist_ok=True)
            with open(self._state_path(), "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[桌宠] 状态保存失败: {e}")

    def _clamp(self):
        for k in ("mood", "satiety", "cleanliness", "energy"):
            self.state[k] = max(0, min(100, round(self.state[k], 1)))
        # 升级
        need = self.state["level"] * 100
        while self.state["exp"] >= need:
            self.state["exp"] -= need
            self.state["level"] += 1
            need = self.state["level"] * 100

    def _add_exp(self, n: int):
        self.state["exp"] += n
        self._clamp()

    async def _decay_loop(self):
        while True:
            await asyncio.sleep(60)
            for k, v in DECAY_PER_MIN.items():
                self.state[k] -= v
            self._clamp()
            self._save_state()
            await self._broadcast({"type": "state", "data": self.state})

    # ---------------- HTTP / WS ----------------

    def _check_token(self, request: web.Request) -> bool:
        token = str(self.config.get("auth_token", "") or "")
        if not token:
            return True
        return (
            request.query.get("token") == token
            or request.headers.get("X-Pet-Token") == token
        )

    async def _http_state(self, request: web.Request):
        if not self._check_token(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(
            {"pet_name": self._cfg("pet_name", "桌宠"), "state": self.state}
        )

    async def _http_chat(self, request: web.Request):
        if not self._check_token(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        text = str(body.get("text", "")).strip()
        if not text:
            return web.json_response({"error": "empty"}, status=400)
        reply = await self._chat(text)
        self._add_exp(3)
        self.state["mood"] = min(100, self.state["mood"] + 1)
        self._save_state()
        await self._broadcast({"type": "speak", "text": reply, "source": "chat"})
        return web.json_response({"reply": reply, "state": self.state})

    async def _http_action(self, request: web.Request):
        """桌宠互动: action = feed/clean/play/sleep/poke"""
        if not self._check_token(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        action = str(body.get("action", ""))
        replies = {
            "feed": ("唔姆唔姆……好好吃！", {"satiety": 25, "mood": 5}, 5),
            "clean": ("洗香香了，舒服～", {"cleanliness": 30, "mood": 5}, 5),
            "play": ("嘿嘿，再陪我玩一会嘛！", {"mood": 20, "energy": -10}, 8),
            "sleep": ("呼……晚安。", {"energy": 40, "satiety": -5}, 3),
            "poke": ("呀！戳我干嘛啦！", {"mood": 3}, 2),
        }
        if action not in replies:
            return web.json_response({"error": "unknown action"}, status=400)
        canned, delta, exp = replies[action]
        text = canned
        # 开启后互动回复由 LLM 按当前人格生成（闲聊/吃饭等场景都有人格口吻）；失败回退到预置台词
        if bool(self._cfg("llm_action_reply", True)):
            try:
                generated = await self._gen_action_reply(action)
                if generated:
                    text = generated
            except Exception as e:
                logger.warning(f"[桌宠] 互动回复 LLM 生成失败，使用预置台词: {e}")
        for k, v in delta.items():
            self.state[k] += v
        self._add_exp(exp)
        self._save_state()
        payload = {"type": "state", "data": self.state}
        await self._broadcast(payload)
        await self._broadcast({"type": "speak", "text": text, "source": action})
        return web.json_response({"reply": text, "state": self.state})

    async def _gen_action_reply(self, action: str) -> str | None:
        """按当前人格为互动行为生成一句回复；无法生成时返回 None。"""
        scene = ACTION_SCENARIOS.get(action)
        if not scene:
            return None
        persona, _ = await self._resolve_persona()
        prompt = (
            f"{scene}请用你的人设口吻，对主人说一两句简短的话。"
            "只输出说的话本身，不要旁白、不要动作描写、不要引号。"
        )
        provider = self.context.get_using_provider()
        if provider is None:
            return None
        resp = await provider.text_chat(prompt=prompt, contexts=[], system_prompt=persona or "")
        reply = (resp.completion_text or "").strip().strip("“”\"'")
        return reply[:120] if reply else None

    def get_full_config(self) -> dict:
        """行为配置 + 人格/对话配置，供桌面端读取。"""
        cfg = self.get_behavior()
        cfg["persona_source"] = str(self._cfg("persona_source", "custom") or "custom")
        cfg["astrbot_persona_id"] = str(self._cfg("astrbot_persona_id", "") or "")
        cfg["persona"] = str(self._cfg("persona", "") or "")
        cfg["pet_name"] = str(self._cfg("pet_name", "桌宠") or "桌宠")
        cfg["llm_action_reply"] = bool(self._cfg("llm_action_reply", True))
        return cfg

    async def _http_get_config(self, request: web.Request):
        if not self._check_token(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(self.get_full_config())

    async def _http_personas(self, request: web.Request):
        """列出 AstrBot 已配置人格，供桌面端下拉选择。"""
        if not self._check_token(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        items: list[dict] = []
        try:
            mgr = self.context.persona_manager
            cands = getattr(mgr, "personas", None)
            if cands is None and hasattr(mgr, "get_all_personas"):
                cands = await self._maybe_await(mgr.get_all_personas())
            if isinstance(cands, dict):
                cands = list(cands.values())
            for p in cands or []:
                if isinstance(p, dict):
                    pid = p.get("persona_id") or p.get("id") or p.get("name")
                    name = p.get("name") or pid
                else:
                    pid = getattr(p, "persona_id", None) or getattr(p, "id", None) or getattr(p, "name", None)
                    name = getattr(p, "name", None) or pid
                if pid:
                    items.append({"id": str(pid), "name": str(name)})
        except Exception as e:
            logger.warning(f"[桌宠] 获取人格列表失败: {e}")
        return web.json_response({"personas": items})

    async def _http_set_config(self, request: web.Request):
        """桌面端回写行为配置；保存覆盖项并广播给所有客户端。"""
        if not self._check_token(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        if isinstance(body.get("chatter_lines"), list):
            body["chatter_lines"] = "\n".join(map(str, body["chatter_lines"]))
        if isinstance(body.get("sleepy_lines"), list):
            body["sleepy_lines"] = "\n".join(map(str, body["sleepy_lines"]))
        for k in OVERRIDE_KEYS:
            if k in body:
                self._behavior_overrides[k] = body[k]
        if "llm_action_reply" in self._behavior_overrides:
            self._behavior_overrides["llm_action_reply"] = bool(self._behavior_overrides["llm_action_reply"])
        self._save_behavior_overrides()
        cfg = self.get_full_config()
        await self._broadcast({"type": "config", "data": cfg})
        return web.json_response(cfg)

    async def _http_ws(self, request: web.Request):
        if not self._check_token(request):
            return web.Response(status=401)
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._clients.add(ws)
        logger.info(f"[桌宠] 桌面端已连接（{len(self._clients)} 个客户端）")
        await ws.send_json(
            {
                "type": "hello",
                "pet_name": self._cfg("pet_name", "桌宠"),
                "state": self.state,
                "behavior": self.get_behavior(),
                "ts": time.time(),
            }
        )
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception:
                        continue
                    if data.get("type") == "ping":
                        await ws.send_json({"type": "pong"})
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            self._clients.discard(ws)
            logger.info(f"[桌宠] 桌面端断开（剩 {len(self._clients)} 个客户端）")
        return ws

    async def _broadcast(self, payload: dict):
        for ws in list(self._clients):
            try:
                await ws.send_json(payload)
            except Exception:
                self._clients.discard(ws)

    # ---------------- LLM ----------------

    @staticmethod
    async def _maybe_await(value):
        import inspect

        if inspect.isawaitable(value):
            return await value
        return value

    async def _resolve_persona(self) -> tuple[str, list[str]]:
        """根据 persona_source 解析 system_prompt 与开场对话。

        返回 (system_prompt, begin_dialogs)。失败时回退到插件自定义人格。
        """
        source = str(self._cfg("persona_source", "custom"))
        fallback = str(self._cfg("persona", "")).strip()
        try:
            mgr = self.context.persona_manager
            if source == "persona":
                pid = str(self._cfg("astrbot_persona_id", "") or "").strip()
                if pid:
                    p = await self._maybe_await(mgr.get_persona(pid))
                    prompt = getattr(p, "system_prompt", "") or (p.get("system_prompt", "") if isinstance(p, dict) else "")
                    if p and prompt:
                        logger.info(f"[桌宠] 使用 AstrBot 人格: {pid}")
                        begin = getattr(p, "begin_dialogs", None) or (p.get("begin_dialogs") if isinstance(p, dict) else []) or []
                        return prompt, list(begin)
                    logger.warning(f"[桌宠] 未找到 AstrBot 人格 {pid}，回退到插件自定义人格")
            elif source == "default":
                d = await self._maybe_await(mgr.get_default_persona_v3(None))
                prompt = getattr(d, "prompt", "") or (d.get("prompt") if isinstance(d, dict) else "")
                if prompt:
                    logger.info("[桌宠] 使用 AstrBot 默认人格")
                    begin = getattr(d, "begin_dialogs", None) or (d.get("begin_dialogs") if isinstance(d, dict) else []) or []
                    return prompt, list(begin)
        except Exception as e:
            logger.warning(f"[桌宠] 获取 AstrBot 人格失败，回退到插件自定义人格: {e}")
        return fallback, []

    async def _chat(self, text: str) -> str:
        # 串行化桌宠对话，避免多个请求同时改写同一段上下文
        async with self._chat_lock:
            return await self._chat_unlocked(text)

    async def _chat_unlocked(self, text: str) -> str:
        persona, begin_dialogs = await self._resolve_persona()
        source = str(self._cfg("persona_source", "custom"))
        pet_name = self._cfg("pet_name", "桌宠")
        if source == "custom":
            # 仅插件自定义人格时加桌宠语境包装；选用 AstrBot 已有/默认人格时
            # 直接使用原文，不叠加其他设定，完全按所选人格对话
            prompt = f"（用户在桌面上对桌宠“{pet_name}”说）{text}"
        else:
            prompt = text
        try:
            provider = self.context.get_using_provider()
            if provider is not None:
                # 人格变更时清空上下文，并注入人格的开场对话
                persona_key = f"{self._cfg('persona_source')}:{self._cfg('astrbot_persona_id')}:{hash(persona)}"
                if getattr(self, "_persona_key", None) != persona_key:
                    self._persona_key = persona_key
                    self._history = []
                    for i in range(0, len(begin_dialogs) - 1, 2):
                        self._history.append({"role": "user", "content": str(begin_dialogs[i])})
                        self._history.append({"role": "assistant", "content": str(begin_dialogs[i + 1])})
                self._history.append({"role": "user", "content": prompt})
                self._history = self._history[-12:]
                resp = await provider.text_chat(
                    prompt=prompt,
                    contexts=self._history[:-1],
                    system_prompt=persona,
                )
                reply = (resp.completion_text or "").strip()
                if reply:
                    self._history.append({"role": "assistant", "content": reply})
                    return reply
        except Exception as e:
            logger.warning(f"[桌宠] text_chat 失败，尝试 llm_generate: {e}")
        try:
            provider_id = await self.context.get_current_chat_provider_id()
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id, prompt=f"{persona}\n\n{prompt}"
            )
            return (resp.completion_text or "……").strip()
        except Exception as e:
            logger.error(f"[桌宠] LLM 调用失败: {e}")
            return "呜……我现在有点说不出话（LLM 调用失败了）。"

    # ---------------- AstrBot 事件 ----------------

    @filter.on_decorating_result(priority=100)
    async def _forward_reply(self, event: AstrMessageEvent):
        """把机器人回复同步推给桌宠气泡。"""
        if not self.config.get("push_bot_reply", True) or not self._clients:
            return
        result = event.get_result()
        if result is None:
            return
        try:
            text = (result.get_plain_text() or "").strip()
        except Exception:
            chain = getattr(result, "chain", None) or []
            parts = [getattr(c, "text", "") for c in chain]
            text = "".join(parts).strip()
        if text:
            await self._broadcast({"type": "speak", "text": text[:200], "source": "bot"})

    @filter.command("桌宠")
    async def pet_status(self, event: AstrMessageEvent):
        """查看桌宠状态"""
        s = self.state
        name = self._cfg("pet_name", "桌宠")
        connected = len(self._clients)
        yield event.plain_result(
            f"🐾 {name} Lv.{s['level']}（经验 {s['exp']}/{s['level'] * 100}）\n"
            f"心情 {s['mood']}｜饱食 {s['satiety']}｜清洁 {s['cleanliness']}｜精力 {s['energy']}\n"
            f"桌面端连接：{connected} 个"
        )

    @filter.command("投喂")
    async def feed(self, event: AstrMessageEvent):
        """在聊天里投喂桌宠"""
        self.state["satiety"] += 25
        self.state["mood"] += 5
        self._add_exp(5)
        self._save_state()
        await self._broadcast({"type": "state", "data": self.state})
        await self._broadcast({"type": "speak", "text": "唔姆唔姆……好好吃！", "source": "feed"})
        yield event.plain_result("🍖 投喂成功！桌宠很开心。")
