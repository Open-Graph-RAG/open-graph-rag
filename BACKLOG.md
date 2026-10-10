# Backlog

## Active
- [ ] Complete review of PR #14 (ready for review): P0 product-validation harness. At current head `80a679f530`, all five checks pass. The prior source-version scoring concern is fixed in the latest commit and has a regression test; the rest of the harness still needs review. Before any external pilot, decide whether to accept the documented single-process ontology UI session constraint. Recommendation: require one UI worker/replica for the initial pilot and revisit shared session storage before scaling. [PR](https://github.com/Open-Graph-RAG/open-graph-rag/pull/14)

## Recent monitoring
- 2026-10-10: PR #14 advanced from `b730cde1e2cf` to `80a679f530`. The latest change makes version/evidence identity come exclusively from the frozen corpus source record, with a regression test for conflicting LightRAG chunk metadata; all five current-head checks pass. Prior review finding resolved; full PR review remains open. PR #15 remains draft; its checks passed at `7d0c502697`, but this backlog update will require fresh checks. Main CI runs `ea992e433a`, `96fdb58b92`, and `7b15bd21cd` succeeded; no open issues.
