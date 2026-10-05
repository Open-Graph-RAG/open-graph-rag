"""Deterministic, in-memory integrations for Activepieces engine acceptance."""
from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

LIGHTRAG_KEY = os.getenv("LIGHTRAG_API_KEY", "e2e-lightrag-key")
TAVILY_KEY = os.getenv("TAVILY_API_KEY", "e2e-tavily-key")
GOOGLE_TOKEN = os.getenv("GOOGLE_ACCESS_TOKEN", "e2e-google-token")
GOOGLE_REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI", "http://activepieces.localhost:18080/redirect",
)
lock = threading.Lock()
config = {"case": "normal", "processing_polls": 1}
requests = []
docs = {}
tracks = {}
PLAN_TEXT = (
    "Fictional Northstar demo: CP-248 proposes account-owner approval before "
    "a plan change takes effect. This proposal is under review and is not a "
    "released capability. Product and design teams must preserve the account "
    "owner's approval record and show pending changes in the subscription "
    "screen. The proposed approval interaction does not change the current "
    "billing policy or promise a release date."
)
SECOND_TEXT = (
    "Fictional Northstar demo: CP-249 proposes a clear audit trail for subscription "
    "changes. An audit record should identify the account, requested plan, time "
    "of request and approving account owner. This is a separate proposal in "
    "review; current product behavior remains unchanged. Engineers must not "
    "describe it as shipped and designers must keep proposal and current "
    "policy evidence separate when preparing customer-facing materials."
)
TAVILY_RESULTS = [
    {"url": "https://docs.example.invalid/plan", "title": "Plan rules", "raw_content": PLAN_TEXT},
    {"url": "https://docs.example.invalid/duplicate", "title": "Duplicate page", "raw_content": PLAN_TEXT},
    {"url": "https://docs.example.invalid/audit", "title": "Audit proposal", "raw_content": SECOND_TEXT},
    {"url": "https://docs.example.invalid/no-raw", "title": "Snippet only", "content": "Snippet must not be ingested."},
    {"url": "https://docs.example.invalid/denied", "title": "Denied", "raw_content": "Access denied"},
    {"url": "javascript:alert(1)", "title": "Invalid URL", "raw_content": "must not be ingested"},
]


class Handler(BaseHTTPRequestHandler):
    server_version = "ActivepiecesE2EMock/1"

    def log_message(self, *_args):
        pass

    def reply(self, status, value, content_type="application/json"):
        if isinstance(value, bytes):
            data = value
        elif content_type.startswith("text/"):
            data = str(value).encode()
        else:
            data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 1_000_000:
            raise ValueError("body too large")
        value = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        return value

    def record(self, body=None, auth_evidence=None):
        # Headers (including authentication values) are never recorded.
        entry = {"method": self.command, "path": self.path, "body": body}
        if auth_evidence is not None:
            entry["auth_evidence"] = auth_evidence
        with lock:
            requests.append(entry)

    def auth(self, expected):
        actual = self.headers.get("Authorization", "")
        evidence = {
            "present": bool(actual),
            "bearerPrefix": actual.startswith("Bearer "),
            "looksObjectString": actual == "[object Object]" or actual.endswith("[object Object]"),
            "looksUndefined": "undefined" in actual.lower(),
            "containsTemplate": "{{" in actual or "}}" in actual,
            "containsFixture": expected in actual,
            "emptyAfterBearerPrefix": actual == "Bearer ",
            "expectedLength": len(actual) == len("Bearer " + expected),
            "matchesFixture": actual == "Bearer " + expected,
            "rawFixtureLength": len(actual) == len(expected),
            "matchesRawFixture": actual == expected,
        }
        if evidence["matchesFixture"]:
            return True
        self.record(auth_evidence=evidence)
        self.reply(401, {"detail": "Unauthorized"})
        return False

    def light_auth(self):
        actual = self.headers.get("X-API-Key", "")
        evidence = {
            "present": bool(actual),
            "bearerPrefix": False,
            "containsTemplate": "{{" in actual or "}}" in actual,
            "expectedLength": len(actual) == len(LIGHTRAG_KEY),
            "matchesFixture": actual == LIGHTRAG_KEY,
        }
        if evidence["matchesFixture"]:
            return True
        self.record(auth_evidence=evidence)
        self.reply(401, {"detail": "Unauthorized"})
        return False

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self.reply(200, {"status": "ok"})
            return
        if path == "/__admin/state":
            with lock:
                snapshot = json.loads(json.dumps({"case": config["case"], "requests": requests,
                                                  "documents": list(docs.values()), "tracks": tracks}))
            self.reply(200, snapshot)
            return
        if path.startswith("/documents/track_status/"):
            if not self.light_auth():
                return
            tid = unquote(path.rsplit("/", 1)[-1])
            self.record(auth_evidence={"present": True, "matchesFixture": True})
            with lock:
                item = tracks.get(tid)
                if item is None:
                    self.reply(404, {"detail": "Tracking ID not found"})
                    return
                item["polls"] += 1
                state = "processed" if item["polls"] > int(config["processing_polls"]) else "processing"
            self.reply(200, {"status": state, "track_id": tid})
            return
        if re.fullmatch(r"/drive/v3/files/([A-Za-z0-9_-]{10,200})/export", path):
            if not self.auth(GOOGLE_TOKEN):
                return
            self.record(auth_evidence={"present": True, "matchesFixture": True})
            if config["case"] == "google_403":
                self.reply(403, "forbidden", "text/plain")
                return
            if config["case"] == "google_empty":
                self.reply(200, "", "text/plain")
                return
            if config["case"] == "google_oversized":
                self.reply(200, "x" * 500001, "text/plain")
                return
            self.reply(200, "Fictional exported NotebookLM note: CP-248 is a proposal.", "text/plain")
            return
        self.reply(404, {"detail": "Not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/oauth/token":
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0 or size > 100_000:
                self.reply(400, {"error": "invalid_request"})
                return
            form = parse_qs(self.rfile.read(size).decode("utf-8", "replace"))
            grant = form.get("grant_type", [""])[0]
            valid_client = (form.get("client_id") == ["e2e-google-client"] and
                            form.get("client_secret") == ["e2e-google-client-secret"])
            redirect_matches = (grant != "authorization_code" or
                                form.get("redirect_uri") == [GOOGLE_REDIRECT_URI])
            valid_grant = ((grant == "authorization_code" and
                            form.get("code") == ["e2e-google-auth-code"] and redirect_matches) or
                           (grant == "refresh_token" and
                            form.get("refresh_token") == ["e2e-google-refresh-token"]))
            evidence = {"clientMatched": valid_client, "grantType": grant,
                        "grantMatched": valid_grant,
                        "redirectUriMatched": redirect_matches}
            self.record(auth_evidence=evidence)
            if not valid_client or not valid_grant:
                self.reply(400, {"error": "invalid_grant"})
                return
            self.reply(200, {
                "access_token": GOOGLE_TOKEN,
                "token_type": "Bearer",
                "expires_in": 1 if grant == "authorization_code" else 3600,
                "refresh_token": "e2e-google-refresh-token",
            })
            return
        try:
            data = self.body()
        except (ValueError, json.JSONDecodeError):
            self.reply(400, {"detail": "Invalid JSON object"})
            return
        if path == "/__admin/reset":
            with lock:
                config.update({"case": str(data.get("case", "normal")),
                               "processing_polls": int(data.get("processing_polls", 1))})
                requests.clear()
                docs.clear()
                tracks.clear()
            self.reply(200, {"status": "reset"})
            return
        if path == "/tavily/search":
            if not self.auth(TAVILY_KEY):
                return
            self.record(data, auth_evidence={"present": True, "matchesFixture": True})
            case = config["case"]
            if case == "tavily_500":
                self.reply(500, {"detail": "mock upstream failure"})
            elif case == "tavily_malformed":
                self.reply(200, {"results": "not-an-array"})
            else:
                self.reply(200, {"results": [] if case == "skipped" else TAVILY_RESULTS})
            return
        if path == "/documents/text":
            if not self.light_auth():
                return
            self.record(data, auth_evidence={"present": True, "matchesFixture": True})
            case = config["case"]
            if case == "lightrag_500" or (case == "partial_second_500" and len(tracks) == 1):
                self.reply(500, {"detail": "mock insert failure"})
                return
            if case == "lightrag_unrelated_409":
                self.reply(409, {"detail": "request conflicted for unrelated reason"})
                return
            source, text = data.get("file_source"), data.get("text")
            if not isinstance(source, str) or not isinstance(text, str) or not text.strip():
                self.reply(422, {"detail": "text and file_source required"})
                return
            with lock:
                if source in docs or case == "all_duplicate":
                    self.reply(409, {"detail": "Document storage already contains this source"})
                    return
                tid = f"mock-track-{len(tracks) + 1}"
                docs[source] = {"file_source": source, "text": text}
                tracks[tid] = {"polls": 0, "file_source": source}
            self.reply(202, {"status": "success", "track_id": tid})
            return
        if path == "/query/data":
            if not self.light_auth():
                return
            self.record(data, auth_evidence={"present": True, "matchesFixture": True})
            words = set(re.findall(r"[\w-]+", str(data.get("query", "")).lower()))
            with lock:
                found = [d for d in docs.values() if not words or words.intersection(
                    re.findall(r"[\w-]+", d["text"].lower()))]
            self.reply(200, {"status": "success", "message": "Mock evidence retrieved", "data": {
                "chunks": [{"content": d["text"], "file_path": d["file_source"]} for d in found[:8]],
                "entities": [], "relationships": []}, "metadata": {"mock": True}})
            return
        self.reply(404, {"detail": "Not found"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "9621"))), Handler).serve_forever()
