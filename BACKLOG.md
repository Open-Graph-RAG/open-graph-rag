# Backlog

## Active
- [ ] Review PR #14 (ready for review): P0 product-validation harness. Five checks pass at current head `b730cde1e2cf`; review update found a source-version scoring inconsistency (see monitoring entry) to resolve before approval. Also confirm the documented single-process ontology UI session constraint is acceptable before any external pilot. Recommendation: require one UI worker/replica for initial pilot; revisit shared session storage before scaling. [PR](https://github.com/Open-Graph-RAG/open-graph-rag/pull/14)

## Recent monitoring
- 2026-10-10: PR #14 head advanced to `b730cde1e2cf`; all five checks succeeded. Review update: `validation/product/scoring/score.py` prefers `chunk.version`/`source_version` over the frozen corpus source version, while README says resolution reports source-level versions, not LightRAG chunk-version metadata. This allows non-frozen chunk metadata to alter the version-resolution score; resolve by using the frozen corpus version or explicitly redefine/validate the metric. PR #15 remains draft and tracks PR #14 review/deployment constraint; its checks pass at `c6ed8af00dcb`. Main CI succeeded; no open issues.
