#!/usr/bin/env python3
"""Prove local Qwen tool-call emission before starting the full test stack."""
import asyncio
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener

MODEL = "qwen2.5:3b"
BASE = "http://127.0.0.1:11435"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise AssertionError("Model endpoint redirected outside the isolated test")


def http(path, payload=None, timeout=240):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {} if data is None else {"Content-Type": "application/json"}
    with build_opener(NoRedirect).open(Request(BASE + path, data=data, headers=headers), timeout=timeout) as response:
        return json.loads(response.read())


async def main():
    tags = http("/api/tags")
    names = {m["name"] for m in tags.get("models", [])}
    if MODEL not in names or not any(n.split(":", 1)[0] == "bge-m3" for n in names):
        raise AssertionError("Both required models must be installed in isolated Ollama")
    embedded = http("/v1/embeddings", {"model": "bge-m3", "input": ["Fictional Northstar capability check."]})
    vector = embedded.get("data", [{}])[0].get("embedding", [])
    if len(vector) != 1024 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vector):
        raise AssertionError("Isolated bge-m3 did not produce a finite 1024-dimensional embedding")
    # Import the actual pinned bridge tool definition; listing tools makes no
    # upstream requests. These dummy values are unrelated to production keys.
    os.environ.setdefault("LIGHTRAG_API_KEY", "model-smoke-dummy")
    os.environ.setdefault("MCP_TOKEN", "model-smoke-dummy-token-" + "0" * 32)
    spec = importlib.util.spec_from_file_location("e2e_bridge", Path(__file__).resolve().parents[2] / "mcp/server.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    tools = await bridge.mcp.list_tools()
    tool = next(t for t in tools if t.name == "knowledge_search")
    function = {"type": "function", "function": {"name": tool.name,
                "description": tool.description, "parameters": tool.inputSchema}}
    started = time.monotonic()
    result = http("/v1/chat/completions", {
        "model": MODEL, "stream": False, "temperature": 0, "max_tokens": 160,
        "messages": [{"role": "user", "content": "Call knowledge_search to find source evidence about CP-263 eligibility policy ownership."}],
        "tools": [function],
    })
    calls = result.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])
    if not calls or calls[0].get("function", {}).get("name") != tool.name:
        raise AssertionError("qwen2.5:3b did not emit the knowledge_search tool call")
    arguments = calls[0]["function"]["arguments"]
    arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
    if (not isinstance(arguments, dict) or not isinstance(arguments.get("query"), str)
            or not 3 <= len(arguments["query"]) <= 4000):
        raise AssertionError("Tool call arguments do not satisfy the actual bridge query contract")
    if arguments.get("mode", "mix") not in {"mix", "local", "global", "hybrid", "naive"}:
        raise AssertionError("Tool call contains unsupported retrieval mode")
    top_k = arguments.get("top_k", 12)
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 30:
        raise AssertionError("Tool call contains invalid top_k")
    # Include the generation-to-embedding model switch and a representative
    # fixture input within LightRAG's unchanged embedding worker deadline.
    fixture = json.loads((Path(__file__).resolve().parents[2] /
                          "sample-data/product-knowledge-demo/dataset.json").read_text())
    longest = max((d["text"] for d in fixture["documents"]), key=len)
    result = http("/v1/embeddings", {"model": "bge-m3", "input": [longest]}, timeout=60)
    vector = result.get("data", [{}])[0].get("embedding", [])
    if len(vector) != 1024 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vector):
        raise AssertionError("Fixture embedding failed within the unchanged worker deadline")
    print(f"PASS isolated model capability: bge-m3 embedding; {MODEL} emitted {tool.name}; {time.monotonic() - started:.1f}s")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        # Never include request/response bodies or environment values.
        print("FAIL isolated model capability:", type(error).__name__, file=sys.stderr)
        sys.exit(1)
