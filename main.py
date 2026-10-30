"""AstrBot 桌宠服务。兼容 0.3.x 桌面端的 HTTP / WebSocket 协议。"""

import asyncio
import contextlib
import copy
import hmac
import inspect
import json
import math
import os
import shutil
import tempfile
import time
import uuid
from typing import Any

from aiohttp import WSMsgType, web

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_VERSION = "0.5.0"
PROTOCOL_VERSION = 1
STATE_FILE = "pet_state.json"
CONFIG_FILE = "pet_behavior.json"
DEFAULT_STATE = {"mood": 80, "satiety": 80, "cleanliness": 80, "energy": 80, "exp": 0, "level": 1}
SLEEP_DEFAULTS = {"is_sleeping": False, "sleep_started_at": None}
DECAY_PER_MIN = {"mood": 0.2, "satiety": 0.5, "cleanliness": 0.2, "energy": 0.3}
DEFAULT_CONFIG = {
    "pet_name": "桌宠", "persona_source": "custom", "astrbot_persona_id": "",
    "persona": "你是一只住在用户电脑桌面上的可爱桌宠。用简短、活泼、口语化的语气说话，每次回复不超过两句话，可以撒娇、吐槽、卖萌。不要暴露你是 AI 模型。",
    "llm_action_reply": True, "enable_chatter": True, "enable_roam": True,
    "chatter_interval_sec": 90, "walk_speed": 1.5, "sleepy_threshold": 20,
    "chatter_lines": "好无聊呀……\n你在忙什么呢？\n今天天气不错呢。\n想吃点心了……\n嘿嘿，我在这儿哦。\n别一直盯着我看啦。\n要不要陪我玩一会？",
    "sleepy_lines": "好困……\n呼啊……想睡觉了。\n眼睛睁不开了……",
}
BEHAVIOR_KEYS = ("enable_chatter", "chatter_lines", "chatter_interval_sec", "sleepy_lines", "walk_speed", "sleepy_threshold", "enable_roam")
PERSONA_KEYS = ("persona_source", "astrbot_persona_id", "persona", "pet_name", "llm_action_reply")
OVERRIDE_KEYS = BEHAVIOR_KEYS + PERSONA_KEYS
ACTION_SCENARIOS = {
    "feed": "主人刚刚给你喂了好吃的。", "clean": "主人刚刚给你洗了个澡，你现在干干净净、香喷喷的。",
    "play": "主人刚刚摸了摸你的头，陪你玩了一会儿。", "sleep": "主人催你去睡觉了。", "wake": "主人轻轻叫醒了你。", "poke": "主人戳了戳你。",
}
ACTION_RESULTS = {
    "feed": ("唔姆唔姆……好好吃！", {"satiety": 25, "mood": 5}, 5),
    "clean": ("洗香香了，舒服～", {"cleanliness": 30, "mood": 5}, 5),
    "play": ("嘿嘿，再陪我玩一会嘛！", {"mood": 20, "energy": -10}, 8),
    "sleep": ("呼……晚安，睡醒再陪你。", {}, 0),
    "wake": ("醒来啦！又可以陪你了。", {}, 0),
    "poke": ("呀！戳我干嘛啦！", {"mood": 3}, 2),
}


class PetError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def _split_lines(value: str | list[str]) -> list[str]:
    lines = value if isinstance(value, list) else str(value).splitlines()
    return [line.strip() for line in lines if isinstance(line, str) and line.strip()]


def _data_dir() -> str:
    try:
        from astrbot.api.star import StarTools
        return str(StarTools.get_data_dir("astrbot_plugin_desktop_pet"))
    except (ImportError, AttributeError):
        # 兼容旧版 SDK；数据不写入会被插件升级覆盖的源码目录。
        return os.path.join("data", "plugin_data", "astrbot_plugin_desktop_pet")


def _atomic_json(path: str, value: dict):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".pet-", suffix=".tmp", delete=False) as stream:
            temp_path = stream.name
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            with contextlib.suppress(OSError):
                os.unlink(temp_path)


def _field(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _number(value, default, low, high):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or (isinstance(value, float) and not math.isfinite(value)):
        return default
    return min(high, max(low, value))


def _validate_config(key: str, value):
    if key in ("enable_chatter", "enable_roam", "llm_action_reply"):
        if type(value) is not bool:
            raise ValueError(f"{key} 必须是布尔值")
    elif key in ("chatter_interval_sec", "sleepy_threshold", "walk_speed"):
        bounds = {"chatter_interval_sec": (5, 86400), "sleepy_threshold": (0, 100), "walk_speed": (0.2, 10)}
        low, high = bounds[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)):
            raise ValueError(f"{key} 必须是有限数字")
        if key != "walk_speed" and int(value) != value:
            raise ValueError(f"{key} 必须是整数")
        if not low <= value <= high:
            raise ValueError(f"{key} 范围为 {low}–{high}")
        value = float(value) if key == "walk_speed" else int(value)
    elif key in ("chatter_lines", "sleepy_lines"):
        if not isinstance(value, (str, list)) or (isinstance(value, list) and any(not isinstance(v, str) for v in value)):
            raise ValueError(f"{key} 必须是文本或字符串数组")
        lines = _split_lines(value)
        if len(lines) > 100 or any(len(line) > 500 for line in lines):
            raise ValueError(f"{key} 最多 100 行，每行最多 500 字")
        value = "\n".join(lines)
    else:
        if not isinstance(value, str):
            raise ValueError(f"{key} 必须是文本")
        limit = 20000 if key == "persona" else 128
        if len(value) > limit:
            raise ValueError(f"{key} 最多 {limit} 字")
        if key == "persona_source" and value not in ("custom", "persona", "default"):
            raise ValueError("persona_source 必须是 custom / persona / default")
        if key == "pet_name":
            value = value.strip()
            if not value:
                raise ValueError("pet_name 不能为空")
    return value


@register("astrbot_plugin_desktop_pet", "iownmmiku", "虚拟桌宠：Live2D 桌面端联动，聊天/投喂/状态养成", "v0.5.0")
class DesktopPetPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.state: dict[str, Any] = dict(DEFAULT_STATE)
        self._clients: set[web.WebSocketResponse] = set()
        self._runner: web.AppRunner | None = None
        self._decay_task: asyncio.Task | None = None
        self._history: list[dict] = []
        self._persona_key = None
        self._chat_lock = asyncio.Lock()
        self._active_llm = 0
        self._behavior_overrides: dict = {}
        self._requests: dict = {}
        self._state_revision = 0
        self._closing = False
        self._action_pending: set[str] = set()
        self._action_times: dict[str, float] = {}
        self._state_fractions = {key: 0.0 for key in DECAY_PER_MIN}
        self._load_state()
        self._load_behavior_overrides()

    def _cfg(self, key: str, default=None):
        value = self._behavior_overrides.get(key, self.config.get(key, DEFAULT_CONFIG.get(key, default)))
        if key in OVERRIDE_KEYS:
            try:
                return _validate_config(key, value)
            except ValueError:
                return DEFAULT_CONFIG[key]
        return value

    def _limit(self, key, default, low, high):
        return _number(self.config.get(key, default), default, low, high)

    def get_behavior(self) -> dict:
        cfg = {key: self._cfg(key) for key in BEHAVIOR_KEYS}
        for key in ("chatter_lines", "sleepy_lines"):
            cfg[key] = _split_lines(cfg[key])
        return cfg

    def get_full_config(self) -> dict:
        return {**self.get_behavior(), **{key: self._cfg(key) for key in PERSONA_KEYS},
                "request_timeout_sec": self._request_timeout()}

    def _request_timeout(self):
        return max(self._limit("llm_timeout_sec", 45, 0.01, 180),
                   self._limit("action_llm_timeout_sec", 8, 0.01, 60)) + 15

    def _config_path(self) -> str:
        return os.path.join(_data_dir(), CONFIG_FILE)

    def _state_path(self) -> str:
        return os.path.join(_data_dir(), STATE_FILE)

    def _load_json(self, path: str):
        # 老版本曾在插件目录存档；新数据目录为空时只读迁移，保留原文件。
        if not os.path.exists(path):
            legacy = os.path.join(os.path.dirname(__file__), os.path.basename(path))
            if os.path.isfile(legacy):
                path = legacy
        try:
            with open(path, encoding="utf-8") as stream:
                data = json.load(stream)
            if not isinstance(data, dict):
                raise ValueError("存档必须是对象")
            return data
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as exc:
            logger.warning(f"[桌宠] 存档读取失败，将使用默认值：{type(exc).__name__}")
            with contextlib.suppress(OSError):
                shutil.copy2(path, f"{path}.corrupt-{time.time_ns()}")
            return {}

    def _load_behavior_overrides(self):
        data = self._load_json(self._config_path())
        for key in OVERRIDE_KEYS:
            if key in data:
                try:
                    self._behavior_overrides[key] = _validate_config(key, data[key])
                except ValueError as exc:
                    logger.warning(f"[桌宠] 忽略无效的已保存配置：{exc}")

    def _save_behavior_overrides(self, data=None):
        _atomic_json(self._config_path(), self._behavior_overrides if data is None else data)

    def _load_state(self):
        data = self._load_json(self._state_path())
        for key, default in DEFAULT_STATE.items():
            high = 100 if key in DECAY_PER_MIN else (1000000 if key == "level" else 1000000000000)
            self.state[key] = _number(data.get(key), default, 1 if key == "level" else 0, high)
        self._clamp()
        self.state["is_sleeping"] = data.get("is_sleeping") is True
        started = data.get("sleep_started_at")
        self.state["sleep_started_at"] = _number(started, time.time(), 0, time.time()) if self.state["is_sleeping"] else None
        self._action_times = {key: _number(value, 0, 0, time.time()) for key, value in data.get("_action_times", {}).items()
                              if isinstance(key, str) and key in ACTION_RESULTS} if isinstance(data.get("_action_times"), dict) else {}

    def _save_state(self, data=None):
        value = dict(self.state if data is None else data)
        value["_action_times"] = dict(self._action_times)
        _atomic_json(self._state_path(), value)

    @staticmethod
    def _normalized_state(state: dict) -> dict:
        state = dict(state)
        for key in DECAY_PER_MIN:
            state[key] = round(max(0, min(100, state[key])), 1)
        level = max(1, int(state["level"]))
        exp = max(0, int(state["exp"]))
        # 用总经验求等级，避免损坏存档造成数十亿次循环。
        total = level * (level - 1) * 50 + exp
        new_level = (1 + math.isqrt(1 + total // 50 * 4)) // 2
        while new_level * (new_level + 1) * 50 <= total:
            new_level += 1
        state["level"] = max(1, new_level)
        state["exp"] = total - state["level"] * (state["level"] - 1) * 50
        return state

    def _clamp(self):
        self.state = self._normalized_state(self.state)

    def _commit_state(self, delta: dict, exp: int = 0, flags: dict | None = None):
        candidate = dict(self.state)
        for key, value in delta.items():
            candidate[key] += value
        candidate["exp"] += exp
        if flags:
            candidate.update(flags)
        candidate = self._normalized_state(candidate)
        try:
            self._save_state(candidate)
        except OSError as exc:
            logger.error(f"[桌宠] 状态保存失败：{type(exc).__name__}")
            raise PetError("storage_error", "状态保存失败，请检查磁盘空间和目录权限。", 503) from exc
        self.state = candidate
        self._state_revision += 1
        return dict(candidate)

    def _check_token(self, request: web.Request) -> bool:
        token = str(self.config.get("auth_token", "") or "")
        if not token:
            return True
        offered = [request.headers.get("X-Pet-Token", ""), request.query.get("token", "")]
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            offered.append(auth[7:])
        return any(hmac.compare_digest(value.encode(), token.encode()) for value in offered)

    def create_app(self):
        @web.middleware
        async def api_middleware(request, handler):
            try:
                if request.method == "OPTIONS":
                    response = web.Response(status=204)
                elif self._closing:
                    raise PetError("shutting_down", "桌宠服务正在关闭。", 503)
                elif not self._check_token(request):
                    raise PetError("unauthorized", "连接令牌不正确。", 401)
                else:
                    response = await handler(request)
            except PetError as exc:
                response = web.json_response({"error": str(exc), "code": exc.code}, status=exc.status)
            except web.HTTPException as exc:
                response = web.json_response({"error": exc.reason, "code": "http_error"}, status=exc.status)
            except Exception:
                logger.exception("[桌宠] 请求处理失败")
                response = web.json_response({"error": "服务内部错误，请查看 AstrBot 日志。", "code": "internal_error"}, status=500)
            if not response.prepared:
                response.headers.update({
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Headers": "Content-Type, X-Pet-Token, Authorization",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Cache-Control": "no-store",
                })
            return response

        app = web.Application(middlewares=[api_middleware], client_max_size=65536)
        app.router.add_get("/api/health", self._http_health)
        app.router.add_get("/api/state", self._http_state)
        app.router.add_get("/api/config", self._http_get_config)
        app.router.add_post("/api/config", self._http_set_config)
        app.router.add_get("/api/personas", self._http_personas)
        app.router.add_post("/api/action", self._http_action)
        app.router.add_post("/api/chat", self._http_chat)
        app.router.add_post("/api/generate-lines", self._http_generate_lines)
        app.router.add_get("/api/requests/{request_id}", self._http_request_status)
        app.router.add_get("/ws", self._http_ws)
        return app

    async def initialize(self):
        if self._runner is not None:
            return
        host = str(self.config.get("ws_host", "127.0.0.1"))
        port = int(self.config.get("ws_port", 9898))
        if not 0 <= port <= 65535:
            raise ValueError("ws_port 必须在 1–65535 之间（0 仅供测试动态分配端口）")
        self._closing = False
        runner = web.AppRunner(self.create_app(), access_log=None, shutdown_timeout=3)
        try:
            await runner.setup()
            await web.TCPSite(runner, host, port).start()
        except Exception:
            await runner.cleanup()
            logger.exception(f"[桌宠] 服务启动失败：{host}:{port}")
            raise
        self._runner = runner
        if host not in ("localhost", "127.0.0.1", "::1") and not self.config.get("auth_token"):
            logger.warning("[桌宠] 正在监听非本机地址且未设置 auth_token，请在远程使用前配置令牌。")
        logger.info(f"[桌宠] v{PLUGIN_VERSION} 服务已启动：http://{host}:{port}")
        # 睡眠恢复独立于衰减开关，养成计时始终运行。
        self._decay_task = asyncio.create_task(self._decay_loop(), name="desktop-pet-decay")

    async def terminate(self):
        self._closing = True
        if self._decay_task:
            self._decay_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._decay_task
            self._decay_task = None
        await asyncio.gather(*(self._close_ws(ws) for ws in list(self._clients)), return_exceptions=True)
        self._clients.clear()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._requests.clear()

    async def _decay_loop(self):
        previous = time.monotonic()
        while True:
            await asyncio.sleep(15)
            now = time.monotonic()
            minutes = (now - previous) / 60
            previous = now
            try:
                await self._tick_state(minutes)
            except PetError:
                # 磁盘暂时不可写时不让定时任务永久退出，也不广播未保存状态。
                continue

    @staticmethod
    async def _read_body(request):
        try:
            body = await request.json()
        except web.HTTPException:
            raise
        except (ValueError, UnicodeError):
            raise PetError("invalid_request", "请求必须是有效的 JSON 对象。") from None
        if not isinstance(body, dict):
            raise PetError("invalid_request", "请求必须是 JSON 对象。")
        return body

    async def _http_health(self, request):
        return web.json_response({"ok": True, "plugin_version": PLUGIN_VERSION, "protocol_version": PROTOCOL_VERSION,
                                  "clients": len(self._clients), "request_timeout_sec": self._request_timeout(),
                                  "capabilities": ["request_id", "request_status", "full_config", "state_revision", "sleep_wake"]})

    async def _http_state(self, request):
        return web.json_response({"pet_name": self._cfg("pet_name"), "state": self.state,
                                  "plugin_version": PLUGIN_VERSION, "protocol_version": PROTOCOL_VERSION,
                                  "revision": self._state_revision, "clients": len(self._clients)})

    async def _http_request_status(self, request):
        request_id = request.match_info["request_id"]
        kind = request.query.get("kind", "chat")
        if kind not in ("chat", "action") or not 1 <= len(request_id) <= 128:
            raise PetError("invalid_request", "无效的请求标识或 kind。")
        entry = self._requests.get(("/api/" + kind, request_id))
        if entry and (entry[2] is None or time.monotonic() - entry[1] <= 600):
            result = {"status": "pending" if entry[2] is None else "completed", "request_id": request_id}
            if entry[2] is not None:
                result["result"] = copy.deepcopy(entry[2])
            return web.json_response(result)
        return web.json_response({"status": "unknown", "request_id": request_id})

    async def _run_once(self, request, body, operation):
        request_id = body.get("request_id")
        if request_id is None:
            request_id = uuid.uuid4().hex
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise PetError("invalid_request", "request_id 必须是 1–128 字的字符串。")
        now = time.monotonic()
        for key, entry in list(self._requests.items()):
            if entry[2] is not None and now - entry[1] > 600:
                self._requests.pop(key, None)
        key = (request.path, request_id)
        signature = json.dumps(body, sort_keys=True, ensure_ascii=False)
        existing = self._requests.get(key)
        if existing:
            if existing[0] != signature:
                raise PetError("request_conflict", "该 request_id 已用于不同的请求。", 409)
            if existing[2] is None:
                raise PetError("request_pending", "相同请求仍在处理中，请稍后查询。", 409)
            return web.json_response(existing[2])
        if sum(entry[2] is None for entry in self._requests.values()) >= 32:
            raise PetError("busy", "请求较多，请稍后再试。", 429)
        while len(self._requests) >= 128:
            oldest = next((k for k, value in self._requests.items() if value[2] is not None), None)
            if oldest is None:
                break
            self._requests.pop(oldest)
        self._requests[key] = (signature, now, None)
        try:
            result = await operation(request_id)
            result["request_id"] = request_id
            self._requests[key] = (signature, time.monotonic(), copy.deepcopy(result))
            return web.json_response(result)
        except BaseException:
            self._requests.pop(key, None)
            raise

    async def _http_chat(self, request):
        body = await self._read_body(request)
        text = body.get("text")
        max_chars = int(self._limit("chat_max_chars", 4000, 1, 20000))
        if not isinstance(text, str) or not text.strip() or len(text) > max_chars:
            raise PetError("invalid_request", f"聊天内容必须是 1–{max_chars} 字的文本。")

        async def operation(request_id):
            old_history, old_key = copy.deepcopy(self._history), self._persona_key
            reply = await self._chat(text.strip())
            try:
                state = self._commit_state({"mood": 1}, 3)
            except PetError:
                self._history, self._persona_key = old_history, old_key
                raise
            revision = self._state_revision
            await self._broadcast({"type": "state", "data": state, "revision": revision})
            await self._broadcast({"type": "speak", "text": reply, "source": "chat", "request_id": request_id})
            return {"reply": reply, "state": state, "revision": revision}

        return await self._run_once(request, body, operation)

    async def _http_action(self, request):
        body = await self._read_body(request)
        action = body.get("action")
        if not isinstance(action, str) or action not in ACTION_RESULTS:
            raise PetError("invalid_request", "不支持的互动动作。")

        async def operation(request_id):
            return await self._perform_action(action, request_id)

        return await self._run_once(request, body, operation)

    async def _http_get_config(self, request):
        return web.json_response(self.get_full_config())

    async def _http_set_config(self, request):
        body = await self._read_body(request)
        candidate = dict(self._behavior_overrides)
        try:
            for key in OVERRIDE_KEYS:
                if key in body:
                    candidate[key] = _validate_config(key, body[key])
        except ValueError as exc:
            raise PetError("invalid_request", str(exc)) from None
        try:
            self._save_behavior_overrides(candidate)
        except OSError as exc:
            logger.error(f"[桌宠] 配置保存失败：{type(exc).__name__}")
            raise PetError("storage_error", "配置保存失败，请检查磁盘空间和目录权限。", 503) from exc
        self._behavior_overrides = candidate
        config = self.get_full_config()
        await self._broadcast({"type": "config", "data": config})
        return web.json_response(config)

    async def _http_personas(self, request):
        try:
            manager = self.context.persona_manager
            if hasattr(manager, "get_all_personas"):
                candidates = await self._maybe_await(manager.get_all_personas())
            else:
                candidates = getattr(manager, "personas", [])
            if isinstance(candidates, dict):
                candidates = candidates.values()
            items = []
            for persona in candidates or []:
                pid = _field(persona, "persona_id") or _field(persona, "id") or _field(persona, "name")
                if pid:
                    items.append({"id": str(pid), "name": str(_field(persona, "name") or pid)})
            return web.json_response({"personas": items})
        except Exception as exc:
            logger.warning(f"[桌宠] 读取人格列表失败：{type(exc).__name__}")
            raise PetError("persona_unavailable", "无法读取 AstrBot 人格列表，请查看日志。", 502) from exc

    async def _http_generate_lines(self, request):
        body = await self._read_body(request)
        kind, count = body.get("kind", "chatter"), body.get("count", 8)
        if not isinstance(kind, str) or kind not in ("chatter", "sleepy"):
            raise PetError("invalid_request", "kind 必须是 chatter 或 sleepy。")
        if type(count) is not int or not 1 <= count <= 20:
            raise PetError("invalid_request", "count 必须是 1–20 的整数。")

        async def generate():
            persona, begin = await self._resolve_persona()
            label = "自言自语" if kind == "chatter" else "犯困"
            reply = await self._complete(f"请根据当前人格生成{count}条桌宠{label}台词。每条一行，不要编号、引号或解释；每条简短自然。", persona, self._begin_messages(begin))
            return _split_lines(reply)[:count]

        try:
            lines = await asyncio.wait_for(generate(), timeout=self._limit("llm_timeout_sec", 45, 0.01, 180))
        except asyncio.TimeoutError:
            raise PetError("llm_timeout", "台词生成超时，请稍后再试。", 504) from None
        return web.json_response({"kind": kind, "lines": [line[:500] for line in lines]})

    async def _http_ws(self, request):
        if len(self._clients) >= 32:
            raise PetError("busy", "桌面端连接数已达到上限。", 429)
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=8192, receive_timeout=90)
        await ws.prepare(request)
        self._clients.add(ws)
        try:
            await asyncio.wait_for(ws.send_json({
                "type": "hello", "pet_name": self._cfg("pet_name"), "state": dict(self.state),
                "behavior": self.get_behavior(), "config": self.get_full_config(),
                "plugin_version": PLUGIN_VERSION, "protocol_version": PROTOCOL_VERSION,
                "revision": self._state_revision, "ts": time.time(), "request_timeout_sec": self._request_timeout(),
            }), timeout=2)
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(message.data)
                    except ValueError:
                        continue
                    if isinstance(data, dict) and data.get("type") == "ping":
                        await asyncio.wait_for(ws.send_json({"type": "pong"}), timeout=2)
                elif message.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            self._clients.discard(ws)
            await self._close_ws(ws)
        return ws

    async def _close_ws(self, ws):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(code=1001 if self._closing else 1000), timeout=1)

    async def _broadcast(self, payload: dict):
        payload = copy.deepcopy(payload)

        async def send(ws):
            try:
                await asyncio.wait_for(ws.send_json(payload), timeout=2)
            except Exception:
                self._clients.discard(ws)
                await self._close_ws(ws)

        await asyncio.gather(*(send(ws) for ws in list(self._clients)))

    @staticmethod
    async def _maybe_await(value):
        return await value if inspect.isawaitable(value) else value

    async def _resolve_persona(self) -> tuple[str, list]:
        source = self._cfg("persona_source")
        if source == "custom":
            return self._cfg("persona"), []
        try:
            manager = self.context.persona_manager
            if source == "persona":
                pid = self._cfg("astrbot_persona_id").strip()
                if not pid:
                    raise ValueError("未选择 AstrBot 人格")
                persona = await self._maybe_await(manager.get_persona(pid))
            else:
                persona = await self._maybe_await(manager.get_default_persona_v3(None))
            if persona is None:
                raise ValueError("人格不存在")
            prompt = _field(persona, "system_prompt")
            if prompt is None:
                prompt = _field(persona, "prompt", "")
            if not isinstance(prompt, str):
                raise ValueError("人格提示词无效")
            begin = _field(persona, "begin_dialogs", []) or []
            return prompt, begin if isinstance(begin, list) else []
        except Exception as exc:
            logger.warning(f"[桌宠] 读取所选人格失败：{type(exc).__name__}")
            # 用户明确选了 AstrBot 人格时，不能悄悄换成另一人格。
            raise PetError("persona_unavailable", "所选 AstrBot 人格不可用，请检查人格设置。", 503) from exc

    @staticmethod
    def _begin_messages(begin: list) -> list[dict]:
        messages = []
        for index in range(0, len(begin) - 1, 2):
            user, assistant = begin[index:index + 2]
            if isinstance(user, str) and isinstance(assistant, str):
                messages.extend([{"role": "user", "content": user}, {"role": "assistant", "content": assistant}])
            elif (isinstance(user, dict) and isinstance(assistant, dict) and user.get("role") == "user"
                  and assistant.get("role") == "assistant" and isinstance(user.get("content"), str)
                  and isinstance(assistant.get("content"), str)):
                messages.extend([{"role": "user", "content": user["content"]}, {"role": "assistant", "content": assistant["content"]}])
        return messages

    async def _complete(self, prompt, system_prompt, contexts):
        if self._active_llm >= int(self._limit("llm_max_concurrency", 2, 1, 8)):
            raise PetError("busy", "桌宠正在思考，请稍后再试。", 429)
        self._active_llm += 1
        try:
            provider_id = str(self.config.get("provider_id", "") or "").strip()
            if provider_id:
                response = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt,
                                                           contexts=copy.deepcopy(contexts), system_prompt=system_prompt)
            else:
                getter = getattr(self.context, "get_using_provider_async", None) or getattr(self.context, "get_using_provider", None)
                if getter:
                    provider = await self._maybe_await(getter())
                    if provider is None:
                        raise PetError("llm_unavailable", "AstrBot 尚未配置可用的聊天模型。", 503)
                    response = await provider.text_chat(prompt=prompt, contexts=copy.deepcopy(contexts), system_prompt=system_prompt)
                else:
                    # 新旧 SDK 都保留 system_prompt 和完整历史，不把人格拼进用户消息。
                    provider_id = await self.context.get_current_chat_provider_id(None)
                    response = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt,
                                                               contexts=copy.deepcopy(contexts), system_prompt=system_prompt)
            reply = _field(response, "completion_text", "")
            if not isinstance(reply, str) or not reply.strip():
                raise PetError("llm_empty", "模型未返回文本，请稍后再试。", 502)
            return reply.strip()[:20000]
        except (PetError, asyncio.CancelledError):
            raise
        except Exception as exc:
            # 不将含地址、密钥或供应商响应的异常正文返回桌面端。
            logger.warning(f"[桌宠] LLM 调用失败：{type(exc).__name__}")
            raise PetError("llm_failed", "模型调用失败，请查看 AstrBot 模型配置和日志。", 502) from exc
        finally:
            self._active_llm -= 1

    async def _chat(self, text: str) -> str:
        if self._chat_lock.locked():
            raise PetError("busy", "上一条聊天还在回复中，请稍后再试。", 429)
        async with self._chat_lock:
            try:
                return await asyncio.wait_for(self._chat_unlocked(text), timeout=self._limit("llm_timeout_sec", 45, 0.01, 180))
            except asyncio.TimeoutError:
                raise PetError("llm_timeout", "模型回复超时，请稍后再试。", 504) from None

    async def _chat_unlocked(self, text: str) -> str:
        persona, begin = await self._resolve_persona()
        key = (self._cfg("persona_source"), self._cfg("astrbot_persona_id"), persona,
               json.dumps(begin, ensure_ascii=False, sort_keys=True), self._cfg("pet_name"), self.config.get("provider_id", ""))
        history = self._history if key == self._persona_key else []
        prompt = f"（用户在桌面上对桌宠“{self._cfg('pet_name')}”说）{text}" if self._cfg("persona_source") == "custom" else text
        # 开场对话每次都保留；仅截断真实对话的完整轮次。
        reply = await self._complete(prompt, persona, self._begin_messages(begin) + history)
        turns = int(self._limit("history_turns", 6, 1, 30))
        self._history = (history + [{"role": "user", "content": prompt}, {"role": "assistant", "content": reply}])[-turns * 2:]
        self._persona_key = key
        return reply

    async def _gen_action_reply(self, action: str) -> str:
        persona, begin = await self._resolve_persona()
        prompt = ACTION_SCENARIOS[action] + "请用你的人设口吻对主人说一两句简短的话。只输出说的话，不要旁白、动作描写或引号。"
        reply = await self._complete(prompt, persona, self._begin_messages(begin))
        reply = reply.strip("“”\"'")[:120]
        if not reply:
            raise PetError("llm_empty", "模型未返回有效互动台词。", 502)
        return reply

    @filter.on_decorating_result(priority=100)
    async def _forward_reply(self, event: AstrMessageEvent):
        if not self.config.get("push_bot_reply", True) or not self._clients:
            return
        result = event.get_result()
        if result is None:
            return
        try:
            text = (result.get_plain_text() or "").strip()
        except Exception:
            text = "".join(getattr(component, "text", "") for component in (getattr(result, "chain", None) or [])).strip()
        if text:
            await self._broadcast({"type": "speak", "text": text[:200], "source": "bot", "request_id": uuid.uuid4().hex})

    @filter.command("桌宠")
    async def pet_status(self, event: AstrMessageEvent):
        state = self.state
        yield event.plain_result(
            f"🐾 {self._cfg('pet_name')} Lv.{state['level']}（经验 {state['exp']}/{state['level'] * 100}）\n"
            f"心情 {state['mood']}｜饱食 {state['satiety']}｜清洁 {state['cleanliness']}｜精力 {state['energy']}\n"
            f"桌面端连接：{len(self._clients)} 个")

    @filter.command("投喂")
    async def feed(self, event: AstrMessageEvent):
        try:
            await self._perform_action("feed", uuid.uuid4().hex, use_llm=False)
        except PetError as exc:
            yield event.plain_result(str(exc))
            return
        yield event.plain_result("🍖 投喂成功！桌宠很开心。")

    async def _tick_state(self, minutes):
        delta = {key: -value * minutes for key, value in DECAY_PER_MIN.items()} if self.config.get("enable_state_decay", True) else {}
        flags = None
        if self.state.get("is_sleeping"):
            delta["energy"] = self._limit("sleep_energy_per_min", 6, 0.1, 60) * minutes
            if self.state["energy"] + delta["energy"] >= 100:
                flags = {"is_sleeping": False, "sleep_started_at": None}
        if delta or flags:
            # 15秒计时下仍保留小数余量，避免每次取一位小数把轻微衰减全部抹掉。
            residuals = dict(self._state_fractions)
            for key, value in delta.items():
                amount = value + residuals.get(key, 0)
                delta[key] = round(amount, 1)
                residuals[key] = amount - delta[key]
            state = self._commit_state(delta, flags=flags)
            self._state_fractions = residuals
            await self._broadcast({"type": "state", "data": state, "revision": self._state_revision})

    async def _perform_action(self, action, request_id, use_llm=True):
        cooldown = self._limit("action_cooldown_sec", 10, 0, 3600)
        remaining = self._action_times.get(action, 0) + cooldown - time.time()
        if action in self._action_pending or remaining > 0:
            raise PetError("action_cooldown", f"这个互动正在冷却，请约 {max(1, math.ceil(remaining))} 秒后再试。", 429)
        if action == "sleep" and self.state.get("is_sleeping"):
            raise PetError("already_sleeping", "桌宠已经睡着了，可以点击唤醒。", 409)
        if action == "wake" and not self.state.get("is_sleeping"):
            raise PetError("already_awake", "桌宠已经醒着。", 409)
        self._action_pending.add(action)
        try:
            text, delta, exp = ACTION_RESULTS[action]
            fallback = False
            if use_llm and self._cfg("llm_action_reply"):
                try:
                    timeout = self._limit("action_llm_timeout_sec", 8, 0.01, 60)
                    text = await asyncio.wait_for(self._gen_action_reply(action), timeout=timeout)
                except (PetError, asyncio.TimeoutError):
                    fallback = True
                    text = ACTION_RESULTS[action][0]
            flags = None
            if action in ("sleep", "wake"):
                flags = {"is_sleeping": action == "sleep", "sleep_started_at": time.time() if action == "sleep" else None}
            previous_time = self._action_times.get(action)
            self._action_times[action] = time.time()
            try:
                state = self._commit_state(delta, exp, flags=flags)
            except PetError:
                if previous_time is None:
                    self._action_times.pop(action, None)
                else:
                    self._action_times[action] = previous_time
                raise
            revision = self._state_revision
            await self._broadcast({"type": "state", "data": state, "revision": revision})
            await self._broadcast({"type": "speak", "text": text, "source": action, "request_id": request_id})
            return {"reply": text, "state": state, "revision": revision, "fallback": fallback, "action": action}
        finally:
            self._action_pending.discard(action)
