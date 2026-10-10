# Product validation harness

**Status: implementation foundation only; product value is not evaluated.**
The checked-in source corpus, cases and gold labels are synthetic and not yet
independently reviewed. Fixture runs check plumbing only; they are not A/B
evidence. No paid provider run, external-user session or real-data pilot is part
of this implementation.

The experiment contract and proposed (not approved or achieved) gates are in
[charter.md](charter.md). The research protocol is in
[review/pilot-protocol.md](review/pilot-protocol.md), and outstanding evidence
is summarized in [implementation-status.md](implementation-status.md) and
[reports/product-validation.md](reports/product-validation.md).

## Current dataset

- 24 development cases and 120 held-out cases, separated by scenario.
- Four equally represented held-out categories: multi-hop, conflicts,
  missing evidence and freshness.
- 116 fictional source documents: 105 generated scenario templates plus the 11
  documents from the existing Northstar Product Knowledge demo in development
  only. The imported demo is still synthetic and is not held-out evidence.
- The gold file is a distinct input to the scorer and is never passed to either
  retrieval or generation adapter. Its `review_status` is currently
  `unreviewed`; live held-out generation is blocked until approval is recorded.

## Offline workflow

Run from the repository root with Python 3.12 or later. The product harness
uses only the standard library.

```sh
# Check dataset contracts and frozen hashes.
python3 -m validation.product.cli preflight

# Run deterministic development fixtures. This makes no network/provider calls.
python3 -m validation.product.cli run --split development --adapter fixture

# Score a completed run; without two independent human reviews, semantic GTS
# remains pending (or conservatively zero in aggregate diagnostics).
python3 -m validation.product.cli score validation/product/.runs/RUN.jsonl \
  --reviews validation/product/private/reviews.jsonl
python3 -m validation.product.cli report validation/product/.runs/RUN.jsonl \
  --reviews validation/product/private/reviews.jsonl

# Prepare deterministic source-grounded candidates; this does not accept them
# or write to the ontology service or LightRAG.
python3 -m validation.product.cli prepare-facts \
  --output validation/product/.runs/candidates.json
```

The first reviewed dataset freeze can be recorded with
`python3 -m validation.product.cli preflight --freeze`. Do not use that flag to
bless unreviewed gold: hashes establish integrity, not annotation quality.
`replay RESPONSES.jsonl --split development` accepts externally recorded rows
for scoring; missing and failed assignments remain in the denominator. `score`
and `report` reject result files unless they contain exactly one row for every
expected case/arm/repeat assignment and match the current frozen input hashes.
Retrieval diagnostics use LightRAG's ordered `data.chunks` only, resolve chunk
paths against that case's visible sources and the frozen corpus, and report
source-level version resolution (not LightRAG chunk-version metadata).

`prepare SOURCE DESTINATION` clones only files listed by a pre-existing,
sanitized `snapshot-manifest.json`. It refuses private configuration, symlinks,
hash mismatches and existing destinations. It does not start services or modify
the deployed checkout. The validation-only Compose recipe and its current
limitations are documented in [environments/README.md](environments/README.md).

Run artifacts belong under the ignored `.runs/` or `private/` paths and must not
be committed. Keep participant notes and the response unblinding key private.
Publish only approved, redacted aggregates.

## Live execution gates

The live adapter retrieves independently from the baseline and candidate
LightRAG endpoints, then sends only the question and returned evidence to the
same configured generation model. It does not submit gold labels, corpus
records, or case source IDs to either endpoint. It is fail-closed unless all of
these are configured and attested:

- Distinct A/B retrieval endpoints with explicit host allowlists and
  validation-only API keys.
- A reviewed environment-equivalence manifest proving a shared initial
  snapshot, identical corpus/model/retrieval settings, and candidate-only
  ontology projection.
- HTTPS provider endpoint, explicit model revision and positive price limits.
- A concrete request/token/cost/time budget and an explicit
  `--approve-paid` invocation.
- Approved, independently reviewed held-out gold before any held-out run.

`config.yaml` leaves `live` empty and zeroes the example prices intentionally,
so a provider run cannot start by default. The live runner still writes every
assignment to its private failure journal and exits non-zero if no response
completes; partial failures remain in the denominator. No live run has been
executed. The current Compose file is a wiring recipe, not a verified deployment; external
user tests and real organizational documents additionally require the safety
gates in the charter, including authorization and offsite-restore commissioning.
