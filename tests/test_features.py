"""0.5 transport and pet-care regressions."""

import asyncio
import unittest
from unittest.mock import patch
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

    async def test_interactions_sync_across_clients_and_request_recovery(self):
        self.plugin.config["action_cooldown_sec"] = 10
        first_ws, first_hello = await self.connect()
        second_ws, second_hello = await self.connect()
        self.assertIn("interactions", first_hello["capabilities"])
        self.assertEqual(set(first_hello["interactions"]["cooldowns"]), set(plugin_module.ACTION_RESULTS))
        self.assertTrue(all(value == 0 for value in second_hello["interactions"]["cooldowns"].values()))
        status, result = await self.post("/api/action", {"action": "feed", "request_id": "cooldown-sync"})
        self.assertEqual(status, 200)
        self.assertEqual(result["action"], "feed")
        self.assertGreater(result["interactions"]["cooldowns"]["feed"], 0)
        self.assertLessEqual(result["interactions"]["cooldowns"]["feed"], 10)
        self.assertEqual(result["interactions"]["revision"], result["revision"])
        for ws in (first_ws, second_ws):
            update = await self.until_type(ws, "interactions")
            self.assertEqual(update["data"], result["interactions"])
            self.assertEqual(update["revision"], result["revision"])
        _, recovery = await self.get("/api/requests/cooldown-sync?kind=action")
        self.assertEqual(recovery["result"]["interactions"], result["interactions"])
        _, state = await self.get("/api/state")
        self.assertGreaterEqual(state["interactions"]["server_time"], result["interactions"]["server_time"])
        self.assertLessEqual(state["interactions"]["cooldowns"]["feed"], result["interactions"]["cooldowns"]["feed"])

    async def test_cooldown_errors_report_retry_delay_and_restart_preserves_metadata(self):
        self.plugin.config["action_cooldown_sec"] = 10
        await self.post("/api/action", {"action": "feed"})
        status, error = await self.post("/api/action", {"action": "feed"})
        self.assertEqual(status, 429)
        self.assertEqual(error["code"], "action_cooldown")
        self.assertGreater(error["retry_after_sec"], 0)
        self.assertLessEqual(error["retry_after_sec"], 10)
        self.assertIsInstance(error["error"], str)
        await self.restart()
        _, state = await self.get("/api/state")
        self.assertGreater(state["interactions"]["cooldowns"]["feed"], 0)
        self.plugin._action_times["feed"] = 0
        _, state = await self.get("/api/state")
        self.assertEqual(state["interactions"]["cooldowns"]["feed"], 0)

    async def test_failed_action_save_does_not_broadcast_or_start_cooldown(self):
        self.plugin.config["action_cooldown_sec"] = 10
        ws, _ = await self.connect()
        with patch.object(self.plugin, "_save_state", side_effect=OSError("disk full")):
            status, result = await self.post("/api/action", {"action": "feed"})
        self.assertEqual(status, 503)
        self.assertEqual(result["code"], "storage_error")
        self.assertEqual(self.plugin._interaction_snapshot()["cooldowns"]["feed"], 0)
        await ws.send_json({"type": "ping"})
        self.assertEqual((await ws.receive_json(timeout=2))["type"], "pong")

    async def test_request_lookup_keeps_cached_result_but_reports_current_cooldowns(self):
        self.plugin.config["action_cooldown_sec"] = 10
        _, completed = await self.post("/api/action", {"action": "feed", "request_id": "old-cooldown"})
        self.assertGreater(completed["interactions"]["cooldowns"]["feed"], 0)
        with patch.object(plugin_module.time, "time", return_value=completed["interactions"]["server_time"] + 60):
            status, recovery = await self.get("/api/requests/old-cooldown?kind=action")
            _, unknown = await self.get("/api/requests/unknown?kind=action")
        self.assertEqual(status, 200)
        self.assertEqual(recovery["status"], "completed")
        self.assertEqual(recovery["result"], completed)
        self.assertEqual(recovery["interactions"]["cooldowns"]["feed"], 0)
        self.assertGreater(recovery["interactions"]["server_time"], completed["interactions"]["server_time"])
        self.assertEqual(unknown["interactions"]["cooldowns"]["feed"], 0)
