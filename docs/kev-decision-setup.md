# Optional Kev decision tool

The regular stack stays a lightweight, search-only bridge. The validated default image contains neither PyTorch nor Kev. The optional decision tool is enabled only when you add `compose.decision.yaml`; it runs on the pinned Kev and Qwen artifacts described in [the phase 1 feasibility report](kev-feasibility-phase-1.md).

## Prepare and start

The deployment host needs an NVIDIA GPU, Docker Compose GPU support, and outbound HTTPS access while downloading model artifacts. Build the optional image and prepare its pinned artifacts:

```bash
docker compose -f compose.yaml -f compose.decision.yaml build kev-artifact-prepare
docker compose -f compose.yaml -f compose.decision.yaml --profile kev-prepare run --rm kev-artifact-prepare
```

Preparation downloads the exact Kev and Qwen revisions, checks every file's byte count and SHA256 against the packaged copy of `docs/kev-feasibility-gpu.json`, and writes `/model-cache/artifacts/manifest.json`. The manifest pins the Kev source commit and both model revisions. Hugging Face's temporary preparation cache is removed after successful verification; a failed download can leave it in the named volume so a retry can resume.

Start the stack with the decision override:

```bash
docker compose -f compose.yaml -f compose.decision.yaml up -d --build
```

The override enables `MCP_DECISION_ENABLED=1`, requests one NVIDIA GPU, and mounts the artifact volume read-only. Inference loads only local artifacts with Hugging Face offline mode enabled. The MCP process runs as UID 10001 with a read-only root filesystem, dropped Linux capabilities, and `no-new-privileges`; the prep command is a separate one-shot service with a writable model-cache volume. At runtime the bridge still uses its existing LightRAG network to retrieve evidence. Preparing artifacts is the only step that needs model-host network access.

If the cache is empty or artifact verification fails, the decision tool returns `unavailable` with no model probabilities. Ordinary LightRAG search remains available. The regular `docker compose up -d --build` command, without the override, keeps the lightweight image and decision tool disabled.

## Request limits and outcomes

The serialized decision `arguments` are limited to 64 KiB. Every HTTP POST in the decision-enabled application is limited to 128 KiB. A request can ask 1–4 questions. Choice questions and score questions accept 2–8 options or levels; up to 8 caller-supplied evidence excerpts and 64 seen fingerprints are accepted. Objective and query text are each limited to 4,000 characters, and each supplied excerpt to 8,000 characters.

Responses use one of four statuses: `evaluated`, `insufficient_context`, `unavailable`, or `failed`. Only `evaluated` includes probabilities. Non-evaluated outcomes return limitations without probability output. An `evaluated` outcome can still contain unresolved-link or token-budget limitations; it assesses only its included context. The single inference worker does not queue concurrent requests: an overlapping call can return `unavailable` with a busy limitation. During model loading, calls can return `unavailable` with a loading limitation.

For example, a bounded conflict investigation can send these tool arguments:

```json
{
  "objective": "Compare approval timing for the proposed customer plan-change workflow.",
  "query": "CP-248 FLOW-02 customer plan change approval effective time",
  "questions": {
    "assessment": {
      "type": "choice",
      "instructions": "Compare original claims, actor, scope, status, and effective time. Choose insufficient if a material comparison fact is missing.",
      "options": [
        {"id": "conflict", "description": "Incompatible claims concern the same workflow and effective scope."},
        {"id": "compatible", "description": "The documented difference is compatible after accounting for scope or timing."},
        {"id": "insufficient", "description": "The evidence cannot establish the relevant comparison."}
      ]
    },
    "scope_complete": {
      "type": "yes_no",
      "instructions": "Does the supplied context establish the actor, workflow scope, status, and timing needed for this comparison?"
    }
  }
}
```

`supplied_evidence` can carry original live excerpts using `id`, `text`, `source_locator`, and caller-claimed `origin`, with optional `retrieved_at`, `status`, `version`, and `original_reference`. Those metadata are caller-provided; the bridge does not verify permission or authenticity. Carry returned `novelty_fingerprints` into `seen_fingerprints` on a targeted follow-up. Different live/indexed contents and versions remain distinct.

Inspect the returned exact context, original source references, included and omitted item IDs and reasons, token counts, limitations, and separate timings alongside answers. Graph assertions are explicitly graph-extracted; inspect their linked source text before presenting them as facts. Model metadata identifies the pinned checkpoint, base, tokenizer, source revision, dtype, temperature, and execution device. Every result remains `calibration_status: not_validated_for_domain`; confidence measures describe the output distribution and do not guarantee correctness.

Retrieval and inference share a 15-second response deadline. If native inference is still running after 60 seconds, the runtime marks its worker stalled and refuses further decision requests; restart the `mcp` service to recover. Shutdown waits at most 20 seconds for the worker, and Compose allows 30 seconds before stopping the container.

```bash
# Recover a stalled enabled runtime after investigating the cause.
docker compose -f compose.yaml -f compose.decision.yaml restart mcp
# Roll back to the lightweight bridge; persistent volumes are retained.
docker compose -f compose.yaml up -d --build mcp
```

Probabilities are model outputs, not calibrated business confidence. Present them with the returned source context and limitations, and do not treat them as a substitute for reviewing the evidence.

## Validation and limits

Run configuration validation and bridge tests with:

```bash
docker compose -f compose.yaml -f compose.decision.yaml config --quiet
python3 -m venv .venv
.venv/bin/pip install -r mcp/requirements.lock
.venv/bin/python -m unittest discover -s mcp/tests -v
```

## Host GPU parity smoke

Run this optional check from a host environment with the pinned Kev checkout, verified artifact directory, PyTorch CUDA support, and the MCP dependencies installed. It uses an empty Hugging Face cache, validates Kev's direct serializer and answer helpers against the adapter for choice, yes/no, and score questions, then invokes the enabled MCP decision tool through the Starlette app lifespan with fixture retrieval data:

```bash
KEV_SOURCE=/opt/kev \
KEV_ARTIFACT_ROOT=/model-cache/artifacts \
HF_HOME=/tmp/kev-empty-hf \
.venv/bin/python mcp/decision_parity_smoke.py
```

The script must report `status: evaluated`, matching direct and adapter outputs, `strict_record_ids_equal: true`, and `offline_hf_cache_empty: true`. The `/opt/kev` and `/model-cache/artifacts` paths should point to the checkout and artifact directory verified by the preparation step.

The 6 GiB additional process-tree RSS gate passed on the tested host GPU in phase 1: measured peak was 5.20 GiB above the idle bridge baseline. The host-GPU parity smoke also passed with a pinned RTX 3070. Docker GPU execution remains unvalidated.

Container checks confirmed that the default image has no `torch` or `kev` installation and exposes search only. The optional image's empty-cache check returned ASGI health 200, rejected unauthenticated MCP access with 401, and listed both tools for an authenticated MCP client; a decision request with no artifacts returned `unavailable` without probabilities. A fresh anonymous model-cache volume initialized with owner `10001:10001`. These checks confirm startup, authentication, tool registration, and empty-cache handling; they do not validate GPU access from Docker.
