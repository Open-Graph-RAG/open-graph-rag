"""Opt-in MCP schema, raw JSON guard, and HTTP app lifespan tests."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

MCP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MCP_DIR))
os.environ.update({
    "MCP_TOKEN": "decision-protocol-token-" + "x" * 40,
    "LIGHTRAG_API_KEY": "decision-protocol-key",
    "MCP_DECISION_ENABLED": "1",
})


class FakeRuntime:
    def __init__(self, *_args, **_kwargs):
        self.closed = False
        self.calls = 0

    async def evaluate(self, _request):
        self.calls += 1
        return SimpleNamespace(model_dump=lambda mode: {"status": "evaluated"})

    async def close(self):
        self.closed = True


import decision_runtime
OriginalRuntime = decision_runtime.DecisionRuntime
decision_runtime.DecisionRuntime = FakeRuntime
spec = importlib.util.spec_from_file_location("enabled_server", MCP_DIR / "server.py")
server = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = server
spec.loader.exec_module(server)
decision_runtime.DecisionRuntime = OriginalRuntime


class DecisionProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_guard_counts_streamed_request_chunks(self):
        forwarded = False
        responses = []

        async def wrapped(_scope, _receive, _send):
            nonlocal forwarded
            forwarded = True

        chunks = [b"x" * 100_000, b"y" * 32_000]
        position = 0

        async def receive():
            nonlocal position
            chunk = chunks[position]
            position += 1
            return {"type": "http.request", "body": chunk,
                    "more_body": position < len(chunks)}

        async def send(message):
            responses.append(message)

        guard = server.DecisionRawGuard(wrapped)
        await guard({"type": "http", "method": "POST", "headers": []}, receive, send)
        self.assertFalse(forwarded)
        self.assertEqual(next(item["status"] for item in responses if item["type"] == "http.response.start"), 413)

    async def test_enabled_tools_raw_guards_search_and_asgi_shutdown(self):
        runtime = server.DECISION_RUNTIME
        transport = httpx.ASGITransport(app=server.app)
        async with server._mcp_http_app.router.lifespan_context(server._mcp_http_app):
            async with httpx.AsyncClient(transport=transport, base_url="http://mcp:8000") as client:
                client.headers.update({
                    "Authorization": "Bearer " + server.MCP_TOKEN,
                    "Accept": "application/json, text/event-stream",
                })
                initialized = await client.post("/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "protocol-test", "version": "1"}},
                })
                self.assertEqual(initialized.status_code, 200, initialized.text)

                listed = await client.post("/mcp", json={
                    "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
                })
                tools = listed.json()["result"]["tools"]
                self.assertEqual({tool["name"] for tool in tools},
                                 {"knowledge_search", "decision_evaluate"})
                decision = next(tool for tool in tools if tool["name"] == "decision_evaluate")
                schema = decision["inputSchema"]
                self.assertEqual(set(schema["properties"]), {
                    "objective", "query", "questions", "supplied_evidence", "seen_fingerprints",
                })
                self.assertEqual(schema["properties"]["questions"]["additionalProperties"]["discriminator"]["propertyName"],
                                 "type")

                async def call(arguments, call_id):
                    return await client.post("/mcp", json={
                        "jsonrpc": "2.0", "id": call_id, "method": "tools/call",
                        "params": {"name": "decision_evaluate", "arguments": arguments},
                    })

                valid = {
                    "objective": "Classify the documented change.",
                    "query": "documented change",
                    "questions": {"kind": {"type": "choice", "instructions": "Which is supported?",
                                  "options": [{"id": "yes", "description": "Supported."},
                                              {"id": "no", "description": "Not supported."}]}},
                }
                response = await call(valid, 3)
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()["result"]
                decision_result = result.get("structuredContent") or json.loads(result["content"][0]["text"])
                self.assertEqual(decision_result["status"], "evaluated")
                self.assertEqual(runtime.calls, 1)

                raw_valid = json.dumps(valid, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                exact_args = raw_valid[:-1] + b" " * (65_536 - len(raw_valid)) + b"}"
                rpc_prefix = (b'{"jsonrpc":"2.0","id":4,"method":"tools/call","params":'
                              b'{"name":"decision_evaluate","arguments":')
                response = await client.post("/mcp", content=rpc_prefix + exact_args + b"}}",
                                             headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(runtime.calls, 2)

                too_large_args = raw_valid[:-1] + b" " * (65_537 - len(raw_valid)) + b"}"
                response = await client.post("/mcp", content=rpc_prefix + too_large_args + b"}}",
                                             headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(runtime.calls, 2)

                multibyte = (b'{"objective":"' + "é".encode("utf-8") * 33_000 +
                             b'","query":"documented change","questions":{}}')
                response = await client.post("/mcp", content=rpc_prefix + multibyte + b"}}",
                                             headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(runtime.calls, 2)

                duplicate = (b'{"jsonrpc":"2.0","id":5,"method":"tools/call","params":'
                             b'{"name":"decision_evaluate","arguments":{"objective":"first",'
                             b'"objective":"second","query":"documented change","questions":{}}}}')
                response = await client.post("/mcp", content=duplicate, headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(runtime.calls, 2)

                search_backend = httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, json={
                        "status": "success", "data": {"chunks": [{"content": "Evidence."}]},
                    })))
                with patch.object(server.httpx, "AsyncClient", return_value=search_backend):
                    searched = await client.post("/mcp", json={
                        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
                        "params": {"name": "knowledge_search", "arguments": {"query": "Find evidence"}},
                    })
                self.assertEqual(searched.status_code, 200, searched.text)
                self.assertFalse(searched.json()["result"].get("isError", False))
        self.assertTrue(runtime.closed)


if __name__ == "__main__":
    unittest.main()
