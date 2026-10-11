"""Controlled LightRAG retrieval followed by identical, bounded generation.

Uses the MCP bridge's /query/data backend contract, not an agent tool loop.
No ingestion or graph writes; corpus/gold are never supplied to generation.
Real execution is operator-gated and needs a frozen environment attestation.
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from ..equivalence import read_and_validate_environment_equivalence


class LiveRunError(RuntimeError):
    """Sanitized execution failure (never contains provider body or headers)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LiveRunError("redirects are forbidden")


def bounded_post(url, payload, headers, timeout, request_limit, response_limit):
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
    if len(body) > request_limit:
        raise LiveRunError("request byte limit exceeded")
    request = urllib.request.Request(url, data=body, headers={**headers, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(response_limit + 1)
        if len(raw) > response_limit:
            raise LiveRunError("response byte limit exceeded")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise LiveRunError("response is not an object")
        return result
    except LiveRunError:
        raise
    except Exception as exc:
        raise LiveRunError(f"request failed: {type(exc).__name__}") from None


def _number(value, name, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"positive finite {name} required")
    if integer and int(value) != value:
        raise ValueError(f"integer {name} required")
    return int(value) if integer else float(value)


class LiveAdapter:
    def __init__(self, cfg, system_prompt, approve_paid=False, transport=None):
        if not approve_paid:
            raise ValueError("live execution requires explicit approved paid-run flag")
        self.cfg = cfg
        self.live = cfg.get("live", {})
        self.budget = cfg["budget"]
        self.transport = transport or bounded_post
        self.prompt = system_prompt
        self.started = time.monotonic()
        self.calls = self.input_tokens = self.output_tokens = 0
        self.cost = 0.0
        self.aborted = False
        self.last_attempt = {}
        self.limits = {k: _number(self.budget.get(k), k, integer=k in {"max_requests", "max_total_input_tokens", "max_total_output_tokens", "max_output_tokens", "max_request_bytes", "max_response_bytes"}) for k in (
            "max_requests", "max_total_input_tokens", "max_total_output_tokens", "max_output_tokens", "max_request_bytes", "max_response_bytes", "max_cost_usd", "max_duration_seconds", "request_timeout_seconds", "input_usd_per_million", "output_usd_per_million")}
        self.model = self.live.get("model")
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("pinned model revision required")
        self.retrieval = self.live.get("retrieval")
        attestation_path = self.live.get("equivalence_manifest")
        if not attestation_path:
            raise ValueError("frozen environment equivalence attestation required")
        base = Path(cfg.get("_config_base", Path(__file__).resolve().parents[1]))
        recipe_path = cfg.get("environment_recipe")
        if not isinstance(recipe_path, str) or not recipe_path.strip():
            raise ValueError("environment recipe required for equivalence validation")
        recipe_path = Path(recipe_path)
        if not recipe_path.is_absolute():
            recipe_path = base / recipe_path
        read_and_validate_environment_equivalence(base / attestation_path,
            base / cfg["dataset"]["corpus"], self.model, self.retrieval, recipe_path)
        self.urls, self.keys = {}, {}
        for arm in cfg["arms"]:
            url_env = self.live.get(f"{arm}_url_env")
            key_env = self.live.get(f"{arm}_key_env")
            self.urls[arm] = self._endpoint(os.environ.get(url_env or "", ""), retrieval=True)
            self.keys[arm] = os.environ.get(key_env or "", "")
            if not self.keys[arm]:
                raise ValueError("retrieval credential environment variable missing")
        if len(set(self.urls.values())) != len(self.urls):
            raise ValueError("A/B retrieval endpoints must be distinct")
        self.provider = self._endpoint(os.environ.get(self.live.get("provider_url_env", "OGR_VALIDATION_PROVIDER_URL"), ""), retrieval=False)
        self.provider_key = os.environ.get(self.live.get("provider_key_env", "OGR_VALIDATION_API_KEY"), "")
        if not self.provider_key:
            raise ValueError("provider credential environment variable missing")

    def _endpoint(self, url, retrieval):
        p = urlsplit(url)
        if p.username or p.password or p.query or p.fragment or p.path not in {"", "/", "/v1"}:
            raise ValueError("endpoint must be a plain configured origin (optional /v1)")
        hosts = self.live.get("allowed_retrieval_hosts" if retrieval else "allowed_provider_hosts", [])
        if not p.hostname or p.hostname not in hosts or p.scheme not in {"http", "https"}:
            raise ValueError("endpoint host not explicitly allowed")
        if not retrieval and p.scheme != "https":
            raise ValueError("provider requires HTTPS")
        return url.rstrip("/")

    def _request(self, url, payload, headers, generation=False):
        if self.aborted:
            raise LiveRunError("run aborted after provider exceeded accounting bounds")
        timeout = self.limits["request_timeout_seconds"]
        if time.monotonic() - self.started + timeout > self.limits["max_duration_seconds"]:
            raise LiveRunError("run duration admission budget exhausted")
        if self.calls >= self.limits["max_requests"]:
            raise LiveRunError("request budget exhausted")
        raw = json.dumps(payload, ensure_ascii=False).encode()
        if len(raw) > self.limits["max_request_bytes"]:
            raise LiveRunError("request byte limit exceeded")
        # UTF-8 bytes are a conservative input-token admission upper bound.
        reserved_in = len(raw) if generation else 0
        reserved_out = self.limits["max_output_tokens"] if generation else 0
        if generation and reserved_in > _number(self.budget.get("max_input_tokens"), "max_input_tokens", integer=True):
            raise LiveRunError("per-request input token admission budget exhausted")
        reservation = (reserved_in * self.limits["input_usd_per_million"] + reserved_out * self.limits["output_usd_per_million"]) / 1_000_000
        if self.input_tokens + reserved_in > self.limits["max_total_input_tokens"] or self.output_tokens + reserved_out > self.limits["max_total_output_tokens"] or self.cost + reservation > self.limits["max_cost_usd"]:
            raise LiveRunError("token or cost admission budget exhausted")
        self.calls += 1
        # Reserve before network; failed/malformed responses remain charged at cap.
        self.input_tokens += reserved_in
        self.output_tokens += reserved_out
        self.cost += reservation
        result = self.transport(url, payload, headers, timeout, self.limits["max_request_bytes"], self.limits["max_response_bytes"])
        if generation:
            usage = result.get("usage", {})
            inp, out = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in (inp, out)):
                raise LiveRunError("provider usage missing or malformed; reservation retained")
            if inp > reserved_in or out > reserved_out:
                self.aborted = True
                raise LiveRunError("provider exceeded reserved token bounds; run must stop")
            self.input_tokens += inp - reserved_in
            self.output_tokens += out - reserved_out
            actual = (inp * self.limits["input_usd_per_million"] + out * self.limits["output_usd_per_million"]) / 1_000_000
            self.cost += actual - reservation
        return result

    def budget_snapshot(self):
        return {"requests": self.calls, "input_tokens_or_reservations": self.input_tokens,
                "output_tokens_or_reservations": self.output_tokens,
                "cost_usd_or_reservations": self.cost, "aborted": self.aborted}

    def answer(self, case, corpus, arm):
        # case source_ids, gold and offline corpus never go to the retrieval/provider.
        started = time.monotonic()
        self.last_attempt = {"tool_calls": [], "evidence": {}, "usage": {}, "adapter": "live_lightrag"}
        request = {**self.retrieval, "query": case["question"]}
        data = self._request(self.urls[arm] + "/query/data", request, {"X-API-Key": self.keys[arm]})
        retrieval_elapsed = time.monotonic() - started
        self.last_attempt.update({"evidence": data.get("data", {}), "tool_calls": [{"tool": "knowledge_search_backend", "arguments": request, "result": data}], "retrieval_elapsed_seconds": retrieval_elapsed})
        if not isinstance(data.get("data"), dict):
            raise LiveRunError("unexpected LightRAG retrieval response")
        content = json.dumps({"question": case["question"], "retrieved_evidence": data["data"]}, ensure_ascii=False)
        payload = {"model": self.model, "messages": [{"role": "system", "content": self.prompt}, {"role": "user", "content": content}], "temperature": 0, "max_tokens": self.limits["max_output_tokens"], "response_format": {"type": "json_object"}}
        before = self.cost
        result = self._request(self.provider + "/chat/completions", payload, {"Authorization": "Bearer " + self.provider_key}, generation=True)
        try:
            answer = json.loads(result["choices"][0]["message"]["content"])
            if not isinstance(answer, dict) or not isinstance(answer.get("response"), str) or not isinstance(answer.get("citations"), list):
                raise ValueError()
        except (KeyError, IndexError, TypeError, ValueError):
            raise LiveRunError("malformed generated answer") from None
        return {"answer": answer, "adapter": "live_lightrag", "evidence_status": "LIVE_NOT_YET_REVIEWED", "evidence": data["data"], "tool_calls": [{"tool": "knowledge_search_backend", "arguments": request, "result": data}], "elapsed_seconds": time.monotonic() - started, "retrieval_elapsed_seconds": retrieval_elapsed, "usage": {**result["usage"], "cost_usd": self.cost - before, "run_requests": self.calls}, "budget_consumed": {"requests": self.calls, "input_tokens_or_reservations": self.input_tokens, "output_tokens_or_reservations": self.output_tokens, "cost_usd_or_reservations": self.cost}}
