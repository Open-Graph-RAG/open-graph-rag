#!/usr/bin/env python3
"""Run imported flows through an isolated Activepieces engine and mock APIs."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from mock_server import PLAN_TEXT, SECOND_TEXT

ALLOWED_PORTS = {18080, 19621}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise AssertionError("Unexpected HTTP redirect in isolated acceptance")


opener = build_opener(NoRedirect)


def checked_url(value, name):
    p = urlparse(value)
    if (p.scheme != "http" or p.hostname not in {"127.0.0.1", "localhost"}
            or p.port not in ALLOWED_PORTS or p.username or p.password or p.query or p.fragment):
        raise SystemExit(f"{name} must use isolated localhost HTTP without credentials/query on an approved port")
    return value.rstrip("/")


def request(method, url, payload=None, headers=None, raw=None):
    data = raw if raw is not None else (None if payload is None else json.dumps(payload).encode())
    h = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        h["Content-Type"] = "application/json"
    try:
        with opener.open(Request(url, data=data, headers=h, method=method), timeout=40) as response:
            raw, code = response.read(), response.status
    except HTTPError as error:
        raw, code = error.read(), error.code
    except (URLError, TimeoutError):
        raise AssertionError(f"request failed for {urlparse(url).path}") from None
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body = raw.decode("utf-8", "replace")
    return code, body


def need(ok, message):
    if not ok:
        raise AssertionError(message)


def mock_reset(base, case="normal", processing_polls=1):
    code, body = request("POST", base + "/__admin/reset", {"case": case, "processing_polls": processing_polls})
    need(code == 200 and body.get("status") == "reset", f"mock reset failed for {case}")


def mock_state(base):
    code, body = request("GET", base + "/__admin/state")
    need(code == 200 and isinstance(body, dict), "mock state unavailable")
    return body


def poll_track(base, track_id, key):
    deadline = time.monotonic() + 10
    for _ in range(8):
        need(time.monotonic() < deadline, "tracking deadline exceeded")
        code, body = request("GET", base + "/documents/track_status/" + quote(track_id, safe=""),
                             headers={"X-API-Key": key})
        need(code == 200 and body.get("track_id") == track_id, "tracking ID lookup failed")
        if body.get("status") == "processed":
            return
        need(body.get("status") == "processing", "unknown tracking state")
        time.sleep(0.05)
    raise AssertionError("tracking did not complete within eight polls")


def error_code(body):
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and error.get("code") in {"invalid_input", "method_not_allowed", "upstream_failure"}:
        need(set(body) == {"status", "error", "queued", "alreadyPresent", "trackIds"},
             "public error contains missing or unsafe fields")
        need(set(error) == {"code", "message"}, "public error details contain unsafe fields")
        messages = {"invalid_input": "Invalid request input.",
                    "method_not_allowed": "Only POST requests are allowed.",
                    "upstream_failure": "An upstream service failed during ingestion."}
        need(error["message"] == messages[error["code"]], "public error message is not fixed and redacted")
    return error.get("code") if isinstance(error, dict) else None


def internet_success(body, queued, present):
    need(set(body) == {"status", "message", "queued", "alreadyPresent", "trackIds"},
         "Internet success fields changed")
    expected_message = ("LightRAG accepted documents for background processing; ingestion is not complete yet."
                        if queued else "All usable sources were already present in LightRAG; no new processing was queued.")
    need(body["status"] == ("queued" if queued else "skipped")
         and body["message"] == expected_message and body["queued"] == queued
         and body["alreadyPresent"] == present and len(body["trackIds"]) == queued,
         "Internet success contract changed")


def google_success(body, queued):
    fields = {"status", "message", "file_source", "queued", "alreadyPresent"}
    if queued:
        fields.add("track_id")
    need(set(body) == fields, "Google success fields changed")
    need(body["status"] == ("queued" if queued else "already_present")
         and body["message"] == ("LightRAG accepted the document for background processing; ingestion is not complete yet."
                                 if queued else "This source is already present in LightRAG.")
         and body["file_source"] == "https://docs.google.com/document/d/abcdefghij12345/edit"
         and body["queued"] == queued and body["alreadyPresent"] == 1 - queued,
         "Google success contract changed")


def expect_upstream_failure(url, token, case, mock, payload):
    mock_reset(mock, case)
    code, body = request("POST", url, payload, {"X-Ingest-Token": token})
    need(code == 502 and isinstance(body, dict) and body.get("status") == "error"
         and error_code(body) == "upstream_failure", f"{case} did not return upstream_failure/502")
    return body


def find_queue_result(value):
    if isinstance(value, dict):
        if "queued" in value and ("trackIds" in value or "track_id" in value):
            return value
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        found = find_queue_result(child)
        if found is not None:
            return found
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--webhook-url", required=True)
    parser.add_argument("--google-url", required=True)
    parser.add_argument("--demo-url", required=True, help="exact authenticated synchronous manual-run endpoint")
    parser.add_argument("--demo-webhook", action="store_true", help="use the isolated authenticated webhook adapter for the manual demo")
    parser.add_argument("--mock-url", default="http://127.0.0.1:19621")
    args = parser.parse_args()
    internet = checked_url(args.webhook_url, "--webhook-url")
    google = checked_url(args.google_url, "--google-url")
    demo = checked_url(args.demo_url, "--demo-url")
    mock = checked_url(args.mock_url, "--mock-url")
    need(urlparse(mock).port == 19621, "mock must use isolated port 19621")
    need(all(urlparse(u).port == 18080 for u in (internet, google, demo)), "engine must use isolated port 18080")
    need(internet.endswith("/sync") and google.endswith("/sync"), "webhook URLs must end in /sync")
    ingest = os.environ["E2E_WEBHOOK_TOKEN"]
    notebook = os.environ["E2E_NOTEBOOK_TOKEN"]
    lightrag = os.environ["E2E_LIGHTRAG_API_KEY"]
    headers = {"X-Ingest-Token": ingest}
    payload = {"prompt": "fictional Northstar plan-change approval evidence", "maxResults": 10}

    mock_reset(mock)
    code, body = request("POST", internet, {"prompt": "valid", "maxResults": 11}, headers)
    need(code == 400 and error_code(body) == "invalid_input", "invalid input did not return 400 invalid_input")
    need(not mock_state(mock)["requests"], "invalid input reached upstreams")
    for auth in ({}, {"X-Ingest-Token": "wrong-e2e-token"}):
        code, body = request("POST", internet, payload, auth)
        need(code >= 400, "unauthorized webhook accepted")
        need(not mock_state(mock)["requests"], "unauthorized webhook reached upstreams")
        # Preserve native status and shape without logging response values or secrets.
        print("Native authentication response:", code, type(body).__name__,
              sorted(body) if isinstance(body, dict) else "text")
    code, body = request("POST", internet, headers=headers, raw=b"{")
    need(code >= 400, "malformed JSON accepted")
    need(not mock_state(mock)["requests"], "malformed request reached upstreams")
    print("Native malformed response:", code, type(body).__name__)
    code, body = request("GET", internet, headers=headers)
    need(code == 405 and error_code(body) == "method_not_allowed", "wrong method did not return explicit 405")
    need(not mock_state(mock)["requests"], "wrong method reached upstreams")

    mock_reset(mock, "normal", processing_polls=2)
    code, body = request("POST", internet, payload, headers)
    need(code == 202 and body.get("status") == "queued" and body.get("queued") == 2,
         "happy path did not queue two unique documents")
    need(set(body) == {"status", "message", "queued", "alreadyPresent", "trackIds"},
         "Internet successful response fields changed")
    internet_success(body, 2, 0)
    ids = body.get("trackIds")
    need(isinstance(ids, list) and len(ids) == 2 and len(set(ids)) == 2, "tracking IDs do not match queued count")
    saved = mock_state(mock)["documents"]
    need(len(saved) == 2 and len({d["text"] for d in saved}) == 2, "filtering/content dedup failed")
    need([d["file_source"] for d in saved] == ["https://docs.example.invalid/plan", "https://docs.example.invalid/audit"],
         "source identity or insertion order changed")
    expected_texts = {"https://docs.example.invalid/plan": PLAN_TEXT,
                      "https://docs.example.invalid/audit": SECOND_TEXT}
    expected = {source: {"text": text, "metadata": {
        "source_url": source, "title": title, "query": payload["prompt"], "content_type": "text/plain"}}
        for (source, text), title in zip(expected_texts.items(), ("Plan rules", "Audit proposal"))}
    inserts = [r["body"] for r in mock_state(mock)["requests"] if r["path"] == "/documents/text"]
    need(len(inserts) == 2 and {r["file_source"]: {"text": r["text"], "metadata": r.get("metadata")}
                              for r in inserts} == expected,
         "exact ingestion text, provenance or filtering changed")
    for tid in ids:
        poll_track(mock, tid, lightrag)
    code, result = request("POST", mock + "/query/data", {"query": "Northstar plan approval", "mode": "mix"},
                           {"X-API-Key": lightrag})
    chunks = result.get("data", {}).get("chunks", [])
    need(code == 200 and len(chunks) == 2
         and {c["file_path"]: c["content"] for c in chunks} == expected_texts,
         "mock source retrieval content changed")
    code, body = request("POST", internet, payload, headers)
    need(code == 200 and body.get("queued") == 0 and body.get("alreadyPresent") == 2 and body.get("trackIds") == [],
         "same-source conflict was not counted as already present")
    internet_success(body, 0, 2)
    mock_reset(mock, "skipped")
    code, body = request("POST", internet, payload, headers)
    need(code == 200 and body.get("queued") == 0 and body.get("trackIds") == [], "empty search did not skip")
    internet_success(body, 0, 0)
    need(not mock_state(mock)["documents"], "skipped search inserted documents")
    for case in ("all_duplicate",):
        mock_reset(mock, case)
        code, body = request("POST", internet, payload, headers)
        need(code == 200 and body.get("alreadyPresent") == 2 and body.get("queued") == 0, "duplicate-only case failed")
        internet_success(body, 0, 2)
    for case in ("tavily_500", "tavily_malformed", "lightrag_500", "lightrag_unrelated_409", "partial_second_500"):
        failed = expect_upstream_failure(internet, ingest, case, mock, payload)
        if case == "partial_second_500":
            need(failed.get("queued") == 1 and len(failed.get("trackIds", [])) == 1
                 and failed.get("alreadyPresent") == 0, "partial acceptance metadata is incorrect")
            inserts = [r for r in mock_state(mock)["requests"] if r["path"] == "/documents/text"]
            need(len(inserts) == 2, "automatic insertion retry detected")
        else:
            need(failed.get("queued") == 0 and failed.get("alreadyPresent") == 0
                 and failed.get("trackIds") == [], "failed first insertion claimed acceptance")

    mock_reset(mock)
    notebook_payload = {"driveFileId": "abcdefghij12345", "notebook": "E2E Northstar"}
    code, body = request("POST", google, {"driveFileId": "bad"}, {"X-Ingest-Token": notebook})
    need(code == 400 and error_code(body) == "invalid_input", "Google invalid input was accepted")
    need(not mock_state(mock)["requests"], "Google invalid input reached upstreams")
    for auth in ({}, {"X-Ingest-Token": "wrong-e2e-token"}):
        code, body = request("POST", google, notebook_payload, auth)
        need(code >= 400, "Unauthorized Google webhook accepted")
        need(not mock_state(mock)["requests"], "Unauthorized Google webhook reached upstreams")
    code, body = request("POST", google, headers={"X-Ingest-Token": notebook}, raw=b"{")
    need(code >= 400 and not mock_state(mock)["requests"], "Malformed Google request reached upstreams")
    code, body = request("GET", google, headers={"X-Ingest-Token": notebook})
    need(code == 405 and error_code(body) == "method_not_allowed", "Google wrong method did not return 405")
    need(not mock_state(mock)["requests"], "Google wrong method reached upstreams")
    code, body = request("POST", google, notebook_payload, {"X-Ingest-Token": notebook})
    need(code == 202 and isinstance(body.get("track_id"), str), "Google happy path lost singular track_id")
    google_success(body, 1)
    saved = mock_state(mock)["documents"]
    need(len(saved) == 1 and saved[0]["file_source"] == "https://docs.google.com/document/d/abcdefghij12345/edit",
         "Google stable source identity changed")
    need(saved[0]["text"] == "Fictional exported NotebookLM note: CP-248 is a proposal.", "Google literal export text changed")
    google_inserts = [r["body"] for r in mock_state(mock)["requests"] if r["path"] == "/documents/text"]
    need(google_inserts == [{"text": "Fictional exported NotebookLM note: CP-248 is a proposal.",
                             "file_source": "https://docs.google.com/document/d/abcdefghij12345/edit"}],
         "Google insertion body or provenance changed")
    poll_track(mock, body["track_id"], lightrag)
    code, body = request("POST", google, notebook_payload, {"X-Ingest-Token": notebook})
    need(code == 200 and body.get("alreadyPresent") == 1, "Google duplicate source was not recognized")
    google_success(body, 0)
    for case in ("google_403", "google_empty", "google_oversized", "lightrag_500", "lightrag_unrelated_409"):
        expect_upstream_failure(google, notebook, case, mock, notebook_payload)

    mock_reset(mock)
    if args.demo_webhook:
        need(demo.endswith("/sync"), "demo adapter must use the generated synchronous webhook URL")
        code, run = request("POST", demo, {}, {"X-Ingest-Token": ingest})
    else:
        run_body = json.loads(os.environ["E2E_DEMO_REQUEST_JSON"])
        auth = os.environ["E2E_ENGINE_BEARER_TOKEN"]
        code, run = request("POST", demo, run_body, {"Authorization": "Bearer " + auth})
    queue = find_queue_result(run)
    need(code == (202 if args.demo_webhook else 200) and queue is not None and queue.get("queued") == 11,
         "manual demo did not queue eleven documents")
    public_fields = {"demo_id", "fictional", "status", "message", "queued", "alreadyPresent", "trackIds"}
    if not args.demo_webhook:
        public_fields.add("httpStatus")
        need(queue.get("httpStatus") == 202, "Manual demo summary lost acceptance status")
    need(set(queue) == public_fields and queue["demo_id"] == "product-knowledge-demo-v1"
         and queue["fictional"] is True and queue["status"] == "queued"
         and queue["alreadyPresent"] == 0
         and queue["message"] == "LightRAG accepted documents for background processing; ingestion is not complete yet.",
         "Demo public success contract changed")
    ids = queue.get("trackIds", [])
    need(len(ids) == 11 and len(set(ids)) == 11, "manual demo tracking IDs are incomplete")
    for tid in ids:
        poll_track(mock, tid, lightrag)
    need(len(mock_state(mock)["documents"]) == 11, "manual demo source count differs from eleven")
    dataset = json.loads((Path(__file__).resolve().parents[2] /
                          "sample-data/product-knowledge-demo/dataset.json").read_text())
    expected_demo = [{"text": d["text"], "file_source": d["file_source"]} for d in dataset["documents"]]
    actual_demo = [r["body"] for r in mock_state(mock)["requests"] if r["path"] == "/documents/text"]
    need(actual_demo == expected_demo, "manual demo text, sources or insertion order changed")
    print("PASS isolated engine mocked acceptance")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, ValueError) as error:
        print("FAIL:", str(error), file=sys.stderr)
        sys.exit(1)
