# Kev and Luna evaluation report

The optional evaluator and decision recipe remain **experimental**. The host GPU passed the revised resource gate, but the direct classifier failed the quality gate. The original Product Knowledge agent and lightweight deployment are unchanged.

## Frozen experiment

The [manifest](frozen-manifest.json) freezes 24 development and 60 scenario-separated held-out English cases, the source-derived rubric and independent label review, prompts, runner, decision modules, dependency lock, and pinned model checksums. Held-out cases contain 20 conflicts, 20 compatible differences, and 20 insufficient-evidence comparisons. Anonymous case-local source IDs avoid scenario/label hints; gold annotations are excluded from model messages.

OpenAI `gpt-6-luna` uses Chat Completions, `reasoning_effort: none`, temperature 0, and a 500-token output limit per call. The paid run allows 180 conversations, at most four generation turns each, 720 total calls, one million input tokens, and 360,000 output tokens, with no retries. Every denominator slot exists before inference or network calls. Failure or budget exhaustion preserves the full denominator and makes the comparison incomplete.

The pinned `tiktoken==0.12.0` / `o200k_base` preflight estimated 957,887 input tokens for an assumed 90% two-turn / 10% three-turn profile with 200-token prior tool-call messages. This is a planning assumption, not a completion guarantee. The separate 500-token prior-output profiles estimate 939,125 / 1,774,743 / 2,854,398 tokens for two / three / four turns. Hard admission reserves the next request's UTF-8 byte upper bound against accumulated actual provider input tokens; bytes are not accumulated as usage.

For both Kev workflow arms, the evaluation removes only duplicate `context.state_text` from model-visible tool results. Every structured context item, original excerpt, reference, and other field remains; full raw results are saved for audit. Production MCP output is unchanged. Baseline search retains graph assertions as the existing bridge does. Passage-only Kev is a test-only ablation.

## Direct graph-context ablation

Both arms received identical original passages, source identities, locators, text, and order. The graph arm adds only extracted assertions linked to those same passages. This comparison does not include adaptive retrieval. Genuine pinned Kev ran on the host RTX 3070 Laptop GPU in fp32 with the shipped temperature, without hosted generation.

| Metric | Graph off | Graph on | Required |
|---|---:|---:|---:|
| Denominator | 60 | 60 | 60 per arm |
| Evaluated | 60 | 58 | Missing outcomes count as failures |
| Conflict precision | 51.5% | 53.3% | ≥90% |
| Conflict recall | 85.0% | 80.0% | ≥90% |
| Three-class macro-F1 | 35.6% | 36.6% | ≥85% |
| Correct classifications | 27 | 27 | — |

Both quality gates failed. Neither arm selected `compatible` for any case. The graph arm returned `insufficient_context` without answers for two missing-source-link cases; these remain failures in the full denominator. Graph context slightly changed predictions but did not establish useful classifier quality. See [all direct results and exact contexts](direct-results.json).

Representative errors: `heldout-compatible-13` states that bulk tagging remains in beta through May 31 and becomes generally available on June 1. Its graph assertion correctly distinguishes those periods, yet graph-on Kev selects conflict. In `heldout-insufficient-06`, approval and automatic invitation sending lack enough timing/actor evidence to establish whether sending follows approval. Passage-only Kev selects conflict (39.4%, versus 38.6% insufficient); graph-on selects insufficient (40.6%) after a graph assertion merely connects both excerpts to enterprise invitations. These close distributions do not establish calibrated reliability.

## Controlled workflow comparison

**Incomplete: no pass.** The conservative next-request input reservation stopped the run after 178 completed conversations. The final passage-only conversation (`heldout-conflict-11`) made two generation calls and a genuine assessment before its next call was refused; its final answer is missing. The same case's graph conversation was not run. All 180 slots remain, with 60 cases per arm. Four other completed conversations produced no parseable final JSON; they also count as failed predictions. No generation was retried and no denominator was reduced.

| Result, denominator 60 per arm | Existing baseline | Passage-only Kev | Graph decision |
|---|---:|---:|---:|
| Completed conversations | 60 | 59 | 59 |
| Parsed final answers | 60 | 57 | 57 |
| Correct conflict classifications / 20 | 20 | 13 | 10 |
| Correctly surfaced conflicts with both claims/citations / 20 | 19 | 12 | 9 |
| False conflicts / 40 | 8 | 1 | 1 |
| Correct compatible classifications / 20 | 20 | 11 | 16 |
| Correct insufficient classifications / 20 | 11 | 17 | 18 |
| Three-class macro-F1, full denominator | 84.0% | 70.9% | 75.1% |
| Semantic citation-error answers | 2 | 4 | 2 |
| Citation or unusable-answer failures (mechanical) | 2 | 7 | 5 |
| Fabricated ownership claims (semantic review) | 0 | 0 | 0 |
| Incorrect source-status claims (semantic review) | 1 | 0 | 0 |

These are partial workflow metrics, not a completed comparison or a pass. Correctly surfaced conflicts require a final conflict conclusion, supported original claims, and both valid citations; an answer that describes contradictory facts but concludes insufficient is an undercall. Semantic citation errors exclude missing/unparseable answers; mechanical failures include those unusable outcomes. Semantically acceptable paraphrases and source-context explanations were reviewed separately from exact metadata matching.

The graph arm surfaces ten fewer supported conflicts than baseline, rather than two more. It reduces false conflicts and preserves more insufficient conclusions, but that tradeoff does not satisfy acceptance. Baseline classifies all 20 conflicts correctly, although one changes punctuation in a quote and therefore fails exact citation support. No agent benefit is proven.

### Actual provider usage

| Usage | Baseline | Passage-only Kev | Graph decision | Total |
|---|---:|---:|---:|---:|
| Generation calls | 138 | 180 | 168 | 486 |
| Input tokens, including cached input | 197,608 | 381,734 | 408,195 | 987,537 |
| Output tokens | 17,570 | 41,814 | 41,995 | 101,379 |
| Cached input tokens (included above) | 172,476 | 277,008 | 281,941 | 731,425 |
| Reasoning output tokens | 0 | 0 | 0 | 0 |

Maximum observed generation turns were four per conversation; maximum output was 500 tokens per call. Cached input is not subtracted from the input cap. The stop came from the conservative next-request reserve, rather than provider-reported usage exceeding one million. Provider prices/costs were not supplied, so no monetary cost is asserted. This controlled run does not call live Jira, Figma, or LightRAG.

### Tool use, expansion, and latency

| Operational measure | Baseline | Passage-only Kev | Graph decision |
|---|---:|---:|---:|
| Conversations using tools / 60 | 60 | 60 | 59 |
| Search attempts | 78 | 61 | 41 |
| Decision attempts / executed | 0 / 0 | 60 / 60 | 70 / 63 |
| Successful inference calls | 0 | 60 | 59 |
| Recorded targeted retrieval attempts | 2 | 4 | 10 |
| New-source additions, summed across attempts | 0 | 1 | 4 |
| Decision recipe violations | 0 | 0 | 7 |
| Provider-request median / p95 | 1.49 / 2.79 s | 2.78 / 3.71 s | 2.93 / 4.14 s |
| Measured conversation-component median / p95 | 3.81 / 5.46 s | 7.70 / 8.76 s | 8.20 / 12.66 s |
| Total measured serial components | 244.61 s | 460.55 s | 493.01 s |

All executed conversations used tools. The graph tool was attempted in 59/60 slots; the last was unrun. There were no identical decision-request retries, and the maximum decision attempts per conversation was three. Seven graph requests failed schema/recipe validation and count toward that cap. Search attempts are separately counted; targeted attempts are retrieval routing events, not necessarily useful expansion rounds, and repeated/empty results remain attempts. Graph novelty measures were returned for 63 calls (219 fingerprints total); this does not prove useful new facts.

Conversation latency is the sum of measured sequential provider-call, retrieval, preparation, and inference components. It is a **lower bound**, excluding dispatch, serialization, unreported work, and model cold-load. Exact per-conversation wall-clock time was not recorded. The three arms total 1,198.18 seconds of measured components. Source fixtures preserve full raw tool results and model-visible transcripts in [results.jsonl](results.jsonl); [summary.json](summary.json) and [the summarizer](summarize_results.py) reproduce the metrics.

### Semantic failures and limitations

Independent review covers all 178 completed answers. Passage-only answers repeatedly state that no Kev inference occurred even though a genuine evaluated result was returned; insufficient classification is not the same as an `insufficient_context` outcome. Graph answers also miss explicit no-inference/context-gap disclosures in two cases and undercall several clear conflicts after inspecting both claims. Some answers alter exact quotations or invent composite citation IDs such as `record-1.md#source_1` instead of the returned identifier. Missing/malformed final answers are failures, not evidence that no inference occurred.

One frozen conflict case (`heldout-conflict-01`) has a Security approver claim alongside an Actor line naming support leads. The gold label treats the incompatible exclusive approver claims as a conflict; a conservative insufficiency answer exposes a genuine ambiguity. This caveat is retained without relabeling after model responses. It does not change the failed verdict.

The audit records and case-specific explanations are in [semantic-review.json](semantic-review.json). Baseline sometimes uses the experimental unavailable wording because Kev is absent by design; that is cautious but may distract from the evidence. Failure examples are preserved rather than repaired or rerun after freezing.

## Limits and rollout

The dataset is synthetic and often states qualifiers explicitly. It cannot establish performance on noisy enterprise evidence. Mechanical graph linkage does not prove semantic support. Probabilities remain `not_validated_for_domain`; the bridge does not enforce document ACLs or make authorization decisions. Docker GPU inference is untested because the host Docker GPU runtime is unavailable. The CPU diagnostic was interrupted and provides no passing CPU feasibility claim.

The direct failure prevents recipe rollout regardless of runtime/API success. Keep the generic tool opt-in and experimental, preserve the original agent, and review all source evidence before acting. Deployment, recovery, and rollback instructions are in [the setup guide](../docs/kev-decision-setup.md).
