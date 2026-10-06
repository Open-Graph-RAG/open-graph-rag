# Kev decision validation ledger

The decision tool and agent recipe remain experimental until the held-out quality evaluation is complete. The original Product Knowledge agent and default lightweight deployment are preserved.

## Agreed execution constraints

The original CPU/4 GiB feasibility requirement was revised to a 6 GiB additional host process-tree RSS allowance and NVIDIA GPU execution. Host GPU testing was authorized when Docker reported that no GPU-capable device driver was available. No Docker runtime or host driver configuration was changed. The experimental agent and controlled comparisons use OpenAI `gpt-6-luna`.

## Completed gates

| Gate | Evidence | Result |
|---|---|---|
| Host GPU feasibility | [Full benchmark](kev-feasibility-gpu.json), [method and limits](kev-feasibility-phase-1.md) | Short p95 239.63 ms; near-limit p95 1,544.21 ms; additional peak RSS 5.20 GiB. Passed the 10-second/6 GiB limits. |
| Contracts and context | `mcp/tests/test_decision_contract.py` | Strict input validation, exact provenance, atomic graph packages, intact excerpts, omissions, fingerprints, and token overflow checks passed. |
| Runtime and protocol | Bridge suite: 43 tests; lightweight environment skips one optional pinned-source helper test | Authentication, original search, typed tool discovery, raw byte limits, streamed envelope cap, busy admission, cancellation, stalled state, and shutdown checks passed. Independent pinned-source review exited successfully. |
| Genuine model parity | [Reproducible smoke](../mcp/decision_parity_smoke.py) | Host CUDA load with empty Hugging Face cache; exact encoded IDs and direct helper/adapter agreement for choice, yes/no, and score; enabled MCP result `evaluated` through the application lifespan. Passed. |
| Live LightRAG and host GPU | Read-only query against the running LightRAG API, followed by genuine Kev evaluation | `evaluated`; context retained one passage and two linked graph assertions. Retrieval 1.47 s, preparation 0.005 s, inference 1.23 s, end-to-end 2.82 s. Passed with explicit unresolved-link and token-budget limitations. |
| Lightweight image | Default image built and inspected with network disabled | No Torch or Kev installation; only `knowledge_search` exposed. Passed. |
| Optional empty-cache image | Heavy image built; read-only filesystem, dropped capabilities, network disabled for this isolated container check | Decision result `unavailable`, no answers/probabilities; health 200, unauthenticated MCP 401, authenticated discovery 200. Passed. |
| Experimental agent | Six offline Node checks plus independent review | Separate manifest/model, preserved defaults and ownership, test-only three-attempt/error/novelty scenarios. Passed; actual model compliance remains an evaluation question. |
| Cache ownership | Fresh disposable anonymous volume | UID/GID 10001:10001. Passed. |
| Compose | Normal and optional `config --quiet` | Passed. |

GPU parity uses fixture retrieval and proves the model/bridge integration; it does not prove the quality of production LightRAG evidence. Docker GPU inference remains untested because this host's Docker GPU runtime is unavailable. The interrupted CPU diagnostic is not a passing CPU benchmark.

Retrieval returned one passage, 17 graph assertions with at least one supporting source each, and 89 unresolved source links. The assembled context retained one passage and two graph assertions, and separately reported 37 omitted items for unresolved links and 15 omitted items for token budget. This is partial graph grounding, not a fully source-resolved graph. Two read-only LightRAG queries were performed during this integration check; underlying LightRAG provider token usage was not measured.

## Review reconciliation

Review identified and resolved reversed yes/no ordering, confidence-helper mismatch, local base loading, uncapped raw envelope buffering, stall timing and cancellation monitoring, shutdown callbacks after event-loop closure, stateless MCP session cleanup closing the shared runtime, public schema wrapping, and missing packaged artifact checksums. Runtime lifecycle states are now inspectable through a derived read-only property. Final lifecycle review fixed late completion releasing admission for a subsequent request, and shutdown during retrieval submitting work to a closed executor. Regression tests reproduce both sequences.

## Remaining gates

Scenario-separated development and held-out datasets, direct graph ablation, controlled three-arm Luna comparison, and the final rollout verdict are pending. No paid generation evaluation has run yet. A read-only provider model-access check confirmed `gpt-6-luna` availability; it is not quality evidence.
