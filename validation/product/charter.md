# P0-VAL experiment charter

Status: implementation protocol; acceptance thresholds proposed, not approved or met.
Question: does ontology governance improve evidence-based decision support relative
to standard LightRAG at sustainable operational cost? Kev is disabled.

## Hypotheses

H1: preserve cross-source identity, directed dependencies, state and provenance.
H2: recognize conflicting, superseded and insufficient evidence without invention.
H3: reduce effort to verify material claims. H4: reduce verified task completion
time. H5: benefit exceeds extraction, curation, update and infrastructure costs.

## Controlled experiment

A is standard LightRAG graph/vector retrieval plus MCP and the answer LLM.
B is the identical initial index plus accepted ontology projections derived only
from the same original documents. Snapshot initial extraction once and clone it;
do not independently re-extract A/B. Original evidence remains available in both.
Identical document/version IDs, source texts, chunking, embedding model/dimensions,
LLM/revision, prompt, retrieval parameters, timeout and context budget are required.
Only B adds canonical facts. This measures incremental projection value, **not**
fully governed ingestion. A separate governed-only gateway suite checks that raw
writes cannot bypass approval. Never expose gold labels to adapters or providers.

Freeze sources, scenario-separated cases, gold, ontology, prompt and configuration
with SHA-256 before held-out inference. Gold critical questions require two
independent reviewers and adjudication; prefer double review of all held-out.
Do not repair held-out cases based on model answers. Use development only for tuning.
Randomize A/B order, use anonymous response labels for semantic review, and retain
an access-controlled unblinding key outside Git. Repeat each arm twice. Repetitions
are not independent scenarios. Every assigned task, including failure, timeout,
budget refusal and malformed output, remains in the denominator.

## Metrics and proposed gates

GTS passes only with factual conclusion, semantically supporting citations,
correct approved/proposed/superseded state, no invented material authorization or
ownership, and explicit necessary unknowns. String matching is diagnostic only.
Review every material claim; adjudicate disagreements before making a decision.

| Measure | Proposed target |
|---|---|
| Grounded task success | B − A ≥10 percentage points |
| Multi-hop success | B − A ≥10 percentage points |
| Semantic citation support | ≥95% of reviewed material claims |
| Conflict recall | ≥90%, no regression from A |
| Insufficiency recognition | ≥90% |
| Accepted fact provenance resolution | 100% |
| Unresolved projection failures or duplicates | 0 after reconciliation |
| Unauthorized cross-workspace reads | 0 |
| End-to-end p95 latency | ≤10s on fixed hardware/provider |
| Projection lag p95 | ≤60s at explicitly recorded load |
| Median verified user task time | ≥20% reduction |

Report retrieval evidence recall/precision, version/source resolution and ranking
separately from answer quality. Report category denominators, all failed cases,
absolute paired effect and 95% scenario-cluster bootstrap interval. A positive
but inconclusive interval yields ITERATE / extend pilot, never automatic GO.
The 120 held-out tasks comprise 30 each multi_hop, conflicts, missing_evidence,
and freshness. Overlapping temporal/distractor/injection tags do not add tasks.
Initial matrix: 120 × 2 variants × 2 repetitions =480 assigned executions, not
necessarily 480 provider calls. Preflight on development measures calls, tokens,
latency and cost. Paid indexing/inference requires a concrete approved cap;
no paid run or bulk ingestion is authorized by this implementation.

## External-pilot gate

Synthetic isolated checks can proceed now. Before external users or sensitive data:
verify server-side session expiry/revocation, HTTPS and Secure/HttpOnly/SameSite;
API and UI workspace permission checks; governed gateway write denial; canonical
provenance and projection recovery; commissioned offsite backups including restore
of integrations. Document-level authorization is not guaranteed: use synthetic or
explicitly authorized data only. Local UI session storage is single-process; do
not infer multi-replica support or persistent sessions.

## Technical evidence checklist

| Contract | Evidence required |
|---|---|
| Defined/undefined entity or predicate | accept/version or reject/quarantine |
| Missing source | never counted as resolved evidence |
| Directed dependency | subject/object not inverted |
| Projector outage/retry | accepted fact retained; reconciliation succeeds |
| Repeat delivery | stable canonical IDs, no duplication |
| Supersession/document edits | distinct versions; current retrieval after reconcile |
| Cross-workspace/API/UI | unauthorized requests denied |
| Gateway raw write | denied even with a valid retrieval credential |
| Source prompt injection | no instruction execution or unrelated tool calls |
| Restore | canonical integrity and retrieval projection reconciled |

Existing ontology tests provide part of this evidence; offline harness smoke is
not proof of live projector, model robustness, backup commissioning or accessibility.

## Decision

GO requires convincing quality, reliable citations, no critical regressions,
sustainable cost and real user interest. ITERATE means partial/inconclusive benefit
and targeted fixes without scope expansion. NO-GO means no meaningful benefit,
critical regressions or disproportionate governance cost. Implementation alone
cannot determine any outcome; the report remains NOT EVALUATED until evidence exists.
