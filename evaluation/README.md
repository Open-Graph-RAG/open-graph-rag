# Decision workflow evaluation

The harness uses the held-out synthetic fixture split only. It never connects to Jira, Figma, or LightRAG. Its three arms are the existing product-knowledge prompt with bounded search, a passage-only Kev ablation, and the graph-aware decision tool.

The evaluation requires `tiktoken==0.12.0` and its `o200k_base` encoding. The UTF-8 fallback is deliberately conservative and exceeds the hard input-token budget for this suite, so a fallback-only environment cannot freeze or run an evaluation. Keep the tokenizer cache in a writable temporary directory:

```bash
python3 -m venv .venv
.venv/bin/pip install -r mcp/requirements.lock tiktoken==0.12.0
mkdir -p /tmp/decision-tiktoken-cache
PYTHONPATH=evaluation:mcp TIKTOKEN_CACHE_DIR=/tmp/decision-tiktoken-cache \
  .venv/bin/python -m unittest discover -s evaluation -p 'test_*.py' -v
PYTHONPATH=evaluation:mcp TIKTOKEN_CACHE_DIR=/tmp/decision-tiktoken-cache \
  .venv/bin/python evaluation/decision_evaluation.py preflight
```

The preflight reports estimated input tokens for 2-, 3-, and 4-turn profiles with 500-token prior assistant messages, plus a likely profile assuming 90% two-turn conversations, 10% three-turn conversations, and 200-token prior tool-call messages. It serializes the fixture's actual decision contexts after the documented lossless prompt projection. Its output identifies the estimator and tokenizer version. Freezing records that output and its SHA-256 in the manifest, making the exact preflight result part of the approved run. Actual provider usage remains authoritative, with hard input, output, and request-count limits enforced during the run.

Before a paid run, freeze the reviewed inputs and retain the printed hash:

```bash
PYTHONPATH=evaluation:mcp TIKTOKEN_CACHE_DIR=/tmp/decision-tiktoken-cache \
  .venv/bin/python evaluation/decision_evaluation.py freeze evaluation/frozen-manifest.json
```

After the manifest, model artifacts, and local preflight have been reviewed, the operator must explicitly pass that exact hash to `run`. Credentials are read from `CHAT_API_KEY`; `CHAT_API_BASE` is recorded without user information, query strings, or fragments. The CLI rejects non-HTTPS endpoints. There are no retries. Provider errors, missing or malformed usage, or budget exhaustion stop the comparison; `results.jsonl` always starts with all 180 held-out case/arm records so the denominator survives an interrupted run.

```bash
PYTHONPATH=evaluation:mcp TIKTOKEN_CACHE_DIR=/tmp/decision-tiktoken-cache \
  .venv/bin/python evaluation/decision_evaluation.py run evaluation/frozen-manifest.json \
  --approve-manifest <reviewed-manifest-hash> \
  --output evaluation/results.jsonl
```

In both Kev arms, the evaluation-only tool response omits the duplicated `context.state_text` field while preserving every `context.items` excerpt and all other fields. The complete, unprojected tool result is stored alongside the transcript for audit. This reduces repeated prompt tokens without shortening or removing evidence; the production MCP response is unchanged.

Results retain model-visible transcripts, raw tool results, per-call usage and HTTP metadata, tool attempts and outcomes, retrieval expansion, latency, mechanical citation/quote/qualifier checks, and fields for human review. Case IDs and hidden gold annotations are used only in result records and scoring; they are excluded from model messages and fixture responses.
