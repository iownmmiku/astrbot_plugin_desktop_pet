"""Run the REAL plugin transport with a FAKE AstrBot model for local UI testing.

This is a development fixture, not a replacement for an installed AstrBot plugin.
No real model or credentials are used. All data is removed on normal shutdown.

    python tests/run_demo_server.py

Connect to 127.0.0.1:19898. Pass --token to test authenticated connections.
"""

import argparse
import asyncio
import logging
import tempfile
from unittest.mock import patch

from test_plugin import FakeContext, plugin_module


async def run(port: int, token: str):
    with tempfile.TemporaryDirectory(prefix="desktop-pet-demo-") as directory:
        with patch.object(plugin_module, "_data_dir", return_value=directory):
            context = FakeContext()
            context.provider.reply = "我在这里陪你。今天也记得休息一下呀！"
            config = {
                "ws_host": "127.0.0.1",
                "ws_port": port,
                "auth_token": token,
                "pet_name": "小尾巴",
                "persona": "你是名叫小尾巴的桌宠。",
                "enable_state_decay": False,
                "llm_action_reply": False,
            }
            plugin = plugin_module.DesktopPetPlugin(context, config)
            try:
                await plugin.initialize()
                actual_port = plugin._runner.addresses[0][1]
                print("DEVELOPMENT FIXTURE: real plugin HTTP/WS, simulated LLM.", flush=True)
                print(f"Listening at http://127.0.0.1:{actual_port}; token {'enabled' if token else 'disabled'}.", flush=True)
                print("State uses a temporary directory. Press Ctrl+C to stop.", flush=True)
                await asyncio.Event().wait()
            finally:
                await plugin.terminate()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=19898)
    parser.add_argument("--token", default="")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        asyncio.run(run(args.port, args.token))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
