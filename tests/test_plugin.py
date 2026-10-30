"""Regression checks with real HTTP/WebSocket transport and an isolated AstrBot stub.

Run: python -m unittest discover -s tests -v
No running AstrBot, credentials, or paid model calls are required.
"""

import asyncio
import copy
import importlib.util
import json
import logging
import math
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import aiohttp


def load_plugin():
    """Stub only AstrBot's import boundary; aiohttp and the plugin are real."""
    modules = {name: types.ModuleType(name) for name in (
        "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star"
    )}
    modules["astrbot.api"].logger = logging.getLogger("desktop-pet-test")

    class Star:
        def __init__(self, context):
            self.context = context

    def decorator(*args, **kwargs):
        return lambda obj: obj

    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        on_decorating_result=decorator, command=decorator
    )
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].register = decorator
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "desktop_pet_under_test", root / "main.py", submodule_search_locations=[str(root)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


plugin_module = load_plugin()


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.delay = 0
        self.reply = "你好呀！"
        self.active = 0
        self.peak = 0

    async def text_chat(self, prompt, contexts=None, system_prompt="", **kwargs):
        self.calls.append({"prompt": prompt, "contexts": copy.deepcopy(contexts or []),
                           "system_prompt": system_prompt})
        self.active += 1
        self.peak = max(self.active, self.peak)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail:
                raise RuntimeError("simulated provider failure")
            return types.SimpleNamespace(completion_text=self.reply)
        finally:
            self.active -= 1


class FakePersonaManager:
    def __init__(self):
        self.personas = [{"persona_id": "test", "name": "测试人格",
                          "system_prompt": "只按原始人格说话。",
                          "begin_dialogs": [{"role": "user", "content": "开场问题"},
                                            {"role": "assistant", "content": "开场回答"}]}]

    async def get_persona(self, persona_id):
        return next((p for p in self.personas if p["persona_id"] == persona_id), None)

    def get_default_persona_v3(self, umo=None):
        p = self.personas[0]
        return {"prompt": p["system_prompt"], "begin_dialogs": p["begin_dialogs"]}


class FakeContext:
    def __init__(self):
        self.provider = FakeProvider()
        self.persona_manager = FakePersonaManager()

    def get_using_provider(self, umo=None):
        return self.provider

    async def get_using_provider_async(self, umo=None):
        return self.provider

    async def get_current_chat_provider_id(self, umo):
        if self.provider is None:
            raise RuntimeError("no provider")
        return "test-model"

    def get_provider_by_id(self, provider_id):
        return self.provider

    async def llm_generate(self, chat_provider_id, prompt, **kwargs):
        if self.provider is None:
            raise RuntimeError("no provider")
        return await self.provider.text_chat(prompt=prompt, **kwargs)


class PluginHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="desktop-pet-test-")
        self.data_dir = Path(self.temp.name)
        self.path_patch = patch.object(plugin_module, "_data_dir", return_value=self.temp.name)
        self.path_patch.start()
        self.context = FakeContext()
        self.config = {"ws_host": "127.0.0.1", "ws_port": 0,
                       "enable_state_decay": False, "auth_token": "test-token",
                       "llm_action_reply": False, "persona": "你是桌宠。", "action_cooldown_sec": 0}
        self.plugin = plugin_module.DesktopPetPlugin(self.context, self.config)
        await self.plugin.initialize()
        self.assertIsNotNone(self.plugin._runner)
        port = self.plugin._runner.addresses[0][1]
        self.base = f"http://127.0.0.1:{port}"
        self.client = aiohttp.ClientSession()
        self.headers = {"X-Pet-Token": "test-token"}
        self.sockets = []

    async def asyncTearDown(self):
        for ws in self.sockets:
            await ws.close()
        await self.client.close()
        await self.plugin.terminate()
        self.path_patch.stop()
        self.temp.cleanup()

    async def post(self, path, body):
        async with self.client.post(self.base + path, json=body, headers=self.headers) as response:
            return response.status, await response.json()

    async def get(self, path):
        async with self.client.get(self.base + path, headers=self.headers) as response:
            return response.status, await response.json()

    async def connect(self):
        ws = await self.client.ws_connect(self.base + "/ws", headers=self.headers)
        self.sockets.append(ws)
        hello = await ws.receive_json(timeout=2)
        self.assertEqual(hello["type"], "hello")
        return ws, hello

    async def until_type(self, ws, kind):
        async with asyncio.timeout(3):
            for _ in range(10):
                message = await ws.receive_json()
                if message.get("type") == kind:
                    return message
        self.fail(f"WebSocket did not receive {kind}")

    async def test_authentication_covers_http_and_websocket(self):
        for method, path in [("GET", "/api/state"), ("GET", "/api/config"),
                             ("GET", "/api/personas"), ("POST", "/api/chat"),
                             ("POST", "/api/action"), ("POST", "/api/config"),
                             ("POST", "/api/generate-lines")]:
            with self.subTest(method=method, path=path):
                async with self.client.request(method, self.base + path, json={}) as response:
                    self.assertEqual(response.status, 401)
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            await self.client.ws_connect(self.base + "/ws")
        self.assertEqual(caught.exception.status, 401)
        async with self.client.get(self.base + "/api/state?token=test-token") as response:
            self.assertEqual(response.status, 200)  # Existing clients use query tokens.

    async def test_non_object_json_is_rejected_for_all_mutations(self):
        for path in ("/api/chat", "/api/action", "/api/config", "/api/generate-lines"):
            for body in ([], None, "text", 1, True):
                with self.subTest(path=path, body=body):
                    status, result = await self.post(path, body)
                    self.assertEqual(status, 400)
                    self.assertIn("error", result)

    async def test_malformed_json_rejected(self):
        for path in ("/api/chat", "/api/action", "/api/config", "/api/generate-lines"):
            with self.subTest(path=path):
                async with self.client.post(self.base + path, data="{", headers={
                    **self.headers, "Content-Type": "application/json"
                }) as response:
                    self.assertEqual(response.status, 400)
                    self.assertIn("error", await response.json())

    async def test_chat_requires_nonempty_bounded_string(self):
        for value in (None, 123, {}, [], "", "   ", "x" * 20000):
            with self.subTest(value_type=type(value).__name__):
                status, _ = await self.post("/api/chat", {"text": value})
                self.assertEqual(status, 400)
        self.assertEqual(self.context.provider.calls, [])

    async def test_unknown_action_does_not_change_state(self):
        before = dict(self.plugin.state)
        status, _ = await self.post("/api/action", {"action": "erase"})
        self.assertEqual(status, 400)
        self.assertEqual(self.plugin.state, before)

    async def test_defaults_are_enabled_and_zero_threshold_round_trips(self):
        status, config = await self.get("/api/config")
        self.assertEqual(status, 200)
        self.assertIs(config["enable_roam"], True)
        self.assertIs(config["enable_chatter"], True)
        status, config = await self.post("/api/config", {"sleepy_threshold": 0, "enable_roam": False})
        self.assertEqual(status, 200)
        self.assertEqual(config["sleepy_threshold"], 0)
        self.assertIs(config["enable_roam"], False)

    async def test_invalid_configuration_is_atomic(self):
        _, before = await self.get("/api/config")
        for invalid in ({"enable_roam": "false"}, {"walk_speed": "fast"},
                        {"walk_speed": float("nan")}, {"walk_speed": float("inf")},
                        {"walk_speed": 10 ** 1000},
                        {"sleepy_threshold": -1}, {"persona_source": "unknown"},
                        {"chatter_lines": [1, {}]}, {"chatter_interval_sec": 0}):
            with self.subTest(invalid=invalid):
                status, _ = await self.post("/api/config", {"pet_name": "不应保存", **invalid})
                self.assertEqual(status, 400)
                _, after = await self.get("/api/config")
                self.assertEqual(after, before)

    async def test_config_update_broadcasts_and_survives_restart(self):
        ws1, _ = await self.connect()
        ws2, _ = await self.connect()
        status, config = await self.post("/api/config", {
            "pet_name": "小尾巴", "walk_speed": 2.5, "chatter_lines": ["早安", "晚安"]
        })
        self.assertEqual(status, 200)
        for ws in (ws1, ws2):
            message = await self.until_type(ws, "config")
            self.assertEqual(message["data"], config)
        fresh = plugin_module.DesktopPetPlugin(self.context, self.config)
        self.assertEqual(fresh.get_full_config(), config)

    async def test_failed_config_persistence_is_not_reported_as_success(self):
        _, before = await self.get("/api/config")
        # A directory is not replaceable by a JSON file on Windows or POSIX.
        with patch.object(self.plugin, "_config_path", return_value=str(self.data_dir)):
            status, result = await self.post("/api/config", {"pet_name": "不能保存"})
        self.assertGreaterEqual(status, 500)
        self.assertIn("error", result)
        _, after = await self.get("/api/config")
        self.assertEqual(after, before)

    async def test_failed_atomic_replace_preserves_previous_saved_configuration(self):
        await self.post("/api/config", {"pet_name": "已保存"})
        path = Path(self.plugin._config_path())
        original_bytes = path.read_bytes()
        with patch.object(plugin_module.os, "replace", side_effect=OSError("disk failure")):
            status, result = await self.post("/api/config", {"pet_name": "失败的更改"})
        self.assertEqual(status, 503)
        self.assertEqual(result["code"], "storage_error")
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(list(self.data_dir.glob(".pet-*.tmp")), [])
        _, config = await self.get("/api/config")
        self.assertEqual(config["pet_name"], "已保存")

    async def test_action_and_chat_synchronize_both_connected_clients(self):
        ws1, _ = await self.connect()
        ws2, _ = await self.connect()
        status, action = await self.post("/api/action", {"action": "feed"})
        self.assertEqual(status, 200)
        self.assertEqual(action["state"]["satiety"], 100)
        for ws in (ws1, ws2):
            state = await self.until_type(ws, "state")
            self.assertEqual(state["data"], action["state"])
            speech = await self.until_type(ws, "speak")
            self.assertEqual(speech["source"], "feed")
        status, chat = await self.post("/api/chat", {"text": "你好"})
        self.assertEqual(status, 200)
        for ws in (ws1, ws2):
            messages = [await ws.receive_json(timeout=3), await ws.receive_json(timeout=3)]
            by_type = {message["type"]: message for message in messages}
            self.assertEqual(by_type["state"]["data"], chat["state"])
            self.assertEqual(by_type["speak"]["text"], chat["reply"])

    async def test_invalid_websocket_messages_do_not_break_ping(self):
        ws, _ = await self.connect()
        for message in ("{", "[]", "null", "true", '"string"'):
            await ws.send_str(message)
        await ws.send_json({"type": "ping"})
        result = await self.until_type(ws, "pong")
        self.assertEqual(result["type"], "pong")

    async def test_model_failure_does_not_grant_experience_or_pollute_history(self):
        status, _ = await self.post("/api/chat", {"text": "成功的一轮"})
        self.assertEqual(status, 200)
        history = copy.deepcopy(self.plugin._history)
        state = dict(self.plugin.state)
        self.context.provider.fail = True
        calls_before = len(self.context.provider.calls)
        status, result = await self.post("/api/chat", {"text": "失败的一轮"})
        self.assertGreaterEqual(status, 500)
        self.assertIn("error", result)
        self.assertEqual(self.plugin._history, history)
        self.assertEqual(self.plugin.state, state)
        self.assertEqual(len(self.context.provider.calls), calls_before + 1)
        self.context.provider.fail = False
        status, _ = await self.post("/api/chat", {"text": "恢复的一轮"})
        self.assertEqual(status, 200)
        self.assertNotIn("失败的一轮", str(self.context.provider.calls[-1]["contexts"]))

    async def test_failed_chat_state_save_rolls_back_history_and_does_not_broadcast(self):
        await self.post("/api/chat", {"text": "已保存的聊天"})
        ws, _ = await self.connect()
        before_state = dict(self.plugin.state)
        before_history = copy.deepcopy(self.plugin._history)
        with patch.object(self.plugin, "_state_path", return_value=str(self.data_dir)):
            status, result = await self.post("/api/chat", {"text": "磁盘失败的聊天"})
        self.assertEqual(status, 503)
        self.assertEqual(result["code"], "storage_error")
        self.assertEqual(self.plugin.state, before_state)
        self.assertEqual(self.plugin._history, before_history)
        await ws.send_json({"type": "ping"})
        self.assertEqual((await ws.receive_json(timeout=2))["type"], "pong")

    async def test_selected_persona_preserves_structured_opening_dialogues(self):
        status, _ = await self.post("/api/config", {"persona_source": "persona", "astrbot_persona_id": "test"})
        self.assertEqual(status, 200)
        status, _ = await self.post("/api/chat", {"text": "原始问题"})
        self.assertEqual(status, 200)
        call = self.context.provider.calls[-1]
        self.assertEqual(call["system_prompt"], "只按原始人格说话。")
        self.assertEqual(call["prompt"], "原始问题")
        self.assertEqual(call["contexts"][:2], self.context.persona_manager.personas[0]["begin_dialogs"])

    async def test_default_persona_preserves_prompt_and_openings(self):
        status, _ = await self.post("/api/config", {"persona_source": "default"})
        self.assertEqual(status, 200)
        status, _ = await self.post("/api/chat", {"text": "默认人格问题"})
        self.assertEqual(status, 200)
        call = self.context.provider.calls[-1]
        self.assertEqual(call["system_prompt"], "只按原始人格说话。")
        self.assertEqual(call["prompt"], "默认人格问题")
        self.assertEqual(call["contexts"][:2], self.context.persona_manager.personas[0]["begin_dialogs"])

    async def test_unavailable_selected_persona_does_not_silently_fall_back(self):
        await self.post("/api/config", {"persona_source": "persona", "astrbot_persona_id": "does-not-exist"})
        status, result = await self.post("/api/chat", {"text": "你是谁"})
        self.assertEqual(status, 503)
        self.assertEqual(result["code"], "persona_unavailable")
        self.assertEqual(self.context.provider.calls, [])

    async def test_unavailable_default_persona_does_not_silently_fall_back(self):
        self.context.persona_manager.get_default_persona_v3 = lambda umo=None: None
        await self.post("/api/config", {"persona_source": "default"})
        status, result = await self.post("/api/chat", {"text": "你是谁"})
        self.assertEqual(status, 503)
        self.assertEqual(result["code"], "persona_unavailable")
        self.assertEqual(self.context.provider.calls, [])

    async def test_legacy_opening_dialogues_remain_compatible(self):
        self.context.persona_manager.personas[0]["begin_dialogs"] = ["旧问题", "旧回答"]
        status, _ = await self.post("/api/config", {"persona_source": "persona", "astrbot_persona_id": "test"})
        self.assertEqual(status, 200)
        status, _ = await self.post("/api/chat", {"text": "继续聊"})
        self.assertEqual(status, 200)
        call = self.context.provider.calls[-1]
        self.assertEqual(call["contexts"][:2], [
            {"role": "user", "content": "旧问题"}, {"role": "assistant", "content": "旧回答"}
        ])

    async def test_persona_change_clears_previous_history(self):
        await self.post("/api/chat", {"text": "旧人格的私聊"})
        await self.post("/api/config", {"persona_source": "persona", "astrbot_persona_id": "test"})
        status, _ = await self.post("/api/chat", {"text": "新人格的问题"})
        self.assertEqual(status, 200)
        self.assertNotIn("旧人格的私聊", str(self.context.provider.calls[-1]["contexts"]))

    async def test_modern_context_api_keeps_persona_and_conversation_separate(self):
        self.plugin.context = types.SimpleNamespace(
            persona_manager=self.context.persona_manager,
            get_current_chat_provider_id=self.context.get_current_chat_provider_id,
            llm_generate=self.context.llm_generate,
        )
        await self.post("/api/config", {"persona_source": "persona", "astrbot_persona_id": "test"})
        status, _ = await self.post("/api/chat", {"text": "新API问题"})
        self.assertEqual(status, 200)
        call = self.context.provider.calls[-1]
        self.assertEqual(call["system_prompt"], "只按原始人格说话。")
        self.assertEqual(call["prompt"], "新API问题")
        self.assertEqual(call["contexts"][:2], self.context.persona_manager.personas[0]["begin_dialogs"])

    async def test_long_conversation_keeps_complete_turns(self):
        for index in range(10):
            status, _ = await self.post("/api/chat", {"text": f"第{index}轮"})
            self.assertEqual(status, 200)
        for call in self.context.provider.calls:
            roles = [message["role"] for message in call["contexts"]]
            self.assertEqual(roles, ["user", "assistant"] * (len(roles) // 2))
        self.assertLessEqual(len(self.context.provider.calls[-1]["contexts"]), 24)

    async def test_concurrent_chats_are_bounded_without_duplicate_model_calls(self):
        self.context.provider.delay = 0.03
        results = await asyncio.gather(*(self.post("/api/chat", {"text": f"并发{i}"}) for i in range(3)))
        self.assertEqual(sorted(status for status, _ in results), [200, 429, 429])
        self.assertEqual(self.context.provider.peak, 1)
        self.assertEqual(len(self.plugin._history), 2)

    async def test_retrying_completed_action_does_not_repeat_reward(self):
        body = {"action": "feed", "request_id": "feed-once"}
        status, first = await self.post("/api/action", body)
        self.assertEqual(status, 200)
        state = dict(self.plugin.state)
        status, replay = await self.post("/api/action", body)
        self.assertEqual(status, 200)
        self.assertEqual(replay, first)
        self.assertEqual(replay["request_id"], "feed-once")
        self.assertEqual(self.plugin.state, state)

    async def test_retrying_completed_chat_does_not_repeat_model_or_broadcast(self):
        ws, _ = await self.connect()
        body = {"text": "只说一次", "request_id": "chat-once"}
        status, first = await self.post("/api/chat", body)
        self.assertEqual(status, 200)
        await self.until_type(ws, "state")
        speech = await self.until_type(ws, "speak")
        self.assertEqual(speech["request_id"], "chat-once")
        status, replay = await self.post("/api/chat", body)
        self.assertEqual(status, 200)
        self.assertEqual(replay, first)
        self.assertEqual(len(self.context.provider.calls), 1)
        await ws.send_json({"type": "ping"})
        self.assertEqual((await ws.receive_json(timeout=2))["type"], "pong")

    async def test_request_id_conflict_does_not_apply_different_action(self):
        await self.post("/api/action", {"action": "feed", "request_id": "same-id"})
        state = dict(self.plugin.state)
        status, result = await self.post("/api/action", {"action": "sleep", "request_id": "same-id"})
        self.assertEqual(status, 409)
        self.assertEqual(result["code"], "request_conflict")
        self.assertEqual(self.plugin.state, state)

    async def test_retrying_inflight_chat_returns_pending_without_second_model_call(self):
        self.context.provider.delay = 0.15
        body = {"text": "慢慢想", "request_id": "inflight"}
        first = asyncio.create_task(self.post("/api/chat", body))
        try:
            async with asyncio.timeout(1):
                while not self.context.provider.calls:
                    await asyncio.sleep(0.005)
            status, result = await self.post("/api/chat", body)
            self.assertEqual(status, 409)
            self.assertEqual(result["code"], "request_pending")
            self.assertEqual(len(self.context.provider.calls), 1)
            self.assertEqual((await first)[0], 200)
        finally:
            if not first.done():
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)

    async def test_slow_websocket_does_not_delay_healthy_client(self):
        class FakeSocket:
            def __init__(self, delay):
                self.delay = delay
                self.delivered = asyncio.Event()
                self.closed = False

            async def send_json(self, payload):
                await asyncio.sleep(self.delay)
                self.delivered.set()

            async def close(self, **kwargs):
                self.closed = True

        slow, fast = FakeSocket(30), FakeSocket(0)
        self.plugin._clients.update([slow, fast])
        task = asyncio.create_task(self.plugin._broadcast({"type": "state", "data": {}}))
        try:
            await asyncio.wait_for(fast.delivered.wait(), timeout=0.5)
            self.assertFalse(slow.delivered.is_set())
            await asyncio.wait_for(task, timeout=3)
            self.assertNotIn(slow, self.plugin._clients)
            self.assertTrue(slow.closed)
        finally:
            self.plugin._clients.discard(slow)
            self.plugin._clients.discard(fast)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_chat_timeout_returns_error_and_releases_provider_capacity(self):
        self.plugin.config["llm_timeout_sec"] = 0.05
        self.context.provider.delay = 30
        before = dict(self.plugin.state)
        status, result = await asyncio.wait_for(self.post("/api/chat", {"text": "会超时吗"}), timeout=2)
        self.assertEqual(status, 504)
        self.assertEqual(result["code"], "llm_timeout")
        self.assertEqual(self.plugin.state, before)
        self.assertEqual(self.plugin._history, [])
        self.assertEqual(self.context.provider.active, 0)
        self.context.provider.delay = 0
        status, _ = await self.post("/api/chat", {"text": "现在恢复了"})
        self.assertEqual(status, 200)

    async def test_action_timeout_uses_canned_reply_and_applies_action(self):
        self.plugin.config["llm_action_reply"] = True
        self.plugin.config["action_llm_timeout_sec"] = 0.05
        self.context.provider.delay = 30
        before_exp = self.plugin.state["exp"]
        status, result = await asyncio.wait_for(self.post("/api/action", {"action": "feed"}), timeout=2)
        self.assertEqual(status, 200)
        self.assertTrue(result["reply"])
        self.assertEqual(result["state"]["exp"], before_exp + 5)
        self.assertEqual(self.context.provider.active, 0)

    async def test_missing_provider_is_a_service_error_not_a_successful_chat(self):
        self.context.provider = None
        before = dict(self.plugin.state)
        status, result = await self.post("/api/chat", {"text": "还没配模型"})
        self.assertEqual(status, 503)
        self.assertIn("error", result)
        self.assertEqual(self.plugin.state, before)

    async def test_health_reports_ready_server_and_matching_version(self):
        status, health = await self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["plugin_version"], plugin_module.PLUGIN_VERSION)
        self.assertEqual(health["protocol_version"], 1)

    async def test_bind_failure_does_not_leave_runner_or_background_task(self):
        config = {**self.config, "ws_port": self.plugin._runner.addresses[0][1], "enable_state_decay": True}
        second = plugin_module.DesktopPetPlugin(self.context, config)
        with self.assertRaises(OSError):
            await second.initialize()
        self.assertIsNone(second._runner)
        self.assertIsNone(second._decay_task)
        await second.terminate()

    async def test_shutdown_cancels_decay_and_closes_websocket(self):
        self.plugin.config["enable_state_decay"] = True
        task = self.plugin._decay_task
        ws, _ = await self.connect()
        # aiohttp's client needs a receive loop to answer the server close frame.
        receive = asyncio.create_task(ws.receive())
        await asyncio.wait_for(self.plugin.terminate(), timeout=3)
        message = await asyncio.wait_for(receive, timeout=1)
        self.assertIn(message.type, (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED))
        self.assertTrue(task.done())
        self.assertEqual(len(self.plugin._clients), 0)


class PersistenceTests(unittest.TestCase):
    def test_huge_integers_in_corrupt_state_cannot_break_startup(self):
        with tempfile.TemporaryDirectory(prefix="desktop-pet-state-") as temp:
            path = Path(temp) / plugin_module.STATE_FILE
            path.write_text(json.dumps({key: 10 ** 1000 for key in plugin_module.DEFAULT_STATE}), encoding="utf-8")
            with patch.object(plugin_module, "_data_dir", return_value=temp):
                pet = plugin_module.DesktopPetPlugin(FakeContext(), {})
                for key in ("mood", "satiety", "cleanliness", "energy"):
                    self.assertGreaterEqual(pet.state[key], 0)
                    self.assertLessEqual(pet.state[key], 100)
                self.assertGreaterEqual(pet.state["level"], 1)
                self.assertGreaterEqual(pet.state["exp"], 0)
                self.assertLess(pet.state["exp"], pet.state["level"] * 100)

    def test_corrupt_state_is_recovered_to_finite_bounded_values(self):
        with tempfile.TemporaryDirectory(prefix="desktop-pet-state-") as temp:
            path = Path(temp) / plugin_module.STATE_FILE
            path.write_text(json.dumps({"mood": "bad", "satiety": -100, "cleanliness": 999,
                                        "energy": float("nan"), "exp": -99, "level": 0}), encoding="utf-8")
            with patch.object(plugin_module, "_data_dir", return_value=temp):
                pet = plugin_module.DesktopPetPlugin(FakeContext(), {})
                for key in ("mood", "satiety", "cleanliness", "energy"):
                    value = pet.state[key]
                    self.assertIsInstance(value, (int, float))
                    self.assertTrue(math.isfinite(value))
                    self.assertGreaterEqual(value, 0)
                    self.assertLessEqual(value, 100)
                self.assertGreaterEqual(pet.state["level"], 1)
                self.assertGreaterEqual(pet.state["exp"], 0)

    def test_truncated_state_does_not_prevent_startup(self):
        with tempfile.TemporaryDirectory(prefix="desktop-pet-state-") as temp:
            (Path(temp) / plugin_module.STATE_FILE).write_text('{"mood":', encoding="utf-8")
            with patch.object(plugin_module, "_data_dir", return_value=temp):
                pet = plugin_module.DesktopPetPlugin(FakeContext(), {})
            self.assertEqual(pet.state, {**plugin_module.DEFAULT_STATE, **plugin_module.SLEEP_DEFAULTS})


if __name__ == "__main__":
    unittest.main()
