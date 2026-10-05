#!/usr/bin/env python3
"""Verify source-bearing real LightRAG/MCP retrieval and a local model tool call."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

OLLAMA = "http://127.0.0.1:11435"
RAG = "http://127.0.0.1:19622"
MCP_URL = "http://127.0.0.1:18000/mcp"
MODEL = "qwen2.5:3b"
EXPECTED_SOURCE = "demo-product-knowledge-v1-jira-CP-263.txt"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise AssertionError("Unexpected redirect in isolated local E2E")


def http(method, url, payload=None, headers=None, timeout=60):
    p = urlparse(url)
    if p.scheme != "http" or p.hostname not in {"localhost", "127.0.0.1"} or p.port not in {11435, 19622, 18000}:
        raise AssertionError("Refusing a non-local or unapproved endpoint")
    data = None if payload is None else json.dumps(payload).encode()
    request_headers = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    try:
        with build_opener(NoRedirect).open(Request(url, data=data, headers=request_headers, method=method), timeout=timeout) as r:
            raw, status = r.read(), r.status
    except HTTPError as error:
        raw, status = error.read(), error.code
    return status, json.loads(raw) if raw else {}


def poll_tracks(key, ids, deadline_seconds):
    deadline = time.monotonic() + deadline_seconds
    remaining = set(ids)
    last_report = 0
    while remaining and time.monotonic() < deadline:
        for tid in list(remaining):
            budget = deadline - time.monotonic()
            if budget <= 0:
                break
            code, state = http("GET", RAG + "/documents/track_status/" + quote(tid, safe=""),
                               headers={"X-API-Key": key}, timeout=min(30, budget))
            if code != 200 or not isinstance(state, dict) or state.get("track_id") != tid:
                raise AssertionError("LightRAG tracking lookup failed")
            documents = state.get("documents")
            if not isinstance(documents, list):
                raise AssertionError("LightRAG tracking response has no document list")
            statuses = [str(d.get("status", "")).split(".")[-1].lower() for d in documents if isinstance(d, dict)]
            if any(s == "failed" for s in statuses):
                raise AssertionError("LightRAG reported failed indexing")
            if any(s not in {"pending", "parsing", "analyzing", "processing", "preprocessed", "processed"} for s in statuses):
                raise AssertionError("LightRAG returned an unknown document state")
            if statuses and len(statuses) == len(documents) and all(s == "processed" for s in statuses):
                remaining.remove(tid)
        if time.monotonic() - last_report >= 30:
            print(f"Tracking completion: {len(ids) - len(remaining)}/{len(ids)}", flush=True)
            last_report = time.monotonic()
        if remaining:
            time.sleep(min(2, max(0, deadline - time.monotonic())))
    if remaining:
        raise AssertionError("Accepted documents did not complete within the shared tracking deadline")


def ingest_fixture(key):
    data = json.loads((Path(__file__).resolve().parents[2] / "sample-data/product-knowledge-demo/dataset.json").read_text())
    documents = data.get("documents")
    if data.get("fictional") is not True or not isinstance(documents, list) or len(documents) != 11:
        raise AssertionError("Only the fictional eleven-document fixture may be ingested")
    ids = []
    for document in documents:
        if not isinstance(document.get("text"), str) or not document["file_source"].startswith("demo-product-knowledge-v1-"):
            raise AssertionError("Fictional fixture text/source contract invalid")
        code, body = http("POST", RAG + "/documents/text",
                          {"text": document["text"], "file_source": document["file_source"]}, {"X-API-Key": key})
        if code == 409 and str(body.get("detail", "")).startswith("Document storage already contains"):
            continue
        if code not in {200, 202} or body.get("status") != "success" or not isinstance(body.get("track_id"), str):
            raise AssertionError("LightRAG did not accept a fictional document; no automatic resubmission")
        ids.append(body["track_id"])
        # Checkpoint before the next insertion; restarting must not lose acceptance evidence.
        checkpoint = Path("/tmp/activepieces-local-e2e-tracks.jsonl")
        fd = os.open(checkpoint, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps({"track_id": body["track_id"], "file_source": document["file_source"], "accepted_at": time.time()}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return ids


def evidence_text(result):
    return json.dumps(getattr(result, "structuredContent", None), ensure_ascii=False) + " " + " ".join(
        getattr(item, "text", "") for item in getattr(result, "content", []))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ingest-fixture", action="store_true", help="direct fictional fixture path; does not prove Activepieces ingestion")
    parser.add_argument("--tracking-file", help="private JSON containing accepted trackIds from the imported demo")
    parser.add_argument("--full-fixture", action="store_true", help="require all eleven accepted documents and both contrasting sources")
    parser.add_argument("--tracking-checkpoint", help="private JSONL checkpoint; resumes polling without insertion")
    parser.add_argument("--track-deadline", type=int, default=1800)
    args = parser.parse_args()
    if not 1 <= args.track_deadline <= 3600:
        raise AssertionError("Tracking deadline must be between 1 and 3600 seconds")
    key, token = os.environ["LIGHTRAG_API_KEY"], os.environ["MCP_TOKEN"]
    code, _ = http("GET", RAG + "/health")
    if code != 200:
        raise AssertionError("Isolated LightRAG is unhealthy")
    code, tags = http("GET", OLLAMA + "/api/tags")
    names = {m["name"] for m in tags.get("models", [])}
    if code != 200 or MODEL not in names or not any(n.split(":", 1)[0] == "bge-m3" for n in names):
        raise AssertionError("Required isolated Ollama models are absent")
    ids = ingest_fixture(key) if args.ingest_fixture else []
    if args.tracking_file:
        ids.extend(json.loads(Path(args.tracking_file).read_text())["trackIds"])
    if args.tracking_checkpoint:
        if args.ingest_fixture:
            raise AssertionError("Resume checkpoint polling without resubmitting documents")
        ids.extend(json.loads(line)["track_id"] for line in Path(args.tracking_checkpoint).read_text().splitlines())
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise AssertionError("Accepted tracking IDs are required to prove completion")
    if args.full_fixture and len(ids) != 11:
        raise AssertionError("Full fixture acceptance requires eleven distinct tracking IDs")
    poll_tracks(key, ids, args.track_deadline)

    queries = [("Who owns eligibility policy? CP-263", EXPECTED_SOURCE, ("CP-263", "UNASSIGNED"))]
    if args.full_fixture:
        queries.append(("CP-248 proposed account owner approval for plan changes",
                        "demo-product-knowledge-v1-jira-CP-248.txt", ("CP-248", "approves", "proposal")))
    for query, source, markers in queries:
        code, result = http("POST", RAG + "/query/data", {"query": query, "mode": "mix", "top_k": 8},
                            {"X-API-Key": key}, timeout=150)
        evidence = json.dumps(result, ensure_ascii=False)
        if code != 200 or source not in evidence or any(marker.lower() not in evidence.lower() for marker in markers):
            raise AssertionError("LightRAG retrieval lacks expected source-bearing fictional evidence")

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    headers = {"Authorization": "Bearer " + token, "Host": "mcp:8000"}
    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool = next((t for t in tools.tools if t.name == "knowledge_search"), None)
            if tool is None:
                raise AssertionError("MCP did not advertise knowledge_search")
            query_started = time.monotonic()
            try:
                direct = await session.call_tool(tool.name, {"query": "Who owns eligibility policy? CP-263", "mode": "mix", "top_k": 8})
            finally:
                print(f"Direct MCP query elapsed: {time.monotonic() - query_started:.1f}s", flush=True)
            text = evidence_text(direct)
            if getattr(direct, "isError", False) or EXPECTED_SOURCE not in text or "CP-263" not in text or "UNASSIGNED" not in text:
                raise AssertionError("MCP retrieval lacks the expected source-bearing fictional evidence")
            if args.full_fixture:
                query, source, markers = queries[1]
                result = await session.call_tool(tool.name, {"query": query, "mode": "mix", "top_k": 8})
                text = evidence_text(result)
                if getattr(result, "isError", False) or source not in text or any(marker.lower() not in text.lower() for marker in markers):
                    raise AssertionError("MCP retrieval lacks the expected CP-248 proposal source")
            function = {"type": "function", "function": {"name": tool.name, "description": tool.description,
                                                         "parameters": tool.inputSchema}}
            code, result = http("POST", OLLAMA + "/v1/chat/completions", {
                "model": MODEL, "temperature": 0, "stream": False, "max_tokens": 160,
                "messages": [{"role": "user", "content": "Call knowledge_search for CP-263 eligibility policy ownership evidence."}],
                "tools": [function]}, timeout=240)
            calls = result.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])
            if code != 200 or not calls or calls[0].get("function", {}).get("name") != tool.name:
                raise AssertionError("Local Qwen did not emit a knowledge_search call")
            arguments = calls[0]["function"]["arguments"]
            arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            executed = await session.call_tool(tool.name, arguments)
            if getattr(executed, "isError", False) or EXPECTED_SOURCE not in evidence_text(executed):
                raise AssertionError("Model-emitted MCP call lacks source-bearing evidence")
    print("PASS local LightRAG/MCP/model smoke; LibreChat UI acceptance remains separate")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        print("FAIL local smoke:", type(error).__name__, file=sys.stderr)
        sys.exit(1)
