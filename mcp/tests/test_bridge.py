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
            "chunks": [{"content": "Manager Giulia", "reference_id": "1"}],
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
            result = await server.knowledge_search("Who manages Acme?")
        self.assertEqual(result, evidence)

    async def test_errors_do_not_leak_upstream_body(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(403, text="PRIVATE INTERNAL DETAIL")))
        with patch.object(server.httpx, "AsyncClient", return_value=client):
            with self.assertRaisesRegex(ValueError, "LightRAG HTTP 403") as raised:
                await server.knowledge_search("A valid question")
        self.assertNotIn("PRIVATE", str(raised.exception))

    async def test_timeout_does_not_leak_upstream_details(self):
        def upstream(request):
            raise httpx.ReadTimeout("PRIVATE TIMEOUT DETAIL", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        with patch.object(server.httpx, "AsyncClient", return_value=client):
            with self.assertRaisesRegex(ValueError, "LightRAG timed out") as raised:
                await server.knowledge_search("A valid question")
        self.assertNotIn("PRIVATE", str(raised.exception))

    async def test_authentication_regression_keeps_bridge_read_only(self):
        reached_downstream = []

        async def downstream(scope, receive, send):
            reached_downstream.append(scope["path"])
            await server.JSONResponse({"status": "ok"})(scope, receive, send)

        transport = httpx.ASGITransport(app=server.BearerAuth(downstream))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://mcp:8000"
        ) as client:
            missing = await client.post("/mcp", json={"jsonrpc": "2.0"})
            self.assertEqual(missing.status_code, 401)
            self.assertEqual(missing.headers["www-authenticate"], "Bearer")

            invalid = await client.post(
                "/mcp", headers={"Authorization": "Bearer invalid-regression-token"},
                json={"jsonrpc": "2.0"},
            )
            self.assertEqual(invalid.status_code, 401)
            self.assertEqual(reached_downstream, [])

            accepted = await client.post(
                "/mcp", headers={"Authorization": f"Bearer {server.MCP_TOKEN}"},
                json={"jsonrpc": "2.0"},
            )
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertEqual(accepted.json(), {"status": "ok"})
            self.assertEqual(reached_downstream, ["/mcp"])

    async def test_mcp_auth_initialize_tools_and_validation(self):
        transport = httpx.ASGITransport(app=server.app)
        async with server.mcp.session_manager.run():
            async with httpx.AsyncClient(transport=transport, base_url="http://mcp:8000") as client:
                self.assertEqual((await client.get("/health")).status_code, 200)
                self.assertEqual((await client.post("/mcp", json={})).status_code, 401)
                with patch.object(server.httpx, "AsyncClient") as upstream_client:
                    response = await client.post("/mcp", headers={
                        "Authorization": "Bearer incorrect-token",
                        "Accept": "application/json, text/event-stream",
                    }, json={
                        "jsonrpc": "2.0", "id": 0, "method": "tools/call",
                        "params": {"name": "knowledge_search", "arguments": {
                            "query": "A valid question",
                        }},
                    })
                    self.assertEqual(response.status_code, 401)
                    upstream_client.assert_not_called()
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
                        "params": {"name": "knowledge_search", "arguments": {"query": "Who manages Acme?"}},
                    })
                result = response.json()["result"]
                self.assertFalse(result.get("isError", False))
                evidence = result.get("structuredContent") or json.loads(result["content"][0]["text"])
                self.assertEqual(evidence["data"]["references"][0]["file_path"], "demo-acme.txt")
                for arguments in ({"query": "ok"}, {"query": "x" * 4001},
                                  {"query": "valid question", "top_k": 0},
                                  {"query": "valid question", "top_k": 31},
                                  {"query": "valid question", "mode": "bypass"}):
                    with self.subTest(arguments=arguments):
                        with patch.object(server.httpx, "AsyncClient") as upstream_client:
                            response = await client.post("/mcp", json={
                                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                "params": {"name": "knowledge_search", "arguments": arguments},
                            })
                            self.assertEqual(response.status_code, 200)
                            self.assertTrue(response.json()["result"]["isError"])
                            upstream_client.assert_not_called()
                response = await client.post("/mcp", headers={"Host": "evil.example"}, json={
                    "jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {},
                })
                self.assertEqual(response.status_code, 421)


if __name__ == "__main__":
    unittest.main()
