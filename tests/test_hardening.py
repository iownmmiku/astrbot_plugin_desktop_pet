"""0.5.2 regressions: browser isolation, reply forwarding scope and decay persistence."""

import asyncio
import unittest
from unittest.mock import patch

import aiohttp

import test_plugin as base

plugin_module = base.plugin_module


class FakeResult:
    def __init__(self, text):
        self.text = text

    def get_plain_text(self):
        return self.text


class FakeEvent:
    def __init__(self, text="你好", admin=False, origin="qq:FriendMessage:1"):
        self.result, self.admin, self.unified_msg_origin = FakeResult(text), admin, origin
        self.replies = []

    def get_result(self):
        return self.result

    def is_admin(self):
        return self.admin

    def plain_result(self, text):
        return text


class HardeningTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.PluginHTTPTests.asyncSetUp
    asyncTearDown = base.PluginHTTPTests.asyncTearDown
    post = base.PluginHTTPTests.post
    get = base.PluginHTTPTests.get
    connect = base.PluginHTTPTests.connect
    until_type = base.PluginHTTPTests.until_type

    async def test_cross_site_simple_post_cannot_change_config(self):
        before = self.plugin.get_full_config()["pet_name"]
        async with self.client.post(self.base + "/api/config", data='{"pet_name":"hacked"}', headers={
            **self.headers, "Origin": "https://evil.example", "Content-Type": "text/plain"
        }) as response:
            self.assertEqual(response.status, 403)
            self.assertEqual((await response.json())["code"], "forbidden_origin")
            self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        self.assertEqual(self.plugin.get_full_config()["pet_name"], before)

    async def test_null_origin_and_cross_site_fetch_metadata_are_rejected(self):
        for headers in ({"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            with self.subTest(headers=headers):
                async with self.client.get(self.base + "/api/state", headers={**self.headers, **headers}) as response:
                    self.assertEqual(response.status, 403)

    async def test_cross_site_websocket_handshake_is_rejected(self):
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            await self.client.ws_connect(self.base + "/ws", headers={**self.headers, "Origin": "https://evil.example"})
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(len(self.plugin._clients), 0)

    async def test_non_json_post_is_rejected_even_without_origin(self):
        async with self.client.post(self.base + "/api/action", data='{"action":"feed"}', headers={
            **self.headers, "Content-Type": "text/plain"
        }) as response:
            self.assertEqual(response.status, 415)
            self.assertEqual((await response.json())["code"], "unsupported_media_type")

    async def test_desktop_requests_get_no_cors_headers(self):
        async with self.client.get(self.base + "/api/state", headers=self.headers) as response:
            self.assertEqual(response.status, 200)
            self.assertNotIn("Access-Control-Allow-Origin", response.headers)
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_explicitly_allowed_origin_gets_cors_and_preflight(self):
        self.plugin.config["allowed_origins"] = ["http://localhost:5173/"]
        origin = {"Origin": "http://localhost:5173", "Sec-Fetch-Site": "same-site"}
        async with self.client.options(self.base + "/api/chat", headers=origin) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "http://localhost:5173")
        async with self.client.get(self.base + "/api/state", headers={**self.headers, **origin}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "http://localhost:5173")
            self.assertEqual(response.headers["Vary"], "Origin")
        self.plugin.config["allowed_origins"] = ["null"]
        async with self.client.get(self.base + "/api/state", headers={**self.headers, "Origin": "null"}) as response:
            self.assertEqual(response.status, 403)

    async def test_reply_forwarding_defaults_to_admin_conversations(self):
        ws, _ = await self.connect()
        await self.plugin._forward_reply(FakeEvent("别人的私聊", admin=False))
        await self.plugin._forward_reply(FakeEvent("主人的对话", admin=True))
        message = await self.until_type(ws, "speak")
        self.assertEqual(message["text"], "主人的对话")

    async def test_reply_forwarding_whitelist_and_all_scopes(self):
        ws, _ = await self.connect()
        self.plugin.config.update(push_bot_reply_scope="whitelist", push_bot_reply_sessions=["qq:GroupMessage:42"])
        await self.plugin._forward_reply(FakeEvent("其他群", admin=True, origin="qq:GroupMessage:7"))
        await self.plugin._forward_reply(FakeEvent("白名单群", origin="qq:GroupMessage:42"))
        self.assertEqual((await self.until_type(ws, "speak"))["text"], "白名单群")
        self.plugin.config["push_bot_reply_scope"] = "all"
        await self.plugin._forward_reply(FakeEvent("所有会话"))
        self.assertEqual((await self.until_type(ws, "speak"))["text"], "所有会话")

    async def test_reply_forwarding_does_not_wait_for_slow_clients(self):
        release = asyncio.Event()

        class SlowSocket:
            async def send_json(self, payload):
                await release.wait()

            async def close(self, **kwargs):
                pass

        self.plugin._clients.add(SlowSocket())
        await asyncio.wait_for(self.plugin._forward_reply(FakeEvent("慢", admin=True)), timeout=0.5)
        self.assertEqual(len(self.plugin._background), 1)
        release.set()
        await asyncio.gather(*self.plugin._background)

    async def test_feed_command_can_be_limited_to_admins(self):
        self.plugin.config["feed_command_admin_only"] = True
        exp = self.plugin.state["exp"]
        replies = [reply async for reply in self.plugin.feed(FakeEvent(admin=False))]
        self.assertIn("管理员", replies[0])
        self.assertEqual(self.plugin.state["exp"], exp)
        replies = [reply async for reply in self.plugin.feed(FakeEvent(admin=True))]
        self.assertIn("投喂成功", replies[0])

    async def test_decay_batches_disk_writes_and_flushes_on_shutdown(self):
        self.plugin.config["enable_state_decay"] = True
        with patch.object(self.plugin, "_save_state", wraps=self.plugin._save_state) as save:
            for _ in range(3):
                await self.plugin._tick_state(.25)
            self.assertEqual(save.call_count, 0)
            self.assertTrue(self.plugin._state_dirty)
            self.plugin._last_state_save -= plugin_module.STATE_SAVE_INTERVAL_SEC
            await self.plugin._tick_state(.25)
            self.assertEqual(save.call_count, 1)
            self.assertFalse(self.plugin._state_dirty)
        satiety = self.plugin.state["satiety"]
        await self.plugin._tick_state(1)
        await self.plugin.terminate()
        restored = plugin_module.DesktopPetPlugin(self.context, self.config)
        self.assertEqual(restored.state["satiety"], satiety - .5)

    async def test_decay_at_floor_neither_writes_nor_broadcasts(self):
        self.plugin.config["enable_state_decay"] = True
        for key in plugin_module.DECAY_PER_MIN:
            self.plugin.state[key] = 0
        revision = self.plugin._state_revision
        self.plugin._state_dirty = False
        with patch.object(self.plugin, "_save_state") as save, patch.object(self.plugin, "_broadcast") as broadcast:
            self.plugin._last_state_save -= plugin_module.STATE_SAVE_INTERVAL_SEC
            for _ in range(8):
                await self.plugin._tick_state(.25)
        save.assert_not_called()
        broadcast.assert_not_called()
        self.assertEqual(self.plugin._state_revision, revision)

    async def test_decay_save_failure_is_retried_without_stopping(self):
        self.plugin.config["enable_state_decay"] = True
        self.plugin._last_state_save -= plugin_module.STATE_SAVE_INTERVAL_SEC
        with patch.object(self.plugin, "_save_state", side_effect=OSError("disk full")):
            await self.plugin._tick_state(1)
        self.assertTrue(self.plugin._state_dirty)
        await self.plugin._tick_state(1)
        self.assertFalse(self.plugin._state_dirty)

    def test_version_comes_from_metadata(self):
        with open(base.Path(plugin_module.__file__).with_name("metadata.yaml"), encoding="utf-8") as stream:
            self.assertIn(f"version: {plugin_module.PLUGIN_VERSION}", stream.read())


if __name__ == "__main__":
    unittest.main()
