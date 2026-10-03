"""Protocol/auth tests and mocked upstream contract; no paid API calls."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx

os.environ.setdefault("MCP_TOKEN", "test-token-" + "a" * 40)
os.environ.setdefault("LIGHTRAG_API_KEY", "test-lightrag-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_upstream_contract_and_references(self):
        evidence = {"status": "success", "message": "ok", "data": {
            "chunks": [{"content": "Responsabile Giulia", "reference_id": "1"}],
            "references": [{"reference_id": "1", "file_path": "demo-acme.txt"}],
        }, "metadata": {"mode": "mix"}}

        def upstream(request):
            self.assertEqual(str(request.url), server.LIGHTRAG_URL + "/query/data")
            self.assertEqual(request.headers["X-API-Key"], server.LIGHTRAG_API_KEY)
            body = json.loads(request.content)
            self.assertEqual(body["mode"], "mix")
            self.assertFalse(body["enable_rerank"])
            self.assertNotIn("conversation_history", body)
            return httpx.Response(200, json=evidence)

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        with patch.object(server.httpx, "AsyncClient", return_value=client):
            result = await server.knowledge_search("Chi gestisce Acme?")
        self.assertEqual(result, evidence)

    async def test_errors_do_not_leak_upstream_body(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(403, text="PRIVATE INTERNAL DETAIL")))
        with patch.object(server.httpx, "AsyncClient", return_value=client):
            with self.assertRaisesRegex(ValueError, "LightRAG HTTP 403") as raised:
                await server.knowledge_search("Una domanda valida")
        self.assertNotIn("PRIVATE", str(raised.exception))

    async def test_mcp_auth_initialize_tools_and_validation(self):
        transport = httpx.ASGITransport(app=server.app)
        async with server.mcp.session_manager.run():
            async with httpx.AsyncClient(transport=transport, base_url="http://mcp:8000") as client:
                self.assertEqual((await client.get("/health")).status_code, 200)
                self.assertEqual((await client.post("/mcp", json={})).status_code, 401)
                client.headers.update({
                    "Authorization": "Bearer " + server.MCP_TOKEN,
                    "Accept": "application/json, text/event-stream",
                })
                response = await client.post("/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "test", "version": "1"}},
                })
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn("serverInfo", response.json()["result"])
                response = await client.post("/mcp", json={
                    "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
                })
                listing = response.json()["result"]["tools"]
                self.assertEqual([t["name"] for t in listing], ["knowledge_search"])
                self.assertTrue(listing[0]["annotations"]["readOnlyHint"])
                # Exercise successful tools/call through the real MCP transport.
                backend = httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json={
                        "status": "success", "message": "ok", "metadata": {},
                        "data": {"references": [{"file_path": "demo-acme.txt", "reference_id": "1"}]},
                    })))
                with patch.object(server.httpx, "AsyncClient", return_value=backend):
                    response = await client.post("/mcp", json={
                        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                        "params": {"name": "knowledge_search", "arguments": {"query": "Chi gestisce Acme?"}},
                    })
                result = response.json()["result"]
                self.assertFalse(result.get("isError", False))
                evidence = result.get("structuredContent") or json.loads(result["content"][0]["text"])
                self.assertEqual(evidence["data"]["references"][0]["file_path"], "demo-acme.txt")
                for arguments in ({"query": "ok", "top_k": 200},
                                  {"query": "valid question", "mode": "bypass"}):
                    response = await client.post("/mcp", json={
                        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "knowledge_search", "arguments": arguments},
                    })
                    self.assertTrue(response.json()["result"]["isError"])
                response = await client.post("/mcp", headers={"Host": "evil.example"}, json={
                    "jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {},
                })
                self.assertEqual(response.status_code, 421)


if __name__ == "__main__":
    unittest.main()
