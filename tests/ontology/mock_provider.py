"""Deterministic, no-network OpenAI-compatible provider for the ontology E2E."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, value):
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self.reply({"status": "ok"})
        else:
            self.send_error(404)

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(size) or b"{}")
        if self.path == "/v1/embeddings":
            raw_input = request.get("input", [])
            inputs = raw_input if isinstance(raw_input, list) else [raw_input]
            self.reply({
                "object": "list",
                "data": [
                    {"object": "embedding", "index": index, "embedding": [1.0] + [0.0] * 1023}
                    for index, _item in enumerate(inputs)
                ],
                "model": request.get("model", "ontology-e2e"),
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            })
        elif self.path == "/v1/chat/completions":
            self.reply({
                "id": "chatcmpl-e2e", "object": "chat.completion", "created": 0,
                "model": request.get("model", "ontology-e2e"),
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({
                            "high_level_keywords": ["DEPENDS_ON"],
                            "low_level_keywords": ["E2E System A", "E2E System B", "e2e-directed-fact"],
                        }),
                    },
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })
        else:
            self.send_error(404)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
