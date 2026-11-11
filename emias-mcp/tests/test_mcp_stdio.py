"""Stdio smoke-тест MCP: initialize, list_tools, session_status, auth_required."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    HAS_MCP = True
except Exception:  # pragma: no cover
    HAS_MCP = False

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(HAS_MCP, "mcp не установлен")
class StdioSmokeTest(unittest.TestCase):
    def test_tools_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "EMIAS_MCP_HOME": tmp,
                "EMIAS_MCP_DISABLE_KEYCHAIN": "1",
                "EMIAS_MCP_IMPORT_CLI": "0",
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONPATH": str(ROOT),
            }

            async def run():
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "emias_mcp.mcp_server"],
                    env=env,
                )
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        names = {tool.name for tool in tools.tools}
                        for required in (
                            "session_status",
                            "open_bootstrap",
                            "quick_login",
                            "patients",
                            "visits",
                            "slots",
                            "booking_prepare",
                            "booking_execute",
                            "cancel_execute",
                            "document_download",
                        ):
                            self.assertIn(required, names)

                        status = await session.call_tool("session_status", {})
                        self.assertFalse(status.isError)
                        payload = json.loads(status.content[0].text)
                        self.assertEqual(payload["status"], "bootstrap")
                        self.assertNotIn("access_token", json.dumps(payload))

                        patients = await session.call_tool("patients", {})
                        data = json.loads(patients.content[0].text)
                        self.assertFalse(data["ok"])
                        self.assertEqual(data["error"], "auth_required")

            anyio.run(run)


if __name__ == "__main__":
    unittest.main()
