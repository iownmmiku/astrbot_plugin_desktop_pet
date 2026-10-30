"""0.5 transport and pet-care regressions."""

import asyncio
import unittest
import test_plugin as base

plugin_module = base.plugin_module


class FeatureTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.PluginHTTPTests.asyncSetUp
    asyncTearDown = base.PluginHTTPTests.asyncTearDown
    post = base.PluginHTTPTests.post
    get = base.PluginHTTPTests.get
    connect = base.PluginHTTPTests.connect
    until_type = base.PluginHTTPTests.until_type

    async def restart(self):
        await self.plugin.terminate()
        self.plugin = plugin_module.DesktopPetPlugin(self.context, self.config)
        await self.plugin.initialize()
        self.base = f"http://127.0.0.1:{self.plugin._runner.addresses[0][1]}"

    async def test_sleep_restores_energy_gradually_persists_and_can_wake(self):
        energy, exp = self.plugin.state["energy"], self.plugin.state["exp"]
        status, asleep = await self.post("/api/action", {"action": "sleep"})
        self.assertEqual(status, 200)
        self.assertTrue(asleep["state"]["is_sleeping"])
        self.assertEqual(asleep["state"]["energy"], energy)
        self.assertEqual(asleep["state"]["exp"], exp)
        await self.plugin._tick_state(1)
        self.assertEqual(self.plugin.state["energy"], energy + 6)
        await self.restart()
        self.assertTrue(self.plugin.state["is_sleeping"])
        self.assertEqual(self.plugin.state["energy"], energy + 6)
        status, awake = await self.post("/api/action", {"action": "wake"})
        self.assertEqual(status, 200)
        self.assertFalse(awake["state"]["is_sleeping"])
        self.assertIsNone(awake["state"]["sleep_started_at"])

    async def test_sleep_auto_wakes_when_full_and_small_decay_is_not_rounded_away(self):
        self.plugin.state["energy"] = 99
        await self.post("/api/action", {"action": "sleep"})
        await self.plugin._tick_state(.25)
        self.assertFalse(self.plugin.state["is_sleeping"])
        self.assertEqual(self.plugin.state["energy"], 100)
        self.plugin.config["enable_state_decay"] = True
        before = self.plugin.state["mood"]
        for _ in range(4):
            await self.plugin._tick_state(.25)
        self.assertAlmostEqual(self.plugin.state["mood"], before - .2)

    async def test_action_cooldown_survives_restart_without_duplicate_reward(self):
        self.plugin.config["action_cooldown_sec"] = 10
        first = (await self.post("/api/action", {"action": "feed", "request_id": "food1"}))[1]
        status, blocked = await self.post("/api/action", {"action": "feed", "request_id": "food2"})
        self.assertEqual(status, 429)
        self.assertEqual(blocked["code"], "action_cooldown")
        self.assertEqual(self.plugin.state["exp"], first["state"]["exp"])
        await self.restart()
        self.assertEqual((await self.post("/api/action", {"action": "feed"}))[0], 429)

    async def test_timeout_negotiation_pending_lookup_and_unknown_request(self):
        self.plugin.config["llm_timeout_sec"] = 90
        _, health = await self.get("/api/health")
        self.assertEqual(health["request_timeout_sec"], 105)
        ws, hello = await self.connect()
        self.assertEqual(hello["config"]["request_timeout_sec"], 105)
        self.context.provider.delay = .15
        task = asyncio.create_task(self.post("/api/chat", {"text": "慢慢来", "request_id": "slow-recover"}))
        while not self.plugin._chat_lock.locked():
            await asyncio.sleep(.005)
        self.assertEqual((await self.get("/api/requests/slow-recover?kind=chat"))[1]["status"], "pending")
        await task
        self.assertEqual((await self.get("/api/requests/slow-recover?kind=chat"))[1]["status"], "completed")
        self.assertEqual((await self.get("/api/requests/never-existed?kind=chat"))[1]["status"], "unknown")
